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

    def flaky(self, sql, limit=200, **kw):
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


# ---- regressions from the pre-submission review

def test_validator_rejects_widened_authorisation_queries():
    from agent.roles import Planner, TEMPLATES
    t = Task(alert_id="AL-011", event_id="E00001", account_id="u1022", dataset_id="DS04", role="analyst", ticket_ref="T-9999")
    tpl = TEMPLATES["exceptions"][1].format(**{k: (v or "") for k, v in vars(t).items()})
    widened = "SELECT exception_id, scope_type, scope_value, dataset_id, clause, valid_from, valid_to, approved_by FROM dexa.policy_exceptions WHERE dataset_id = 'DS04'"
    assert Planner._validate(widened, t, template=tpl, evidence="exceptions")[0] is False
    assert Planner._validate("SELECT * FROM dexa.policy_exceptions WHERE dataset_id = 'DS04'", t, template=tpl, evidence="exceptions")[0] is False
    rewritten = tpl.replace("SELECT", "select", 1).replace(" FROM ", "\nFROM ")      # same filters and columns, cosmetic rewrite: accepted
    assert Planner._validate(rewritten, t, template=tpl, evidence="exceptions")[0] is True


def test_evaluator_ignores_exception_rows_for_other_accounts(monkeypatch):
    """Even if a widened query slipped through, another account's exception must not authorise this export."""
    real = zetaris.SampleSQLite.query

    def widened(self, sql, limit=200, **kw):
        if "dexa.policy_exceptions" in sql:
            return real(self, "SELECT exception_id, scope_type, scope_value, dataset_id, clause, valid_from, valid_to, approved_by FROM dexa.policy_exceptions WHERE dataset_id = 'DS04'", limit)
        return real(self, sql, limit)

    monkeypatch.setattr(zetaris.SampleSQLite, "query", widened)
    _, out = agent_main.run(Task(alert_id="AL-011"))          # u1022 on DS04; X03 belongs to u1014
    assert out["verdict"] == "cannot-confirm"
    assert any("do not apply to this alert" in c for c in out["conflicts"])


def test_summary_gate_falls_back_when_critic_keeps_failing(monkeypatch):
    from agent import roles

    def always_bad(self, task, a):
        return "Verdict investigate: approved by ticket T-4242 for 99999 rows."

    monkeypatch.setattr(roles.Reporter, "draft_summary", always_bad)
    monkeypatch.setattr(roles.Reporter, "revise", lambda self, task, a, summary, fix: summary)   # revision does not help
    _, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["critic"]["review_status"].startswith("failed")
    assert "T-4242" not in out["summary"] and out["verdict"] in out["summary"]


def test_run_py_respects_sample_mode_env_and_outage_keys(monkeypatch):
    import run as entry
    monkeypatch.setenv("SAMPLE_MODE", "true")
    monkeypatch.setenv("SIMULATE_OUTAGE", "")        # triage() writes this env var; monkeypatch restores it after the test
    body = entry.triage({"run_id": "t-1", "input": {"alert_id": "AL-001"}, "options": {"simulate_outage": "logs"}})
    assert body["status"] == "success" and body["output"]["data_layer"] == "sqlite"
    assert body["output"]["verdict"] == "cannot-confirm" and body["output"]["triage_status"] == "partial"
    for key in ("confidence", "priority", "queue", "timeline", "cannot_confirm"):
        assert key in body["output"]


def test_slow_backend_yields_partial_not_complete(monkeypatch):
    import time as _t
    real = zetaris.SampleSQLite.query

    def slow(self, sql, limit=200, **kw):
        _t.sleep(0.05); return real(self, sql, limit)

    monkeypatch.setattr(zetaris.SampleSQLite, "query", slow)
    monkeypatch.setenv("TRIAGE_TIMEOUT_S", "0.2")
    _, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["status"] in ("partial", "timed-out")



# ---- second review round

def test_authorisation_lookups_reject_semantic_widening():
    from agent.roles import Planner, TEMPLATES
    t = Task(alert_id="AL-004", event_id="E00233", account_id="u1012", dataset_id="DS02", role="finance", ticket_ref="T-2629")
    base = TEMPLATES["policy"][1].format(**{k: (v or "") for k, v in vars(t).items()})
    bad = [base.replace("p.role = a.role AND p.dataset_id = e.dataset_id", "1=1") + " ORDER BY p.max_rows_without_ticket DESC",
           base + " OR 1=1 ORDER BY p.max_rows_without_ticket DESC",
           base.replace("SELECT p.policy_id,", "SELECT p.policy_id, a.EMAIL,"),
           base.replace("p.notes FROM", "p.notes, a.* FROM"),
           base.replace("WHERE e.event_id = 'E00233'", "AND e.event_id = 'E00233'"),
           base.replace("JOIN dexa.policies p ON", "LEFT JOIN dexa.policies p ON")]
    for sql in bad:
        assert Planner._validate(sql, t, template=base, evidence="policy")[0] is False, sql
    ok = base.replace("SELECT", "select").replace(" FROM ", "\n  from ").replace(" JOIN ", "\n  join ") + ";"
    assert Planner._validate(ok, t, template=base, evidence="policy")[0] is True


