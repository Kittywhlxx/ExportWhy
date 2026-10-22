"""World Model: Python port of the Meterless World Model reference implementation.

Source: https://github.com/Meterless/Meterless/tree/main/engines/world-model
(reference/src/*.ts). Conformance cases are inline in conformance/runner.ts and
are reproduced in tests/test_worldmodel_conformance.py.

Event-sourced and deterministic: state is a fold over an append-only event log,
ids are content-derived (docs/stable-ids.md), every write carries provenance,
and ingest is idempotent. Zero third-party dependencies, in-memory with
optional JSON persistence (``storage="local"``: <directory>/.world/<namespace>.json,
same layout as the reference). Write primitives are synchronous here (the TS
ones are async only because of a FIFO write queue).
"""

from __future__ import annotations

from .ingest import CLUSTER_JACCARD, HOUR_MS, run_ingest_pipeline, world_state_shape
from .models import (
    AuditEntry,
    Context,
    Entity,
    Fact,
    IngestReport,
    MergeProposal,
    Relationship,
    Snapshot,
    WorldEvent,
)
from .resolve import decide, jaccard, levenshtein, name_similarity
from .stable_ids import (
    canonical_json,
    context_id_from_attrs,
    entity_id_from_name,
    external_id,
    fact_id,
    normalize_name,
    relationship_id,
    sha256_hex,
)
from .store import EventStore, FoldedState
from .world import CONFLICT_POLICIES, FactsView, GraphView, SnapshotView, TimelineView, WorldModel

__all__ = [
    "WorldModel",
    "EventStore",
    "FoldedState",
    "GraphView",
    "FactsView",
    "SnapshotView",
    "TimelineView",
    "run_ingest_pipeline",
    "world_state_shape",
    "sha256_hex",
    "canonical_json",
    "normalize_name",
    "external_id",
    "entity_id_from_name",
    "context_id_from_attrs",
    "relationship_id",
    "fact_id",
    "levenshtein",
    "name_similarity",
    "decide",
    "jaccard",
    "Entity",
    "Context",
    "Relationship",
    "Fact",
    "Snapshot",
    "WorldEvent",
    "AuditEntry",
    "MergeProposal",
    "IngestReport",
    "CONFLICT_POLICIES",
    "CLUSTER_JACCARD",
    "HOUR_MS",
]
