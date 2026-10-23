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
    """One persisted H-MEM store, two actors (the agent "exportwhy" and the human "analyst"). With persistence
    every public operation re-opens the store from disk first, so a UI session, a CLI run and an analyst
    action never work from a stale snapshot and never overwrite each other's writes (the reference
    implementation persists the whole state file on every mutation)."""

    def __init__(self, persist: bool = True, clock=None):
        from .hmem import HMEM  # the Python port of the Meterless H-MEM spec (agent/hmem/)
        self.persist, self.clock = persist, clock
        if persist:
            MEMORY_DIR.mkdir(exist_ok=True)
        self._mem_only = None if persist else HMEM(actor="exportwhy", **({"clock": clock} if clock else {}))

    @property
    def h(self):
        return self._open("exportwhy")

    def _open(self, actor: str):
        if not self.persist:
            return self._mem_only                     # memory-only (tests): a single shared instance
        from .hmem import HMEM
        kwargs = {"actor": actor, "persist_dir": str(MEMORY_DIR)}
        if self.clock:
            kwargs["clock"] = self.clock
        return HMEM(**kwargs)

    def _reload(self):
        pass                                          # every operation already opens a fresh instance

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
        h = self.h                                   # one fresh instance for lookup + write
        prev = self._latest_with_key(h, key) if key else None
        if prev is not None and _g(prev, "content") == content:
            h.feedback(_g(prev, "id"), "helpful")
            return h.store.get(_g(prev, "id")), "reconfirmed"
        rec = h.add(content=content, source=source, type=mtype, tags=tags + ([key] if key else []), confidence=confidence,
                    provenance={"origin": "manual", "learnedAt": now, "label": f"learned from {source}", "refs": refs},
                    goalRunId=goal_run_id, supersedes=_g(prev, "id") if prev is not None else None)
        if prev is not None:
            h.store.update(_g(prev, "id"), {"supersededBy": _g(rec, "id")})
            return rec, "superseded"
        return rec, "created"

    @staticmethod
    def _latest_with_key(h, key: str):
        cands = [m for m in h.store.all() if key in (_g(m, "tags") or []) and not _g(m, "supersededBy")]
        return max(cands, key=lambda m: _g(m, "timestamp") or 0) if cands else None

    def feedback(self, memory_id: str, kind: str):
        """Human feedback on a fact. Logged by a separate actor ("analyst") so it is never confused with the
        agent's own re-verification, which also uses the helpful delta."""
        self._as_analyst().feedback(memory_id, kind)

    def _as_analyst(self):
        return self._open("analyst")

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

    def verdict_feedback(self, alert_id: str, agree: bool, note: str = "") -> dict | None:
        """Analyst feedback on the verdict itself (Google SecOps-style thumbs on the disposition). Applied to the
        verdict memory as helpful/wrong, so the ledger keeps who-said-what and the next recall ranks it accordingly."""
        cands = [m for m in self.all() if f"alert:{alert_id}" in (m.get("tags") or []) and "kind:verdict" in (m.get("tags") or []) and not m["superseded"]]
        rec = cands[-1] if cands else None          # the current verdict record, never a superseded one
        if rec is None:
            return None
        self._as_analyst().feedback(rec["id"], "helpful" if agree else "wrong")
        if note:
            self.remember(f"Analyst note on {alert_id}: {note}", source=f"analyst:{alert_id}", refs=[], tags=[f"alert:{alert_id}", "kind:analyst-note"], confidence=1.0, mtype="general")
        return self.calibration()

    def calibration(self) -> dict:
        """How often analysts agreed with the verdicts, by verdict type, from the trust ledger."""
        out: dict[str, dict] = {}
        for m in self.all():
            tags = m.get("tags") or []
            if "kind:verdict" not in tags:
                continue
            verdict = (m["content"].split("verdict ", 1)[1].split(";")[0].strip() if "verdict " in m["content"] else "?")
            hist = [h for h in self.history(m["id"]) if h["action"] == "feedback" and h["actor"] == "analyst"]   # humans only; re-verification is not agreement
            agree = sum(1 for h in hist if (h["details"] or {}).get("kind") == "helpful")
            disagree = sum(1 for h in hist if (h["details"] or {}).get("kind") == "wrong")
            d = out.setdefault(verdict, {"verdicts": 0, "agree": 0, "disagree": 0})
            d["verdicts"] += 1; d["agree"] += agree; d["disagree"] += disagree
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
