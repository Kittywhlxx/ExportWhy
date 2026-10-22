"""DepRisk: an agent that answers "should we adopt / keep this dependency?" from evidence spread
across the internal inventory, PyPI and deps.dev, all reached through Zetaris as one SQL schema.

  python -m agent.main request ADR-2041         evaluate an adoption request
  python -m agent.main audit auth-service       audit a service's pins
  python -m agent.main memory                   list what the agent remembers
  python -m agent.main feedback <mem-id> wrong  correct a memory (helpful | not_helpful | wrong)
  python -m agent.main health                   data layer + model provider status
Set SAMPLE_MODE=true to run fully offline (SQLite + deterministic stub).
"""
from __future__ import annotations

import argparse
import sys

from . import catalog, config
from .llm import get_llm
from .memory import Memory
from .roles import INITIAL, MAX_ROUNDS, Evaluator, Planner, Reporter, Retriever, Task
from .trace import Trace
from .zetaris import DataLayerError, get_backend


def build_task(args) -> Task:
    if args.cmd == "request":
        return Task(kind="request", text=f"evaluate {args.id}", request_id=args.id)
    if args.cmd == "audit":
        return Task(kind="audit", text=f"audit {args.service}", service=args.service)
    raise SystemExit("unknown task")


def resolve_request(task: Task, backend, trace) -> None:
    """Round 0: the request row tells us package/version/service; everything else keys off it."""
    r = backend.query(f"SELECT service, package, requested_version FROM {catalog.SCHEMA}.adoption_requests WHERE request_id = '{task.request_id}'")
    trace.step("retriever", "query", step="q0", evidence="resolve", backend=r.source, query_id=r.query_id, rows=len(r.rows), sql=r.sql)
    if not r.rows:
        raise DataLayerError(f"adoption request {task.request_id} not found")
    row = r.rows[0]
    task.service, task.package, task.version = row["service"], row["package"], row["requested_version"]


def run(task: Task, persist_memory: bool = True) -> tuple[str, dict]:
    mode = "sample" if config.sample_mode() else "live"
    trace = Trace(task.describe(), mode)
    backend, llm = get_backend(), get_llm()
    memory = Memory(persist=persist_memory)
    planner, retriever, evaluator, reporter = Planner(llm, trace), Retriever(backend, trace), Evaluator(trace), Reporter(llm, trace)

    if task.kind == "request":
        resolve_request(task, backend, trace)
    mem_query = f"{task.package or task.service} {task.version or ''} risk license vulnerability".strip()
    memory_context, recalled = memory.recall(mem_query)
    trace.step("memory", "recall", query=mem_query, hits=[{k: v for k, v in m.items() if k != "content"} | {"content": m["content"][:120]} for m in recalled])

    done: dict[str, list] = {}
    classes = list(INITIAL[task.kind])
    reason = "initial evidence for the decision" + (" (memory recalled earlier findings; re-verifying against live sources)" if recalled else "")
    assessment = None
    for rounds in range(1, MAX_ROUNDS + 1):
        steps = planner.plan(task, classes, memory_context, reason)
        for s in retriever.run(steps):
            done.setdefault(s.evidence, []).append(s)
        assessment = evaluator.assess(task, done, retriever.footprint, rounds)
        trace.data["rounds"] = rounds
        if assessment.verdict is not None:
            break
        classes = [m for m in assessment.missing if m not in done]
        if not classes:
            break
        reason = "evaluator found the evidence insufficient: " + ", ".join(classes)
    if assessment.verdict is None:
        assessment.verdict = assessment.derived.get("verdict", "needs-review")

    report = reporter.report(task, assessment, memory_context, trace.data["rounds"])

    # remember what this run established (facts carry their Zetaris provenance; keys make re-runs idempotent)
    subject = f"{task.package}:{task.version}" if task.kind == "request" else f"service:{task.service}"
    actions: dict[str, list[str]] = {"created": [], "reconfirmed": [], "superseded": []}
    for f in assessment.facts:
        c = f.cites[0]
        rec, action = memory.remember(f.text, source=f"zetaris:{c['table']}:{c['step']}:{trace.run_id}",
                                      refs=[u for u in [c.get("source_url")] if u],
                                      tags=[f"package:{task.package or task.service}", f"kind:{task.kind}"],
                                      confidence=0.9 if not assessment.conflicts else 0.7, goal_run_id=trace.run_id,
                                      key=f"fact:{subject}:{f.key}")
        actions[action].append(rec.id)
    d = assessment.disclosure
    fp, _ = memory.remember(f"Run {trace.run_id} for {task.describe()} accessed {', '.join(d['tables_accessed'])}; unnecessary: {', '.join(d['accessed_but_unused']) or 'none'}.",
                            source=f"trace:{trace.run_id}", refs=[], tags=["footprint"], confidence=1.0, mtype="general", goal_run_id=trace.run_id)
    trace.step("memory", "remember", **actions, footprint=fp.id, ledger=memory.ledger_stats())
    stored = actions["created"] + actions["superseded"]

    outcome = {"verdict": assessment.verdict, "reasons": assessment.reasons, "conflicts": assessment.conflicts, "missing": assessment.missing,
               "facts": len(assessment.facts), "disclosure": assessment.disclosure, "memory_recalled": len(recalled),
               "memory": {k: len(v) for k, v in actions.items()},
               "backend": backend.name, "llm": llm.name}
    path = trace.finish(outcome)
    report += f"\n\n_trace: {path.relative_to(config.ROOT)} | rounds: {trace.data['rounds']} | data: {backend.name} | model: {llm.name}_\n"
    return report, outcome


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="depriskr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("request").add_argument("id")
    sub.add_parser("audit").add_argument("service")
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

    task = build_task(args)
    try:
        report, outcome = run(task)
    except DataLayerError as exc:
        print(f"data layer error: {exc}", file=sys.stderr); return 2
    print(report)
    return 0 if outcome["verdict"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