def test_evaluator_ignores_policy_rows_for_other_roles(monkeypatch):
    """Even if a widened policy query ran, the Evaluator must only use the row for this role and dataset."""
    real = zetaris.SampleSQLite.query

    def widened(self, sql, limit=200, **kw):
        if "dexa.policies" in sql:
            return real(self, "SELECT policy_id, role, dataset_id, allowed, allowed_hours_start, allowed_hours_end, max_rows_without_ticket, requires_ticket, notes FROM dexa.policies ORDER BY max_rows_without_ticket DESC", limit)
        return real(self, sql, limit)

    monkeypatch.setattr(zetaris.SampleSQLite, "query", widened)
    _, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["verdict"] == "investigate"
    assert any("other roles/datasets" in c for c in out["conflicts"])


def test_deadline_stops_queries_and_model_calls(monkeypatch):
    """Once the budget is spent, no further query is issued and no model call is attempted."""
    import time as _t
    from agent import llm as llm_mod
    real = zetaris.SampleSQLite.query
    calls = {"queries": 0, "late_queries": 0}
    start = {"t": None}

    def slow(self, sql, limit=200, **kw):
        if start["t"] is None:
            start["t"] = _t.time()
        if _t.time() - start["t"] > 0.3:
            calls["late_queries"] += 1
        calls["queries"] += 1
        _t.sleep(0.12)
        return real(self, sql, limit)

    monkeypatch.setattr(zetaris.SampleSQLite, "query", slow)
    monkeypatch.setenv("TRIAGE_TIMEOUT_S", "0.3")
    _, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["status"] in ("partial", "timed-out")
    assert calls["late_queries"] <= 1, calls           # at most the query that was already in flight when the budget ran out
    assert out["model_calls"] == 0 or out["llm"] != "stub"


def test_chat_zero_budget_does_not_call_and_retry_timeout_is_recomputed(monkeypatch):
    import types
    from agent.llm import LLMError, Nightshift
    n = Nightshift.__new__(Nightshift)
    n.model = "meterless/fast"
    seen = {"timeouts": []}

    class FakeClient:
        def with_options(self, timeout):
            seen["timeouts"].append(round(timeout, 1)); return self
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("should not be called")
    n.client = FakeClient()
    try:
        n.chat("s", "u", max_seconds=0)
        assert False, "zero budget must not call the model"
    except LLMError as exc:
        assert "budget exhausted" in str(exc)
    assert seen["timeouts"] == []
    # a positive budget bounds the per-attempt timeout
    class Resp:
        model = "m"; usage = None
        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]
        model_extra = {}
    FakeClient.chat.completions.create = staticmethod(lambda **kw: Resp())
    text, _ = n.chat("s", "u", max_seconds=7)
    assert text == "ok" and seen["timeouts"] and seen["timeouts"][0] <= 7.0


def test_logs_outage_report_never_calls_the_reporter_model(monkeypatch):
    from agent import roles
    def boom(self, task, a):
        raise AssertionError("draft_summary must not run on the early-exit path")
    monkeypatch.setattr(roles.Reporter, "draft_summary", boom)
    monkeypatch.setenv("SIMULATE_OUTAGE", "logs")
    report, out = agent_main.run(Task(alert_id="AL-001"))
    assert out["status"] == "partial" and out["summary"] in report


def test_model_call_cap_is_enforced():
    from agent.llm import CountedLLM, LLMError, StubLLM
    c = CountedLLM(StubLLM(), max_calls=2)
    c.chat("s", "u"); c.chat("s", "u")
    try:
        c.chat("s", "u"); assert False
    except LLMError:
        pass
    assert c.calls == 2


def test_memory_two_sessions_do_not_lose_each_others_writes():
    from agent.memory import Memory
    a, b = Memory(), Memory()                          # two live sessions on the same store
    r1, _ = a.remember("fact one", source="t", refs=[], tags=["alert:X"], confidence=0.9, key="fact:X:1")
    r2, _ = b.remember("fact two", source="t", refs=[], tags=["alert:X"], confidence=0.9, key="fact:X:2")
    ids = {m["id"] for m in Memory().all()}
    assert r1.id in ids and r2.id in ids and r1.id != r2.id
    a.feedback(r2.id, "wrong")                         # analyst actor, via session a, on a record session b wrote
    rec = next(m for m in Memory().all() if m["id"] == r2.id)
    assert abs(float(rec["confidence"]) - 0.7) < 1e-9
    assert any(h["actor"] == "analyst" for h in Memory().history(r2.id))


