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
from .trace import Trace
from .zetaris import DataLayerError, get_backend

# logical model calls per alert: 3 planning + 2 second opinions + 1 summary + 2 critiques + 1 revision
# (retries inside a call are not counted)
MAX_MODEL_CALLS = 9


def _skipped_critique(attempt: int = 0, note: str = "skipped: time budget exhausted") -> dict:
    return {
        "attempt": attempt,
        "scores": None,
        "total": None,
        "passed": None,
        "fix": None,
        "source": "none",
        "note": note,
    }


class Investigation:
    """One triage of one alert: the agents, the time budget and the state they share.

    `run()` walks the phases in order; each phase is a method so the loop reads top to bottom.
    """

    def __init__(self, task: Task, persist_memory: bool = True, run_id: str | None = None):
        self.task = task
        mode = "sample" if config.sample_mode() else "live"
        self.trace = Trace(task.describe(), mode, run_id=run_id)
        self.backend = get_backend()
        self.llm = CountedLLM(get_llm(), max_calls=MAX_MODEL_CALLS)
        self.memory = Memory(persist=persist_memory)
        self.world = World(persist=persist_memory)

        # whole-triage wall clock; beyond it the run returns what it has and asks for manual review
        self.budget_s = float(config.env("TRIAGE_TIMEOUT_S", "480") or 480)
        self.deadline = self.trace.started + self.budget_s
        remaining = lambda: max(0.0, self.deadline - time.time())

        self.planner = Planner(self.llm, self.trace, remaining)
        self.retriever = Retriever(self.backend, self.trace, remaining)
        self.evaluator = Evaluator(self.trace, self.llm, remaining)
        self.reporter = Reporter(self.llm, self.trace, remaining)
        self.critic = Critic(self.llm, self.trace, remaining)

        self.status = "complete"  # complete | partial | timed-out
        self.done: dict[str, list] = {}  # evidence class -> query steps run for it
        self.rounds = 0
        self.assessment = None
        self.opinion: dict = {}
        self.critique: dict = {}
        self.summary = ""
        self.memory_context = ""
        self.world_context = ""
        self.recalled: list[dict] = []
        self.noted_skipped: set[str] = set()

    # ---- helpers -------------------------------------------------------------------------

    def out_of_time(self) -> bool:
        return time.time() > self.deadline

    def fetch(self, classes: list[str], context: str, reason: str) -> None:
        """Plan the queries for these evidence classes, run them, file the results by class."""
        for step in self.retriever.run(self.planner.plan(self.task, classes, context, reason)):
            self.done.setdefault(step.evidence, []).append(step)

    def assess(self) -> None:
        self.assessment = self.evaluator.assess(
            self.task, self.done, self.retriever.footprint, self.rounds
        )

    def mark_partial(self) -> None:
        if self.status == "complete":
            self.status = "partial"

    # ---- phases --------------------------------------------------------------------------

    def run(self) -> tuple[str, dict]:
        outage = config.env("SIMULATE_OUTAGE")
        if outage:
            self.trace.data["simulated_outage"] = outage

        self.identify_alert()
        if not self.task.event_id:
            return self.finish_without_evidence()

        self.identify_account()
        self.recall_context()
        self.evidence_rounds()
        self.settle_budget("evidence rounds")
        self.second_opinion()
        self.write_summary()
        confidence = self.confidence()
        world_stats, memory_actions, memory_updates = self.record(confidence)
        return self.finish(confidence, world_stats, memory_actions, memory_updates)

    def identify_alert(self) -> None:
        """Round 0a: the alert row and its export event (the keys for everything else)."""
        self.fetch(["alert"], "", "identify the alert and its export event")
        self.assess()

    def identify_account(self) -> None:
        """Round 0b: the exporting account."""
        self.fetch(["account"], "", "identify the exporting account")
        self.assess()

    def recall_context(self) -> None:
        """What memory and the world model already hold about this account and dataset."""
        task = self.task
        query = f"{task.account_id} {task.dataset_id} export alert ticket exception"
        self.memory_context, self.recalled = self.memory.recall(query)
        hits = [
            {**{k: v for k, v in m.items() if k != "content"}, "content": m["content"][:120]}
            for m in self.recalled
        ]
        self.trace.step("memory", "recall", query=query, hits=hits)

        self.world_context, dossier = self.world.dossier(task.account_id, task.alert_id)
        task.prior = dossier
        self.trace.step(
            "world",
            "dossier",
            known=dossier["known"],
            prior_alerts=dossier["prior_alerts"],
            status_history=dossier["status_history"],
        )

    def evidence_rounds(self) -> None:
        """Planner -> Retriever -> Evaluator, until the verdict is grounded or the rounds are spent."""
        a = self.assessment
        context = "\n".join(x for x in (self.memory_context, self.world_context) if x)
        reason = "evidence the verdict needs: " + ", ".join(a.missing)
        if self.recalled:
            reason += " (memory holds earlier findings on this account/dataset; re-verifying)"

        while a.verdict is None and a.missing and self.rounds < MAX_ROUNDS:
            if self.out_of_time():
                self.status = "timed-out"
                self.trace.step(
                    "system", "timeout", budget_s=self.budget_s, phase="evidence rounds"
                )
                break
            classes = [m for m in a.missing if m not in self.done]
            if not classes:
                break
            self.rounds += 1
            self.fetch(classes, context, reason)
            self.assess()
            a = self.assessment
            problems = ", ".join(a.conflicts + a.gaps) or "evidence still insufficient"
            reason = f"evaluator: {problems} -> fetch " + ", ".join(a.missing)

    def settle_budget(self, phase: str, unplanned: list[str] | None = None) -> None:
        """Evidence the Retriever skipped for lack of time is evidence the verdict never saw.

        That counts as a timeout whether or not the clock has passed the deadline by now. Called after
        every evidence round, on the assessment that round produced. `unplanned` names classes that were
        asked for but never even planned because their round could not start.
        """
        a = self.assessment
        skipped = {
            cls for cls, steps in self.done.items() if steps and all(s.skipped for s in steps)
        }
        skipped |= set(unplanned or [])
        skipped_classes = sorted(skipped)

        if skipped - self.noted_skipped:
            self.noted_skipped |= skipped
            self.status = "timed-out"
            self.trace.step(
                "system", "timeout", budget_s=self.budget_s, phase=phase, skipped=skipped_classes
            )
        if a.verdict is None:
            a.verdict = a.derived.get("verdict") or "cannot-confirm"
        if self.status != "timed-out":
            return

        not_seen = sorted(set(a.derived.get("unfetched") or []) | set(a.missing) | skipped)
        previous = a.derived.get("budget_reason")
        if previous and not_seen == a.derived.get("unfetched"):
            return  # already settled on this assessment, nothing new
        a.derived["unfetched"] = not_seen
        a.missing = []
        reason = f"time budget of {self.budget_s:.0f}s exhausted before the evidence was complete"
        if not_seen:
            reason += f" (not retrieved: {', '.join(not_seen)})"
        reason += "; manual review needed"
        if previous in a.reasons:
            # settled before on this assessment: refresh the list of what was not retrieved
            a.reasons[a.reasons.index(previous)] = reason
        else:
            a.verdict = "cannot-confirm"
            a.reasons.insert(0, reason)
            a.next_steps.insert(
                0,
                "re-run the triage with a larger TRIAGE_TIMEOUT_S, "
                "or review the partial evidence below by hand",
            )
        a.derived["budget_reason"] = reason

    def second_opinion(self) -> None:
        """The model judges the same evidence. A disagreement buys one more evidence round, then escalates."""
        a = self.assessment
        if self.out_of_time():
            self.mark_partial()
            self.opinion = {
                "verdict": "skipped",
                "reasons": ["skipped: time budget exhausted"],
                "missing_evidence": [],
                "usable": False,
                "agrees": None,
                "source": "none",
            }
            self.trace.step("evaluator", "second_opinion", rules_verdict=a.verdict, **self.opinion)
        else:
            self.opinion = self.evaluator.second_opinion(self.task, a)

        if self.opinion.get("usable") and not self.opinion["agrees"]:
            extra = [m for m in self.opinion.get("missing_evidence", []) if m not in self.done]
            if extra and self.rounds < MAX_ROUNDS and not self.out_of_time():
                self.rounds += 1
                context = "\n".join(x for x in (self.memory_context, self.world_context) if x)
                self.fetch(
                    extra,
                    context,
                    "second opinion disagrees with the rules; fetching the evidence it asked for: "
                    + ", ".join(extra),
                )
                self.assess()
                self.settle_budget("second-opinion evidence round")
                if not self.out_of_time():
                    self.opinion = self.evaluator.second_opinion(self.task, self.assessment)
            elif extra and self.out_of_time():
                # the round could not start: what the model asked for is "not retrieved"
                self.settle_budget("second-opinion evidence round", unplanned=extra)
            elif extra:
                # the round budget (not the clock) is spent: say what was asked for and not fetched
                a.derived["unfetched"] = sorted(set(a.derived.get("unfetched") or []) | set(extra))
                a.reasons.insert(
                    0,
                    f"the {MAX_ROUNDS}-round evidence budget was spent before the second opinion's "
                    f"request ({', '.join(extra)}) could be fetched",
                )
            a = self.assessment
            if not self.opinion["agrees"]:
                a.conflicts.append(
                    f"rules say {a.verdict}, model second opinion says {self.opinion['verdict']}: "
                    + "; ".join(self.opinion.get("reasons", [])[:2])
                )
                a.reasons.insert(
                    0,
                    f"rules-based verdict {a.verdict} disputed by the model second opinion; "
                    "a human must decide",
                )
                a.next_steps.insert(
                    0,
                    "human review: resolve the disagreement between the rules and the model "
                    "before closing the alert",
                )
                a.verdict = "needs-human"
        self.opinion["rules_verdict"] = self.assessment.derived.get("verdict")
        self.trace.data["rounds"] = self.rounds

    def write_summary(self) -> None:
        """Proposer + Critic: draft the summary, score it, revise once if it fails the rubric."""
        a = self.assessment
        if self.out_of_time():
            self.mark_partial()
            self.summary = self.reporter.template_summary(a)
            self.critique = _skipped_critique()
            self.trace.step("critic", "critique", **self.critique)
        else:
            self.summary = self.reporter.draft_summary(self.task, a)
            if self.out_of_time():
                self.critique = _skipped_critique()
                self.trace.step("critic", "critique", **self.critique)
            else:
                self.critique = self.critic.review(self.task, a, self.summary, attempt=1)
            first = self.critique
            if first["passed"] is False and first.get("fix") and not self.out_of_time():
                self.summary = self.reporter.revise(self.task, a, self.summary, first["fix"])
                if self.out_of_time():
                    second = _skipped_critique(attempt=2)
                    self.trace.step("critic", "critique", **second)
                else:
                    second = self.critic.review(self.task, a, self.summary, attempt=2)
                second["fix_applied"] = first["fix"]
                second["first_total"] = first["total"]
                self.critique = second

        # the rubric is a gate: a summary that did not pass (or could not be reviewed) never ships as-is
        if self.critique.get("passed") is True:
            self.critique["review_status"] = "passed"
        else:
            if self.critique.get("passed") is False:
                self.critique["review_status"] = "failed; deterministic summary used"
            else:
                self.critique["review_status"] = "unavailable; deterministic summary used"
            self.summary = self.reporter.template_summary(a)
            self.trace.step(
                "reporter", "summary_fallback", review_status=self.critique["review_status"]
            )
        if self.out_of_time():
            self.mark_partial()

    def confidence(self) -> dict:
        """A heuristic score, not a probability: evidence completeness, second-opinion agreement,
        critic score, capped by open gaps, unfetched evidence and anything that needs a human."""
        a = self.assessment
        task = self.task

        # the decision-critical classes this alert needed, and how many actually returned rows
        needed = ["account", "policy"] + (["ticket_cited"] if task.ticket_ref else [])
        if a.derived.get("needs_auth") and not self._cited_ticket_covered():
            needed += ["tickets_account", "exceptions"]  # the widened search was required
        got = [c for c in needed if any(st.result for st in self.done.get(c, []))]
        completeness = len(got) / max(1, len(needed))

        agrees = self.opinion.get("agrees")
        agree_term = 1.0 if agrees else (0.5 if agrees is None else 0.0)
        total = self.critique.get("total")
        critic_term = (total / 9) if total is not None else 0.5

        score = 0.5 * completeness + 0.3 * agree_term + 0.2 * critic_term
        unfetched = a.derived.get("unfetched") or []
        if a.gaps:
            score = min(score, 0.6)
        if unfetched:
            score = min(score, 0.85)
        if a.verdict == "needs-human" or self.status != "complete":
            score = min(score, 0.4)

        factors = [
            f"evidence completeness {completeness:.2f}",
            "second opinion "
            + ("agrees" if agrees else "unavailable" if agrees is None else "disagrees"),
            f"critic {total}/9" if total is not None else "critic unavailable",
        ]
        if a.gaps:
            factors.append(f"{len(a.gaps)} open gap(s)")
        if unfetched:
            factors.append(f"not retrieved: {', '.join(unfetched)}")
        confidence = {
            "score": round(score, 2),
            "label": "high" if score >= 0.8 else "medium" if score >= 0.6 else "low",
            "factors": factors,
        }
        self.trace.step("evaluator", "confidence", **confidence)
        return confidence

    def _cited_ticket_covered(self) -> bool:
        """True when the cited ticket was read, returned a row and raised no ticket conflict."""
        a = self.assessment
        if not self.task.ticket_ref:
            return False
        if any(c.startswith("ticket ") for c in a.conflicts):
            return False
        return any(st.result and st.result.rows for st in self.done.get("ticket_cited", []))

    def record(self, confidence: dict) -> tuple[dict, dict, dict]:
        """World model ingest and memory writes (keys make re-runs idempotent). Happens before the
        report so the report can list the memory updates."""
        task, a, trace = self.task, self.assessment, self.trace
        rows_by_class = {
            cls: [r for st in steps if st.result for r in st.result.rows]
            for cls, steps in self.done.items()
        }
        world_stats = self.world.ingest_run(task, a, rows_by_class, trace.run_id)
        trace.step("world", "ingest", **world_stats, **self.world.stats())

        actions: dict[str, list[str]] = {"created": [], "reconfirmed": [], "superseded": []}
        updates: dict[str, list[dict]] = {"created": [], "reconfirmed": [], "superseded": []}
        tags = [
            f"alert:{task.alert_id}",
            f"account:{task.account_id}",
            f"dataset:{task.dataset_id}",
        ]

        for f in a.facts:
            c = f.cites[0]
            rec, action = self.memory.remember(
                f.text,
                source=f"zetaris:{c['table']}:{c['step']}:{trace.run_id}",
                refs=[],
                tags=tags + [f"kind:{f.kind}"],
                confidence=0.9 if f.kind != "gap" else 0.6,
                goal_run_id=trace.run_id,
                key=f"fact:{task.alert_id}:{f.key}",
            )
            actions[action].append(rec.id)
            updates[action].append(
                {"id": rec.id, "content": f.text, "source": f"{c['table']} via {c['step']}"}
            )

        rec, action = self.memory.remember(
            f"Alert {task.alert_id}: verdict {a.verdict}; " + "; ".join(a.reasons),
            source=f"verdict:{trace.run_id}",
            refs=[],
            tags=tags + ["kind:verdict"],
            confidence=0.85,
            goal_run_id=trace.run_id,
            key=f"verdict:{task.alert_id}",
        )
        actions[action].append(rec.id)
        updates[action].append(
            {"id": rec.id, "content": f"verdict {a.verdict}", "source": "evaluator"}
        )

        d = a.disclosure
        footprint, _ = self.memory.remember(
            f"Run {trace.run_id} ({task.alert_id}) accessed {', '.join(d['tables_accessed'])}; "
            f"sensitive columns touched: {', '.join(d['sensitive_columns_touched']) or 'none'}; "
            f"unnecessary tables: {', '.join(d['accessed_but_unused']) or 'none'}.",
            source=f"trace:{trace.run_id}",
            refs=[],
            tags=["footprint"],
            confidence=1.0,
            mtype="general",
            goal_run_id=trace.run_id,
        )
        trace.step(
            "memory",
            "remember",
            **actions,
            footprint=footprint.id,
            ledger=self.memory.ledger_stats(),
        )
        return world_stats, actions, updates

    def finish(self, confidence, world_stats, memory_actions, memory_updates) -> tuple[str, dict]:
        task, a, trace = self.task, self.assessment, self.trace
        timeline = _timeline(trace)
        report = self.reporter.report(
            task,
            a,
            self.memory_context,
            self.rounds,
            self.world_context,
            summary=self.summary,
            critique=self.critique,
            second_opinion=self.opinion,
            confidence=confidence,
            timeline=timeline,
            memory_updates=memory_updates,
            status=self.status,
        )
        if a.verdict in ("cannot-confirm", "needs-human"):
            queue = "manual-review"
        else:
            queue = a.priority["bucket"] if a.priority else "none"
        outcome = {
            "alert_id": task.alert_id,
            "verdict": a.verdict,
            "reasons": a.reasons,
            "conflicts": a.conflicts,
            "gaps": a.gaps,
            "missing": a.missing,
            "facts": len(a.facts),
            "rounds": self.rounds,
            "disclosure": {k: v for k, v in a.disclosure.items() if k != "columns_seen"},
            "evidence": [
                {"text": redact(f.text)[0], "kind": f.kind, "cites": f.cites} for f in a.facts
            ],
            "next_steps": a.next_steps,
            "run_id": trace.run_id,
            "log_file": str(trace.log_file.relative_to(config.ROOT)),
            "summary": redact(self.summary)[0],
            "second_opinion": self.opinion,
            "critic": self.critique,
            "confidence": confidence,
            "priority": a.priority,
            "status": self.status,
            "timeline": timeline,
            "queue": queue,
            "memory_recalled": len(self.recalled),
            "memory": {k: len(v) for k, v in memory_actions.items()},
            "world": world_stats,
            "backend": self.backend.name,
            "llm": self.llm.name,
            "model_calls": self.llm.calls,
        }
        path = trace.finish(outcome)
        report += (
            f"\n\n_trace: {self._shown(path)} | evaluator rounds: {self.rounds} | "
            f"data: {self.backend.name} | model: {self.llm.name}_\n"
        )
        return report, outcome

    def finish_without_evidence(self) -> tuple[str, dict]:
        """The alert row could not be read (source down, or the budget was gone before the first
        query). Report that; do not pretend to know anything about the export."""
        task, a, trace = self.task, self.assessment, self.trace
        alert_steps = self.done.get("alert", [])
        skipped = any(s.skipped for s in alert_steps)
        if not skipped and not any(s.error for s in alert_steps):
            raise DataLayerError(f"alert {task.alert_id} not found")

        a.verdict = "cannot-confirm"
        if skipped:
            self.status = "timed-out"
            a.reasons.insert(
                0,
                f"time budget of {self.budget_s:.0f}s exhausted before the alert could be read; "
                "manual review needed",
            )
            a.gaps.append("the alert and its export event were not retrieved (time budget)")
            a.next_steps.insert(0, "re-run the triage with a larger TRIAGE_TIMEOUT_S")
        else:
            self.status = "partial"
        confidence = {
            "score": 0.0,
            "label": "low",
            "factors": [
                "no evidence retrieved" if skipped else "the alert source could not be queried"
            ],
        }
        critique = _skipped_critique(note="skipped: no evidence to summarise")
        critique["review_status"] = "skipped"
        template = self.reporter.template_summary(a)
        report = self.reporter.report(
            task,
            a,
            "",
            0,
            summary=template,
            confidence=confidence,
            critique=critique,
            status=self.status,
        )
        outcome = {
            "alert_id": task.alert_id,
            "verdict": "cannot-confirm",
            "reasons": a.reasons,
            "conflicts": [],
            "gaps": a.gaps,
            "missing": [],
            "facts": 0,
            "rounds": 0,
            "disclosure": a.disclosure,
            "memory_recalled": 0,
            "evidence": [],
            "next_steps": a.next_steps,
            "run_id": trace.run_id,
            "log_file": str(trace.log_file.relative_to(config.ROOT)),
            "summary": template,
            "second_opinion": None,
            "critic": critique,
            "confidence": confidence,
            "priority": None,
            "queue": "manual-review",
            "status": self.status,
            "timeline": _timeline(trace),
            "model_calls": self.llm.calls,
            "memory": {"created": 0, "reconfirmed": 0, "superseded": 0},
            "world": {"appended": 0},
            "backend": self.backend.name,
            "llm": self.llm.name,
        }
        path = trace.finish(outcome)
        report += f"\n\n_trace: {self._shown(path)} | data: {self.backend.name} | model: {self.llm.name}_\n"
        return report, outcome

    @staticmethod
    def _shown(path: Path):
        return path.relative_to(config.ROOT) if config.ROOT in path.parents else path


