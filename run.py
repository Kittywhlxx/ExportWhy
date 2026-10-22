"""ExportWhy programmatic entry point (Technical Execution Guide §2).

  python run.py --input input_examples/example_1.json       # one triage, JSON result on stdout
  python run.py --alert AL-003 [--sample] [--outage tickets]  # shorthand
  python run.py --serve                                       # HTTP: POST /run (JSON in/out), GET /health, on 0.0.0.0:$PORT

Request JSON:  {"run_id": "demo-001", "input": {"alert_id": "AL-003"},
                "options": {"sample_mode": false, "llm_stub": false, "simulate_outage": ""}}
Response JSON: {"run_id", "status": "success"|"error", "output": {...}, "agents": [...], "trace_id", "log_file", "execution_time_seconds"}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

AGENTS = [
    {"name": "Planner", "role": "Decides which evidence classes the verdict still lacks and drafts their SQL (Nemotron via Nightshift; validator-gated)"},
    {"name": "Retriever", "role": "Runs read-only SQL against Zetaris (three sources, one schema) and records the access footprint"},
    {"name": "Evaluator", "role": "Applies the decision rules, finds conflicts and gaps, sends the Planner back for more evidence (max 3 rounds); then asks the model for an independent second opinion and escalates to a human on disagreement"},
    {"name": "Reporter", "role": "Proposer: drafts the summary and the report with per-claim citations, what could not be confirmed, next checks, redacted"},
    {"name": "Critic", "role": "Scores the summary on a 0-3 x 3 rubric (accuracy, appropriateness, actionability); below 7/9 returns one fix and the Reporter revises once"},
    {"name": "H-MEM (Meterless engine)", "role": "Remembers findings with provenance across investigations; re-verification raises confidence, changes supersede"},
    {"name": "World Model (Meterless engine)", "role": "Shared entity graph: accounts, datasets, tickets, exceptions; dossier feeds the Planner and Evaluator"},
]


def triage(request: dict) -> dict:
    """Run one triage from a request dict and return the response dict (never raises)."""
    t0 = time.time()
    run_id = str(request.get("run_id") or f"run-{int(t0)}")
    opts = request.get("options") or {}
    alert_id = (request.get("input") or {}).get("alert_id")
    if opts.get("sample_mode") is not None:
        os.environ["SAMPLE_MODE"] = "true" if opts["sample_mode"] else "false"
    if opts.get("llm_stub") is not None:
        os.environ["LLM_STUB"] = "true" if opts["llm_stub"] else "false"
    os.environ["SIMULATE_OUTAGE"] = str(opts.get("simulate_outage") or "")
    from agent import config
    from agent.main import run
    from agent.roles import Task
    from agent.zetaris import DataLayerError
    try:
        if not alert_id:
            raise ValueError("input.alert_id is required")
        report, out = run(Task(alert_id=str(alert_id)), run_id=run_id)
        return {"run_id": run_id, "status": "success",
                "output": {"alert_id": out["alert_id"], "verdict": out["verdict"], "confidence": out["confidence"], "priority": out["priority"],
                           "triage_status": out["status"], "reasons": out["reasons"], "evidence": out["evidence"], "timeline": out["timeline"],
                           "conflicts": out["conflicts"], "cannot_confirm": out["gaps"], "next_steps": out["next_steps"],
                           "disclosure": out["disclosure"], "rounds": out["rounds"], "memory": out.get("memory"), "world_model": out.get("world"),
                           "report_markdown": report, "data_layer": out["backend"], "model_provider": out["llm"]},
                "agents": AGENTS, "trace_id": out["run_id"], "log_file": out["log_file"],
                "execution_time_seconds": round(time.time() - t0, 1)}
    except DataLayerError as exc:
        return {"run_id": run_id, "status": "error", "error": {"type": "DataLayerError", "message": str(exc), "recoverable": True,
                                                              "hint": "retry when the source is reachable, or set options.sample_mode=true"},
                "trace_id": run_id, "execution_time_seconds": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001 - structured error instead of a crash
        return {"run_id": run_id, "status": "error", "error": {"type": type(exc).__name__, "message": str(exc)[:500], "recoverable": False},
                "trace_id": run_id, "execution_time_seconds": round(time.time() - t0, 1)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            from agent.llm import get_llm
            from agent.zetaris import get_backend
            status = {"status": "ok", "sample_mode": os.environ.get("SAMPLE_MODE", "false")}
            try:
                b = get_backend(); b.query("SELECT 1 AS one"); status["data_layer"] = b.name
            except Exception as exc:  # noqa: BLE001
                status["data_layer"] = f"unavailable: {str(exc)[:120]}"
            try:
                status["model_provider"] = get_llm().health()
            except Exception as exc:  # noqa: BLE001
                status["model_provider"] = f"unavailable: {str(exc)[:120]}"
            self._send(200, status)
        elif self.path.startswith("/alerts"):
            from agent.zetaris import get_backend
            try:
                rows = get_backend().query("SELECT alert_id, rule, severity FROM dexa.alerts ORDER BY alert_id", limit=500).rows
                self._send(200, {"alerts": rows})
            except Exception as exc:  # noqa: BLE001
                self._send(503, {"status": "error", "error": {"type": "DataLayerError", "message": str(exc)[:300], "recoverable": True}})
        else:
            self._send(404, {"status": "error", "error": {"type": "NotFound", "message": "use POST /run, GET /health, GET /alerts"}})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/run"):
            return self._send(404, {"status": "error", "error": {"type": "NotFound", "message": "POST /run"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc:
            return self._send(400, {"status": "error", "error": {"type": "BadRequest", "message": f"invalid JSON: {exc}", "recoverable": True}})
        body = triage(request)
        self._send(200 if body["status"] == "success" else 500, body)

    def log_message(self, fmt, *args):  # readable access log on stderr
        print(f"[INFO] http {self.address_string()} {fmt % args}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="JSON request file")
    ap.add_argument("--alert", help="alert id (shorthand for a request)")
    ap.add_argument("--run-id")
    ap.add_argument("--sample", action="store_true", help="SAMPLE_MODE: SQLite twin + stub model, no network")
    ap.add_argument("--outage", default="", help="simulate an outage: logs | policy | tickets")
    ap.add_argument("--output", help="write the JSON response here as well as stdout")
    ap.add_argument("--serve", action="store_true", help="start the HTTP API on 0.0.0.0:$PORT (default 8000)")
    a = ap.parse_args()
    if a.serve:
        port = int(os.environ.get("PORT", "8000"))
        print(f"[INFO] ExportWhy API on http://0.0.0.0:{port}  (POST /run, GET /health, GET /alerts)  sample_mode={os.environ.get('SAMPLE_MODE', 'false')}", file=sys.stderr, flush=True)
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
        return 0
    if a.input:
        request = json.loads(Path(a.input).read_text())
    elif a.alert:
        request = {"run_id": a.run_id or f"cli-{a.alert}", "input": {"alert_id": a.alert}, "options": {"sample_mode": a.sample, "simulate_outage": a.outage}}
    else:
        ap.error("give --input FILE, --alert ID, or --serve")
    if a.run_id:
        request["run_id"] = a.run_id
    body = triage(request)
    text = json.dumps(body, indent=1, default=str)
    if a.output:
        Path(a.output).write_text(text)
    print(text)
    return 0 if body["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
