"""One JSON trace per run: every agent step, decision, query and retry reason."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import config


class Trace:
    def __init__(self, task: str, mode: str):
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self.started = time.time()
        self.data = {"run_id": self.run_id, "task": task, "mode": mode, "started_at": _iso(self.started),
                     "steps": [], "rounds": 0, "outcome": None}

    def step(self, agent: str, kind: str, **payload) -> dict:
        entry = {"t": round(time.time() - self.started, 3), "agent": agent, "kind": kind, **payload}
        self.data["steps"].append(entry)
        return entry

    def finish(self, outcome: dict) -> Path:
        self.data["outcome"] = outcome
        self.data["finished_at"] = _iso(time.time())
        self.data["elapsed_s"] = round(time.time() - self.started, 2)
        config.TRACE_DIR.mkdir(exist_ok=True)
        path = config.TRACE_DIR / f"{self.run_id}.json"
        path.write_text(json.dumps(self.data, indent=1, default=str))
        return path


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
