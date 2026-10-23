"""ExportWhy: single-page UI for the demo. Everything is produced by the same agent loop as the CLI
(agent.main.run); this page only renders the report and the trace.

    .venv/bin/streamlit run ui/app.py
"""

from __future__ import annotations

import json
import os
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
VERDICT_STYLE = {"compliant": "success", "investigate": "error", "cannot-confirm": "warning"}

# ------------------------------------------------------------------ sidebar: mode + alert
with st.sidebar:
    st.title("ExportWhy")
    st.caption(
        "Why does this export alert need investigation? Evidence from three sources, federated by Zetaris."
    )
    mode = st.radio(
        "Run mode",
        ["Live (Zetaris + Nightshift)", "Live data, stub model", "Offline sample (SQLite + stub)"],
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
        "Triage this alert", type="primary", disabled=not alert_id, use_container_width=True
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
        "checks whether the evidence is sufficient and consistent, and reports a verdict with a citation per claim, "
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
c0, c1, c2, c3, c4, c5 = st.columns(6)
c0.metric(
    "evidence/review score",
    f"{conf.get('score', 0):.2f}",
    conf.get("label"),
    help="heuristic, not a probability: evidence completeness, second-opinion agreement, critic score, open gaps",
)
c1.metric(
    "queue",
    {"manual-review": "manual review", "none": "not queued"}.get(queue, queue),
    (
        f"{prio['score']:.0f}/10"
        if prio
        else ("unresolved evidence" if queue == "manual-review" else "no action")
    ),
)
c2.metric("evaluator rounds", outcome["rounds"])
c3.metric("facts cited", outcome["facts"])
c4.metric("cannot confirm", len(outcome["gaps"]))
c5.metric("sensitive columns read", len(outcome["disclosure"]["sensitive_columns_touched"]))
if conf.get("factors"):
    st.caption(
        "score: "
        + "; ".join(conf["factors"])
        + ((" · priority: " + "; ".join(prio["factors"])) if prio else "")
        + (
            " · offline: the second opinion is a deterministic stub"
            if outcome.get("llm") == "stub"
            else ""
        )
    )
fb1, fb2, fb3 = st.columns([1, 1, 6])
if fb1.button("👍 agree with verdict", key="v-agree"):
    st.session_state["cal"] = Memory().verdict_feedback(alert_id, True)
    st.rerun()
if fb2.button("👎 disagree", key="v-disagree"):
    st.session_state["cal"] = Memory().verdict_feedback(alert_id, False)
    st.rerun()
try:
    cal = st.session_state.get("cal") or Memory().calibration()
except Exception:  # memory store unavailable or mid-write
    cal = {}
if cal:
    fb3.caption(
        "analyst feedback tally (humans only): "
        + "; ".join(
            f"{k}: {v['agree']} agree / {v['disagree']} disagree over {v['verdicts']} verdict(s)"
            for k, v in cal.items()
        )
    )

# ------------------------------------------------------------------ report + trace
left, right = st.columns([3, 2])
with left:
    body = report.split("\n_trace:")[0]
    st.markdown(body.replace("# Export-alert triage", "## Export-alert triage"))
with right:
    st.subheader("How the agent got there")
    for s in trace.get("steps", []):
        k = s["kind"]
        if k == "plan":
            st.markdown(
                f"**{s['t']:.1f}s · planner** → {', '.join(s['evidence'])}  \n_{s['reason'][:140]}_"
                + (f"  \nmodel drafted {s['llm_drafted']} SQL" if s.get("llm_drafted") else "")
            )
        elif k == "query":
            with st.expander(
                f"{s['t']:.1f}s · {s['step']} {s['evidence']} → {s['rows']} rows ({s['backend']}, {s['elapsed_ms']} ms)"
            ):
                st.code(s["sql"], language="sql")
        elif k == "query_failed":
            st.error(f"{s['t']:.1f}s · {s['step']} {s['evidence']} failed: {s['error'][:160]}")
        elif k == "rejected_llm_sql":
            st.warning(f"{s['t']:.1f}s · planner rejected model SQL for {s['step']}: {s['why']}")
        elif k == "assess":
            st.markdown(
                f"**{s['t']:.1f}s · evaluator** round {s['round']}: "
                + (
                    f"verdict **{s['verdict']}**"
                    if s["verdict"]
                    else "needs more: " + ", ".join(s["missing"])
                )
            )
        elif k == "second_opinion":
            ag = s.get("agrees")
            (st.success if ag else st.warning)(
                f"{s['t']:.1f}s · evaluator second opinion ({s.get('source')}): model says **{s.get('verdict')}**, rules say **{s.get('rules_verdict')}** → "
                + (
                    "agrees"
                    if ag
                    else "no usable opinion; rules stand"
                    if ag is None
                    else "disagrees, escalating"
                )
            )
        elif k == "critique":
            sc = s.get("scores")
            if sc is None:
                st.warning(f"{s['t']:.1f}s · critic unavailable: {s.get('note')}")
            else:
                (st.success if s["passed"] else st.warning)(
                    f"{s['t']:.1f}s · critic attempt {s['attempt']}: accuracy {sc['accuracy']} · appropriateness {sc['appropriateness']} · actionability {sc['actionability']} = {s['total']}/9"
                    + (f" → fix: {s['fix']}" if s.get("fix") else "")
                )
        elif k == "revise":
            st.info(f"{s['t']:.1f}s · reporter revised the summary once")
        elif k == "llm":
            st.caption(
                f"{s['t']:.1f}s · {s['agent']} ← {s.get('routed_model') or s.get('model_served')} ({(s.get('usage') or {}).get('total_tokens', '?')} tokens)"
            )
        elif k == "recall":
            st.caption(f"{s['t']:.1f}s · H-MEM recalled {len(s['hits'])} memories")
        elif k == "dossier":
            st.caption(
                f"{s['t']:.1f}s · World Model: account {'known' if s['known'] else 'new'}, {len(s['prior_alerts'])} prior alert(s)"
            )
        elif k == "remember":
            st.caption(
                f"{s['t']:.1f}s · H-MEM: {len(s['created'])} created, {len(s['reconfirmed'])} re-verified, {len(s['superseded'])} superseded"
            )
        elif k == "ingest":
            st.caption(
                f"{s['t']:.1f}s · World Model: +{s['appended']} events ({s['entities']} entities, {s['events']} events total)"
            )
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
        "Trust-ledger history for", [m["id"] for m in records] or ["-"], key="ledger-pick"
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
