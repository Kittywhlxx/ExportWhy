"""Event-sourced canonical store (port of reference/src/store.ts).

State is a fold over an append-only event log; views are projections of the
fold. ``storage="local"`` persists to ``<directory>/.world/<namespace>.json``
(events, audit, observations, stories, scores, mergeQueue) and replays the
events on load. Every mutation writes an audit entry.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .models import (
    AuditEntry,
    Context,
    Entity,
    Fact,
    MergeProposal,
    Relationship,
    WorldEvent,
)
from .stable_ids import strip_none

EPOCH_ISO = "1970-01-01T00:00:00.000Z"  # new Date(0).toISOString()


@dataclass
class FoldedState:
    entities: Dict[str, Entity] = field(default_factory=dict)
    contexts: Dict[str, Context] = field(default_factory=dict)
    relationships: Dict[str, Relationship] = field(default_factory=dict)
    facts: Dict[str, Fact] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)  # aliasId -> primaryId


def _payload_dict(payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "to_dict"):
        return payload.to_dict()  # deep copy, None fields omitted
    return copy.deepcopy(dict(payload))


class EventStore:
    def __init__(self, namespace: str, storage: str = "memory", directory: Optional[str] = None) -> None:
        if storage not in ("memory", "local"):
            raise ValueError(f"World Model: unknown storage {storage!r}")
        self.events: List[WorldEvent] = []
        self.audit: List[AuditEntry] = []
        self.observations: Dict[str, Dict[str, Any]] = {}
        self.stories: Dict[str, Dict[str, Any]] = {}
        self.scores: List[Dict[str, Any]] = []
        self.merge_queue: List[MergeProposal] = []
        self.state = FoldedState()
        self._seq = 0
        self._audit_seq = 0
        self._file: Optional[str] = None
        self._subscribers: List[Callable[[WorldEvent], None]] = []

        if storage == "local":
            d = os.path.join(directory if directory is not None else os.getcwd(), ".world")
            os.makedirs(d, exist_ok=True)
            self._file = os.path.join(d, f"{namespace}.json")
            if os.path.exists(self._file):
                with open(self._file, encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.audit = [AuditEntry.from_dict(a) for a in saved.get("audit") or []]
                self._audit_seq = self.audit[-1].seq if self.audit else 0
                self.observations = {k: v for k, v in saved.get("observations") or []}
                self.stories = {k: v for k, v in saved.get("stories") or []}
                self.scores = saved.get("scores") or []
                self.merge_queue = [MergeProposal.from_dict(p) for p in saved.get("mergeQueue") or []]
                for e in saved.get("events") or []:
                    self._apply_and_record(WorldEvent.from_dict(e), False)

    @property
    def file(self) -> Optional[str]:
        return self._file

    def append(self, kind: str, payload: Any, at: str) -> WorldEvent:
        event = WorldEvent(
            id=f"evt_{self._seq + 1}", seq=self._seq + 1, kind=kind,
            payload=_payload_dict(payload), schemaVersion=1, at=at,
        )
        self._apply_and_record(event, True)
        return event

    def _apply_and_record(self, event: WorldEvent, is_new: bool) -> None:
        self._seq = max(self._seq, event.seq)
        self.events.append(event)
        self._fold(event)
        if is_new:
            self.persist()
            for fn in list(self._subscribers):
                fn(event)

    def _fold(self, event: WorldEvent) -> None:
        kind, p = event.kind, event.payload
        st = self.state
        if kind == "upsertEntity":
            existing = st.entities.get(p["id"])
            if existing is not None:
                existing.attrs = {**existing.attrs, **copy.deepcopy(p.get("attrs") or {})}
                existing.updatedAt = p.get("updatedAt")
                existing.source = copy.deepcopy(p.get("source"))
                name = p.get("name")
                if name and name not in existing.aliases:
                    existing.aliases.append(name)
                if name:
                    existing.name = name
            else:
                st.entities[p["id"]] = Entity.from_dict(p)
        elif kind == "upsertContext":
            existing = st.contexts.get(p["id"])
            if existing is not None:
                existing.attrs = {**existing.attrs, **copy.deepcopy(p.get("attrs") or {})}
                existing.updatedAt = p.get("updatedAt")
            else:
                st.contexts[p["id"]] = Context.from_dict(p)
        elif kind == "relate":
            r = Relationship.from_dict(p)
            # Superseding: a new open edge with the same (from, type, context) but a
            # different target CLOSES the previous open edge.
            for other in st.relationships.values():
                if (
                    other.from_ == r.from_ and other.type == r.type
                    and (other.context if other.context is not None else "")
                    == (r.context if r.context is not None else "")
                    and other.to != r.to and not other.validTo and not other.supersededBy
                ):
                    other.validTo = r.validFrom if r.validFrom is not None else r.createdAt
                    other.supersededBy = r.id
            if r.id not in st.relationships:
                st.relationships[r.id] = r
        elif kind == "assertFact":
            if p["id"] not in st.facts:
                st.facts[p["id"]] = Fact.from_dict(p)
        elif kind == "alias":
            st.aliases[p["aliasId"]] = p["primaryId"]
        # snapshot / tombstone / view-rebuild: no fold effect (as in the reference)

    def fold_until(self, at: str) -> FoldedState:
        """Replay the log up to and including ``at`` (string comparison): the snapshot view."""
        scratch = EventStore("scratch", "memory")
        for e in self.events:
            if e.at <= at:
                scratch._fold(copy.deepcopy(e))
        return scratch.state

    def resolve_alias(self, id: str) -> str:
        cur = id
        seen = set()
        while cur in self.state.aliases and cur not in seen:
            seen.add(cur)
            cur = self.state.aliases[cur]
        return cur

    def log_audit(
        self, kind: str, actor: str, summary: str,
        event_id: Optional[str] = None, run_id: Optional[str] = None, at: Optional[str] = None,
    ) -> None:
        self._audit_seq += 1
        self.audit.append(AuditEntry(
            seq=self._audit_seq, at=at if at is not None else EPOCH_ISO, kind=kind,
            actor=actor, summary=summary, eventId=event_id, runId=run_id,
        ))
        self.persist()

    def subscribe(self, fn: Callable[[WorldEvent], None]) -> Callable[[], None]:
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return unsubscribe

    def persist(self) -> None:
        if not self._file:
            return
        data = {
            "events": self.events,
            "audit": self.audit,
            "observations": [[k, v] for k, v in self.observations.items()],
            "stories": [[k, v] for k, v in self.stories.items()],
            "scores": self.scores,
            "mergeQueue": self.merge_queue,
        }
        text = json.dumps(strip_none(data), indent=2, ensure_ascii=False) + "\n"
        with open(self._file, "w", encoding="utf-8") as fh:
            fh.write(text)