# ---- third review round

def test_drafts_with_comments_or_exotic_whitespace_fall_back_to_template():
    from agent.roles import Planner, TEMPLATES
    t = Task(alert_id="AL-004", event_id="E00233", account_id="u1012", dataset_id="DS02", role="finance", ticket_ref="T-2629")
    ds = TEMPLATES["dataset"][1].format(**{k: (v or "") for k, v in vars(t).items()})
    pol = TEMPLATES["policy"][1].format(**{k: (v or "") for k, v in vars(t).items()})
    comment_bypass = ("SELECT -- from\n d.dataset_id, d.name, d.sensitivity, d.owner_team, d.contains_pii, a.EMAIL "
                      "FROM dexa.datasets d JOIN dexa.accounts a ON 1=1 WHERE d.dataset_id = 'DS02'")
    assert Planner._validate(comment_bypass, t, template=ds, evidence="dataset")[0] is False
    assert Planner._validate(pol.replace(" FROM ", "　FROM　"), t, template=pol, evidence="policy")[0] is False
    assert Planner._validate(pol.replace(" FROM ", " /* x */ FROM "), t, template=pol, evidence="policy")[0] is False
    assert Planner._validate(pol.replace("'E00233'", "'e00233'"), t, template=pol, evidence="policy")[0] is False
    assert Planner._validate(pol + " AND 1=1", t, template=pol, evidence="policy")[0] is False
    assert Planner._validate(pol.replace("SELECT", "select").replace(" FROM ", "\n from ") + ";", t, template=pol, evidence="policy")[0] is True
    # literals are compared verbatim
    assert Planner._canonical("SELECT x FROM t WHERE a = 'data  steward'") != Planner._canonical("SELECT x FROM t WHERE a = 'data steward'")
    assert Planner._canonical("SELECT x FROM t WHERE a = 'a ,b'") != Planner._canonical("SELECT x FROM t WHERE a = 'a,b'")


def test_data_layer_request_timeout_and_retries_respect_the_budget(monkeypatch):
    import requests as rq
    from agent.zetaris import DataLayerError, ZetarisREST
    z = ZetarisREST(base_url="http://127.0.0.1:9", org_id="1", api_key="x", timeout=120)
    seen = {"timeouts": [], "sleeps": []}

    class Resp:
        status_code = 200
        text = ""
        def json(self): return {"headers": ["one"], "data": [["1"]]}

    clock = {"t": 1000.0}                             # fake clock advanced by the request and the wait

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["timeouts"].append(timeout)
        if len(seen["timeouts"]) == 1:
            clock["t"] += 7.0                             # the first request burns 7 s before failing
            raise rq.ConnectionError("blip")
        return Resp()

    monkeypatch.setattr(rq, "post", fake_post)
    monkeypatch.setattr("agent.zetaris.time.time", lambda: clock["t"])
    def fake_sleep(sec):
        seen["sleeps"].append(sec); clock["t"] += sec
    monkeypatch.setattr("agent.zetaris.time.sleep", fake_sleep)
    r = z.query("SELECT 1 AS one", max_seconds=10)
    assert r.rows == [{"one": "1"}]
    assert seen["timeouts"][0] == 10 and seen["sleeps"] == [1.5]
    assert abs(seen["timeouts"][1] - 1.5) < 1e-6              # second attempt gets only what is left: 10 - 7 - 1.5
    seen["timeouts"].clear(); seen["sleeps"].clear()
    try:
        z.query("SELECT 1 AS one", max_seconds=0.5)
        assert False, "no time left: must not send"
    except DataLayerError as exc:
        assert "budget exhausted" in str(exc)
    assert seen["timeouts"] == []
    # a blip with only a little budget left: no retry, surfaced as a data-layer error
    def always_blip(url, headers=None, json=None, timeout=None):
        seen["timeouts"].append(timeout); raise rq.ConnectionError("blip")
    monkeypatch.setattr(rq, "post", always_blip)
    try:
        z.query("SELECT 1 AS one", max_seconds=2.0); assert False
    except DataLayerError:
        pass
    assert len(seen["timeouts"]) == 1 and seen["sleeps"] == []


