"""H-MEM conformance suite (pytest port of engines/hmem/conformance/runner.ts).

Vectors: agent/hmem/conformance_vectors.json is a byte-identical copy of
vectors/vectors.json from
https://github.com/Meterless/Meterless/tree/main/engines/hmem/conformance
Each runner.ts section is reproduced as a parametrized case with the same
assertions and tolerance (approx: |a - b| < 1e-9). Extra tests at the end
cover persistence round-trip, query() touch + "read" ledger entries, chatId
scoping, and sleep execute -> restore.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.hmem import HMEM  # noqa: E402

VECTORS_PATH = ROOT / "agent" / "hmem" / "conformance_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
T0: int = VECTORS["clock"]
DAY = 24 * 60 * 60 * 1000


def approx(a, b, eps: float = 1e-9) -> bool:
    # runner.ts: Math.abs(a - b) < eps (undefined/None compares false)
    if a is None or b is None:
        return False
    return abs(a - b) < eps


def index_of(seq, item) -> int:
    # Array.prototype.indexOf semantics (-1 when absent)
    try:
        return seq.index(item)
    except ValueError:
        return -1


def cases(section: str):
    return pytest.mark.parametrize("c", VECTORS["sections"][section], ids=lambda c: c["id"])


# ---------------------------------------------------------------- record-validation
@cases("record-validation")
def test_record_validation(c):
    hmem = HMEM(clock=lambda: T0)
    rejected = False
    try:
        hmem.add(c["input"]["content"], source=c["input"]["source"])
    except Exception:
        rejected = True
    got = "reject" if rejected else "accept"
    assert rejected == (c["expect"] == "reject"), f"expected {c['expect']}, got {got}"


# ---------------------------------------------------------------- feedback-deltas
@cases("feedback-deltas")
def test_feedback_deltas(c):
    hmem = HMEM(clock=lambda: T0)
    m = hmem.add("feedback target record", confidence=c["start"], source="conformance")
    access_before = m.accessCount
    hmem.feedback(m.id, c["kind"])
    after = hmem.store.get(m.id)
    assert approx(after.confidence, c["expectConfidence"]), \
        f"confidence {after.confidence}, expected {c['expectConfidence']}"
    if c.get("expectAccessDelta") is not None:
        assert after.accessCount - access_before == c["expectAccessDelta"], \
            f"accessCount delta {after.accessCount - access_before}, expected {c['expectAccessDelta']}"
    if c.get("expectTag"):
        assert c["expectTag"] in after.tags, f"missing tag {c['expectTag']}"


# ---------------------------------------------------------------- ranking-order
@cases("ranking-order")
def test_ranking_order(c):
    hmem = HMEM(clock=lambda: T0)
    by_key = {}
    for r in c["records"]:
        by_key[r["key"]] = hmem.add(r["content"], confidence=r["confidence"], layer=r["layer"], source="conformance")
    for r in c["records"]:
        if r.get("supersededByKey"):
            by_key[r["key"]].supersededBy = by_key[r["supersededByKey"]].id
    result = hmem.query(c["query"], threshold=0, topN=10)
    order = [m.memory.id for m in result.memories]
    expected = [by_key[k].id for k in c["expectOrder"]]
    for i in range(len(expected) - 1):
        assert index_of(order, expected[i]) < index_of(order, expected[i + 1]), \
            f"expected {c['expectOrder'][i]} before {c['expectOrder'][i + 1]}, got [{', '.join(order)}]"


# ---------------------------------------------------------------- sleep-guardrails
@cases("sleep-guardrails")
def test_sleep_guardrails(c):
    now = [T0]
    hmem = HMEM(clock=lambda: now[0])
    by_key = {}
    for r in c["records"]:
        by_key[r["key"]] = hmem.add(r["content"], source=r["source"], derivedFrom=r.get("derivedFrom"))
    now[0] = T0 + c["ageDays"] * DAY
    before = sorted(m.id for m in hmem.store.all())
    preview = hmem.sleep.preview()
    assert sorted(m.id for m in hmem.store.all()) == before, "preview mutated the store"
    for k in c["expectArchived"]:
        assert by_key[k].id in preview.toArchive, f"{k} should be archivable"
    for k in c["expectProtected"]:
        assert by_key[k].id not in preview.toArchive, f"{k} is guardrail-protected and must not archive"
    report = hmem.sleep.execute(preview)
    for k in c["expectProtected"]:
        assert hmem.store.get(by_key[k].id) is not None, f"{k} was deleted despite guardrails"
    assert isinstance(report.backupId, str) and len(report.backupId) > 0, "execute must return a backupId"


# ---------------------------------------------------------------- conflict-gate
@cases("conflict-gate")
def test_conflict_gate(c):
    hmem = HMEM(clock=lambda: T0)
    a = hmem.add(c["a"]["content"], confidence=c["a"]["confidence"], source="conformance")
    b = hmem.add(c["b"]["content"], confidence=c["b"]["confidence"], source="conformance")
    found = hmem.conflicts.scan()
    assert len(found) > 0, "opposing always/never pair on similar content must be detected"
    outcome = hmem.conflicts.auto_resolve(found[0])
    assert outcome in ("resolved", "queued"), f"outcome must be resolved or queued, got {outcome}"
    if outcome == "resolved":
        loser = next((m for m in (hmem.store.get(a.id), hmem.store.get(b.id)) if m and m.supersededBy), None)
        assert loser is not None, "resolved conflicts must mark the loser supersededBy"
    assert hmem.store.get(a.id) and hmem.store.get(b.id), "conflict resolution must never delete records"


# ---------------------------------------------------------------- ledger-audit
@cases("ledger-audit")
def test_ledger_audit(c):
    hmem = HMEM(clock=lambda: T0)
    m = hmem.add("audited record", confidence=c["start"], source="conformance")
    hmem.feedback(m.id, c["kind"])
    history = hmem.ledger.history(m.id)
    actions = [h.action for h in history]
    assert actions == c["expectActions"], f"actions {actions}, expected {c['expectActions']}"
    fb = history[-1]
    prev_conf = (fb.previousState or {}).get("confidence")
    new_conf = (fb.newState or {}).get("confidence")
    assert approx(prev_conf, c["expectPrev"]), f"previousState.confidence {prev_conf}, expected {c['expectPrev']}"
    assert approx(new_conf, c["expectNew"]), f"newState.confidence {new_conf}, expected {c['expectNew']}"


# ---------------------------------------------------------------- dream-boundary
@cases("dream-boundary")
def test_dream_boundary(c):
    hmem = HMEM(clock=lambda: T0)
    for r in c["records"]:
        hmem.add(r["content"], type=r["type"], source="conformance")
    count_before = len(hmem.store.all())
    proposals = hmem.dreaming.dream()
    assert len(proposals) > 0, "two related preferences must produce at least one proposal"
    assert len(hmem.store.all()) == count_before, "dreaming must not materialize anything before approval"
    created = hmem.dreaming.approve(proposals[0].id)
    if created:
        assert created.layer == "long_term", "approved proposals materialize as long_term"
        assert len(created.derivedFrom or []) > 0, "approved proposals carry derivedFrom lineage"


# ================================================================ extra tests

def test_persistence_round_trip(tmp_path):
    persist = tmp_path / "hmem"
    h1 = HMEM(clock=lambda: T0, persist_dir=str(persist), actor="tester")
    a = h1.add("we deploy the api with docker on mondays", confidence=0.9, source="chat", chatId="c1")
    b = h1.add("prefer python for scripting", type="preference", source="chat")
    h1.feedback(a.id, "wrong")

    state = json.loads((persist / "state.json").read_text(encoding="utf-8"))
    assert state["seq"] == 2
    assert [r["id"] for r in state["records"]] == [a.id, b.id]
    ledger_lines = (persist / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["action"] for l in ledger_lines] == ["create", "create", "feedback"]
    assert all(json.loads(l)["actor"] == "tester" for l in ledger_lines)
    # snapshots exclude the embedding; the persisted record keeps it
    assert "embedding" not in json.loads(ledger_lines[0])["newState"]
    assert len(state["records"][0]["embedding"]) == 64

    h2 = HMEM(clock=lambda: T0 + DAY, persist_dir=str(persist), actor="tester")
    assert [r.to_dict() for r in h2.store.all()] == [r.to_dict() for r in h1.store.all()]
    assert h2.store.get(a.id).provenance.origin == "manual"
    assert "review" in h2.store.get(a.id).tags and math.isclose(h2.store.get(a.id).confidence, 0.7, abs_tol=1e-9)
    hist = h2.ledger.history(a.id)
    assert [e.action for e in hist] == ["create", "feedback"]
    assert approx(hist[1].previousState["confidence"], 0.9) and approx(hist[1].newState["confidence"], 0.7)
    # sequence continues rather than reusing ids
    c = h2.add("third record after reload", source="chat")
    assert c.id == "mem-3"
    assert len((persist / "ledger.jsonl").read_text(encoding="utf-8").splitlines()) == 4


def test_query_touches_access_and_logs_read():
    now = [T0]
    hmem = HMEM(clock=lambda: now[0])
    m = hmem.add("the deploy window is monday morning", confidence=0.9, source="conformance")
    other = hmem.add("completely unrelated gardening note", confidence=0.9, source="conformance")
    now[0] = T0 + 1000
    result = hmem.query("deploy window monday morning", topN=1, threshold=0)
    assert [r.memory.id for r in result.memories] == [m.id]
    assert m.accessCount == 1 and m.lastAccessed == T0 + 1000
    assert other.accessCount == 0 and other.lastAccessed == T0
    hist = hmem.ledger.history(m.id)
    assert [e.action for e in hist] == ["create", "read"]
    assert hist[-1].details == {"query": "deploy window monday morning"}
    assert hist[-1].timestamp == T0 + 1000
    assert [e.action for e in hmem.ledger.history(other.id)] == ["create"]
    # final-score threshold: confidence multiplies after the sum, so a 0.0-confidence record never passes 0.35
    zero = hmem.add("the deploy window is monday morning", confidence=0.0, source="conformance")
    assert zero.id not in [r.memory.id for r in hmem.query("deploy window monday morning").memories]


def test_chat_id_scoping():
    hmem = HMEM(clock=lambda: T0)
    in_a = hmem.add("we deploy the api with docker", chatId="chat-a", source="conformance")
    in_b = hmem.add("we deploy the api with docker", chatId="chat-b", source="conformance")
    unscoped = hmem.add("we deploy the api with docker", source="conformance")
    scoped = {r.memory.id for r in hmem.query("deploy the api with docker", threshold=0, topN=10, chatId="chat-a").memories}
    assert scoped == {in_a.id, unscoped.id}
    unscoped_query = {r.memory.id for r in hmem.query("deploy the api with docker", threshold=0, topN=10).memories}
    assert unscoped_query == {in_a.id, in_b.id, unscoped.id}


def test_sleep_execute_then_restore():
    now = [T0]
    hmem = HMEM(clock=lambda: now[0])
    plain = hmem.add("random one-off note about parking near the office", source="chat")
    derived = hmem.add("synthesized insight about the quarterly roadmap", source="dreaming", derivedFrom=["mem-x"])
    now[0] = T0 + 40 * DAY
    report = hmem.sleep.execute()
    assert hmem.store.get(plain.id) is None
    assert hmem.store.get(derived.id) is not None
    assert [e.action for e in hmem.ledger.history(plain.id)] == ["create", "sleep_archive", "delete"]
    hmem.sleep.restore(report.backupId)
    assert hmem.store.get(plain.id) is not None
    assert hmem.ledger.all()[-1].action == "restore" and hmem.ledger.all()[-1].memoryId == "*"
    with pytest.raises(KeyError):
        hmem.sleep.restore("backup-does-not-exist")
