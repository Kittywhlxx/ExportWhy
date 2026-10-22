"""World Model facade (port of reference/src/index.ts).

The TS write primitives are async only because of a per-instance FIFO write
queue; here they are plain synchronous methods serialized by a re-entrant lock
(single-writer discipline).
"""

from __future__ import annotations

import copy
import sys
import threading
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional

from .ingest import run_ingest_pipeline, world_state_shape
from .models import Context, Entity, Fact, IngestReport, MergeProposal, Relationship, Snapshot, WorldEvent
from .resolve import decide, name_similarity
from .stable_ids import (
    canonical_json,
    context_id_from_attrs,
    entity_id_from_name,
    external_id,
    fact_id,
    js_stringify,
    relationship_id,
    sha256_hex,
    to_iso,
    to_ms,
    utf16_key,
)
from .store import EventStore

CONFLICT_POLICIES = ("most-recent-wins", "highest-confidence-wins", "source-priority", "manual")


def _to_fixed3(x: float) -> str:
    """Number.prototype.toFixed(3): exact binary value, ties away from zero."""
    return str(Decimal(x).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _id_key(x) -> list:
    # Deviation: TS sorts with localeCompare (ICU); this uses UTF-16 code-unit order.
    return utf16_key(x.id)


class WorldModel:
    def __init__(
        self,
        namespace: str,
        *,
        storage: str = "memory",
        directory: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
        auto_merge_at: float = 0.85,
        review_lower: float = 0.82,
        conflict_policy: str = "most-recent-wins",
        source_priority: Optional[List[str]] = None,
    ) -> None:
        if not namespace:
            raise ValueError("WorldModel: namespace is required")
        if conflict_policy not in CONFLICT_POLICIES:
            raise ValueError(f"WorldModel: unknown conflict policy {conflict_policy!r}")
        self.namespace = namespace
        self.auto_merge_at = auto_merge_at
        self.review_lower = review_lower
        self.conflict_policy = conflict_policy
        self.source_priority = list(source_priority or [])
        self.clock: Callable[[], datetime] = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self.store = EventStore(namespace, storage, directory)
        self._lock = threading.RLock()
        self._custom_views: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ writes

    @staticmethod
    def _normalize_provenance(source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not source or not source.get("kind") or source.get("at") is None:
            raise ValueError(
                "World Model: every write carries provenance (kind + at). No provenance, no write."
            )
        at = source["at"]
        if isinstance(at, datetime):
            at = to_iso(at)
        out = {k: v for k, v in source.items() if v is not None}
        out["at"] = at
        return out

    def upsert_entity(
        self,
        type: str,
        *,
        external_key: Optional[Dict[str, str]] = None,
        name: Optional[str] = None,
        attrs: Optional[Dict[str, Any]] = None,
        source: Optional[Dict[str, Any]] = None,
        state: str = "active",
    ) -> Entity:
        with self._lock:
            src = self._normalize_provenance(source)
            if name is None and attrs is not None:
                name = attrs.get("name")
            if external_key:
                id = external_id(type, external_key["system"], external_key["id"])
            else:
                id = entity_id_from_name(type, _must_name(name))

            # Resolution pass for name-keyed entities: near-duplicates of the same
            # type either auto-merge (alias event) or queue for operator review.
            if not external_key and name:
                for other in list(self.store.state.entities.values()):
                    if other.type != type or other.id == id or not other.name:
                        continue
                    similarity = name_similarity(name, other.name)
                    decision = decide(similarity, self.auto_merge_at, self.review_lower)
                    if decision == "auto-merge":
                        self.store.append("alias", {
                            "aliasId": id, "primaryId": other.id, "mergedAt": src["at"],
                            "mergedBy": "resolver", "reason": f"similarity {_to_fixed3(similarity)}",
                        }, src["at"])
                        self.store.log_audit(
                            "auto-merge", "resolver",
                            f"{name} -> {other.name} ({_to_fixed3(similarity)})", None, None, src["at"],
                        )
                        return self._fold_entity_upsert(other.id, type, attrs, state, src, name)
                    if decision == "review":
                        proposal_id = f"merge_{len(self.store.merge_queue) + 1}"
                        if not any(
                            (p.leftId == id and p.rightId == other.id) or (p.leftId == other.id and p.rightId == id)
                            for p in self.store.merge_queue
                        ):
                            self.store.merge_queue.append(MergeProposal(
                                id=proposal_id, leftId=id, rightId=other.id, leftName=name,
                                rightName=other.name, similarity=similarity,
                                reason="name similarity in review band [0.82, 0.85)",
                                status="pending", proposedAt=src["at"],
                            ))
                            self.store.log_audit(
                                "merge-proposed", "resolver",
                                f"{name} ~ {other.name} ({_to_fixed3(similarity)}) queued", None, None, src["at"],
                            )
            return self._fold_entity_upsert(id, type, attrs, state, src, name)

    def _fold_entity_upsert(
        self, id: str, type: str, attrs: Optional[Dict[str, Any]], state: Optional[str],
        source: Dict[str, Any], name: Optional[str],
    ) -> Entity:
        resolved = self.store.resolve_alias(id)
        entity = Entity(
            type=type, id=resolved, attrs=dict(attrs or {}), schemaVersion=1, source=source,
            createdAt=source["at"], updatedAt=source["at"], name=name,
            aliases=[name] if name else [], state=state or "active",
        )
        event = self.store.append("upsertEntity", entity, source["at"])
        self.store.log_audit(
            "upsertEntity", source.get("by") or "system", f"{resolved} ({type})",
            event.id, source.get("runId"), source["at"],
        )
        return self.store.state.entities[resolved]

    def upsert_context(
        self,
        type: str,
        *,
        external_key: Optional[Dict[str, str]] = None,
        attrs: Optional[Dict[str, Any]] = None,
        parent: Optional[str] = None,
        source: Optional[Dict[str, Any]] = None,
    ) -> Context:
        with self._lock:
            src = self._normalize_provenance(source)
            if external_key:
                id = external_id(type, external_key["system"], external_key["id"])
            else:
                id = context_id_from_attrs(type, attrs or {}, parent)
            context = Context(
                type=type, id=id, attrs=dict(attrs or {}), parent=parent, schemaVersion=1,
                source=src, createdAt=src["at"], updatedAt=src["at"],
            )
            event = self.store.append("upsertContext", context, src["at"])
            self.store.log_audit(
                "upsertContext", src.get("by") or "system", f"{id} ({type})", event.id, src.get("runId"), src["at"],
            )
            return self.store.state.contexts[id]

    def relate(
        self,
        from_: str,
        type: str,
        to: str,
        *,
        context: Optional[str] = None,
        attrs: Optional[Dict[str, Any]] = None,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        source: Optional[Dict[str, Any]] = None,
    ) -> Relationship:
        with self._lock:
            src = self._normalize_provenance(source)
            if isinstance(valid_from, datetime):
                valid_from = to_iso(valid_from)
            if isinstance(valid_to, datetime):
                valid_to = to_iso(valid_to)
            f = self.store.resolve_alias(from_)
            t = self.store.resolve_alias(to)
            # The id hashes the CALLER's validFrom (None -> ""), not the defaulted one.
            id = relationship_id(f, type, t, context, valid_from)
            if id in self.store.state.relationships:
                return self.store.state.relationships[id]  # content-addressed no-op
            rel = Relationship(
                id=id, from_=f, type=type, to=t, context=context,
                attrs=copy.deepcopy(attrs) if attrs is not None else None,
                validFrom=valid_from if valid_from is not None else src["at"],
                validTo=valid_to, schemaVersion=1, source=src, createdAt=src["at"],
            )
            event = self.store.append("relate", rel, src["at"])
            self.store.log_audit(
                "relate", src.get("by") or "system", f"{f} -[{type}]-> {t}", event.id, src.get("runId"), src["at"],
            )
            return self.store.state.relationships[id]

    def assert_fact(
        self,
        about: str,
        predicate: str,
        value: Any,
        *,
        confidence: float = 0.5,
        context: Optional[str] = None,
        source: Optional[Dict[str, Any]] = None,
    ) -> Fact:
        with self._lock:
            src = self._normalize_provenance(source)
            about = self.store.resolve_alias(about)
            id = fact_id(about, predicate, value, src["at"], src["kind"], src.get("url"))
            facts = self.store.state.facts
            if id in facts:
                return facts[id]

            fact = Fact(
                id=id, about=about, predicate=predicate, value=copy.deepcopy(value),
                confidence=confidence if confidence is not None else 0.5,
                context=context, source=src, assertedAt=src["at"],
            )

            # Conflict policy decides which fact's value lands on the entity.
            policy = self.conflict_policy
            rivals = [f for f in facts.values() if f.about == about and f.predicate == predicate and not f.supersededBy]
            winner = fact
            for rival in rivals:
                if policy == "highest-confidence-wins":
                    rival_wins = rival.confidence > winner.confidence
                elif policy == "source-priority":
                    rival_wins = _priority_of(rival.source.get("kind"), self.source_priority) < _priority_of(
                        winner.source.get("kind"), self.source_priority
                    )
                else:  # most-recent-wins (also the fallback for "manual", as in the reference)
                    rival_wins = rival.assertedAt > winner.assertedAt
                if rival_wins:
                    winner = rival
            event = self.store.append("assertFact", fact, src["at"])
            for f in [fact, *rivals]:
                if f.id != winner.id:
                    facts[f.id].supersededBy = winner.id

            # Ensure the subject entity exists and carries the winning value.
            if about not in self.store.state.entities:
                inferred_type = about.split(":")[0] if ":" in about else "entity"
                self.store.append("upsertEntity", Entity(
                    type=inferred_type, id=about, attrs={}, schemaVersion=1, source=src,
                    createdAt=src["at"], updatedAt=src["at"], aliases=[], state="emerging",
                ), src["at"])
            entity = self.store.state.entities[about]
            entity.attrs[predicate] = copy.deepcopy(winner.value)
            entity.updatedAt = src["at"]

            self.store.log_audit(
                "assertFact", src.get("by") or "system",
                f"{about}.{predicate} = {js_stringify(winner.value)} ({policy})",
                event.id, src.get("runId"), src["at"],
            )
            self.store.persist()
            return facts[id]

    def snapshot(
        self,
        *,
        entities: List[str],
        source: Optional[Dict[str, Any]] = None,
        context: Optional[str] = None,
        attrs: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        """Append a ``snapshot`` event (types.ts Snapshot shape).

        NOT in the TS reference: index.ts has no snapshot write primitive (only the
        point-in-time "snapshot" view, ported as ``entity_at``), and store.ts folds
        snapshot events as a no-op. The id grammar ``snap_<16 hex>`` is this port's
        choice: sha256 of canonical {at, context, entities(sorted, alias-resolved), attrs}.
        Identical snapshots are a content-addressed no-op.
        """
        with self._lock:
            src = self._normalize_provenance(source)
            ents = sorted({self.store.resolve_alias(e) for e in entities}, key=utf16_key)
            id = "snap_" + sha256_hex(canonical_json(
                {"at": src["at"], "context": context, "entities": ents, "attrs": attrs}
            ))[:16]
            for e in self.store.events:
                if e.kind == "snapshot" and e.payload.get("id") == id:
                    return Snapshot.from_dict(e.payload)
            snap = Snapshot(
                id=id, at=src["at"], context=context, entities=ents,
                attrs=copy.deepcopy(attrs) if attrs is not None else None, schemaVersion=1, source=src,
            )
            event = self.store.append("snapshot", snap, src["at"])
            self.store.log_audit(
                "snapshot", src.get("by") or "system", f"{id} ({len(ents)} entities)",
                event.id, src.get("runId"), src["at"],
            )
            return Snapshot.from_dict(event.payload)

    # ------------------------------------------------------------ ingest

    def ingest(self, observations: List[Dict[str, Any]]) -> IngestReport:
        with self._lock:
            return run_ingest_pipeline(
                self.store, observations, self._ingest_upsert, lambda: to_ms(self.clock()),
            )

    def _ingest_upsert(self, obs: Dict[str, Any]):
        source = self._normalize_provenance({**(obs.get("source") or {}), "at": obs.get("at")})
        otype = obs.get("type") or "item"
        entity = self._fold_entity_upsert(
            external_id(otype, "ingest", obs["externalId"]), otype,
            {"title": obs.get("title"), "text": obs.get("text")}, None, source, obs.get("title"),
        )
        entity_ids: List[str] = []
        for mention in obs.get("entities") or []:
            mid = entity_id_from_name(mention["type"], mention["name"])
            self._fold_entity_upsert(mid, mention["type"], None, None, source, mention["name"])
            entity_ids.append(self.store.resolve_alias(mid))
            to = self.store.resolve_alias(mid)
            rid = relationship_id(entity.id, "mentions", to)
            if rid not in self.store.state.relationships:
                self.store.append("relate", Relationship(
                    id=rid, from_=entity.id, type="mentions", to=to, validFrom=source["at"],
                    schemaVersion=1, source=source, createdAt=source["at"],
                ), source["at"])
        return entity.id, sorted(entity_ids, key=utf16_key)

    # ------------------------------------------------------------ operator

    def approve_merge(self, proposal_id: str, by: str) -> None:
        with self._lock:
            p = next((m for m in self.store.merge_queue if m.id == proposal_id and m.status == "pending"), None)
            if p is None:
                raise ValueError(f"World Model: no pending merge proposal {proposal_id}")
            at = to_iso(self.clock())
            p.status, p.resolvedAt, p.resolvedBy = "approved", at, by
            self.store.append("alias", {
                "aliasId": p.leftId, "primaryId": p.rightId, "mergedAt": at, "mergedBy": by, "reason": p.reason,
            }, at)
            self.store.log_audit("merge-approved", by, f"{p.leftName} -> {p.rightName}", None, None, at)

    def reject_merge(self, proposal_id: str, by: str) -> None:
        with self._lock:
            p = next((m for m in self.store.merge_queue if m.id == proposal_id and m.status == "pending"), None)
            if p is None:
                raise ValueError(f"World Model: no pending merge proposal {proposal_id}")
            p.status, p.resolvedAt, p.resolvedBy = "rejected", to_iso(self.clock()), by
            # As in the reference: no `at` is passed, so this audit entry is stamped 1970-01-01.
            self.store.log_audit("merge-rejected", by, f"{p.leftName} stays separate from {p.rightName}")
            self.store.persist()

    # ------------------------------------------------------------ reads

    def entity(self, id: str) -> Optional[Entity]:
        return self.store.state.entities.get(self.store.resolve_alias(id))

    def entities(self, type: Optional[str] = None) -> List[Entity]:
        return sorted(
            (e for e in self.store.state.entities.values() if type is None or e.type == type), key=_id_key
        )

    def contexts(self, type: Optional[str] = None) -> List[Context]:
        return sorted(
            (c for c in self.store.state.contexts.values() if type is None or c.type == type), key=_id_key
        )

    def relationships(
        self,
        *,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        type: Optional[str] = None,
        context: Optional[str] = None,
        open_only: bool = False,
    ) -> List[Relationship]:
        """Edges in append order. from_/to are alias-resolved; open_only drops closed (validTo) edges."""
        f = self.store.resolve_alias(from_) if from_ is not None else None
        t = self.store.resolve_alias(to) if to is not None else None
        return [
            r for r in self.store.state.relationships.values()
            if (f is None or r.from_ == f) and (t is None or r.to == t)
            and (type is None or r.type == type) and (context is None or r.context == context)
            and (not open_only or not r.validTo)
        ]

    def facts(self, about: str, predicate: Optional[str] = None, current_only: bool = True) -> List[Fact]:
        """Facts about an entity (alias-resolved), sorted by assertedAt; current_only drops superseded."""
        rid = self.store.resolve_alias(about)
        out = [
            f for f in self.store.state.facts.values()
            if f.about == rid and (predicate is None or f.predicate == predicate)
            and (not current_only or not f.supersededBy)
        ]
        return sorted(out, key=lambda f: utf16_key(f.assertedAt))

    def snapshots(self) -> List[Snapshot]:
        return [Snapshot.from_dict(e.payload) for e in self.store.events if e.kind == "snapshot"]

    def entity_at(self, id: str, at: str) -> Optional[Entity]:
        """Point-in-time entity (reference SnapshotView.entityAt). A bare date means end of that day."""
        return SnapshotView(self.store).entity_at(id, at)

    def neighbors_of(self, id: str, *, via: Optional[str] = None, in_context: Optional[str] = None,
                     at: Optional[str] = None) -> List[Entity]:
        return GraphView(self.store).neighbors_of(id, via=via, in_context=in_context, at=at)

    def events(self) -> List[WorldEvent]:
        return list(self.store.events)

    def audit(self) -> list:
        return list(self.store.audit)

    def merge_queue(self) -> List[MergeProposal]:
        return list(self.store.merge_queue)

    def state_shape(self) -> Dict[str, Any]:
        return world_state_shape(self.store)

    def state_shape_canonical(self) -> str:
        return canonical_json(world_state_shape(self.store))

    def subscribe_events(self, fn: Callable[[WorldEvent], None]) -> Callable[[], None]:
        return self.store.subscribe(fn)

    def view(self, name: str):
        if name == "graph":
            return GraphView(self.store)
        if name == "facts":
            return FactsView(self.store)
        if name == "snapshot":
            return SnapshotView(self.store)
        if name == "timeline":
            return TimelineView(self.store)
        custom = self._custom_views.get(name)
        if custom is None:
            raise ValueError(f'World Model: unknown view "{name}"')
        return _CustomView(self.store, custom)

    def register_view(self, name: str, *, events: List[str], initial_state: Any,
                      reducer: Callable[[Any, WorldEvent], Any]) -> None:
        self._custom_views[name] = {"events": list(events), "initial_state": initial_state, "reducer": reducer}


# ------------------------------------------------------------------ views


class GraphView:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def entity(self, id: str) -> Optional[Entity]:
        return self.store.state.entities.get(self.store.resolve_alias(id))

    def neighbors_of(self, id: str, *, via: Optional[str] = None, in_context: Optional[str] = None,
                     at: Optional[str] = None) -> List[Entity]:
        rid = self.store.resolve_alias(id)
        out = []
        for rel in self.store.state.relationships.values():
            if via and rel.type != via:
                continue
            if in_context and rel.context != in_context:
                continue
            if at and not _valid_at(rel, at):
                continue
            if not at and rel.validTo:
                continue  # closed edges excluded from "now" queries
            other = rel.to if rel.from_ == rid else rel.from_ if rel.to == rid else None
            if not other:
                continue
            e = self.store.state.entities.get(other)
            if e is not None:
                out.append(e)
        return sorted(out, key=_id_key)

    def incoming(self, id: str, *, via: Optional[str] = None) -> List[Entity]:
        rid = self.store.resolve_alias(id)
        out = []
        for rel in self.store.state.relationships.values():
            if rel.to != rid or (via and rel.type != via) or rel.validTo:
                continue
            e = self.store.state.entities.get(rel.from_)
            if e is not None:
                out.append(e)
        return sorted(out, key=_id_key)

    def entities_in_context(self, context_id: str, *, type: Optional[str] = None) -> List[Entity]:
        seen: Dict[str, Entity] = {}
        for rel in self.store.state.relationships.values():
            if rel.context != context_id:
                continue
            for i in (rel.from_, rel.to):
                e = self.store.state.entities.get(i)
                if e is not None and (not type or e.type == type):
                    seen[e.id] = e
        return sorted(seen.values(), key=_id_key)

    def traverse(self, start_id: str, *, via: str, direction: str, max_depth: int = 5) -> List[Entity]:
        out: List[Entity] = []
        visited = set()
        frontier = [self.store.resolve_alias(start_id)]
        depth = 0
        while depth < max_depth and frontier:
            nxt = []
            for id in frontier:
                for rel in self.store.state.relationships.values():
                    if rel.type != via or rel.validTo:
                        continue
                    if direction == "ancestors":
                        target = rel.to if rel.from_ == id else None
                    else:
                        target = rel.from_ if rel.to == id else None
                    if target and target not in visited:
                        visited.add(target)
                        e = self.store.state.entities.get(target)
                        if e is not None:
                            out.append(e)
                        nxt.append(target)
            frontier = nxt
            depth += 1
        return out

    def query(self, *, type: Optional[str] = None, where: Optional[Callable[[Entity], bool]] = None) -> List[Entity]:
        return sorted(
            (e for e in self.store.state.entities.values()
             if (not type or e.type == type) and (where is None or where(e))),
            key=_id_key,
        )


class FactsView:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def about(self, id: str) -> List[Fact]:
        rid = self.store.resolve_alias(id)
        return sorted((f for f in self.store.state.facts.values() if f.about == rid),
                      key=lambda f: utf16_key(f.assertedAt))


class SnapshotView:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def entity_at(self, id: str, at: str) -> Optional[Entity]:
        iso = f"{at}T23:59:59.999Z" if len(at) == 10 else at
        state = self.store.fold_until(iso)
        cur, seen = id, set()
        while cur in state.aliases and cur not in seen:
            seen.add(cur)
            cur = state.aliases[cur]
        return state.entities.get(cur)


class TimelineView:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def subscribe(self, fn: Callable[[WorldEvent], None]) -> Callable[[], None]:
        return self.store.subscribe(fn)

    def get(self) -> List[WorldEvent]:
        return list(self.store.events)


class _CustomView:
    def __init__(self, store: EventStore, spec: Dict[str, Any]) -> None:
        self.store, self.spec = store, spec

    def get(self) -> Any:
        state = copy.deepcopy(self.spec["initial_state"])
        for e in self.store.events:
            if e.kind in self.spec["events"]:
                state = self.spec["reducer"](state, e)
        return state


def _valid_at(rel: Relationship, at: str) -> bool:
    if rel.validFrom and at < rel.validFrom:
        return False
    if rel.validTo and at >= rel.validTo:
        return False
    return True


def _priority_of(kind: Optional[str], ranking: List[str]) -> int:
    try:
        return ranking.index(kind)  # type: ignore[arg-type]
    except ValueError:
        return sys.maxsize


def _must_name(name: Optional[str]) -> str:
    if not name:
        raise ValueError("World Model: name-keyed entities need a name (or provide an externalKey)")
    return name
