"""Record shapes (port of reference/src/types.ts).

Attribute names stay camelCase to match the JSON shapes. ``None`` plays the
role of TypeScript ``undefined``: ``to_dict()`` omits ``None`` fields, exactly
as ``JSON.stringify`` drops ``undefined`` properties. Exception: the
relationship endpoint ``from`` is a Python keyword, so the attribute is
``from_``; ``to_dict()`` / ``from_dict()`` use the JSON key ``"from"``.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_RENAME_OUT = {"from_": "from"}
_RENAME_IN = {"from": "from_"}


class _Record:
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            if v is None:
                continue
            out[_RENAME_OUT.get(f.name, f.name)] = copy.deepcopy(v)
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        kwargs = {}
        for k, v in d.items():
            k = _RENAME_IN.get(k, k)
            if k in names:
                kwargs[k] = copy.deepcopy(v)
        return cls(**kwargs)


@dataclass
class Entity(_Record):
    type: str
    id: str
    attrs: Dict[str, Any]
    schemaVersion: int
    source: Dict[str, Any]
    createdAt: str
    updatedAt: str
    name: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    state: str = "active"  # "emerging" | "active" | "archived"
    # {mentionCount, firstSeenAt, lastSeenAt, linkedStoryIds}; rebuilt by ingest step 9
    aggregates: Optional[Dict[str, Any]] = None


@dataclass
class Context(_Record):
    type: str
    id: str
    attrs: Dict[str, Any]
    schemaVersion: int
    source: Dict[str, Any]
    createdAt: str
    updatedAt: str
    parent: Optional[str] = None
    startedAt: Optional[str] = None
    endedAt: Optional[str] = None


@dataclass
class Relationship(_Record):
    """Bitemporal edge. validTo None means open-ended; superseding closes, never deletes."""

    id: str
    from_: str
    type: str
    to: str
    schemaVersion: int
    source: Dict[str, Any]
    createdAt: str
    context: Optional[str] = None
    attrs: Optional[Dict[str, Any]] = None
    validFrom: Optional[str] = None
    validTo: Optional[str] = None
    supersededBy: Optional[str] = None


@dataclass
class Fact(_Record):
    id: str
    about: str
    predicate: str
    value: Any
    confidence: float
    source: Dict[str, Any]
    assertedAt: str
    context: Optional[str] = None
    supersededBy: Optional[str] = None


@dataclass
class Snapshot(_Record):
    id: str
    at: str
    entities: List[str]
    schemaVersion: int
    source: Dict[str, Any]
    context: Optional[str] = None
    attrs: Optional[Dict[str, Any]] = None


@dataclass
class WorldEvent(_Record):
    """Append-only log entry; payload is the JSON shape of the record at append time."""

    id: str
    seq: int
    kind: str
    payload: Dict[str, Any]
    schemaVersion: int
    at: str


@dataclass
class AuditEntry(_Record):
    seq: int
    at: str
    kind: str
    actor: str
    summary: str
    eventId: Optional[str] = None
    runId: Optional[str] = None


@dataclass
class MergeProposal(_Record):
    id: str
    leftId: str
    rightId: str
    leftName: str
    rightName: str
    similarity: float
    reason: str
    status: str  # "pending" | "approved" | "rejected"
    proposedAt: str
    resolvedAt: Optional[str] = None
    resolvedBy: Optional[str] = None


@dataclass
class IngestReport(_Record):
    runId: str
    processed: int
    skippedUnchanged: int
    clusters: int
    eventsAppended: int
    changed: bool
    # wall-clock step timings: [{"step", "name", "ms"}]; reported only, never persisted
    stepTimingsMs: List[Dict[str, Any]] = field(default_factory=list)


RECORD_TYPES = {
    "upsertEntity": Entity,
    "upsertContext": Context,
    "relate": Relationship,
    "assertFact": Fact,
    "snapshot": Snapshot,
}
