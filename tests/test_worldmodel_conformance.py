"""World Model conformance suite (pytest port of engines/world-model/conformance/runner.ts).

The runner has no vectors file: its cases (sid-001 ... audit-001) and the
OBSERVATIONS batch are inline, and are reproduced here one test per
``record(...)`` call with the same assertions and messages. Extra tests at the
end cover cross-instance idempotency of the event log, local persistence
round-trip, and fact supersede semantics; one strict xfail pins a replay gap
inherited from the reference (fact side effects are not events).
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.worldmodel import (  # noqa: E402
    WorldModel,
    canonical_json,
    decide,
    entity_id_from_name,
    normalize_name,
    sha256_hex,
)

OBSERVATIONS = [
    {"externalId": "a1", "title": "Fed holds rates", "at": "2026-05-17T14:00:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"},
     "entities": [{"type": "person", "name": "Jerome Powell"}]},
    {"externalId": "a2", "title": "Powell signals patience", "at": "2026-05-17T16:30:00.000Z",
     "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"},
     "entities": [{"type": "person", "name": "Jerome Powell"}]},
]


def clock() -> datetime:
    return datetime(2026, 5, 18, tzinfo=timezone.utc)


def at(s: str) -> dict:
    return {"kind": "manual", "at": s}


# ---------------------------------------------------------------- runner.ts cases


def test_sid_001_diacritics_and_nbsp_normalize_identically():
    # runner.ts uses "José  García" (NBSP + space) on the left-hand side.
    assert normalize_name("José  García") == normalize_name("Jose Garcia"), \
        "accented + NBSP name must normalize to the plain form"
    assert entity_id_from_name("person", "José García") == entity_id_from_name("person", "jose garcia"), \
        "same normalized name must produce the same id"


def test_sid_002_distinct_names_stay_distinct():
    assert entity_id_from_name("org", "Acme Corp") != entity_id_from_name("org", "Acme Corporation"), \
        "different names must produce different ids"
    assert entity_id_from_name("person", "x") != entity_id_from_name("org", "x"), "type participates in the key"


def test_band_001_merge_bands_at_the_boundaries():
    assert decide(0.85) == "auto-merge", "0.85 exactly is auto-merge (>= reading)"
    assert decide(0.84) == "review", "0.84 queues for review"
    assert decide(0.82) == "review", "0.82 exactly queues"
    assert decide(0.81) == "new", "0.81 is a new entity"


def test_prov_001_writes_without_provenance_are_rejected():
    world = WorldModel("conf", storage="memory")
    rejected = False
    try:
        world.upsert_entity("person", name="No Provenance", source=None)
    except Exception:
        rejected = True
    assert rejected, "no provenance, no write"


def test_idem_001_same_batch_twice_produces_an_empty_diff():
    world = WorldModel("conf", storage="memory", clock=clock)
    world.ingest(OBSERVATIONS)
    shape1 = world.state_shape_canonical()
    second = world.ingest(OBSERVATIONS)
    assert second.changed is False, "second run must report changed=false"
    assert world.state_shape_canonical() == shape1, "canonical state must be identical after re-ingest"


def test_idem_002_a_mutated_batch_changes_the_shape():
    world = WorldModel("conf", storage="memory", clock=clock)
    world.ingest(OBSERVATIONS)
    shape1 = world.state_shape_canonical()
    mutated = [{**OBSERVATIONS[0], "title": OBSERVATIONS[0]["title"] + " (edited)"}, OBSERVATIONS[1]]
    run = world.ingest(mutated)
    assert run.changed is True, "an edited observation must report changed=true"
    assert world.state_shape_canonical() != shape1, "canonical state must differ after a real edit"


def test_bitemp_001_superseding_edge_closes_the_old_one():
    world = WorldModel("conf", storage="memory")
    world.upsert_entity("player", external_key={"system": "g", "id": "p1"}, attrs={}, source=at("2087-01-01T00:00:00.000Z"))
    world.upsert_entity("team", external_key={"system": "g", "id": "a"}, attrs={}, source=at("2087-01-01T00:00:00.000Z"))
    world.upsert_entity("team", external_key={"system": "g", "id": "b"}, attrs={}, source=at("2088-03-01T00:00:00.000Z"))
    world.relate("player:g:p1", "plays-for", "team:g:a", valid_from="2087-01-01T00:00:00.000Z",
                 source=at("2087-01-01T00:00:00.000Z"))
    world.relate("player:g:p1", "plays-for", "team:g:b", valid_from="2088-03-01T00:00:00.000Z",
                 source=at("2088-03-01T00:00:00.000Z"))
    now = world.view("graph").neighbors_of("player:g:p1", via="plays-for")
    assert len(now) == 1 and now[0].id == "team:g:b", \
        f"open edge should point at team b; got [{','.join(e.id for e in now)}]"
    then = world.view("graph").neighbors_of("player:g:p1", via="plays-for", at="2087-06-01T00:00:00.000Z")
    assert len(then) == 1 and then[0].id == "team:g:a", \
        f"2087 query should return team a; got [{','.join(e.id for e in then)}]"


def test_audit_001_every_mutation_writes_an_audit_entry_in_order():
    world = WorldModel("conf", storage="memory")
    world.upsert_entity("x", name="Audit One", source={"kind": "manual", "at": "2026-01-01T00:00:00.000Z"})
    world.upsert_entity("x", name="Audit Two", source={"kind": "manual", "at": "2026-01-02T00:00:00.000Z"})
    audit = world.audit()
    assert len(audit) >= 2, f"expected >= 2 audit entries, got {len(audit)}"
    for i in range(1, len(audit)):
        assert audit[i].seq > audit[i - 1].seq, "audit seq must be strictly increasing"


# ---------------------------------------------------------------- extra tests


def _log(world: WorldModel) -> str:
    return canonical_json([e.to_dict() for e in world.events()])


def test_idempotency_event_log_identical_across_instances_and_reruns():
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    w1 = WorldModel("idem", clock=clock)
    w2 = WorldModel("idem", clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    r1 = w1.ingest(copy.deepcopy(OBSERVATIONS))
    r2 = w2.ingest(copy.deepcopy(OBSERVATIONS))
    assert r1.processed == 2 and r1.eventsAppended > 0
    assert _log(w1) == _log(w2), "same inputs into fresh instances must yield identical event logs"
    assert canonical_json(w1.audit()) == canonical_json(w2.audit())
    assert w1.state_shape_canonical() == w2.state_shape_canonical()

    n_events = len(w1.events())
    log_before = _log(w1)
    again = w1.ingest(copy.deepcopy(OBSERVATIONS))
    assert again.processed == 0 and again.skippedUnchanged == 2
    assert again.eventsAppended == 0 and len(w1.events()) == n_events, "re-run must append no events"
    assert _log(w1) == log_before

    # Content-addressed no-ops on the primitives too.
    src = at("2026-05-17T18:00:00.000Z")
    powell = entity_id_from_name("person", "Jerome Powell")
    w1.relate("item:ingest:a1", "cites", powell, source=src)
    n = len(w1.events())
    w1.relate("item:ingest:a1", "cites", powell, source=src)
    w1.assert_fact(powell, "role", "Fed chair", source=src)
    w1.assert_fact(powell, "role", "Fed chair", source=src)
    assert len(w1.events()) == n + 1, "re-relating the same edge and re-asserting the same fact are no-ops"


def test_local_persistence_round_trip(tmp_path):
    w = WorldModel("persist", storage="local", directory=str(tmp_path), clock=clock)
    w.upsert_entity("player", external_key={"system": "g", "id": "p1"}, attrs={"overall": 87},
                    source=at("2087-01-01T00:00:00.000Z"))
    w.upsert_entity("player", external_key={"system": "g", "id": "p1"}, attrs={"overall": 94.5},
                    source=at("2088-01-20T12:00:00.000Z"))
    for team in ("a", "b"):
        w.upsert_entity("team", external_key={"system": "g", "id": team}, source=at("2087-01-01T00:00:00.000Z"))
    w.upsert_context("season", attrs={"year": 2087}, source=at("2087-01-01T00:00:00.000Z"))
    w.relate("player:g:p1", "plays-for", "team:g:a", valid_from="2087-01-01T00:00:00.000Z",
             source=at("2087-01-01T00:00:00.000Z"))
    w.relate("player:g:p1", "plays-for", "team:g:b", valid_from="2088-03-01T00:00:00.000Z",
             source=at("2088-03-01T00:00:00.000Z"))
    w.upsert_entity("person", name="Jerome Powell abc", source=at("2026-01-01T00:00:00.000Z"))
    w.upsert_entity("person", name="Jerome Powell x", source=at("2026-01-01T00:00:00.000Z"))
    assert any(p.status == "pending" for p in w.merge_queue())
    assert (tmp_path / ".world" / "persist.json").exists()

    r = WorldModel("persist", storage="local", directory=str(tmp_path), clock=clock)
    assert _log(r) == _log(w)
    assert canonical_json(r.audit()) == canonical_json(w.audit())
    assert canonical_json(r.merge_queue()) == canonical_json(w.merge_queue())
    assert r.state_shape_canonical() == w.state_shape_canonical()
    assert [e.id for e in r.neighbors_of("player:g:p1", via="plays-for")] == ["team:g:b"]
    assert r.entity_at("player:g:p1", "2087-08-15").attrs["overall"] == 87

    # Appending after reload continues the sequences.
    r.upsert_entity("team", external_key={"system": "g", "id": "c"}, source=at("2089-01-01T00:00:00.000Z"))
    assert r.events()[-1].seq == len(w.events()) + 1
    assert r.audit()[-1].seq == w.audit()[-1].seq + 1


def test_fact_supersede_most_recent_wins():
    world = WorldModel("facts")
    about = "person:fed:powell"
    first = world.assert_fact(about, "title", "Chair", confidence=0.9,
                              source={"kind": "rss", "url": "a", "at": "2026-05-17T09:00:00.000Z"})
    second = world.assert_fact(about, "title", "Chairman", confidence=0.7,
                               source={"kind": "rss", "url": "b", "at": "2026-05-17T09:30:00.000Z"})
    assert world.facts(about, current_only=False)[0].id == first.id
    assert first.supersededBy == second.id, "the older fact is superseded by the newer one"
    assert second.supersededBy is None
    assert [f.id for f in world.facts(about)] == [second.id]
    assert [f.id for f in world.facts(about, "title", current_only=False)] == [first.id, second.id]
    assert world.entity(about).attrs["title"] == "Chairman"
    assert world.entity(about).type == "person" and world.entity(about).state == "emerging"


def test_no_provenance_raises_value_error_on_every_primitive():
    world = WorldModel("prov")
    with pytest.raises(ValueError, match="No provenance, no write"):
        world.upsert_entity("x", name="A", source={"kind": "manual"})
    with pytest.raises(ValueError, match="No provenance, no write"):
        world.relate("a", "r", "b", source={"at": "2026-01-01T00:00:00.000Z"})
    with pytest.raises(ValueError, match="No provenance, no write"):
        world.assert_fact("a", "p", 1, source=None)
    with pytest.raises(ValueError, match="No provenance, no write"):
        world.upsert_context("c", attrs={}, source={})
    assert world.events() == [] and world.audit() == []


@pytest.mark.xfail(strict=True, reason=(
    "Inherited from the TS reference: assertFact marks supersededBy and writes the winning "
    "value onto entity.attrs by mutating folded state, not via events, so a replay of the "
    "log (storage='local' reload) loses both."
))
def test_fact_supersede_survives_local_reload(tmp_path):
    w = WorldModel("replay", storage="local", directory=str(tmp_path))
    w.assert_fact("person:fed:powell", "title", "Chair", source={"kind": "rss", "at": "2026-05-17T09:00:00.000Z"})
    w.assert_fact("person:fed:powell", "title", "Chairman", source={"kind": "rss", "at": "2026-05-17T09:30:00.000Z"})
    r = WorldModel("replay", storage="local", directory=str(tmp_path))
    assert r.state_shape_canonical() == w.state_shape_canonical()
