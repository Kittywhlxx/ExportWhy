"""End-to-end checks of the triage loop in SAMPLE_MODE, including the failure path."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["SAMPLE_MODE"] = "true"

from agent import main as agent_main          # noqa: E402
from agent import zetaris                    # noqa: E402
from agent.roles import Task                  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Keep H-MEM and the world model out of the repo's memory/ during tests."""
    from agent import memory, world
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setattr(world, "WORLD_DIR", tmp_path / "world")
    monkeypatch.setattr(agent_main.config, "TRACE_DIR", tmp_path / "traces")


def test_ground_truth_sample_mode():
    import csv
    truth = {r["alert_id"]: r["expected_verdict"] for r in csv.DictReader(open(ROOT / "data/sample/ground_truth.csv"))}
    for aid, expected in truth.items():
        _, out = agent_main.run(Task(alert_id=aid))
        assert out["verdict"] == expected, (aid, out["reasons"])
        assert out["rounds"] <= 3


def test_same_account_two_alerts_builds_one_entity_and_prior_context():
    # AL-001 and AL-011 share an account (generator guarantees it); the second run must see the first
    _, first = agent_main.run(Task(alert_id="AL-001"))
    report, second = agent_main.run(Task(alert_id="AL-011"))
    assert "World state" in report and "earlier alert AL-001" in report
    assert second["world"]["appended"] > 0
    # re-running the same alert appends nothing new to the world (content-addressed, provenance at = event time)
    _, again = agent_main.run(Task(alert_id="AL-011"))
    assert again["world"]["appended"] == 0


def test_unreachable_ticket_source_never_yields_compliant(monkeypatch):
    real = zetaris.SampleSQLite.query

    def flaky(self, sql, limit=200):
        if "dexa.tickets" in sql:
            raise zetaris.DataLayerError("simulated: ticket system unreachable")
        return real(self, sql, limit)

    monkeypatch.setattr(zetaris.SampleSQLite, "query", flaky)
    _, out = agent_main.run(Task(alert_id="AL-001"))      # normally compliant via its cited ticket
    assert out["verdict"] == "cannot-confirm"
    assert any("could not be queried" in g for g in out["gaps"])


def test_redaction_of_external_destination():
    report, out = agent_main.run(Task(alert_id="AL-007"))  # emailed export
    assert "<email redacted>" in report and "@example.com" not in report


@pytest.mark.parametrize("source", ["tickets", "policy", "logs"])
def test_simulated_outage_degrades_to_cannot_confirm(monkeypatch, source):
    monkeypatch.setenv("SIMULATE_OUTAGE", source)
    _, out = agent_main.run(Task(alert_id="AL-001"))      # normally compliant
    assert out["verdict"] == "cannot-confirm"
    assert out["gaps"], "an unreachable source must be reported as a gap"
    assert not any(r.startswith("evidence budget exhausted") for r in out["reasons"])


def test_second_opinion_and_critic_on_every_alert():
    import csv
    for aid in [r["alert_id"] for r in csv.DictReader(open(ROOT / "data/sample/ground_truth.csv"))]:
        _, out = agent_main.run(Task(alert_id=aid))
        assert out["second_opinion"]["agrees"], aid
        assert out["critic"]["total"] >= 7 and out["critic"]["passed"], (aid, out["critic"])
        assert out["verdict"] in out["summary"]


def test_disagreeing_second_opinion_escalates_to_human(monkeypatch):
    monkeypatch.setenv("SECOND_OPINION_FORCE", "compliant")
    _, out = agent_main.run(Task(alert_id="AL-004"))      # rules: investigate
    assert out["verdict"] == "needs-human"
    assert any("second opinion" in c for c in out["conflicts"])
    assert out["next_steps"][0].startswith("human review")


def test_critic_catches_fabricated_claim_and_reporter_revises(monkeypatch):
    from agent import roles

    def bad_draft(self, task, a):
        return f"Verdict {a.verdict}: ticket T-4242 approved 99999 rows for this export. Next: {a.next_steps[0] if a.next_steps else 'none'}."

    monkeypatch.setattr(roles.Reporter, "draft_summary", bad_draft)
    _, out = agent_main.run(Task(alert_id="AL-004"))
    c = out["critic"]
    assert c["attempt"] == 2 and c["first_total"] < 7 and c["passed"], c
    assert "T-4242" not in out["summary"] and "99999" not in out["summary"]
    assert "T-4242" in c["fix_applied"]


def test_confidence_priority_timeline_present():
    _, out = agent_main.run(Task(alert_id="AL-004"))
    assert 0.0 <= out["confidence"]["score"] <= 1.0 and out["confidence"]["label"] in ("high", "medium", "low")
    assert out["priority"]["bucket"] in ("P1", "P2", "P3") and out["priority"]["factors"]
    assert out["status"] == "complete"
    agents = {t["agent"] for t in out["timeline"]}
    assert {"planner", "retriever", "evaluator", "critic"} <= agents
    _, clean = agent_main.run(Task(alert_id="AL-001"))         # compliant: not queued
    assert clean["priority"] is None


def test_time_budget_exhausted_returns_partial_result(monkeypatch):
    monkeypatch.setenv("TRIAGE_TIMEOUT_S", "0")
    report, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["status"] == "timed-out" and out["verdict"] == "cannot-confirm"
    assert out["confidence"]["score"] <= 0.4 and "timed-out" in report


def test_verdict_feedback_is_recorded_in_ledger_and_calibration():
    from agent.memory import Memory
    agent_main.run(Task(alert_id="AL-004"))
    stats = Memory().verdict_feedback("AL-004", agree=False, note="scope amended later")
    assert stats["investigate"]["disagree"] == 1
    assert Memory().verdict_feedback("AL-999", agree=True) is None
