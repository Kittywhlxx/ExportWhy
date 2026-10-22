"""Hybrid retrieval and reinjection (port of reference/src/memoryRetrieval.ts; AGENTS.md section 6).

  raw = 0.35*semantic + 0.20*keyword + 0.10*tag + 0.10*domain
      + 0.15*entity   + 0.05*layer   + 0.05*recency - 0.20*superseded
  score = clamp01(raw) * confidence          <- confidence multiplies AFTER the sum

keyword = Jaccard of the top-8 query tokens vs memory tokens.
layer weights: long_term 1.0, working 0.8, short_term 0.6.
recency = exponential decay on lastAccessed, 14-day time constant.
superseded records are penalised, never deleted or filtered.
Threshold 0.35 applies to the FINAL score; top N = 5; ties broken by id.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

from .embeddings import cosine, embed, jaccard, tokenize
from .memory_store import MemoryStoreService
from .models import (
    DAY_MS,
    DEFAULTS,
    FEEDBACK_DELTAS,
    MemoryRecord,
    QueryResult,
    RetrievedMemory,
    clamp01,
    js_to_fixed,
    snapshot,
)
from .trust_ledger import TrustLedgerService

LAYER_WEIGHT: Dict[str, float] = {
    "long_term": 1.0,
    "working": 0.8,
    "short_term": 0.6,
}


def _utf16_key(s: str) -> bytes:
    """Sort key matching JS default (UTF-16 code unit) string ordering."""
    return s.encode("utf-16-be", "surrogatepass")


class MemoryRetrievalService:
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

    def _prepare(self, query: str):
        query_embedding = embed(query)
        query_tokens = tokenize(query)[:8]
        query_entities = self._store.extract_entities(query)
        query_domain = self._store.classify_domain(query, [], "general")
        return query_embedding, query_tokens, query_entities, query_domain

    def score_one(self, memory: MemoryRecord, query: str) -> float:
        return self._score(memory, *self._prepare(query))

    def _score(
        self,
        m: MemoryRecord,
        query_embedding: List[float],
        query_tokens: List[str],
        query_entities: List[str],
        query_domain: str,
    ) -> float:
        semantic = clamp01(cosine(query_embedding, m.embedding))
        keyword = jaccard(query_tokens, tokenize(m.content))
        tag = jaccard(query_tokens, [t.lower() for t in m.tags])
        domain = 1 if m.domain == query_domain else 0
        entity = jaccard(query_entities, m.entities)
        layer = LAYER_WEIGHT[m.layer]
        age_days = (self._clock() - m.lastAccessed) / DAY_MS
        recency = math.exp(-age_days / DEFAULTS["recencyDays"])
        superseded = 1 if m.supersededBy else 0
        raw = (
            0.35 * semantic + 0.2 * keyword + 0.1 * tag + 0.1 * domain
            + 0.15 * entity + 0.05 * layer + 0.05 * recency - 0.2 * superseded
        )
        return clamp01(raw) * m.confidence

    def query(
        self,
        query: str,
        *,
        topN: Optional[int] = None,
        threshold: Optional[float] = None,
        chatId: Optional[str] = None,
    ) -> QueryResult:
        query_embedding, query_tokens, query_entities, query_domain = self._prepare(query)
        threshold = DEFAULTS["retrievalThreshold"] if threshold is None else threshold
        top_n = DEFAULTS["retrievalTopN"] if topN is None else topN

        scored: List[RetrievedMemory] = []
        for m in self._store.all():
            if chatId and m.chatId and m.chatId != chatId:
                continue
            s = self._score(m, query_embedding, query_tokens, query_entities, query_domain)
            if s >= threshold:
                scored.append(RetrievedMemory(memory=m, relevance=s))
        # relevance descending, ties by id (reference: localeCompare; ids are ASCII "mem-N").
        scored.sort(key=lambda r: (-r.relevance, r.memory.id))
        top = scored[:top_n]

        for item in top:
            self._store.touch(item.memory.id)
            self._ledger.log(item.memory.id, "read", self._actor, details={"query": query})

        if len(top) <= 1:
            strategy = "minimal"
        elif any(t.memory.type in ("personal", "preference") for t in top):
            strategy = "personal"
        else:
            strategy = "comprehensive"
        return QueryResult(
            memories=top,
            context=self.format_reinjection(top),
            trace={
                "retrievalReason": f'hybrid ranking for "{query}" (domain: {query_domain})',
                "strategy": strategy,
                "scores": {t.memory.id: js_to_fixed(t.relevance, 4) for t in top},
            },
        )

    # Domain-grouped reinjection blocks with layer markers and entities (section 6.2).
    def format_reinjection(self, items: List[RetrievedMemory]) -> str:
        if not items:
            return ""
        by_domain: Dict[str, List[RetrievedMemory]] = {}
        for it in items:
            by_domain.setdefault(it.memory.domain, []).append(it)
        lines = ["[MEMORY CONTEXT]"]
        # JS `[...entries].sort()` compares String([domain, list]) = "domain,[object Object],...".
        entries = sorted(
            by_domain.items(),
            key=lambda kv: _utf16_key(kv[0] + "," + ",".join(["[object Object]"] * len(kv[1]))),
        )
        for domain, lst in entries:
            lines.append(f"## {domain}")
            for it in lst:
                memory = it.memory
                marker = "LT" if memory.layer == "long_term" else "WK" if memory.layer == "working" else "ST"
                ents = f" (entities: {', '.join(memory.entities)})" if memory.entities else ""
                lines.append(f"- [{marker}] {memory.content}{ents} <{memory.id}>")
        lines.append("[/MEMORY CONTEXT]")
        return "\n".join(lines)

    # Exact feedback deltas (AGENTS.md section 9.2).
    def feedback(self, memory_id: str, kind: str) -> None:
        if kind not in FEEDBACK_DELTAS:
            raise ValueError(f"H-MEM: unknown feedback kind {kind!r}")
        m = self._store.get(memory_id)
        if m is None:
            raise KeyError(f"H-MEM: unknown memory id {memory_id}")
        prev = snapshot(m)
        if kind == "helpful":
            m.accessCount += 1
        m.confidence = clamp01(m.confidence + FEEDBACK_DELTAS[kind])
        if kind == "wrong" and "review" not in m.tags:
            m.tags.append("review")
        self._ledger.log(
            memory_id, "feedback", self._actor,
            previousState=prev, newState=snapshot(m),
            details={"kind": kind, "delta": FEEDBACK_DELTAS[kind]},
        )
        self._store.persist()
