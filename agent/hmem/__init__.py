"""H-MEM: Python port of the Meterless H-MEM reference implementation.

Source: https://github.com/Meterless/Meterless/tree/main/engines/hmem (reference/src/*.ts).
Conformance vectors (conformance_vectors.json, byte-identical copy) come from
https://github.com/Meterless/Meterless/tree/main/engines/hmem/conformance

Deterministic, zero third-party dependencies, in-memory with optional JSON
persistence (``persist_dir``: state.json + ledger.jsonl, same layout as the
reference). This is the minimal runnable expression of AGENTS.md, not a
production library.
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

from .conflict_detection import ConflictDetectionService
from .dreaming import DreamingService
from .embeddings import cosine, embed, fnv1a, jaccard, tokenize
from .memory_mining import MemoryMiningService, parse_strict_json_array
from .memory_retrieval import MemoryRetrievalService
from .memory_store import MemoryStoreService
from .models import (
    DAY_MS,
    DEFAULTS,
    FEEDBACK_DELTAS,
    LEDGER_ACTIONS,
    ConflictRecord,
    DreamProposal,
    LedgerEntry,
    MemoryRecord,
    Provenance,
    QueryResult,
    RetrievedMemory,
    SleepPreview,
    SleepReport,
    clamp01,
    snapshot,
)
from .sleep_cycle import SleepCycleService
from .trust_ledger import TrustLedgerService


def _default_clock() -> int:
    return int(time.time() * 1000)  # Date.now(): integer ms since epoch


class HMEM:
    """Facade wiring the services together (port of reference/src/index.ts)."""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], int]] = None,
        persist_dir: Optional[str] = None,
        actor: Optional[str] = None,
        miner_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.clock: Callable[[], int] = clock if clock is not None else _default_clock
        actor = actor if actor is not None else "system"
        self.ledger = TrustLedgerService(self.clock, persist_dir)
        self.store = MemoryStoreService(self.clock, self.ledger, actor, persist_dir)
        self.mining = MemoryMiningService(self.store, self.clock, miner_fn)
        self.retrieval = MemoryRetrievalService(self.store, self.ledger, self.clock, actor)
        self.conflicts = ConflictDetectionService(self.store, self.ledger, self.clock, actor)
        self.sleep = SleepCycleService(self.store, self.ledger, self.clock, actor)
        self.dreaming = DreamingService(self.store, self.ledger, self.clock, actor)

    # Convenience surface mirroring HOW_TO_USE_H-MEM.md.
    def add(self, content: str, **kwargs: Any) -> MemoryRecord:
        return self.store.add(content, **kwargs)

    def mine(self, event: str, text: str, *, chatId: Optional[str] = None) -> List[MemoryRecord]:
        return self.mining.mine_interaction(event, text, chatId=chatId)

    def query(
        self,
        query: str,
        *,
        topN: Optional[int] = None,
        threshold: Optional[float] = None,
        chatId: Optional[str] = None,
    ) -> QueryResult:
        return self.retrieval.query(query, topN=topN, threshold=threshold, chatId=chatId)

    def feedback(self, memory_id: str, kind: str) -> None:
        self.retrieval.feedback(memory_id, kind)


__all__ = [
    "HMEM",
    "MemoryStoreService",
    "TrustLedgerService",
    "MemoryMiningService",
    "MemoryRetrievalService",
    "ConflictDetectionService",
    "SleepCycleService",
    "DreamingService",
    "parse_strict_json_array",
    "embed",
    "cosine",
    "tokenize",
    "jaccard",
    "fnv1a",
    "MemoryRecord",
    "Provenance",
    "LedgerEntry",
    "ConflictRecord",
    "DreamProposal",
    "SleepPreview",
    "SleepReport",
    "RetrievedMemory",
    "QueryResult",
    "FEEDBACK_DELTAS",
    "DEFAULTS",
    "DAY_MS",
    "LEDGER_ACTIONS",
    "clamp01",
    "snapshot",
]