def test_model_retry_is_attempted_and_its_timeout_recomputed(monkeypatch):
    import types
    from agent.llm import Nightshift
    n = Nightshift.__new__(Nightshift); n.model = "meterless/fast"
    seen = {"timeouts": [], "calls": 0}

    class Err(Exception):
        status_code = 503

    class Resp:
        model = "m"; usage = None; model_extra = {}
        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]

    class FakeClient:
        def with_options(self, timeout):
            seen["timeouts"].append(round(timeout, 1)); return self
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen["calls"] += 1
                    if seen["calls"] == 1:
                        raise Err("503")
                    return Resp()
    n.client = FakeClient()
    clock = {"t": 5000.0}
    monkeypatch.setattr("agent.llm.time.time", lambda: clock["t"])
    def fake_sleep(sec):
        clock["t"] += sec
    monkeypatch.setattr("agent.llm.time.sleep", fake_sleep)
    orig_create = FakeClient.chat.completions.create
    def create_with_time(**kw):
        if seen["calls"] == 0:
            clock["t"] += 7.0                              # first attempt burns 7 s, then 503
        return orig_create(**kw)
    FakeClient.chat.completions.create = staticmethod(create_with_time)
    text, _ = n.chat("s", "u", max_seconds=10)
    assert text == "ok" and seen["calls"] == 2
    assert seen["timeouts"][0] == 10.0 and abs(seen["timeouts"][1] - 1.0) < 1e-6   # 10 - 7 - 2 s wait


def test_memory_concurrent_writers_lose_nothing():
    import threading
    from agent.memory import Memory
    errors = []

    def writer(tag, n):
        try:
            m = Memory()
            for i in range(n):
                m.remember(f"{tag} fact {i}", source="t", refs=[], tags=["alert:C"], confidence=0.9, key=f"fact:C:{tag}:{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{k}", 15)) for k in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    recs = Memory().all()
    assert len(recs) == 60 and len({m["id"] for m in recs}) == 60



# ---- fourth review round

def test_reader_waits_for_a_writer_holding_the_lock(monkeypatch):
    """A reader must never see a half-written state.json: it blocks on the directory lock until the writer is done."""
    import threading, time as _t
    from agent import memory as memory_mod
    from agent.memory import Memory
    m = Memory()
    m.remember("seed fact", source="t", refs=[], tags=["alert:R"], confidence=0.9, key="fact:R:0")
    state = memory_mod.MEMORY_DIR / "state.json"
    release = threading.Event(); holding = threading.Event()

    def writer_holding_lock():
        with Memory()._lock():
            original = state.read_text()
            state.write_text("")                           # simulate the reference store mid-rewrite (truncated)
            holding.set()
            release.wait(5)
            state.write_text(original)

    th = threading.Thread(target=writer_holding_lock); th.start(); assert holding.wait(5)
    # confirm the reader actually reaches the lock request (so "blocked" is not just "not scheduled yet")
    import fcntl as _fcntl
    attempted = threading.Event(); real_flock = _fcntl.flock
    def flock(fd, op):
        if op == _fcntl.LOCK_EX:
            attempted.set()
        return real_flock(fd, op)
    monkeypatch.setattr(memory_mod.fcntl, "flock", flock)
    result = {}
    def reader():
        try:
            result["recs"] = Memory().all()
        except Exception as exc:                        # noqa: BLE001 - surfaced by the assertions below
            result["error"] = exc
    rt = threading.Thread(target=reader); rt.start()
    assert attempted.wait(5)                                # the reader asked for the lock ...
    _t.sleep(0.1)
    assert "recs" not in result and "error" not in result   # ... and is still blocked while the file is truncated
    release.set(); th.join(5); rt.join(5)
    assert "error" not in result, result.get("error")
    assert [r["content"] for r in result["recs"]] == ["seed fact"]


def test_small_budget_is_reported_as_budget_not_source_failure(monkeypatch):
    monkeypatch.setenv("TRIAGE_TIMEOUT_S", "0.5")
    report, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["status"] == "timed-out"
    assert not any("unreachable" in r for r in out["reasons"]) and "budget" in " ".join(out["reasons"])
    assert "unreachable" not in report


def test_budget_spent_mid_investigation_is_timed_out_with_retrieval_guidance(monkeypatch):
    """The first query eats most of the budget; later queries are skipped. That must surface as timed-out with the
    unretrieved evidence named, never as a complete verdict with 'no further action required'."""
    import time as _t
    real = zetaris.SampleSQLite.query
    calls = {"n": 0}
    def slow_first(self, sql, limit=200, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            _t.sleep(1.1)
        return real(self, sql, limit)
    monkeypatch.setattr(zetaris.SampleSQLite, "query", slow_first)
    monkeypatch.setenv("TRIAGE_TIMEOUT_S", "1.5")
    report, out = agent_main.run(Task(alert_id="AL-004"))
    assert out["status"] == "timed-out" and out["verdict"] == "cannot-confirm"
    assert any("budget" in r and "not retrieved" in r for r in out["reasons"]), out["reasons"]
    assert any("TRIAGE_TIMEOUT_S" in n for n in out["next_steps"])
    assert "No further action required" not in report and "not retrieved" in report
    assert any("skipped" in e["what"] for e in out["timeline"])
