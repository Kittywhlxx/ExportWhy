"""One trace per run, in two forms:
  traces/<run_id>.json           the full nested trace (every plan, query, rejection, round, receipt)
  logs/trace-<run_id>.jsonl      one JSON object per step, appended as it happens (judge-readable format)
plus readable [INFO] lines on stderr so `docker logs` shows how the agent worked. No secrets are ever logged."""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import config

LOG_DIR = config.ROOT / "logs"
ACTION = {"plan": "plan_evidence", "query": "query_zetaris", "query_failed": "query_zetaris", "rejected_llm_sql": "validate_model_sql",
          "assess": "evaluate_evidence", "llm": "model_call", "recall": "hmem_recall", "remember": "hmem_store",
          "dossier": "world_model_read", "ingest": "world_model_write", "second_opinion": "evaluate_second_opinion",
          "critique": "critique_report", "revise": "revise_report", "model_unavailable": "model_fallback"}


class Trace:
    def __init__(self, task: str, mode: str, run_id: str | None = None):
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self.started = time.time()
        self.data = {"run_id": self.run_id, "task": task, "mode": mode, "started_at": _iso(self.started),
                     "steps": [], "rounds": 0, "outcome": None}
        self.quiet = (os.environ.get("LOG_LEVEL", "INFO") or "INFO").upper() in ("QUIET", "ERROR", "NONE")
        LOG_DIR.mkdir(exist_ok=True)
        self.log_file = LOG_DIR / f"trace-{self.run_id}.jsonl"
        self._log({"action": "start", "agent_name": "system", "output_summary": f"{task} ({mode})", "status": "success"})

    def step(self, agent: str, kind: str, **payload) -> dict:
        entry = {"t": round(time.time() - self.started, 3), "agent": agent, "kind": kind, **payload}
        self.data["steps"].append(entry)
        self._log(self._line(agent, kind, payload))
        return entry

    def finish(self, outcome: dict) -> Path:
        self.data["outcome"] = outcome
        self.data["finished_at"] = _iso(time.time())
        self.data["elapsed_s"] = round(time.time() - self.started, 2)
        self.data["log_file"] = str(self.log_file.relative_to(config.ROOT))
        config.TRACE_DIR.mkdir(exist_ok=True)
        path = config.TRACE_DIR / f"{self.run_id}.json"
        path.write_text(json.dumps(self.data, indent=1, default=str))
        self._log({"action": "finish", "agent_name": "system", "output_summary": f"verdict={outcome.get('verdict')} in {self.data['elapsed_s']}s",
                   "status": "success" if outcome.get("verdict") else "error"})
        return path

    # ---- judge-readable form
    def _line(self, agent: str, kind: str, p: dict) -> dict:
        line = {"agent_name": agent, "action": ACTION.get(kind, kind), "status": "success"}
        if kind == "plan":
            line.update(input_summary=p.get("reason", "")[:200], output_summary=f"evidence classes: {', '.join(p.get('evidence', []))}; model-drafted SQL kept: {p.get('llm_drafted', 0)}",
                        target_agent="retriever")
        elif kind == "query":
            line.update(input_summary=p.get("sql", "")[:300], output_summary=f"{p.get('rows')} rows from {', '.join(p.get('tables', []))} via {p.get('backend')} in {p.get('elapsed_ms')} ms",
                        query_id=p.get("query_id"), step=p.get("step"))
        elif kind == "query_failed":
            line.update(input_summary=p.get("sql", "")[:300], output_summary=p.get("error", "")[:200], status="error", step=p.get("step"))
        elif kind == "rejected_llm_sql":
            line.update(input_summary=p.get("sql", "")[:300], output_summary=f"rejected: {p.get('why')}; template used instead", status="recovered", step=p.get("step"))
        elif kind == "assess":
            v = p.get("verdict"); line.update(output_summary=(f"verdict {v}: " + "; ".join(p.get("reasons", []))[:300]) if v else ("needs more evidence: " + ", ".join(p.get("missing", []))),
                                             round=p.get("round"), conflicts=p.get("conflicts"), gaps=p.get("gaps"), target_agent="reporter" if v else "planner")
        elif kind == "llm":
            line.update(model=p.get("routed_model") or p.get("model_served"), model_requested=p.get("model_requested"), receipt_id=p.get("receipt_id"),
                        output_summary=f"{p.get('provider')} served by {p.get('routed_model') or p.get('model_served')}", tokens=(p.get("usage") or {}).get("total_tokens"))
        elif kind == "recall":
            line.update(input_summary=p.get("query", ""), output_summary=f"H-MEM recalled {len(p.get('hits', []))} memories")
        elif kind == "remember":
            line.update(output_summary=f"H-MEM: {len(p.get('created', []))} created, {len(p.get('reconfirmed', []))} re-verified, {len(p.get('superseded', []))} superseded; ledger {p.get('ledger')}")
        elif kind == "dossier":
            line.update(output_summary=f"World Model: account {'known' if p.get('known') else 'new'}, prior alerts {[a['alert'] for a in p.get('prior_alerts', [])]}")
        elif kind == "ingest":
            line.update(output_summary=f"World Model: +{p.get('appended')} events ({p.get('entities')} entities, {p.get('events')} events)")
        elif kind == "second_opinion":
            ag = p.get("agrees")
            line.update(model=p.get("source"), output_summary=f"model verdict {p.get('verdict')} vs rules {p.get('rules_verdict')}: " + ("agrees" if ag else "no usable opinion" if ag is None else "DISAGREES"),
                        status="success" if ag else ("error" if ag is None else "escalated"), target_agent="reporter" if ag is not False else "planner")
        elif kind == "critique":
            sc = p.get("scores") or {}
            if p.get("passed") is None:
                line.update(model=p.get("source"), output_summary=f"critic unavailable: {p.get('note')}", status="error")
            else:
                line.update(model=p.get("source"), output_summary=f"rubric {sc.get('accuracy')}/{sc.get('appropriateness')}/{sc.get('actionability')} = {p.get('total')}/9 "
                            f"{'pass' if p.get('passed') else 'FAIL: ' + str(p.get('fix'))}", status="success" if p.get("passed") else "recovered", target_agent="reporter",
                            retry_count=(p.get("attempt", 1) - 1))
        elif kind == "model_unavailable":
            line.update(output_summary=f"model unavailable ({p.get('error', '')[:120]}); fallback: {p.get('fallback')}", status="recovered")
        elif kind == "revise":
            line.update(input_summary=str(p.get("fix"))[:200], output_summary="summary revised once after the critic's fix", status="recovered")
        else:
            line.update(output_summary=json.dumps(p, default=str)[:300])
        return line

    def _log(self, line: dict) -> None:
        line = {"timestamp": _iso(time.time()), "run_id": self.run_id, **line}
        with open(self.log_file, "a") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        if not self.quiet:
            print(f"[INFO] run_id={self.run_id} {line['agent_name']} {line['action']}: {line.get('output_summary', '')}", file=sys.stderr, flush=True)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
