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
from pathlib import Path

from . import catalog, config
from .llm import get_llm
from .memory import Memory
from .world import World
from .roles import MAX_ROUNDS, Evaluator, Planner, Reporter, Retriever, Task, redact
from .trace import Trace
from .zetaris import DataLayerError, get_backend


def run(task: Task, persist_memory: bool = True, run_id: str | None = None) -> tuple[str, dict]:
    mode = "sample" if config.sample_mode() else "live"
    trace = Trace(task.describe(), mode, run_id=run_id)
    backend, llm = get_backend(), get_llm()
    memory = Memory(persist=persist_memory)
    planner, retriever, evaluator, reporter = Planner(llm, trace), Retriever(backend, trace), Evaluator(trace), Reporter(llm, trace)

    # round 0: identify the alert and its export event, then the exporting account (keys for everything else)
    done: dict[str, list] = {}
    for s in retriever.run(planner.plan(task, ["alert"], "", "identify the alert and its export event")):
        done.setdefault(s.evidence, []).append(s)
    assessment = evaluator.assess(task, done, retriever.footprint, 0)
    outage = config.env("SIMULATE_OUTAGE")
    if outage:
        trace.data["simulated_outage"] = outage
    if not task.event_id:
        if any(s.error for s in done.get("alert", [])):
            # the alert source itself is down: report that, do not pretend to know anything
            assessment.verdict = "cannot-confirm"
            report = reporter.report(task, assessment, "", 0)
            outcome = {"alert_id": task.alert_id, "verdict": "cannot-confirm", "reasons": assessment.reasons, "conflicts": [], "gaps": assessment.gaps,
                       "missing": [], "facts": 0, "rounds": 0, "disclosure": assessment.disclosure, "memory_recalled": 0, "evidence": [],
                       "next_steps": assessment.next_steps, "run_id": trace.run_id, "log_file": str(trace.log_file.relative_to(config.ROOT)),
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
        rounds += 1
        classes = [m for m in assessment.missing if m not in done]
        if not classes:
            break
        for s in retriever.run(planner.plan(task, classes, planner_context, reason)):
            done.setdefault(s.evidence, []).append(s)
        assessment = evaluator.assess(task, done, retriever.footprint, rounds)
        reason = "evaluator: " + (", ".join(assessment.conflicts + assessment.gaps) or "evidence still insufficient") + " -> fetch " + ", ".join(assessment.missing)
    trace.data["rounds"] = rounds
    if assessment.verdict is None:
        assessment.verdict = assessment.derived.get("verdict") or "cannot-confirm"

    rows_by_class = {cls: [r for st in steps if st.result for r in st.result.rows] for cls, steps in done.items()}
    world_stats = world.ingest_run(task, assessment, rows_by_class, trace.run_id)
    trace.step("world", "ingest", **world_stats, **world.stats())
    report = reporter.report(task, assessment, memory_context, rounds, world_context)

    # remember what this investigation established (keys make re-runs idempotent)
    actions: dict[str, list[str]] = {"created": [], "reconfirmed": [], "superseded": []}
    for f in assessment.facts:
        c = f.cites[0]
        rec, action = memory.remember(f.text, source=f"zetaris:{c['table']}:{c['step']}:{trace.run_id}", refs=[],
                                      tags=[f"alert:{task.alert_id}", f"account:{task.account_id}", f"dataset:{task.dataset_id}", f"kind:{f.kind}"],
                                      confidence=0.9 if f.kind != "gap" else 0.6, goal_run_id=trace.run_id, key=f"fact:{task.alert_id}:{f.key}")
        actions[action].append(rec.id)
    rec, action = memory.remember(f"Alert {task.alert_id}: verdict {assessment.verdict}; " + "; ".join(assessment.reasons), source=f"verdict:{trace.run_id}", refs=[],
                                  tags=[f"alert:{task.alert_id}", f"account:{task.account_id}", f"dataset:{task.dataset_id}", "kind:verdict"],
                                  confidence=0.85, goal_run_id=trace.run_id, key=f"verdict:{task.alert_id}")
    actions[action].append(rec.id)
    d = assessment.disclosure
    fp, _ = memory.remember(f"Run {trace.run_id} ({task.alert_id}) accessed {', '.join(d['tables_accessed'])}; sensitive columns touched: {', '.join(d['sensitive_columns_touched']) or 'none'}; "
                            f"unnecessary tables: {', '.join(d['accessed_but_unused']) or 'none'}.", source=f"trace:{trace.run_id}", refs=[], tags=["footprint"], confidence=1.0,
                            mtype="general", goal_run_id=trace.run_id)
    trace.step("memory", "remember", **actions, footprint=fp.id, ledger=memory.ledger_stats())

    outcome = {"alert_id": task.alert_id, "verdict": assessment.verdict, "reasons": assessment.reasons, "conflicts": assessment.conflicts, "gaps": assessment.gaps,
               "missing": assessment.missing, "facts": len(assessment.facts), "rounds": rounds, "disclosure": {k: v for k, v in d.items() if k != "columns_seen"},
               "evidence": [{"text": redact(f.text)[0], "kind": f.kind, "cites": f.cites} for f in assessment.facts],
               "next_steps": assessment.next_steps, "run_id": trace.run_id, "log_file": str(trace.log_file.relative_to(config.ROOT)),
               "memory_recalled": len(recalled), "memory": {k: len(v) for k, v in actions.items()}, "world": world_stats, "backend": backend.name, "llm": llm.name}
    path = trace.finish(outcome)
    shown = path.relative_to(config.ROOT) if config.ROOT in path.parents else path
    report += f"\n\n_trace: {shown} | evaluator rounds: {rounds} | data: {backend.name} | model: {llm.name}_\n"
    return report, outcome


def evaluate(write_examples: bool) -> int:
    truth = {}
    gt = config.ROOT / "data" / "sample" / "ground_truth.csv"
    if gt.exists():
        with open(gt, newline="") as fh:
            truth = {r["alert_id"]: r for r in csv.DictReader(fh)}
    backend = get_backend()
    ids = [r["alert_id"] for r in backend.query(f"SELECT alert_id FROM {catalog.SCHEMA}.alerts ORDER BY alert_id", limit=500).rows]
    hits = 0; rows = []
    for aid in ids:
        try:
            report, out = run(Task(alert_id=aid))
        except DataLayerError as exc:
            rows.append((aid, "ERROR", str(exc)[:60], truth.get(aid, {}).get("expected_verdict", "?"), "")); continue
        exp = truth.get(aid, {}).get("expected_verdict", "?")
        ok = out["verdict"] == exp; hits += ok
        rows.append((aid, out["verdict"], truth.get(aid, {}).get("scenario", ""), exp, "OK" if ok else "MISS"))
        if write_examples:
            (config.ROOT / "output_examples").mkdir(exist_ok=True); (config.ROOT / "input_examples").mkdir(exist_ok=True)
            (config.ROOT / "output_examples" / f"{aid}.md").write_text(report)
            (config.ROOT / "input_examples" / f"{aid}.txt").write_text(f"python -m agent.main alert {aid}\n")
    print(f"{'alert':8s} {'verdict':15s} {'scenario':24s} {'expected':15s} result")
    for r in rows:
        print(f"{r[0]:8s} {r[1]:15s} {r[2]:24s} {r[3]:15s} {r[4]}")
    print(f"\n{hits}/{len(rows)} verdicts match ground truth" + (" (ground truth file missing)" if not truth else ""))
    return 0 if hits == len(rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="exportwhy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("alert"); al.add_argument("id")
    al.add_argument("--simulate-outage", metavar="SOURCES", help="comma-separated: logs, policy, tickets (or table names); the data layer fails for them")
    ev = sub.add_parser("evaluate"); ev.add_argument("--write-examples", action="store_true")
    sub.add_parser("memory")
    fb = sub.add_parser("feedback"); fb.add_argument("memory_id"); fb.add_argument("kind", choices=["helpful", "not_helpful", "wrong"])
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
