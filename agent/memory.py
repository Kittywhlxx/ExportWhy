"""H-MEM adapter: what the loop remembers across runs and how it recalls it.

Memories are factual records with provenance pointing at the Zetaris query (step id, table,
source URL). Recall is scoped by package so a second run on the same package sees what an earlier
run established, and the Evaluator re-verifies instead of trusting it blindly.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config

MEMORY_DIR = config.ROOT / "memory"


class Memory:
    def __init__(self, persist: bool = True, clock=None):
        from .hmem import HMEM  # the Python port of the Meterless H-MEM spec (agent/hmem/)
        kwargs = {"actor": "depriskr"}
        if persist:
            MEMORY_DIR.mkdir(exist_ok=True)
            kwargs["persist_dir"] = str(MEMORY_DIR)
        if clock:
            kwargs["clock"] = clock
        self.h = HMEM(**kwargs)

    # -- recall ------------------------------------------------------------------------------
    def recall(self, query: str, top_n: int = 5) -> tuple[str, list[dict]]:
        res = self.h.query(query, topN=top_n)
        items = []
        for rm in _memories(res):
            m = rm["memory"] if isinstance(rm, dict) else rm.memory
            items.append({"id": _g(m, "id"), "content": _g(m, "content"), "confidence": _g(m, "confidence"),
                          "source": _g(m, "source"), "relevance": rm["relevance"] if isinstance(rm, dict) else rm.relevance,
                          "superseded": bool(_g(m, "supersededBy"))})
        context = _g(res, "context") or ""
        return context, items

    # -- remember ----------------------------------------------------------------------------
    def remember(self, content: str, source: str, refs: list[str], tags: list[str], confidence: float,
                 mtype: str = "factual", goal_run_id: str | None = None, key: str | None = None) -> tuple[object, str]:
        """Store a finding. `key` (e.g. "fact:python-jose:3.5.0:vulns_pypi") makes it idempotent across runs:
        same key + same content  -> re-verified: feedback(helpful) raises confidence, ledger records it;
        same key + new content   -> the new record supersedes the old one (old kept, ranked down, never deleted).
        Returns (record, action) with action in {"created", "reconfirmed", "superseded"}."""
        now = int(time.time() * 1000)
        prev = self._latest_with_key(key) if key else None
        if prev is not None and _g(prev, "content") == content:
            self.h.feedback(_g(prev, "id"), "helpful")
            return self.h.store.get(_g(prev, "id")), "reconfirmed"
        rec = self.h.add(content=content, source=source, type=mtype, tags=tags + ([key] if key else []), confidence=confidence,
                         provenance={"origin": "manual", "learnedAt": now, "label": f"learned from {source}", "refs": refs},
                         goalRunId=goal_run_id, supersedes=_g(prev, "id") if prev is not None else None)
        if prev is not None:
            self.h.store.update(_g(prev, "id"), {"supersededBy": _g(rec, "id")})
            return rec, "superseded"
        return rec, "created"

    def _latest_with_key(self, key: str):
        cands = [m for m in self.h.store.all() if key in (_g(m, "tags") or []) and not _g(m, "supersededBy")]
        return max(cands, key=lambda m: _g(m, "timestamp") or 0) if cands else None

    def feedback(self, memory_id: str, kind: str):
        self.h.feedback(memory_id, kind)

    def all(self) -> list[dict]:
        return [{"id": _g(m, "id"), "content": _g(m, "content"), "confidence": _g(m, "confidence"), "tags": _g(m, "tags"),
                 "source": _g(m, "source"), "superseded": bool(_g(m, "supersededBy"))} for m in self.h.store.all()]

    def ledger_stats(self) -> dict:
        return self.h.ledger.stats()

    def history(self, memory_id: str) -> list[dict]:
        """The trust-ledger entries for one memory, oldest first (action, actor, confidence before/after)."""
        out = []
        for e in self.h.ledger.history(memory_id):
            prev, new = _g(e, "previousState") or {}, _g(e, "newState") or {}
            out.append({"action": _g(e, "action"), "actor": _g(e, "actor"), "timestamp": _g(e, "timestamp"),
                        "confidence_before": _g(prev, "confidence"), "confidence_after": _g(new, "confidence"),
                        "details": _g(e, "details") or {}})
        return out

    def for_alert(self, alert_id: str) -> list[dict]:
        tag = f"alert:{alert_id}"
        return [m for m in self.all() if tag in (m.get("tags") or [])]


def _g(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _memories(res):
    return _g(res, "memories") or []
