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
import streamlit.components.v1

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import config
from agent.catalog import TABLES
from agent.main import run
from agent.memory import Memory
from agent.roles import TEMPLATES, Task, handoff, redact
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
# built from the structured outcome instead (first screen, grouped evidence) or shown step by step
# in the right-hand column
DROPPED = (
    "Evidence",
    "Conflicts",
    "Cannot be confirmed",
    "Evidence not retrieved",
    "Suggested next checks",
    "Investigation timeline",
)
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
        if (
            not title
        ):  # the preamble: verdict, reasons, summary; the banner and the first screen show them
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


def shorten(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def trace_step(s: dict) -> tuple[str, str, str | None]:
    """One trace step as (role, text, sql): every kind renders the same way."""
    k = s["kind"]
    if k == "plan":
        text = "fetch " + ", ".join(s["evidence"]) + f"  \n_{shorten(s['reason'])}_"
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
        default=[o for o in st.query_params.get("outage", "").split(",") if o],
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
    wanted = st.query_params.get("alert")  # deep link: ?alert=AL-004 opens that alert triaged
    preselect = list(labels).index(wanted) if wanted in labels else 0
    alert_id = (
        st.selectbox("Alert", list(labels), index=preselect, format_func=lambda k: labels[k])
        if labels
        else None
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
if wanted and alert_id == wanted and st.session_state.get("deep_link") != wanted:
    st.session_state["deep_link"] = wanted
    go = True
if go and alert_id:
    with st.spinner(f"Investigating {alert_id} ..."):
        try:
            report, outcome = run(Task(alert_id=alert_id))
            st.session_state["last"] = (alert_id, report, outcome)
            st.session_state["just_ran"] = True
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

selected = alert_id
alert_id, report, outcome = st.session_state["last"]
if selected and selected != alert_id:
    st.info(
        f"Showing the previous result ({alert_id}). Press **Triage this alert** to run {selected}."
    )
if st.session_state.pop("just_ran", False):  # a fresh result starts at the top of the page
    st.components.v1.html(
        "<script>window.parent.document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 0);</script>",
        height=0,
    )
trace_path = (
    config.TRACE_DIR / f"{outcome['run_id']}.json"
)  # the trace of THIS outcome, not the newest file
trace = json.loads(trace_path.read_text()) if trace_path.exists() else {}

# ------------------------------------------------------------------ first screen: conclusion
verdict = outcome["verdict"]
conf = outcome.get("confidence") or {}
prio = outcome.get("priority")
status = outcome.get("status", "complete")
event = next((r for r in alerts if r["alert_id"] == alert_id), {})
if status != "complete":
    skipped = outcome.get("missing") or []
    st.warning(
        f"Run status: **{status}**. "
        + (
            "Evidence not retrieved: " + ", ".join(skipped) + ". "
            if skipped
            else "The run stopped early. "
        )
        + "The verdict below stands on what was retrieved."
    )
reasons = outcome["reasons"] or ["no reasons recorded"]
headline = f"**{alert_id} → {verdict.upper()}**"
if event:
    headline += f" · {event['account_id']} → {event['dataset_id']}, {event['rows_exported']:,} rows"
headline += f"  \n{reasons[0]}"
if len(reasons) > 1:
    headline += f" (+{len(reasons) - 1} more below)"
getattr(st, VERDICT_STYLE.get(verdict, "info"))(headline)

# verdict feedback, right under the conclusion
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

# ------------------------------------------------------------------ first screen: why, and what next
evidence = [e for e in outcome["evidence"] if e["kind"] != "gap"]
step_class = {
    q["id"]: q["evidence"]
    for s in trace.get("steps", [])
    if s["kind"] == "plan"
    for q in s.get("steps", [])
}


def cite_tag(e: dict) -> str:
    c = e["cites"][0]
    return f"`{c['table']}.{c.get('column') or '*'} | {c['step']}`"


def evidence_about(prefix: str) -> dict | None:
    return next((e for e in evidence if e["text"].startswith(prefix)), None)


ROWS_CONFLICT = re.compile(r"(\d+) rows exported exceed the ticket's max_rows (\d+)")
TICKET = re.compile(r"ticket (T-\d+)")

why, nxt = st.columns([1.2, 1])
with why:
    st.subheader(
        "Why", help="conflicts between the sources, what could not be confirmed, the reasons"
    )
    for conflict in outcome["conflicts"]:
        with st.container(border=True):
            m = ROWS_CONFLICT.search(conflict)
            ticket = TICKET.search(conflict)
            if m and ticket:
                a, b = st.columns(2)
                a.metric("exported", f"{int(m.group(1)):,}")
                b.metric(f"approved by {ticket.group(1)}", f"{int(m.group(2)):,}")
                line = evidence_about(f"Ticket {ticket.group(1)}")
                st.markdown(
                    f"⚠ {conflict}" + (f"  \n{line['text']} {cite_tag(line)}" if line else "")
                )
            else:
                st.markdown(f"⚠ {conflict}")
    for gap in outcome["gaps"]:
        with st.container(border=True):
            st.markdown(f"**Cannot confirm:** {gap}")
    unfetched = [m for m in (outcome.get("missing") or []) if m in TEMPLATES]
    if unfetched:
        with st.container(border=True):
            st.markdown(
                "**Evidence not retrieved** (budget): "
                + "; ".join(f"{m}: {TEMPLATES[m][0]}" for m in unfetched)
            )
    if len(reasons) > 1 or not (outcome["conflicts"] or outcome["gaps"]):
        st.markdown("\n".join(f"- {r}" for r in reasons))
with nxt:
    st.subheader("Next checks", help="owner to be assigned by the analyst; SLA not modelled")
    for step in outcome["next_steps"]:
        owner, done = handoff(step)
        with st.container(border=True):
            st.markdown(f"{step}  \n_owner: {owner} · done when: {done}_")
    if not outcome["next_steps"]:
        st.caption("none: the verdict is grounded and no action is required")

# ------------------------------------------------------------------ evidence and the trace
GROUPS = [
    ("Cited ticket", ("ticket_cited",)),
    ("Policy", ("policy",)),
    ("Other authorisation", ("tickets_account", "exceptions")),
    ("Context", ("alert", "account", "dataset", "sensitivity", "account_history", "destination")),
]
try:
    mem = Memory()
    records = {
        redact(m["content"])[0]: m
        for m in mem.for_alert(alert_id)
        if "kind:verdict" not in (m.get("tags") or [])
    }
except Exception as exc:  # memory store unavailable or mid-write
    mem, records = None, {}
    st.caption(f"memory unavailable: {exc}")


def fact_feedback(e: dict, key: str) -> None:
    """A small popover per evidence line: the stored memory's confidence, helpful / wrong."""
    m = records.get(e["text"])
    if not (m and mem):
        return
    flags = (" · superseded" if m["superseded"] else "") + (
        " · review" if "review" in (m.get("tags") or []) else ""
    )
    with st.popover("✎", help="correct this fact (H-MEM trust ledger)"):
        st.caption(f"`{m['id']}` confidence **{float(m['confidence']):.2f}**{flags}")
        a, b = st.columns(2)
        if a.button("helpful", key=f"ok-{key}"):
            mem.feedback(m["id"], "helpful")
            st.session_state["flash"] = (
                f"{m['id']}: helpful, +0.05 confidence, ledger entry written"
            )
            st.rerun()
        if b.button("wrong", key=f"bad-{key}", type="primary"):
            mem.feedback(m["id"], "wrong")
            st.session_state["flash"] = (
                f"{m['id']}: marked wrong, -0.20 confidence, tagged review, ledger entry written"
            )
            st.rerun()


left, right = st.columns([3, 2])
with left:
    st.subheader(
        "Evidence", help="every line ends with the table, column and query step it came from"
    )
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    queries = {s["step"]: s for s in trace.get("steps", []) if s["kind"] == "query"}
    for title, classes in GROUPS:
        lines = [e for e in evidence if step_class.get(e["cites"][0]["step"]) in classes]
        if not lines:
            continue
        st.markdown(f"**{title}**")
        for i, e in enumerate(lines):
            text_col, fb_col = st.columns([12, 1])
            text_col.markdown(f"- {e['text']} {cite_tag(e)}")
            with fb_col:
                fact_feedback(e, f"{title}-{i}")
        used = sorted({e["cites"][0]["step"] for e in lines}, key=lambda q: int(q[1:]))
        with st.expander("queries: " + ", ".join(used)):
            for q in used:
                if q in queries:
                    s = queries[q]
                    st.caption(
                        f"{q} {s['evidence']} · {s['rows']} rows · {s['backend']}, {s['elapsed_ms']} ms"
                    )
                    st.code(s["sql"], language="sql")
    other = [e for e in evidence if step_class.get(e["cites"][0]["step"]) is None]
    for e in other:
        st.markdown(f"- {e['text']} {cite_tag(e)}")
    show_report(report.split("\n_trace:")[0])


def cross_source(tables: list[str]) -> bool:
    """True when the query joined tables that live in different source databases."""
    systems = {TABLES[t]["system"].split(" ")[0] for t in tables if t in TABLES}
    return len(systems) > 1


def _conf(before, after) -> str:
    if before is not None:
        return f"{float(before):.2f} → {float(after):.2f}"
    return f"{float(after):.2f}" if after is not None else ""


def round_title(n: int) -> str:
    return "Identify the alert and the account" if n == 0 else f"Round {n}"


with right:
    st.subheader("How the agent got there")
    rounds: dict[int, list] = {}
    pending: list = []
    for s in trace.get("steps", []):
        if s["kind"] == "assess":
            rounds.setdefault(s["round"], []).extend(pending + [s])
            pending = []
        else:
            pending.append(s)
    for n, steps in rounds.items():
        with st.container(border=True):
            st.markdown(f"**{round_title(n)}**")
            for s in steps:
                if s["kind"] == "llm":
                    continue
                if s["kind"] == "query":
                    joined = " · cross-source" if cross_source(s.get("tables") or []) else ""
                    st.markdown(f"- `{s['step']}` {s['evidence']} → {s['rows']} rows{joined}")
                    continue
                role, text, _ = trace_step(s)
                st.markdown(f"- **{role}** {text}")
    if pending:
        with st.container(border=True):
            st.markdown("**Review and memory**")
            for s in pending:
                if s["kind"] == "llm":
                    continue
                role, text, _ = trace_step(s)
                st.markdown(f"- **{role}** {text}")
    with st.expander("Technical details: SQL, model calls, receipts"):
        for s in trace.get("steps", []):
            if s["kind"] == "query":
                st.caption(
                    f"`{s['t']:.1f}s` {s['step']} {s['evidence']} · {s['rows']} rows · "
                    f"{s['backend']}, {s['elapsed_ms']} ms"
                )
                st.code(s["sql"], language="sql")
            elif s["kind"] == "llm":
                tokens = (s.get("usage") or {}).get("total_tokens", "?")
                st.caption(
                    f"`{s['t']:.1f}s` {s['agent']} ← {s.get('routed_model') or s.get('model_served')} "
                    f"· {tokens} tokens · receipt {s.get('receipt_id') or '-'}"
                )
    with st.expander("Raw JSON trace"):
        st.json(trace)

# ------------------------------------------------------------------ trust ledger (folded)
with st.expander("Trust ledger (H-MEM): every stored fact, its confidence and its history"):
    st.caption(
        "Each evidence line above was stored with its provenance. Marking one wrong drops its confidence "
        "by 0.20, tags it `review` and ranks it lower next time; helpful adds 0.05. Use the ✎ next to a line."
    )
    if records and mem:
        st.table(
            [
                {
                    "id": m["id"],
                    "confidence": f"{float(m['confidence']):.2f}",
                    "flags": ("superseded " if m["superseded"] else "")
                    + ("review" if "review" in (m.get("tags") or []) else ""),
                    "fact": text[:90],
                }
                for text, m in records.items()
            ]
        )
        pick = st.selectbox("history for", [m["id"] for m in records.values()], key="ledger-pick")
        st.table(
            [
                {
                    "action": h["action"],
                    "actor": h["actor"],
                    "confidence": _conf(h["confidence_before"], h["confidence_after"]),
                    "details": json.dumps(h["details"])[:80],
                }
                for h in mem.history(pick)
            ]
        )
        st.caption(f"ledger totals: {mem.ledger_stats()}")
    else:
        st.caption("no stored facts for this alert")
