"""Canonical H-MEM shapes (port of reference/src/types.ts; AGENTS.md sections 2 and 9).

Record attribute names stay camelCase because records are serialised to JSON
and read by other code. ``None`` plays the role of TypeScript ``undefined``:
``to_dict()`` omits ``None`` fields, exactly as ``JSON.stringify`` drops
``undefined`` properties.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any, Dict, List, Optional

MEMORY_TYPES = ("personal", "factual", "preference", "general")
MEMORY_LAYERS = ("short_term", "working", "long_term")
PROVENANCE_ORIGINS = (
    "chat_message", "user_correction", "file_save", "file_open", "file_edit",
    "model_response", "plan_completion", "document", "manual", "dream", "sleep_synthesis",
)
# The canonical 17 ledger action types (AGENTS.md section 9.1).
LEDGER_ACTIONS = (
    "create", "read", "update", "delete", "feedback", "promote", "demote", "merge",
    "conflict_detected", "conflict_resolved",
    "sleep_consolidate", "sleep_archive", "sleep_synthesize",
    "dream_proposed", "dream_approved", "dream_rejected", "restore",
)
MINING_EVENT_TYPES = (
    "chat_message", "user_correction", "file_save", "file_open", "file_edit",
    "model_response", "plan_completion",
)

FEEDBACK_DELTAS = MappingProxyType({"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.2})

DEFAULTS = MappingProxyType({
    "retrievalTopN": 5,
    "retrievalThreshold": 0.35,  # on the final score, after the confidence multiplier
    "consolidationDays": 7,
    "archiveMinDays": 30,
    "archiveMaxAccess": 2,
    "synthesisCosine": 0.82,
    "dreamClusterMinSize": 2,
    "dreamRelatedness": 0.35,
    "maxDreamProposalsPerType": 8,
    "conflictAutoResolveGate": 0.7,
    "entityCap": 10,
    "relatedCap": 5,
    "recencyDays": 14,
})

DAY_MS = 24 * 60 * 60 * 1000


def clamp01(x: float) -> float:
    """Math.min(1, Math.max(0, x)); NaN propagates as in JavaScript."""
    if isinstance(x, float) and math.isnan(x):
        return x
    return min(1.0, max(0.0, float(x)))


def js_to_fixed(x: float, digits: int) -> float:
    """Number(x.toFixed(digits)): exact decimal of the double, ties away from zero."""
    if math.isnan(x) or math.isinf(x):
        return x
    q = Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return float(q)


def to_json_obj(value: Any) -> Any:
    """Recursively convert dataclasses to JSON-ready dicts, dropping None (undefined)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            if v is not None:
                out[f.name] = to_json_obj(v)
        return out
    if isinstance(value, dict):
        return {k: to_json_obj(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_json_obj(v) for v in value]
    return value


@dataclass
class Provenance:
    origin: str
    learnedAt: int
    label: str
    refs: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)

    @classmethod
    def from_dict(cls, d: Any) -> "Provenance":
        if isinstance(d, Provenance):
            return Provenance(d.origin, d.learnedAt, d.label, list(d.refs) if d.refs is not None else None)
        refs = d.get("refs")
        return cls(origin=d["origin"], learnedAt=d["learnedAt"], label=d["label"],
                   refs=list(refs) if refs is not None else None)


@dataclass(kw_only=True)
class MemoryRecord:
    # Field order mirrors the object literal in memoryStore.add() so JSON key
    # order matches the reference; supersededBy/score are appended later there.
    id: str
    content: str
    type: str
    layer: str
    timestamp: int
    lastAccessed: int
    accessCount: int
    tags: List[str]
    embedding: List[float]
    confidence: float  # mandatory
    source: str  # mandatory; construction rejects records without it
    provenance: Optional[Provenance]  # mandatory for new writes; may be absent on legacy loads
    chatId: Optional[str] = None
    missionId: Optional[str] = None
    goalRunId: Optional[str] = None
    domain: str = "general"
    namespace: str = "general/notes"
    entities: List[str] = field(default_factory=list)  # cap 10
    relatedTo: List[str] = field(default_factory=list)  # cap 5
    supersedes: Optional[str] = None
    derivedFrom: Optional[List[str]] = None
    supersededBy: Optional[str] = None  # set by conflict resolution; drives the retrieval penalty
    score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        prov = kwargs.get("provenance")
        kwargs["provenance"] = Provenance.from_dict(prov) if prov is not None else None
        for k in ("tags", "embedding", "entities", "relatedTo"):
            kwargs[k] = list(kwargs.get(k) or [])
        if kwargs.get("derivedFrom") is not None:
            kwargs["derivedFrom"] = list(kwargs["derivedFrom"])
        return cls(**kwargs)


MEMORY_RECORD_FIELDS = tuple(f.name for f in dataclasses.fields(MemoryRecord))


def snapshot(r: MemoryRecord) -> Dict[str, Any]:
    """Deep JSON snapshot of a record without its embedding (memoryStore.snapshot)."""
    d = r.to_dict()
    d.pop("embedding", None)
    return d


@dataclass
class LedgerEntry:
    memoryId: str
    action: str
    actor: str
    timestamp: int
    previousState: Optional[Dict[str, Any]] = None
    newState: Optional[Dict[str, Any]] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LedgerEntry":
        return cls(
            memoryId=d["memoryId"], action=d["action"], actor=d["actor"], timestamp=d["timestamp"],
            previousState=d.get("previousState"), newState=d.get("newState"), details=d.get("details"),
        )


@dataclass
class DreamProposal:
    id: str
    type: str  # "insight" | "domain" | "invariant"
    content: str
    derivedFrom: List[str]
    status: str = "proposed"  # "proposed" | "approved" | "rejected"
    suggestedDomain: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)


@dataclass
class SleepPreview:
    toConsolidate: List[str]
    toArchive: List[str]
    toSynthesize: List[List[str]]

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)


@dataclass
class SleepReport:
    backupId: str
    consolidated: int
    archived: int
    synthesized: int
    actionLog: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)


@dataclass
class ConflictRecord:
    id: str
    memoryA: str
    memoryB: str
    reason: str
    confidence: float
    resolved: bool
    resolution: Optional[str] = None  # "keep_a" | "keep_b" | "keep_both" | "merge" | "delete_both"

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)


@dataclass
class RetrievedMemory:
    memory: MemoryRecord
    relevance: float

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)


@dataclass
class QueryResult:
    memories: List[RetrievedMemory]
    context: str
    # {"retrievalReason": str, "strategy": "minimal"|"personal"|"comprehensive", "scores": {id: float}}
    trace: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return to_json_obj(self)
