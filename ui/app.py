"""ExportWhy: single-page UI for the demo. Everything is produced by the same agent loop as the CLI
(agent.main.run); this page only renders the report and the trace.

    .venv/bin/streamlit run ui/app.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import config
from agent.main import run
from agent.memory import Memory
from agent.roles import Task, redact
from agent.zetaris import DataLayerError, get_backend

st.set_page_config(page_title="ExportWhy", page_icon="🔎", layout="wide")
st.markdown(
    """<style>
    [data-testid="stMarkdownContainer"] code {
        background: #eef2f7; color: #1f3a5f; border: 1px solid #dde3ea; border-radius: 4px;
        padding: 1px 6px; font-size: 0.82em; white-space: nowrap;
    }
    </style>""",
    unsafe_allow_html=True,
)
VERDICT_STYLE = {
    "compliant": "success",
    "investigate": "error",
    "cannot-confirm": "warning",
}

# The report text is written once, for the CLI, by Reporter.report. The page shows the same text
# but with short section titles (the explanation moves to the title's help icon), without the parts
# the banner, the metrics row and the right-hand trace already show, and with each evidence line's
# citation as a tag.
SECTIONS = {
    "Evidence": (
        "Evidence",
        "each line ends with the table, column and query step it came from",
    ),
    "Conflicts": ("Conflicts", "flagged, not silently resolved"),
    "Cannot be confirmed from the sources": (
        "Cannot confirm",
        "the sources did not answer this",
    ),
    "Evidence not retrieved": ("Evidence not retrieved", None),
    "Suggested next checks": (
        "Next checks",
        "owner to be assigned by the analyst; SLA not modelled",
    ),
    "Access footprint": (
        "Access footprint",
        "every table read, and which of them the verdict used",
    ),
    "Second opinion": ("Second opinion", None),
    "Review": (
        "Critic review",
        "0-3 x 3 rubric: accuracy, appropriateness, actionability",
    ),
    "World state": (
        "World state",
        "Meterless World Model: what is already known about this account",
    ),
}
FOLDED = ("Memory used", "Memory updated")  # also listed under "Correct the agent"
DROPPED = ("Investigation timeline",)  # the right-hand column shows it step by step
PREAMBLE_SHOWN_ELSEWHERE = ("# ", "**Verdict:", "Score factors:", "Priority:", "Why:")
CITATION = re.compile(r"\s+\[([^\]|]+) \| (q\d+)\]\s*$", re.MULTILINE)


def report_sections(body: str) -> list[tuple[str, str]]:
    """(title, markdown) per report section; the preamble's title is ''. Only the report's own
    titles start a section: the memory context quoted inside it has '## domain' lines of its own,
    which are demoted to bold text."""
    known = tuple(SECTIONS) + FOLDED + DROPPED
    out = [["", []]]
    for line in body.splitlines():
        if line.startswith("## "):
            title = line[3:]
            if title.startswith(known):
                out.append([title, []])
                continue
            line = f"**{title}**"
        out[-1][1].append(line)
    return [(title, "\n".join(lines)) for title, lines in out]


def show_report(body: str) -> None:
    folded = []
    for title, text in report_sections(body):
        text = CITATION.sub(r"  `\1 | \2`", text).strip()
        if not title:
            for paragraph in text.split("\n\n"):
                if paragraph.strip() and not paragraph.startswith(PREAMBLE_SHOWN_ELSEWHERE):
                    st.markdown(paragraph)
            continue
        base, _, detail = title.partition(" (")
        if base.startswith(DROPPED):
            continue
        if base.startswith(FOLDED):
            folded.append((title, text))
            continue
        short, help_text = next(
            (v for k, v in SECTIONS.items() if base.startswith(k)), (base, None)
        )
        st.subheader(short, help=help_text or detail.rstrip(")") or None)
        st.markdown(text)
    for title, text in folded:
        with st.expander(title):
            st.markdown(text)


def trace_step(s: dict) -> tuple[str, str, str | None]:
    """One trace step as (role, text, sql): every kind renders the same way."""
    k = s["kind"]
    if k == "plan":
        text = "fetch " + ", ".join(s["evidence"]) + f"  \n_{s['reason'][:140]}_"
        if s.get("llm_drafted"):
            text += f"  \nmodel drafted {s['llm_drafted']} SQL"
        return "planner", text, None
    if k == "query":
        return (
            "retriever",
            f"{s['step']} {s['evidence']} → {s['rows']} rows ({s['backend']}, {s['elapsed_ms']} ms)",
            s["sql"],
        )
    if k == "query_failed":
        return "retriever", f"⚠ {s['step']} {s['evidence']} failed: {s['error'][:160]}", None
    if k == "rejected_llm_sql":
        return "planner", f"⚠ model SQL for {s['step']} rejected: {s['why']}", None
    if k == "assess":
        if s["verdict"]:
            return "evaluator", f"round {s['round']}: verdict **{s['verdict']}**", None
        return "evaluator", f"round {s['round']}: needs more: " + ", ".join(s["missing"]), None
    if k == "second_opinion":
        ag = s.get("agrees")
        outcome_text = (
            "✓ agrees"
            if ag
            else ("no usable opinion; rules stand" if ag is None else "⚠ disagrees, escalating")
        )
        text = f"second opinion ({s.get('source')}): model says **{s.get('verdict')}**, "
        text += f"rules say **{s.get('rules_verdict')}** → {outcome_text}"
        return "evaluator", text, None
    if k == "critique":
        sc = s.get("scores")
        if sc is None:
            return "critic", f"⚠ unavailable: {s.get('note')}", None
        text = (
            f"attempt {s['attempt']}: accuracy {sc['accuracy']} · appropriateness "
            f"{sc['appropriateness']} · actionability {sc['actionability']} = {s['total']}/9 "
            + ("✓ pass" if s["passed"] else "⚠ fail")
        )
        if s.get("fix"):
            text += f" → fix: {s['fix']}"
        return "critic", text, None
    if k == "revise":
        return "reporter", "revised the summary once", None
    if k == "llm":
        model = s.get("routed_model") or s.get("model_served")
        tokens = (s.get("usage") or {}).get("total_tokens", "?")
        return "model", f"{s['agent']} ← {model} ({tokens} tokens)", None
    if k == "recall":
        return "H-MEM", f"recalled {len(s['hits'])} memories", None
    if k == "dossier":
        known = "known" if s["known"] else "new"
        return "World Model", f"account {known}, {len(s['prior_alerts'])} prior alert(s)", None
    if k == "remember":
        text = f"{len(s['created'])} created, {len(s['reconfirmed'])} re-verified, "
        text += f"{len(s['superseded'])} superseded"
        return "H-MEM", text, None
    if k == "confidence":
        return (
            "evaluator",
            f"score {s['score']:.2f} ({s['label']}): " + "; ".join(s["factors"]),
            None,
        )
    if k == "ingest":
        return (
            "World Model",
            f"+{s['appended']} events ({s['entities']} entities, {s['events']} events total)",
            None,
        )
    return k, str({x: y for x, y in s.items() if x not in ("kind", "t")})[:160], None


# ------------------------------------------------------------------ sidebar: mode + alert
# ------------------------------------------------------------------ sidebar: mode + alert
with st.sidebar:
    st.title("ExportWhy")
    st.caption(
        "Why does this export alert need investigation? Evidence from three sources, federated by Zetaris."
    )
    mode = st.radio(
        "Run mode",
        [
            "Live (Zetaris + Nightshift)",
            "Live data, stub model",
            "Offline sample (SQLite + stub)",
        ],
        index=2,
    )
    os.environ["SAMPLE_MODE"] = "true" if mode.startswith("Offline") else "false"
    os.environ["LLM_STUB"] = "true" if mode.startswith("Live data") else "false"
    outage = st.multiselect(
        "Simulate an outage",
        ["tickets", "policy", "logs"],
        help="The data layer fails for that source; watch the verdict degrade to cannot-confirm instead of guessing.",
    )
    os.environ["SIMULATE_OUTAGE"] = ",".join(outage)
    try:
        backend = get_backend()
        alerts = backend.query(
            "SELECT a.alert_id, a.rule, a.severity, e.account_id, e.dataset_id, e.rows_exported "
            "FROM dexa.alerts a JOIN dexa.export_events e ON e.event_id = a.event_id ORDER BY a.alert_id",
            limit=200,
        ).rows
    except DataLayerError as exc:
        st.error(f"data layer: {exc}")
        alerts = []
    labels = {
        r[
            "alert_id"
        ]: f"{r['alert_id']} · {r['severity']} · {r['account_id']} → {r['dataset_id']} ({r['rows_exported']} rows)"
        for r in alerts
    }
    alert_id = (
        st.selectbox("Alert", list(labels), format_func=lambda k: labels[k]) if labels else None
    )
    if alert_id:
        st.caption(next(r["rule"] for r in alerts if r["alert_id"] == alert_id))
    go = st.button(
        "Triage this alert",
        type="primary",
        disabled=not alert_id,
        use_container_width=True,
    )
    st.divider()
    st.caption(
        f"data: {backend.name if alerts else '-'} · model: {'stub' if os.environ['SAMPLE_MODE'] == 'true' or os.environ['LLM_STUB'] == 'true' else 'nightshift'}"
    )

# ------------------------------------------------------------------ run
if go and alert_id:
    with st.spinner(f"Investigating {alert_id} ..."):
        try:
            report, outcome = run(Task(alert_id=alert_id))
            st.session_state["last"] = (alert_id, report, outcome)
        except DataLayerError as exc:
            st.error(f"data layer error: {exc}")

if "last" not in st.session_state:
    st.markdown("### Pick an alert on the left and press **Triage this alert**.")
    st.markdown(
        "The agent plans which evidence it needs, queries Zetaris (access log, policy and tickets as one schema), "
        "checks whether the evidence is sufficient and consistent, and reports a verdict with a citation on every evidence line, "
        "the conflicts it found, what it could not confirm, and which tables and sensitive columns it touched."
    )
    st.stop()

alert_id, report, outcome = st.session_state["last"]
trace_path = (
    config.TRACE_DIR / f"{outcome['run_id']}.json"
)  # the trace of THIS outcome, not the newest file
trace = json.loads(trace_path.read_text()) if trace_path.exists() else {}

# ------------------------------------------------------------------ verdict banner
verdict = outcome["verdict"]
conf = outcome.get("confidence") or {}
prio = outcome.get("priority")
tstatus = outcome.get("status", "complete")
banner = f"**{alert_id} → {verdict.upper()}**  —  " + (
    "; ".join(outcome["reasons"]) or "no reasons recorded"
)
if tstatus != "complete":
    banner = f"[{tstatus.upper()}] " + banner
getattr(st, VERDICT_STYLE.get(verdict, "info"))(banner)
queue = outcome.get("queue", "none")
queue_value = (
    prio["bucket"] if prio else {"manual-review": "review", "none": "none"}.get(queue, queue)
)
queue_note = (
    f"priority {prio['score']:.0f}/10: " + "; ".join(prio["factors"])
    if prio
    else ("manual review: unresolved evidence" if queue == "manual-review" else "no action")
)
c0, c1, c2, c3, c4, c5 = st.columns(6)
c0.metric(
    "evidence score",
    f"{conf.get('score', 0):.2f}",
    help="heuristic, not a probability: evidence completeness, second-opinion agreement, critic score, open gaps",
)
c1.metric(
    "queue", queue_value, help="P1-P3 from the priority score; 'review' when evidence is unresolved"
)
c2.metric("rounds", outcome["rounds"], help="evidence rounds the Evaluator asked for (max 3)")
c3.metric("facts cited", outcome["facts"])
c4.metric("cannot confirm", len(outcome["gaps"]))
c5.metric("sensitive cols", len(outcome["disclosure"]["sensitive_columns_touched"]))
notes = []
if conf.get("factors"):
    notes.append(f"score {conf['score']:.2f} ({conf.get('label')}): " + "; ".join(conf["factors"]))
notes.append(queue_note)
if outcome.get("llm") == "stub":
    notes.append("offline: the second opinion and the critic are deterministic stubs")
st.caption(" · ".join(notes))

# ------------------------------------------------------------------ verdict feedback
try:
    cal = st.session_state.get("cal") or Memory().calibration()
except Exception:  # memory store unavailable or mid-write
    cal = {}
fb1, fb2, fb3, fb4 = st.columns([1.3, 1.3, 1.2, 4.2])
if fb1.button("👍 agree", key="v-agree", help="record that you agree with this verdict"):
    st.session_state["cal"] = Memory().verdict_feedback(alert_id, True)
    st.rerun()
if fb2.button("👎 disagree", key="v-disagree", help="record that you disagree with this verdict"):
    st.session_state["cal"] = Memory().verdict_feedback(alert_id, False)
    st.rerun()
with fb3.popover("tally", help="analyst verdict feedback so far (humans only)"):
    if cal:
        st.table(
            [
                {
                    "verdict": k,
                    "agree": v["agree"],
                    "disagree": v["disagree"],
                    "verdicts": v["verdicts"],
                }
                for k, v in cal.items()
            ]
        )
    else:
        st.caption("no verdict feedback recorded yet")
mine = cal.get(verdict)
if mine:
    fb4.caption(
        f"{verdict}: {mine['agree']} agree / {mine['disagree']} disagree over {mine['verdicts']} verdict(s)"
    )

# ------------------------------------------------------------------ report + trace
left, right = st.columns([3, 2])
with left:
    st.subheader("What the agent found")
    show_report(report.split("\n_trace:")[0])
with right:
    st.subheader("How the agent got there")
    for s in trace.get("steps", []):
        role, text, sql = trace_step(s)
        label = f"`{s['t']:.1f}s` **{role}** · {text}"
        if sql:
            with st.expander(label):
                st.code(sql, language="sql")
        else:
            with st.container(border=True):
                st.markdown(label)
    with st.expander("Raw JSON trace"):
        st.json(trace)

# ------------------------------------------------------------------ correct the agent (H-MEM feedback)
st.divider()
st.subheader("Correct the agent")
st.caption(
    "Each fact above was stored in H-MEM with its provenance. Mark one as wrong and the trust ledger records the change: "
    "confidence drops by 0.20, the record gets a `review` tag and ranks lower in the next investigation. Helpful adds 0.05."
)
try:
    mem = Memory()
    records = [m for m in mem.for_alert(alert_id) if "kind:verdict" not in (m.get("tags") or [])]
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    for m in records:
        c_text, c_conf, c_ok, c_bad = st.columns([7, 1.2, 1, 1])
        flags = (" · superseded" if m["superseded"] else "") + (
            " · review" if "review" in (m.get("tags") or []) else ""
        )
        c_text.markdown(f"`{m['id']}` {redact(m['content'])[0]}")
        c_conf.markdown(f"**{float(m['confidence']):.2f}**{flags}")
        if c_ok.button("helpful", key=f"ok-{m['id']}"):
            mem.feedback(m["id"], "helpful")
            st.session_state["flash"] = (
                f"{m['id']}: helpful, +0.05 confidence, ledger entry written"
            )
            st.rerun()
        if c_bad.button("wrong", key=f"bad-{m['id']}", type="primary"):
            mem.feedback(m["id"], "wrong")
            st.session_state["flash"] = (
                f"{m['id']}: marked wrong, -0.20 confidence, tagged review, ledger entry written"
            )
            st.rerun()
    pick = st.selectbox(
        "Trust-ledger history for",
        [m["id"] for m in records] or ["-"],
        key="ledger-pick",
    )
    if records:
        hist = mem.history(pick)
        st.table(
            [
                {
                    "action": h["action"],
                    "actor": h["actor"],
                    "confidence": f"{h['confidence_before']} → {h['confidence_after']}"
                    if h["confidence_before"] is not None
                    else (h["confidence_after"] if h["confidence_after"] is not None else ""),
                    "details": json.dumps(h["details"])[:80],
                }
                for h in hist
            ]
        )
    st.caption(f"ledger totals: {mem.ledger_stats()}")
except Exception as exc:
    st.caption(f"memory unavailable: {exc}")