def run(task: Task, persist_memory: bool = True, run_id: str | None = None) -> tuple[str, dict]:
    """Triage one alert. Returns the markdown report and the structured outcome."""
    return Investigation(task, persist_memory, run_id).run()


_TIMELINE_LINES = {
    "plan": lambda st: (
        "planner",
        "fetch "
        + ", ".join(st["evidence"])
        + (f" (model drafted {st['llm_drafted']} SQL)" if st.get("llm_drafted") else ""),
    ),
    "query": lambda st: (
        "retriever",
        f"{st['step']} {st['evidence']}: {st['rows']} rows from {', '.join(st['tables'])} "
        f"({st['backend']}, {st['elapsed_ms']} ms)",
    ),
    "query_failed": lambda st: (
        "retriever",
        f"{st['step']} {st['evidence']} FAILED: {st['error'][:80]}",
    ),
    "query_skipped": lambda st: (
        "retriever",
        f"{st['step']} {st['evidence']} skipped: {st['why']}",
    ),
    "assess": lambda st: (
        "evaluator",
        f"verdict {st['verdict']}" if st["verdict"] else "needs more: " + ", ".join(st["missing"]),
    ),
    "second_opinion": lambda st: (
        "evaluator",
        f"second opinion {st.get('verdict')} ("
        + (
            "agrees"
            if st.get("agrees")
            else "no usable opinion"
            if st.get("agrees") is None
            else "DISAGREES"
        )
        + f"; {st.get('source')})",
    ),
    "critique": lambda st: (
        "critic",
        (
            f"rubric {st['total']}/9 attempt {st['attempt']}"
            + (f", fix: {st['fix']}" if st.get("fix") else "")
        )
        if st.get("scores")
        else f"unavailable ({st.get('note')})",
    ),
    "revise": lambda st: ("reporter", "revised the summary once"),
    "timeout": lambda st: (
        "system",
        f"time budget {st['budget_s']:.0f}s exhausted during {st['phase']}",
    ),
    "summary_fallback": lambda st: ("reporter", f"review {st['review_status']}"),
    "model_unavailable": lambda st: (
        st["agent"],
        f"model unavailable, fallback: {st.get('fallback')}",
    ),
}


