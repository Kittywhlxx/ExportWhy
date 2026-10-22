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
