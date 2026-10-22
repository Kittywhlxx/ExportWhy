"""The canonical ingest pipeline (port of reference/src/ingest.ts).

Idempotency contract: re-running the same batch produces the same world state
and an empty diff. Every derived timestamp comes from the OBSERVATION, never
the wall clock (the clock only feeds the reported step timings).

Observations are dicts with the TS keys: ``externalId``, ``title``, ``at``,
``source`` (provenance dict), optional ``type``, ``text``, ``entities``
(list of ``{"type", "name"}``). Observation records, stories and score
snapshots are kept as plain dicts with the TS keys.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Tuple

from .models import IngestReport
from .resolve import jaccard
from .stable_ids import canonical_json, ms_to_iso, parse_iso_ms, sha256_hex, utf16_key
from .store import EventStore

CLUSTER_JACCARD = 0.5
HOUR_MS = 60 * 60 * 1000


def run_ingest_pipeline(
    store: EventStore,
    observations: List[Dict[str, Any]],
    upsert: Callable[[Dict[str, Any]], Tuple[str, List[str]]],
    timer: Callable[[], float],
) -> IngestReport:
    run_id = "run_" + sha256_hex(canonical_json([o["externalId"] for o in observations]))[:12]
    timings: List[Dict[str, Any]] = []
    before = canonical_json(world_state_shape(store))
    events_before = len(store.events)

    def time(step: int, name: str, fn: Callable[[], Any]) -> Any:
        t0 = timer()
        out = fn()
        timings.append({"step": step, "name": name, "ms": timer() - t0})
        return out

    # 1. Reconcile stale edges/aggregates: recompute staleness from observation times.
    def reconcile() -> None:
        latest = ""
        for o in observations:
            if o["at"] > latest:
                latest = o["at"]
        cutoff = _iso_days_before(latest, 30) if latest != "" else None
        for rec in store.observations.values():
            rec["stale"] = latest != "" and rec["at"] < cutoff

    time(1, "reconcile-stale", reconcile)

    # 2. Cluster/classify raw items: checksum-based skip of unchanged observations.
    fresh: List[Tuple[Dict[str, Any], str]] = []
    skipped: List[Dict[str, Any]] = []

    def classify() -> None:
        for obs in observations:
            text = obs.get("text")
            checksum = sha256_hex(canonical_json({
                "externalId": obs["externalId"], "title": obs.get("title"),
                "text": text if text is not None else "", "at": obs["at"],
            }))
            existing = store.observations.get(obs["externalId"])
            if existing is not None and existing.get("checksum") == checksum:
                skipped.append(obs)
            else:
                fresh.append((obs, checksum))

    time(2, "cluster-classify", classify)

    # 3. Persist primary aggregates: observation entities + mentioned entities.
    def persist_aggregates() -> None:
        for obs, checksum in fresh:
            entity_id, entity_ids = upsert(obs)
            store.observations[obs["externalId"]] = {
                "id": "obs_" + sha256_hex(obs["externalId"])[:12],
                "externalId": obs["externalId"],
                "entityId": entity_id,
                "checksum": checksum,
                "at": obs["at"],
                "entityIds": entity_ids,
                "stale": False,
            }

    time(3, "persist-aggregates", persist_aggregates)

    # Group fresh + existing observations into stories by entity overlap.
    clusters: List[List[Dict[str, Any]]] = []

    def story_grouping() -> None:
        # Deviation: TS sorts with localeCompare (ICU collation); this sorts by
        # UTF-16 code units. Identical for the lowercase/hex ids the pipeline produces.
        all_recs = sorted(store.observations.values(), key=lambda r: utf16_key(r["externalId"]))
        used = set()
        for seed in all_recs:
            if seed["externalId"] in used:
                continue
            group = [seed]
            used.add(seed["externalId"])
            for other in all_recs:
                if other["externalId"] in used:
                    continue
                if jaccard(seed.get("entityIds") or [], other.get("entityIds") or []) >= CLUSTER_JACCARD:
                    group.append(other)
                    used.add(other["externalId"])
            clusters.append(group)

    time(2, "story-grouping", story_grouping)

    # 4. Score/measure (bucketed velocity), 5. milestones, 8. patch source items.
    def score_measure() -> None:
        for group in clusters:
            member_ids = sorted((g["externalId"] for g in group), key=utf16_key)
            story_id = "story_" + sha256_hex(canonical_json(member_ids))[:12]
            times = sorted(parse_iso_ms(g["at"]) for g in group)
            last_at = times[-1]

            def velocity(window_ms: int) -> float:
                in_window = len([t for t in times if t > last_at - window_ms])
                return in_window / (window_ms / HOUR_MS)

            prev_hour = len([t for t in times if last_at - 2 * HOUR_MS < t <= last_at - HOUR_MS])
            curr_hour = len([t for t in times if t > last_at - HOUR_MS])
            diff = curr_hour - prev_hour
            score = {
                "storyId": story_id,
                "at": ms_to_iso(last_at),
                "velocity1h": velocity(HOUR_MS),
                "velocity24h": velocity(24 * HOUR_MS),
                "velocity7d": velocity(7 * 24 * HOUR_MS),
                "trendDirection": (diff > 0) - (diff < 0),
            }
            store.scores = [s for s in store.scores if s["storyId"] != story_id]
            store.scores.append(score)

            entity_ids = sorted({i for g in group for i in (g.get("entityIds") or [])}, key=utf16_key)
            store.stories[story_id] = {
                "id": story_id,
                "title": group[0]["externalId"],
                "memberIds": member_ids,
                "entityNames": list(entity_ids),
                "entityIds": entity_ids,
                "sources": [],
                "firstAt": ms_to_iso(times[0]),
                "lastAt": ms_to_iso(last_at),
                "milestones": [{"kind": "multi-source", "at": ms_to_iso(times[1])}] if len(group) >= 2 else [],
            }
            for rec in group:
                rec["storyId"] = story_id

    time(4, "score-measure", score_measure)

    # 9. Rebuild aggregates on the folded entities (not an event; see report notes).
    def rebuild_aggregates() -> None:
        for entity in store.state.entities.values():
            mentions = [o for o in store.observations.values() if entity.id in (o.get("entityIds") or [])]
            if not mentions:
                continue
            ts = sorted((m["at"] for m in mentions), key=utf16_key)
            entity.aggregates = {
                "mentionCount": len(mentions),
                "firstSeenAt": ts[0],
                "lastSeenAt": ts[-1],
                "linkedStoryIds": sorted({m["storyId"] for m in mentions if m.get("storyId")}, key=utf16_key),
            }

    time(9, "rebuild-aggregates", rebuild_aggregates)

    store.persist()
    after = canonical_json(world_state_shape(store))
    report = IngestReport(
        runId=run_id,
        processed=len(fresh),
        skippedUnchanged=len(skipped),
        clusters=len(clusters),
        eventsAppended=len(store.events) - events_before,
        changed=before != after,
        stepTimingsMs=timings,
    )
    store.log_audit(
        "ingest", "pipeline", f"run {run_id}: {len(fresh)} processed, {len(skipped)} skipped",
        None, run_id, observations[0]["at"] if observations else None,
    )
    return report


def world_state_shape(store: EventStore) -> Dict[str, Any]:
    """The idempotency diff surface, in canonical (sorted-collection) form.

    Deviation: TS sorts ids with localeCompare; this sorts by UTF-16 code units.
    """
    def by_id(xs):
        return sorted(xs, key=lambda x: utf16_key(x.id if hasattr(x, "id") else x["id"]))

    return {
        "entities": [e.to_dict() for e in by_id(store.state.entities.values())],
        "contexts": [c.to_dict() for c in by_id(store.state.contexts.values())],
        "relationships": [r.to_dict() for r in by_id(store.state.relationships.values())],
        "facts": [f.to_dict() for f in by_id(store.state.facts.values())],
        "observations": sorted(store.observations.values(), key=lambda o: utf16_key(o["externalId"])),
        "stories": by_id(store.stories.values()),
    }


def _iso_days_before(iso: str, days: int) -> str:
    ms = parse_iso_ms(iso)
    if isinstance(ms, float) and math.isnan(ms):
        raise ValueError("Invalid time value")
    return ms_to_iso(ms - days * 24 * 60 * 60 * 1000)