def _timeline(trace) -> list[dict]:
    """Compact, judge-readable list of what happened, in order (from the trace)."""
    out = []
    for st in trace.data["steps"]:
        line = _TIMELINE_LINES.get(st["kind"])
        if line:
            agent, what = line(st)
            out.append({"t": st["t"], "agent": agent, "what": what})
    return out


def evaluate(write_examples: bool) -> int:
    truth = {}
    gt = config.ROOT / "data" / "sample" / "ground_truth.csv"
    if gt.exists():
        with open(gt, newline="") as fh:
            truth = {r["alert_id"]: r for r in csv.DictReader(fh)}
    backend = get_backend()
    ids = [
        r["alert_id"]
        for r in backend.query(
            f"SELECT alert_id FROM {catalog.SCHEMA}.alerts ORDER BY alert_id", limit=500
        ).rows
    ]
    hits = 0
    rows = []
    traj = []
    for aid in ids:
        try:
            report, out = run(Task(alert_id=aid))
        except DataLayerError as exc:
            rows.append(
                (aid, "ERROR", str(exc)[:60], truth.get(aid, {}).get("expected_verdict", "?"), "")
            )
            continue
        exp = truth.get(aid, {}).get("expected_verdict", "?")
        ok = out["verdict"] == exp
        hits += ok
        rows.append(
            (
                aid,
                out["verdict"],
                truth.get(aid, {}).get("scenario", ""),
                exp,
                "OK" if ok else "MISS",
            )
        )
        tl = out.get("timeline", [])
        queries = [t for t in tl if t["agent"] == "retriever" and "rows from" in t["what"]]
        zero = [t for t in queries if ": 0 rows" in t["what"]]
        traj.append(
            {
                "alert": aid,
                "verdict": out["verdict"],
                "rounds": out["rounds"],
                "queries": len(queries),
                "zero_row_queries": len(zero),
                "model_calls": sum(
                    1
                    for t in tl
                    if t["agent"] in ("evaluator", "critic")
                    and ("second opinion" in t["what"] or "rubric" in t["what"])
                ),
                "sensitive_cols": len(out["disclosure"]["sensitive_columns_touched"]),
                "confidence": out["confidence"]["score"],
                "compliant_on_zero_rows": out["verdict"] == "compliant"
                and any(
                    ("ticket" in t["what"] or "exceptions" in t["what"])
                    and ": 0 rows" in t["what"]
                    and "tickets_account" not in t["what"]
                    for t in queries
                ),
            }
        )
        if write_examples:
            (config.ROOT / "output_examples").mkdir(exist_ok=True)
            (config.ROOT / "input_examples").mkdir(exist_ok=True)
            (config.ROOT / "output_examples" / f"{aid}.md").write_text(report)
            (config.ROOT / "input_examples" / f"{aid}.txt").write_text(
                f"python -m agent.main alert {aid}\n"
            )
    print(f"{'alert':8s} {'verdict':15s} {'scenario':24s} {'expected':15s} result")
    for r in rows:
        print(f"{r[0]:8s} {r[1]:15s} {r[2]:24s} {r[3]:15s} {r[4]}")
    print(
        f"\n{hits}/{len(rows)} verdicts match ground truth"
        + (" (ground truth file missing)" if not truth else "")
    )
    if traj:
        n = len(traj)
        print("\ntrajectory (Alert-Bench style: judge the path, not only the decision)")
        print(f"{'alert':8s} {'verdict':15s} rounds queries zero-row sens conf")
        for t in traj:
            print(
                f"{t['alert']:8s} {t['verdict']:15s} {t['rounds']:6d} {t['queries']:7d} {t['zero_row_queries']:8d} {t['sensitive_cols']:4d} {t['confidence']:.2f}"
            )
        print(
            f"mean rounds {sum(t['rounds'] for t in traj) / n:.1f}, mean queries {sum(t['queries'] for t in traj) / n:.1f}, "
            f"mean confidence {sum(t['confidence'] for t in traj) / n:.2f}, compliant verdicts resting on a zero-row authorisation query: {sum(t['compliant_on_zero_rows'] for t in traj)}"
        )
    return 0 if hits == len(rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="exportwhy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("alert")
    al.add_argument("id")
    al.add_argument(
        "--simulate-outage",
        metavar="SOURCES",
        help="comma-separated: logs, policy, tickets (or table names); the data layer fails for them",
    )
    ev = sub.add_parser("evaluate")
    ev.add_argument("--write-examples", action="store_true")
    sub.add_parser("memory")
    fb = sub.add_parser("feedback")
    fb.add_argument("memory_id")
    fb.add_argument("kind", choices=["helpful", "not_helpful", "wrong"])
    fv = sub.add_parser("feedback-verdict")
    fv.add_argument("alert_id")
    fv.add_argument("judgement", choices=["agree", "disagree"])
    fv.add_argument("--note", default="")
    sub.add_parser("health")
    args = ap.parse_args(argv)

    if args.cmd == "health":
        try:
            b = get_backend()
            r = b.query("SELECT 1 AS one")
            print(f"data layer: {b.name} OK ({r.rows})")
        except Exception as exc:
            print(f"data layer: FAIL {exc}")
        try:
            l = get_llm()
            print(f"model provider: {l.name} {l.health()}")
        except Exception as exc:
            print(f"model provider: FAIL {exc}")
        return 0
    if args.cmd == "memory":
        for m in Memory().all():
            flag = " (superseded)" if m["superseded"] else ""
            print(
                f"{m['id']:10s} conf={float(m['confidence']):.2f}{flag} {m['content'][:110]}  <{m['source']}>"
            )
        return 0
    if args.cmd == "feedback":
        Memory().feedback(args.memory_id, args.kind)
        print("recorded in the trust ledger")
        return 0
    if args.cmd == "feedback-verdict":
        stats = Memory().verdict_feedback(args.alert_id, args.judgement == "agree", args.note)
        if stats is None:
            print(f"no verdict memory for {args.alert_id}; triage it first")
            return 1
        print("recorded; calibration so far:", stats)
        return 0
    if args.cmd == "evaluate":
        return evaluate(args.write_examples)
    if getattr(args, "simulate_outage", None):
        os.environ["SIMULATE_OUTAGE"] = args.simulate_outage
    try:
        report, outcome = run(Task(alert_id=args.id))
    except DataLayerError as exc:
        print(f"data layer error: {exc}", file=sys.stderr)
        return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
