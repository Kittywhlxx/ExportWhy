"""Tiered memory store with enrichment (port of reference/src/memoryStore.ts; AGENTS.md sections 2, 3, 5).

A record with no source is rejected by construction. Enrichment runs on every
add: domain classification, namespace, entity extraction (cap 10), relatedTo
discovery (cap 5, bidirectional), supersedes detection.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from .embeddings import cosine, embed, js_trim, tokenize
from .models import (
    DEFAULTS,
    MEMORY_RECORD_FIELDS,
    MemoryRecord,
    Provenance,
    clamp01,
    snapshot,
)
from .trust_ledger import TrustLedgerService

TECH_TERMS = [
    "react", "python", "typescript", "javascript", "rust", "sqlite", "postgres", "indexeddb",
    "node", "docker", "graphql", "redis", "webrtc", "tauri", "electron", "llm", "embedding",
]
DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "tech": ["code", "bug", "api", "database", "deploy", "framework", "library", "server", *TECH_TERMS],
    "work": ["meeting", "deadline", "client", "project", "sprint", "review", "report"],
    "personal": ["family", "hobby", "prefer", "like", "dislike", "morning", "evening"],
}

# re.ASCII reproduces JS non-unicode regex semantics for \b and /i.
_CAPITALISED = re.compile(r"\b([A-Z][a-zA-Z]{2,})\b", re.ASCII)
_CORRECTION = re.compile(r"\b(actually|instead|no longer|not anymore|changed to|correction)\b", re.I | re.ASCII)
_ENTITY_STOP = ("the", "this", "that", "with")

ProvenanceLike = Union[Provenance, Mapping[str, Any]]


class MemoryStoreService:
    def __init__(
        self,
        clock: Callable[[], int],
        ledger: TrustLedgerService,
        actor: str,
        persist_dir: Optional[str] = None,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._actor = actor
        self._records: Dict[str, MemoryRecord] = {}
        self._seq = 0
        self._state_file: Optional[str] = None
        if persist_dir:
            persist_dir = os.fspath(persist_dir)
            os.makedirs(persist_dir, exist_ok=True)
            self._state_file = os.path.join(persist_dir, "state.json")
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                self._seq = saved["seq"]
                for r in saved["records"]:
                    rec = MemoryRecord.from_dict(r)
                    self._records[rec.id] = rec

    def persist(self) -> None:
        if not self._state_file:
            return
        # Reference sorts with localeCompare; ids are ASCII "mem-N" so code-point order agrees.
        records = sorted(self._records.values(), key=lambda r: r.id)
        payload = {"seq": self._seq, "records": [r.to_dict() for r in records]}
        with open(self._state_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def add(
        self,
        content: str,
        *,
        source: Optional[str] = None,
        type: Optional[str] = None,
        layer: Optional[str] = None,
        tags: Optional[List[str]] = None,
        confidence: Optional[float] = None,
        provenance: Optional[ProvenanceLike] = None,
        chatId: Optional[str] = None,
        missionId: Optional[str] = None,
        goalRunId: Optional[str] = None,
        supersedes: Optional[str] = None,
        derivedFrom: Optional[List[str]] = None,
    ) -> MemoryRecord:
        if not isinstance(source, str) or js_trim(source) == "":
            raise ValueError(
                "H-MEM: a memory record with no source must be rejected by construction (AGENTS.md section 2)"
            )
        now = self._clock()
        self._seq += 1
        rec_id = f"mem-{self._seq}"
        mem_type = type if type is not None else "general"
        tag_list = list(tags) if tags is not None else []
        domain = self.classify_domain(content, tag_list, mem_type)
        record = MemoryRecord(
            id=rec_id,
            content=content,
            type=mem_type,
            layer=layer if layer is not None else "working",
            timestamp=now,
            lastAccessed=now,
            accessCount=0,
            tags=tag_list,
            embedding=embed(content),
            confidence=clamp01(confidence if confidence is not None else 0.7),
            source=source,
            provenance=(
                Provenance.from_dict(provenance)
                if provenance is not None
                else Provenance(origin="manual", learnedAt=now, label=source)
            ),
            chatId=chatId,
            missionId=missionId,
            goalRunId=goalRunId,
            domain=domain,
            namespace=self._namespace(domain, mem_type),
            entities=self.extract_entities(content),
            relatedTo=[],
            supersedes=supersedes,
            derivedFrom=list(derivedFrom) if derivedFrom is not None else None,
        )
        self._discover_relationships(record)
        self._records[rec_id] = record
        self._ledger.log(rec_id, "create", self._actor, newState=snapshot(record))
        self.persist()
        return record

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        return self._records.get(memory_id)

    def all(self) -> List[MemoryRecord]:
        return list(self._records.values())

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> MemoryRecord:
        """Object.assign(record, patch); patch keys are camelCase record fields."""
        rec = self._must_get(memory_id)
        unknown = [k for k in patch if k not in MEMORY_RECORD_FIELDS]
        if unknown:
            raise ValueError(f"H-MEM: unknown memory record field(s) {unknown}")
        prev = snapshot(rec)
        for k, v in patch.items():
            if k == "provenance" and v is not None:
                v = Provenance.from_dict(v)
            setattr(rec, k, v)
        self._ledger.log(memory_id, "update", self._actor, previousState=prev, newState=snapshot(rec))
        self.persist()
        return rec

    def delete(self, memory_id: str, reason: str) -> None:
        rec = self._must_get(memory_id)
        del self._records[memory_id]
        self._ledger.log(memory_id, "delete", self._actor, previousState=snapshot(rec), details={"reason": reason})
        self.persist()

    def touch(self, memory_id: str) -> None:
        # Mirrors the reference: no ledger entry and no persist here (query() logs "read").
        rec = self._must_get(memory_id)
        rec.lastAccessed = self._clock()
        rec.accessCount += 1

    def promote(self, memory_id: str, layer: str) -> None:
        rec = self._must_get(memory_id)
        prev = snapshot(rec)
        rec.layer = layer
        self._ledger.log(memory_id, "promote", self._actor, previousState=prev, newState=snapshot(rec))
        self.persist()

    def restore_all(self, records: List[MemoryRecord], seq: int) -> None:
        self._records = {r.id: copy.deepcopy(r) for r in records}
        self._seq = seq
        self.persist()

    def snapshot_all(self) -> Dict[str, Any]:
        return {"records": copy.deepcopy(list(self._records.values())), "seq": self._seq}

    def _must_get(self, memory_id: str) -> MemoryRecord:
        rec = self._records.get(memory_id)
        if rec is None:
            raise KeyError(f"H-MEM: unknown memory id {memory_id}")
        return rec

    # Multi-pass domain classification (AGENTS.md section 5).
    def classify_domain(self, content: str, tags: List[str], type: str) -> str:
        for t in tags:
            if t.startswith("project:"):
                return t[len("project:"):]
            if t.startswith("tech:"):
                return "tech"
        tokens = set(tokenize(content))
        best = "general"
        best_score = 0
        for domain, kws in DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in kws if kw in tokens)
            if domain == "personal" and type in ("personal", "preference"):
                score += 1
            if score > best_score:
                best = domain
                best_score = score
        return best if best_score > 0 else "general"

    @staticmethod
    def _namespace(domain: str, type: str) -> str:
        if type in ("preference", "personal"):
            bucket = "profile"
        elif type == "factual":
            bucket = "facts"
        else:
            bucket = "notes"
        return f"{domain}/{bucket}"

    def extract_entities(self, content: str) -> List[str]:
        entities: Dict[str, None] = {}  # insertion-ordered set
        for t in tokenize(content):
            if t in TECH_TERMS:
                entities[t] = None
        for m in _CAPITALISED.finditer(content):
            norm = m.group(1).lower()
            if norm not in _ENTITY_STOP:
                entities[norm] = None
        return list(entities)[: DEFAULTS["entityCap"]]

    def _discover_relationships(self, record: MemoryRecord) -> None:
        for other in self._records.values():
            if len(record.relatedTo) >= DEFAULTS["relatedCap"]:
                break
            shared_entities = [e for e in other.entities if e in record.entities]
            sim = cosine(record.embedding, other.embedding)
            if not record.supersedes and sim > 0.85 and _CORRECTION.search(record.content):
                record.supersedes = other.id
                continue
            if len(shared_entities) >= 1 and other.domain == record.domain:
                record.relatedTo.append(other.id)
                if len(other.relatedTo) < DEFAULTS["relatedCap"] and record.id not in other.relatedTo:
                    other.relatedTo.append(record.id)  # bidirectional link
