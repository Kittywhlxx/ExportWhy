"""Conflict detection and resolution (port of reference/src/conflictDetection.ts; AGENTS.md section 10).

The losing record is marked supersededBy (kept, penalised in ranking), never
deleted. Auto-resolve only at decision confidence >= 0.70; everything else queues.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from .embeddings import cosine, js_len
from .memory_store import MemoryStoreService
from .models import DEFAULTS, ConflictRecord, MemoryRecord, clamp01, js_to_fixed, snapshot
from .trust_ledger import TrustLedgerService

_F = re.I | re.ASCII  # JS non-unicode /i and ASCII \b
OPPOSING_PAIRS: List[Tuple[re.Pattern, re.Pattern]] = [
    (re.compile(r"\balways\b", _F), re.compile(r"\bnever\b", _F)),
    (re.compile(r"\benable[ds]?\b", _F), re.compile(r"\bdisable[ds]?\b", _F)),
    # Alternation precedence kept from the reference: (\bavoid[s]?) | (dislike[s]?\b).
    (re.compile(r"\bprefer[s]?\b", _F), re.compile(r"\bavoid[s]?|dislike[s]?\b", _F)),
    (re.compile(r"\bon\b", _F), re.compile(r"\boff\b", _F)),
    (re.compile(r"\byes\b", _F), re.compile(r"\bno\b", _F)),
]
_NUMBER = re.compile(r"\d+(\.\d+)?", re.ASCII)


class ConflictDetectionService:
    def __init__(
        self,
        store: MemoryStoreService,
        ledger: TrustLedgerService,
        clock: Callable[[], int],
        actor: str,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._actor = actor
        self._conflicts: List[ConflictRecord] = []
        self._seq = 0

    def scan(self) -> List[ConflictRecord]:
        found: List[ConflictRecord] = []
        records = self._store.all()
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                conflict = self._check(records[i], records[j])
                if conflict:
                    found.append(conflict)
                    self._conflicts.append(conflict)
                    self._ledger.log(
                        conflict.memoryA, "conflict_detected", self._actor,
                        details={"with": conflict.memoryB, "reason": conflict.reason, "confidence": conflict.confidence},
                    )
        return found

    def _check(self, a: MemoryRecord, b: MemoryRecord) -> Optional[ConflictRecord]:
        # supersedes relation suppresses conflict: it is an intended replacement.
        if a.supersedes == b.id or b.supersedes == a.id or a.supersededBy == b.id or b.supersededBy == a.id:
            return None

        sim = cosine(a.embedding, b.embedding)
        reason = ""
        confidence = 0.0

        for p, q in OPPOSING_PAIRS:
            if (p.search(a.content) and q.search(b.content)) or (q.search(a.content) and p.search(b.content)):
                if sim > 0.5:
                    reason = "opposing phrase pair on similar content"
                    confidence = 0.5 + 0.3 * sim
        if not reason and sim > 0.8:
            nums_a = [m.group(0) for m in _NUMBER.finditer(a.content)]
            nums_b = [m.group(0) for m in _NUMBER.finditer(b.content)]
            if nums_a and nums_b and ",".join(nums_a) != ",".join(nums_b):
                reason = "high-similarity conflicting numeric values"
                confidence = 0.55 + 0.25 * sim
        if not reason:
            return None

        # Confidence modifiers (section 10.1).
        shared_entities = [e for e in a.entities if e in b.entities]
        if shared_entities:
            confidence += 0.1
        if a.domain == b.domain:
            confidence += 0.05
        if b.id in a.relatedTo:
            confidence -= 0.1  # explicit relation lowers false positives

        self._seq += 1
        return ConflictRecord(
            id=f"conflict-{self._seq}",
            memoryA=a.id,
            memoryB=b.id,
            reason=reason,
            confidence=clamp01(confidence),
            resolved=False,
        )

    def _score_of(self, m: MemoryRecord) -> float:
        origin = m.provenance.origin if m.provenance is not None else None
        return (
            0.3 * (m.lastAccessed / max(1, self._clock()))
            + 0.2 * min(1, m.accessCount / 10)
            + 0.25 * m.confidence
            + 0.1 * min(1, js_len(m.content) / 200)
            + 0.15 * (1 if origin == "user_correction" else 0.5)
        )

    # Weighted auto-decision heuristics (section 10.2).
    def auto_resolve(self, conflict: ConflictRecord) -> str:
        a = self._store.get(conflict.memoryA)
        b = self._store.get(conflict.memoryB)
        if a is None or b is None:
            raise KeyError(f"H-MEM: conflict {conflict.id} references a missing memory")
        sa = self._score_of(a)
        sb = self._score_of(b)
        margin = abs(sa - sb) / max(sa, sb)
        decision_confidence = clamp01(conflict.confidence * 0.6 + margin * 0.8)

        if decision_confidence < DEFAULTS["conflictAutoResolveGate"]:
            return "queued"

        winner, loser = (a, b) if sa >= sb else (b, a)
        prev = snapshot(loser)
        loser.supersededBy = winner.id  # kept and penalised, never deleted
        conflict.resolved = True
        conflict.resolution = "keep_a" if winner.id == conflict.memoryA else "keep_b"
        self._ledger.log(
            loser.id, "conflict_resolved", self._actor,
            previousState=prev, newState=snapshot(loser),
            details={"conflictId": conflict.id, "winner": winner.id,
                     "decisionConfidence": js_to_fixed(decision_confidence, 3)},
        )
        self._store.persist()
        return "resolved"

    def queue(self) -> List[ConflictRecord]:
        return [c for c in self._conflicts if not c.resolved]
