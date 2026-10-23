"""ExportWhy: an agent that explains why a sensitive-data export alert does or does not need
investigation, from evidence spread across access logs, data-access policies and approval tickets,
all reached through Zetaris as one SQL schema.

  python -m agent.main alert AL-003             triage one alert
  python -m agent.main evaluate                 triage every alert and score against ground truth
  python -m agent.main memory                   what the agent remembers across investigations
  python -m agent.main feedback <mem-id> wrong  correct a memory (helpful | not_helpful | wrong)
  python -m agent.main health                   data layer + model provider status
SAMPLE_MODE=true runs fully offline (SQLite twin + deterministic stub).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

from . import catalog, config
from .llm import CountedLLM, get_llm
from .memory import Memory
from .world import World
from .roles import MAX_ROUNDS, Critic, Evaluator, Planner, Reporter, Retriever, Task, redact

MAX_MODEL_CALLS = 9     # 3 planning + 2 second opinions + 1 summary + 2 critiques + 1 revision; retries excluded
from .trace import Trace
from .zetaris import DataLayerError, get_backend


def run(task: Task, persist_memory: bool = True, run_id: str | None = None) -> tuple[str, dict]:
    mode = "sample" if config.sample_mode() else "live"
    trace = Trace(task.describe(), mode, run_id=run_id)
    backend, llm = get_backend(), CountedLLM(get_llm(), max_calls=MAX_MODEL_CALLS)
    memory = Memory(persist=persist_memory)
    budget_s = float(config.env("TRIAGE_TIMEOUT_S", "480") or 480)   # whole-triage wall clock; beyond it: partial result, manual review
    deadline = trace.started + budget_s
    remaining = lambda: max(0.0, deadline - time.time())
    status = "complete"
    planner, retriever, evaluator, reporter, critic = (Planner(llm, trace, remaining), Retriever(backend, trace, remaining), Evaluator(trace, llm, remaining),
                                                       Reporter(llm, trace, remaining), Critic(llm, trace, remaining))

    # round 0: identify the alert and its export event, then the exporting account (keys for everything else)
    done: dict[str, list] = {}
    for s in retriever.run(planner.plan(task, ["alert"], "", "identify the alert and its export event")):
        done.setdefault(s.evidence, []).append(s)
    assessment = evaluator.assess(task, done, retriever.footprint, 0)
    outage = config.env("SIMULATE_OUTAGE")
    if outage:
        trace.data["simulated_outage"] = outage
    if not task.event_id:
        skipped = any(s.skipped for s in done.get("alert", []))
        if skipped or any(s.error for s in done.get("alert", [])):
            # the alert source is down, or the budget was gone before the first query: report that, do not pretend to know anything
            assessment.verdict = "cannot-confirm"
            if skipped:
                status = "timed-out"
                assessment.reasons.insert(0, f"time budget of {budget_s:.0f}s exhausted before the alert could be read; manual review needed")
                assessment.gaps.append("the alert and its export event were not retrieved (time budget)")
                assessment.next_steps.insert(0, "re-run the triage with a larger TRIAGE_TIMEOUT_S")
            else:
                status = "partial"
            confidence = {"score": 0.0, "label": "low", "factors": ["the alert source could not be queried" if not skipped else "no evidence retrieved"]}
            critique = {"attempt": 0, "scores": None, "total": None, "passed": None, "fix": None, "source": "none", "note": "skipped: no evidence to summarise", "review_status": "skipped"}
            template = reporter.template_summary(assessment)
            report = reporter.report(task, assessment, "", 0, summary=template, confidence=confidence, critique=critique, status=status)
            outcome = {"alert_id": task.alert_id, "verdict": "cannot-confirm", "reasons": assessment.reasons, "conflicts": [], "gaps": assessment.gaps,
                       "missing": [], "facts": 0, "rounds": 0, "disclosure": assessment.disclosure, "memory_recalled": 0, "evidence": [],
                       "next_steps": assessment.next_steps, "run_id": trace.run_id, "log_file": str(trace.log_file.relative_to(config.ROOT)),
                       "summary": template, "second_opinion": None, "critic": critique, "confidence": confidence,
                       "priority": None, "queue": "manual-review", "status": status, "timeline": _timeline(trace), "model_calls": llm.calls,
                       "memory": {"created": 0, "reconfirmed": 0, "superseded": 0}, "world": {"appended": 0}, "backend": backend.name, "llm": llm.name}
            path = trace.finish(outcome)
            return report + f"\n\n_trace: {path.relative_to(config.ROOT) if config.ROOT in path.parents else path} | data: {backend.name} | model: {llm.name}_\n", outcome
        raise DataLayerError(f"alert {task.alert_id} not found")
    for s in retriever.run(planner.plan(task, ["account"], "", "identify the exporting account")):
        done.setdefault(s.evidence, []).append(s)
    assessment = evaluator.assess(task, done, retriever.footprint, 0)

    mem_query = f"{task.account_id} {task.dataset_id} export alert ticket exception"
    memory_context, recalled = memory.recall(mem_query)
    trace.step("memory", "recall", query=mem_query, hits=[{**{k: v for k, v in m.items() if k != "content"}, "content": m["content"][:120]} for m in recalled])
    world = World(persist=persist_memory)
    world_context, dossier = world.dossier(task.account_id, task.alert_id)
    task.prior = dossier
    trace.step("world", "dossier", known=dossier["known"], prior_alerts=dossier["prior_alerts"], status_history=dossier["status_history"])
    planner_context = "\n".join(x for x in (memory_context, world_context) if x)

    rounds = 0
    reason = "evidence the verdict needs: " + ", ".join(assessment.missing) + (" (memory holds earlier findings on this account/dataset; re-verifying)" if recalled else "")
    while assessment.verdict is None and assessment.missing and rounds < MAX_ROUNDS:
        if time.time() > deadline:
            status = "timed-out"; trace.step("system", "timeout", budget_s=budget_s, phase="evidence rounds"); break
        rounds += 1
        classes = [m for m in assessment.missing if m not in done]
        if not classes:
            break
        for s in retriever.run(planner.plan(task, classes, planner_context, reason)):
            done.setdefault(s.evidence, []).append(s)
        assessment = evaluator.assess(task, done, retriever.footprint, rounds)
        reason = "evaluator: " + (", ".join(assessment.conflicts + assessment.gaps) or "evidence still insufficient") + " -> fetch " + ", ".join(assessment.missing)
    # a query the Retriever skipped for lack of budget is evidence the verdict never saw: that is a timeout too,
    # whether or not the wall clock has passed the deadline by the time the rounds end. Runs after every evidence
    # round (the main rounds and the one the second opinion may buy), on the assessment that round produced.
    noted_skipped: set[str] = set()

    def settle_budget(phase: str):
        nonlocal status, assessment
        skipped_classes = sorted(cls for cls, steps in done.items() if steps and all(s.skipped for s in steps))
        if set(skipped_classes) - noted_skipped:
            noted_skipped.update(skipped_classes)
            status = "timed-out"; trace.step("system", "timeout", budget_s=budget_s, phase=phase, skipped=skipped_classes)
        if assessment.verdict is None:
            assessment.verdict = assessment.derived.get("verdict") or "cannot-confirm"
        if status == "timed-out" and not assessment.derived.get("budget_settled"):
            assessment.derived["budget_settled"] = True
            assessment.verdict = "cannot-confirm"
            not_seen = sorted(set(assessment.derived.get("unfetched") or []) | set(assessment.missing) | set(skipped_classes))
            assessment.derived["unfetched"] = not_seen; assessment.missing = []
            assessment.reasons.insert(0, f"time budget of {budget_s:.0f}s exhausted before the evidence was complete"
                                      + (f" (not retrieved: {', '.join(not_seen)})" if not_seen else "") + "; manual review needed")
            assessment.next_steps.insert(0, "re-run the triage with a larger TRIAGE_TIMEOUT_S, or review the partial evidence below by hand")

    settle_budget("evidence rounds")

    # second opinion: the model judges the same evidence; a disagreement buys one more evidence round, then escalates
    if time.time() > deadline:
        status = status if status != "complete" else "partial"
        opinion = {"verdict": "skipped", "reasons": ["skipped: time budget exhausted"], "missing_evidence": [], "usable": False, "agrees": None, "source": "none"}
        trace.step("evaluator", "second_opinion", rules_verdict=assessment.verdict, **opinion)
    else:
        opinion = evaluator.second_opinion(task, assessment)
    if opinion.get("usable") and not opinion["agrees"]:
        extra = [m for m in opinion.get("missing_evidence", []) if m not in done]
        if extra and rounds < MAX_ROUNDS and time.time() < deadline:
            rounds += 1
            for s in retriever.run(planner.plan(task, extra, planner_context, "second opinion disagrees with the rules; fetching the evidence it asked for: " + ", ".join(extra))):
                done.setdefault(s.evidence, []).append(s)
            assessment = evaluator.assess(task, done, retriever.footprint, rounds)
            settle_budget("second-opinion evidence round")
            if time.time() < deadline:
                opinion = evaluator.second_opinion(task, assessment)
        if not opinion["agrees"]:
            assessment.conflicts.append(f"rules say {assessment.verdict}, model second opinion says {opinion['verdict']}: " + "; ".join(opinion.get("reasons", [])[:2]))
            assessment.reasons.insert(0, f"rules-based verdict {assessment.verdict} disputed by the model second opinion; a human must decide")
            assessment.next_steps.insert(0, "human review: resolve the disagreement between the rules and the model before closing the alert")
            assessment.verdict = "needs-human"
    opinion["rules_verdict"] = assessment.derived.get("verdict")
    trace.data["rounds"] = rounds

    # proposer + critic: draft the summary, score it, revise once if it fails the rubric
    if time.time() > deadline:
        status = status if status != "complete" else "partial"
        summary = reporter.template_summary(assessment)
        critique = {"attempt": 0, "scores": None, "total": None, "passed": None, "fix": None, "source": "none", "note": "skipped: time budget exhausted"}
        trace.step("critic", "critique", **critique)
    else:
        summary = reporter.draft_summary(task, assessment)
        if time.time() < deadline:
            critique = critic.review(task, assessment, summary, attempt=1)
        else:
            critique = {"attempt": 0, "scores": None, "total": None, "passed": None, "fix": None, "source": "none", "note": "skipped: time budget exhausted"}
            trace.step("critic", "critique", **critique)
        if critique["passed"] is False and critique.get("fix") and time.time() < deadline:
            summary = reporter.revise(task, assessment, summary, critique["fix"])
            if time.time() < deadline:
                second = critic.review(task, assessment, summary, attempt=2)
            else:
                second = {"attempt": 2, "scores": None, "total": None, "passed": None, "fix": None, "source": "none", "note": "skipped: time budget exhausted"}
                trace.step("critic", "critique", **second)
            second["fix_applied"] = critique["fix"]; second["first_total"] = critique["total"]
            critique = second
    # the rubric is a gate: a summary that did not pass (or could not be reviewed) is never delivered as-is
    if critique.get("passed") is True:
        critique["review_status"] = "passed"
    else:
        critique["review_status"] = "failed; deterministic summary used" if critique.get("passed") is False else "unavailable; deterministic summary used"
        summary = reporter.template_summary(assessment)
        trace.step("reporter", "summary_fallback", review_status=critique["review_status"])
    if time.time() > deadline and status == "complete":
        status = "partial"

    # confidence in the verdict: evidence completeness, second-opinion agreement, critic score, open gaps
    # evidence completeness: the decision-critical classes this alert needed, and how many actually returned
    needed = ["account", "policy"] + (["ticket_cited"] if task.ticket_ref else [])
    if assessment.derived.get("needs_auth") and not (task.ticket_ref and not any(c.startswith("ticket ") for c in assessment.conflicts) and (rows_tc := [st for st in done.get("ticket_cited", []) if st.result and st.result.rows])):
        needed += ["tickets_account", "exceptions"]          # widened search was required
    got = [c for c in needed if any(st.result for st in done.get(c, []))]
    completeness = len(got) / max(1, len(needed))
    agree_term = 1.0 if opinion.get("agrees") else (0.5 if opinion.get("agrees") is None else 0.0)
    critic_term = (critique["total"] / 9) if critique.get("total") is not None else 0.5
    conf = 0.5 * completeness + 0.3 * agree_term + 0.2 * critic_term
    if assessment.gaps:
        conf = min(conf, 0.6)
    unfetched = assessment.derived.get("unfetched") or []
    if unfetched:
        conf = min(conf, 0.85)
    if assessment.verdict == "needs-human" or status != "complete":
        conf = min(conf, 0.4)
    confidence = {"score": round(conf, 2), "label": "high" if conf >= 0.8 else "medium" if conf >= 0.6 else "low",
                  "factors": [f"evidence completeness {completeness:.2f}", "second opinion " + ("agrees" if opinion.get("agrees") else "unavailable" if opinion.get("agrees") is None else "disagrees"),
                              f"critic {critique.get('total', '-')}/9" if critique.get("total") is not None else "critic unavailable"]
                             + ([f"{len(assessment.gaps)} open gap(s)"] if assessment.gaps else []) + ([f"not retrieved: {', '.join(unfetched)}"] if unfetched else [])}
    trace.step("evaluator", "confidence", **confidence)

    rows_by_class = {cls: [r for st in steps if st.result for r in st.result.rows] for cls, steps in done.items()}
    world_stats = world.ingest_run(task, assessment, rows_by_class, trace.run_id)
    trace.step("world", "ingest", **world_stats, **world.stats())

    # remember what this investigation established (keys make re-runs idempotent) -- before the report, so it can list the updates
    actions: dict[str, list[str]] = {"created": [], "reconfirmed": [], "superseded": []}
    updates: dict[str, list[dict]] = {"created": [], "reconfirmed": [], "superseded": []}
    for f in assessment.facts:
        c = f.cites[0]
        rec, action = memory.remember(f.text, source=f"zetaris:{c['table']}:{c['step']}:{trace.run_id}", refs=[],
                                      tags=[f"alert:{task.alert_id}", f"account:{task.account_id}", f"dataset:{task.dataset_id}", f"kind:{f.kind}"],
                                      confidence=0.9 if f.kind != "gap" else 0.6, goal_run_id=trace.run_id, key=f"fact:{task.alert_id}:{f.key}")
        actions[action].append(rec.id); updates[action].append({"id": rec.id, "content": f.text, "source": f"{c['table']} via {c['step']}"})
    rec, action = memory.remember(f"Alert {task.alert_id}: verdict {assessment.verdict}; " + "; ".join(assessment.reasons), source=f"verdict:{trace.run_id}", refs=[],
                                  tags=[f"alert:{task.alert_id}", f"account:{task.account_id}", f"dataset:{task.dataset_id}", "kind:verdict"],
                                  confidence=0.85, goal_run_id=trace.run_id, key=f"verdict:{task.alert_id}")
    actions[action].append(rec.id); updates[action].append({"id": rec.id, "content": f"verdict {assessment.verdict}", "source": "evaluator"})
    d = assessment.disclosure
    fp, _ = memory.remember(f"Run {trace.run_id} ({task.alert_id}) accessed {', '.join(d['tables_accessed'])}; sensitive columns touched: {', '.join(d['sensitive_columns_touched']) or 'none'}; "
                            f"unnecessary tables: {', '.join(d['accessed_but_unused']) or 'none'}.", source=f"trace:{trace.run_id}", refs=[], tags=["footprint"], confidence=1.0,
                            mtype="general", goal_run_id=trace.run_id)
    trace.step("memory", "remember", **actions, footprint=fp.id, ledger=memory.ledger_stats())

    timeline = _timeline(trace)
    report = reporter.report(task, assessment, memory_context, rounds, world_context, summary=summary, critique=critique, second_opinion=opinion,
                             confidence=confidence, timeline=timeline, memory_updates=updates, status=status)

    outcome = {"alert_id": task.alert_id, "verdict": assessment.verdict, "reasons": assessment.reasons, "conflicts": assessment.conflicts, "gaps": assessment.gaps,
               "missing": assessment.missing, "facts": len(assessment.facts), "rounds": rounds, "disclosure": {k: v for k, v in d.items() if k != "columns_seen"},
               "evidence": [{"text": redact(f.text)[0], "kind": f.kind, "cites": f.cites} for f in assessment.facts],
               "next_steps": assessment.next_steps, "run_id": trace.run_id, "log_file": str(trace.log_file.relative_to(config.ROOT)),
               "summary": redact(summary)[0], "second_opinion": opinion, "critic": critique, "confidence": confidence,
               "priority": assessment.priority, "status": status, "timeline": timeline,
               "queue": ("manual-review" if assessment.verdict in ("cannot-confirm", "needs-human") else assessment.priority["bucket"] if assessment.priority else "none"),
               "memory_recalled": len(recalled), "memory": {k: len(v) for k, v in actions.items()}, "world": world_stats, "backend": backend.name, "llm": llm.name,
               "model_calls": llm.calls}
    path = trace.finish(outcome)
    shown = path.relative_to(config.ROOT) if config.ROOT in path.parents else path
    report += f"\n\n_trace: {shown} | evaluator rounds: {rounds} | data: {backend.name} | model: {llm.name}_\n"
    return report, outcome


def _timeline(trace) -> list[dict]:
    """Compact, judge-readable list of what happened, in order (from the trace)."""
    out = []
    for st in trace.data["steps"]:
        k = st["kind"]
        if k == "plan":
            out.append({"t": st["t"], "agent": "planner", "what": "fetch " + ", ".join(st["evidence"]) + (f" (model drafted {st['llm_drafted']} SQL)" if st.get("llm_drafted") else "")})
        elif k == "query":
            out.append({"t": st["t"], "agent": "retriever", "what": f"{st['step']} {st['evidence']}: {st['rows']} rows from {', '.join(st['tables'])} ({st['backend']}, {st['elapsed_ms']} ms)"})
        elif k == "query_failed":
            out.append({"t": st["t"], "agent": "retriever", "what": f"{st['step']} {st['evidence']} FAILED: {st['error'][:80]}"})
        elif k == "query_skipped":
            out.append({"t": st["t"], "agent": "retriever", "what": f"{st['step']} {st['evidence']} skipped: {st['why']}"})
        elif k == "assess":
            out.append({"t": st["t"], "agent": "evaluator", "what": (f"verdict {st['verdict']}" if st["verdict"] else "needs more: " + ", ".join(st["missing"]))})
        elif k == "second_opinion":
            out.append({"t": st["t"], "agent": "evaluator", "what": f"second opinion {st.get('verdict')} ({'agrees' if st.get('agrees') else 'no usable opinion' if st.get('agrees') is None else 'DISAGREES'}; {st.get('source')})"})
        elif k == "critique":
            sc = st.get("scores"); out.append({"t": st["t"], "agent": "critic", "what": (f"rubric {st['total']}/9 attempt {st['attempt']}" + (f", fix: {st['fix']}" if st.get("fix") else "")) if sc else f"unavailable ({st.get('note')})"})
        elif k == "revise":
            out.append({"t": st["t"], "agent": "reporter", "what": "revised the summary once"})
        elif k == "timeout":
            out.append({"t": st["t"], "agent": "system", "what": f"time budget {st['budget_s']:.0f}s exhausted during {st['phase']}"})
        elif k == "summary_fallback":
            out.append({"t": st["t"], "agent": "reporter", "what": f"review {st['review_status']}"})
        elif k == "model_unavailable":
            out.append({"t": st["t"], "agent": st["agent"], "what": f"model unavailable, fallback: {st.get('fallback')}"})
    return out


def evaluate(write_examples: bool) -> int:
    truth = {}
    gt = config.ROOT / "data" / "sample" / "ground_truth.csv"
    if gt.exists():
        with open(gt, newline="") as fh:
            truth = {r["alert_id"]: r for r in csv.DictReader(fh)}
    backend = get_backend()
    ids = [r["alert_id"] for r in backend.query(f"SELECT alert_id FROM {catalog.SCHEMA}.alerts ORDER BY alert_id", limit=500).rows]
    hits = 0; rows = []; traj = []
    for aid in ids:
        try:
            report, out = run(Task(alert_id=aid))
        except DataLayerError as exc:
            rows.append((aid, "ERROR", str(exc)[:60], truth.get(aid, {}).get("expected_verdict", "?"), "")); continue
        exp = truth.get(aid, {}).get("expected_verdict", "?")
        ok = out["verdict"] == exp; hits += ok
        rows.append((aid, out["verdict"], truth.get(aid, {}).get("scenario", ""), exp, "OK" if ok else "MISS"))
        tl = out.get("timeline", [])
        queries = [t for t in tl if t["agent"] == "retriever" and "rows from" in t["what"]]
        zero = [t for t in queries if ": 0 rows" in t["what"]]
        traj.append({"alert": aid, "verdict": out["verdict"], "rounds": out["rounds"], "queries": len(queries), "zero_row_queries": len(zero),
                     "model_calls": sum(1 for t in tl if t["agent"] in ("evaluator", "critic") and ("second opinion" in t["what"] or "rubric" in t["what"])),
                     "sensitive_cols": len(out["disclosure"]["sensitive_columns_touched"]), "confidence": out["confidence"]["score"],
                     "compliant_on_zero_rows": out["verdict"] == "compliant" and any(("ticket" in t["what"] or "exceptions" in t["what"]) and ": 0 rows" in t["what"] and "tickets_account" not in t["what"] for t in queries)})
        if write_examples:
            (config.ROOT / "output_examples").mkdir(exist_ok=True); (config.ROOT / "input_examples").mkdir(exist_ok=True)
            (config.ROOT / "output_examples" / f"{aid}.md").write_text(report)
            (config.ROOT / "input_examples" / f"{aid}.txt").write_text(f"python -m agent.main alert {aid}\n")
    print(f"{'alert':8s} {'verdict':15s} {'scenario':24s} {'expected':15s} result")
    for r in rows:
        print(f"{r[0]:8s} {r[1]:15s} {r[2]:24s} {r[3]:15s} {r[4]}")
    print(f"\n{hits}/{len(rows)} verdicts match ground truth" + (" (ground truth file missing)" if not truth else ""))
    if traj:
        n = len(traj)
        print("\ntrajectory (Alert-Bench style: judge the path, not only the decision)")
        print(f"{'alert':8s} {'verdict':15s} rounds queries zero-row sens conf")
        for t in traj:
            print(f"{t['alert']:8s} {t['verdict']:15s} {t['rounds']:6d} {t['queries']:7d} {t['zero_row_queries']:8d} {t['sensitive_cols']:4d} {t['confidence']:.2f}")
        print(f"mean rounds {sum(t['rounds'] for t in traj)/n:.1f}, mean queries {sum(t['queries'] for t in traj)/n:.1f}, "
              f"mean confidence {sum(t['confidence'] for t in traj)/n:.2f}, compliant verdicts resting on a zero-row authorisation query: {sum(t['compliant_on_zero_rows'] for t in traj)}")
    return 0 if hits == len(rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="exportwhy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("alert"); al.add_argument("id")
    al.add_argument("--simulate-outage", metavar="SOURCES", help="comma-separated: logs, policy, tickets (or table names); the data layer fails for them")
    ev = sub.add_parser("evaluate"); ev.add_argument("--write-examples", action="store_true")
    sub.add_parser("memory")
    fb = sub.add_parser("feedback"); fb.add_argument("memory_id"); fb.add_argument("kind", choices=["helpful", "not_helpful", "wrong"])
    fv = sub.add_parser("feedback-verdict"); fv.add_argument("alert_id"); fv.add_argument("judgement", choices=["agree", "disagree"]); fv.add_argument("--note", default="")
    sub.add_parser("health")
    args = ap.parse_args(argv)

    if args.cmd == "health":
        try:
            b = get_backend(); r = b.query("SELECT 1 AS one"); print(f"data layer: {b.name} OK ({r.rows})")
        except Exception as exc:  # noqa: BLE001
            print(f"data layer: FAIL {exc}")
        try:
            l = get_llm(); print(f"model provider: {l.name} {l.health()}")
        except Exception as exc:  # noqa: BLE001
            print(f"model provider: FAIL {exc}")
        return 0
    if args.cmd == "memory":
        for m in Memory().all():
            flag = " (superseded)" if m["superseded"] else ""
            print(f"{m['id']:10s} conf={float(m['confidence']):.2f}{flag} {m['content'][:110]}  <{m['source']}>")
        return 0
    if args.cmd == "feedback":
        Memory().feedback(args.memory_id, args.kind); print("recorded in the trust ledger"); return 0
    if args.cmd == "feedback-verdict":
        stats = Memory().verdict_feedback(args.alert_id, args.judgement == "agree", args.note)
        if stats is None:
            print(f"no verdict memory for {args.alert_id}; triage it first"); return 1
        print("recorded; calibration so far:", stats); return 0
    if args.cmd == "evaluate":
        return evaluate(args.write_examples)
    if getattr(args, "simulate_outage", None):
        os.environ["SIMULATE_OUTAGE"] = args.simulate_outage
    try:
        report, outcome = run(Task(alert_id=args.id))
    except DataLayerError as exc:
        print(f"data layer error: {exc}", file=sys.stderr); return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
