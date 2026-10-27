"""The four roles. Planner -> Retriever -> Evaluator -> Reporter; the Evaluator sends the Planner back
for more evidence (bounded by MAX_ROUNDS). Rules decide the verdict; the model drafts SQL and prose.

Evidence classes are the unit of planning. Which classes a round needs depends on what the previous
round found (a cited ticket that fails a check triggers a search for other authorisation, etc.).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field

from . import catalog
from .llm import LLMError, extract_json
from .zetaris import BudgetExhausted, DataLayerError, QueryResult

MAX_ROUNDS = 3


# ------------------------------------------------------------------ task -----------------------


@dataclass
class Task:
    alert_id: str
    # resolved from the alert in round 0
    event_id: str | None = None
    ts: str | None = None
    account_id: str | None = None
    dataset_id: str | None = None
    rows: int | None = None
    ticket_ref: str | None = None
    rule: str | None = None
    role: str | None = None  # known after the account row is read
    prior: dict | None = None  # world-model dossier for the account (prior alerts, status history)

    def describe(self) -> str:
        return f"Triage alert {self.alert_id}"


@dataclass
class Step:
    sid: str
    evidence: str
    purpose: str
    sql: str
    origin: str = "rules"  # "rules" | "llm"
    result: QueryResult | None = None
    error: str | None = None
    skipped: bool = False  # not run: the time budget was already spent


# evidence class -> (purpose, SQL template). ANSI; identical on Zetaris Lightning SQL and SQLite.
# Templates select only the columns the decision needs: no email/phone/destination/sample ids.
TEMPLATES: dict[str, tuple[str, str]] = {
    "alert": (
        "the alert and the export event it points at",
        "SELECT a.alert_id, a.rule, a.severity, a.raised_at, e.event_id, e.ts, e.account_id, e.dataset_id, e.rows_exported, e.destination_type, e.tool, e.ticket_ref "
        "FROM dexa.alerts a JOIN dexa.export_events e ON e.event_id = a.event_id WHERE a.alert_id = '{alert_id}'",
    ),
    "account": (
        "who the exporting account is: role and employment status",
        "SELECT account_id, role, department, status, status_since FROM dexa.accounts WHERE account_id = '{account_id}'",
    ),
    "dataset": (
        "what was exported: dataset sensitivity",
        "SELECT dataset_id, name, sensitivity, owner_team, contains_pii FROM dexa.datasets WHERE dataset_id = '{dataset_id}'",
    ),
    "policy": (
        "what the role may export from that dataset without a ticket (logs ⋈ policy, joined by Zetaris)",
        "SELECT p.policy_id, p.role, p.dataset_id, p.allowed, p.allowed_hours_start, p.allowed_hours_end, p.max_rows_without_ticket, p.requires_ticket, p.notes "
        "FROM dexa.export_events e JOIN dexa.accounts a ON a.account_id = e.account_id "
        "JOIN dexa.policies p ON p.role = a.role AND p.dataset_id = e.dataset_id WHERE e.event_id = '{event_id}'",
    ),
    "sensitivity": (
        "which columns are sensitive (for redaction and the disclosure check)",
        "SELECT table_name, column_name, level FROM dexa.field_sensitivity",
    ),
    "ticket_cited": (
        "the ticket the exporter cited (logs ⋈ tickets, joined by Zetaris)",
        "SELECT t.ticket_id, t.requester_account_id, t.dataset_id, t.max_rows, t.valid_from, t.valid_to, t.status, t.approver_id "
        "FROM dexa.export_events e JOIN dexa.tickets t ON t.ticket_id = e.ticket_ref WHERE e.event_id = '{event_id}'",
    ),
    "tickets_account": (
        "any other ticket for this account and dataset",
        "SELECT ticket_id, requester_account_id, dataset_id, max_rows, valid_from, valid_to, status, approver_id FROM dexa.tickets "
        "WHERE requester_account_id = '{account_id}' AND dataset_id = '{dataset_id}' ORDER BY valid_to DESC",
    ),
    "exceptions": (
        "standing policy exceptions for the role or the account on that dataset",
        "SELECT exception_id, scope_type, scope_value, dataset_id, clause, valid_from, valid_to, approved_by FROM dexa.policy_exceptions "
        "WHERE dataset_id = '{dataset_id}' AND ((scope_type = 'role' AND scope_value = '{role}') OR (scope_type = 'account' AND scope_value = '{account_id}'))",
    ),
    "destination": (
        "where the data went (sensitive column, fetched only for external destinations)",
        "SELECT event_id, destination_type, destination FROM dexa.export_events WHERE event_id = '{event_id}'",
    ),
    "account_history": (
        "the account's export baseline on that dataset before this event",
        "SELECT COUNT(*) AS prior_exports, MAX(rows_exported) AS max_rows_before, SUM(rows_exported) AS total_rows_before FROM dexa.export_events "
        "WHERE account_id = '{account_id}' AND dataset_id = '{dataset_id}' AND ts < '{ts}'",
    ),
}

# Classes whose SQL is fully determined by the alert's keys: the model adds nothing, so they are never
# sent for drafting (keeps a triage at <= 3 model calls: two planning rounds and one summary).
NO_DRAFT = {"alert", "account", "sensitivity", "destination", "account_history"}


# ------------------------------------------------------------------ planner --------------------


def _budget(obj) -> float | None:
    fn = getattr(obj, "remaining", None)
    return fn() if fn else None


class Planner:
    name = "planner"

    def __init__(self, llm, trace, remaining=None):
        self.llm, self.trace = llm, trace
        self.counter = 0
        self.remaining = remaining  # callable -> seconds left in the triage budget

    def plan(self, task: Task, classes: list[str], memory_context: str, reason: str) -> list[Step]:
        params = {k: (v if v is not None else "") for k, v in vars(task).items()}
        steps = [self._template(c, params) for c in classes]
        draftable = [st for st in steps if st.evidence not in NO_DRAFT]
        drafted = (
            self._llm_refine(task, draftable, memory_context, reason)
            if self.llm.name != "stub" and draftable
            else 0
        )
        self.trace.step(
            self.name,
            "plan",
            reason=reason,
            evidence=classes,
            llm_drafted=drafted,
            steps=[
                {"id": s.sid, "evidence": s.evidence, "origin": s.origin, "sql": s.sql}
                for s in steps
            ],
        )
        return steps

    def _template(self, cls: str, params: dict) -> Step:
        self.counter += 1
        purpose, sql = TEMPLATES[cls]
        sql = sql.format(**params)
        if os.environ.get("PLANNER", "").lower() == "naive":
            # ablation: same tables, joins and filters, but SELECT * (no column scoping)
            sql = re.sub(r"^SELECT\s.*?\sFROM\s", "SELECT * FROM ", sql, count=1, flags=re.S | re.I)
        return Step(sid=f"q{self.counter}", evidence=cls, purpose=purpose, sql=sql)

    def _llm_refine(self, task: Task, steps: list[Step], memory_context: str, reason: str) -> int:
        """Let the model write the SQL per evidence class; keep a draft only if it is read-only, uses known
        tables, stays on this alert's keys, and does not widen into sensitive columns without need."""
        system = (
            "You write ANSI SQL for a security analyst triaging a data-export alert. Only SELECT statements, only the "
            'tables below, only the columns needed for the stated purpose. Reply with a JSON array of {"id": step id, "sql": "..."}.\n'
            + catalog.schema_card()
        )
        user = (
            f"Alert {task.alert_id} (event={task.event_id}, account={task.account_id}, role={task.role}, dataset={task.dataset_id}, ts={task.ts}, ticket_ref={task.ticket_ref}).\n"
            f"Why we are planning now: {reason}\nMemory from earlier investigations:\n{memory_context or '(none)'}\n\nSteps:\n"
            + "\n".join(
                f"- {s.sid} [{s.evidence}]: {s.purpose}. Reference SQL: {s.sql}" for s in steps
            )
        )
        try:
            text, meta = self.llm.chat(system, user, json_mode=True, max_seconds=_budget(self))
        except LLMError as exc:
            self.trace.step(
                self.name,
                "model_unavailable",
                error=str(exc)[:200],
                fallback="rule-based SQL templates",
            )
            return 0
        drafts = extract_json(text) or []
        by_id = {s.sid: s for s in steps}
        kept = 0
        for d in drafts if isinstance(drafts, list) else []:
            s = by_id.get(str(d.get("id")))
            sql = str(d.get("sql", "")).strip().rstrip(";")
            if not s or not sql:
                continue
            ok, why = self._validate(sql, task, template=s.sql, evidence=s.evidence)
            if ok:
                s.sql, s.origin = sql, "llm"
                kept += 1
            else:
                self.trace.step(self.name, "rejected_llm_sql", step=s.sid, why=why, sql=sql[:300])
        self.trace.step(self.name, "llm", **meta)
        return kept

    AUTH_CLASSES = {"policy", "ticket_cited", "tickets_account", "exceptions", "account", "alert"}
    _ALLOWED = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.,=()'<>!-+*/%:@ \n\t\r"
    )

    @staticmethod
    def _canonical(sql: str) -> str | None:
        """Canonical form of a query: whitespace collapsed and case folded OUTSIDE string literals; literals
        kept verbatim (including their spaces and case); trailing ';' dropped. Returns None when the text
        contains a comment, a character outside the SQL subset the data layer accepts, or non-ASCII whitespace,
        so such drafts are rejected instead of silently 'equalised'."""
        text = sql.strip()
        while text.endswith(";"):
            text = text[:-1].rstrip()
        if "--" in text or "/*" in text or "*/" in text:
            return None
        if any(ch not in Planner._ALLOWED for ch in text):
            return None
        out, lit = [], False
        tight = set(",()=")  # no spaces around these, outside literals only
        for ch in text:
            if ch == "'":
                lit = not lit
                out.append(ch)
            elif lit:
                out.append(ch)
            elif ch in " \n\t\r":
                if out and out[-1] != " " and out[-1] not in tight:
                    out.append(" ")
            else:
                if ch in tight and out and out[-1] == " ":
                    out.pop()
                out.append(ch.lower())
        if lit:
            return None  # unterminated literal
        return "".join(out).strip()

    @classmethod
    def _validate(
        cls, sql: str, task: Task, template: str | None = None, evidence: str | None = None
    ) -> tuple[bool, str]:
        """Model proposes, template disposes: a draft runs only if it is canonically identical to the vetted
        template for its evidence class. Anything else (a widened join, an extra column, a comment, an exotic
        whitespace character, a subquery) falls back to the template. This is what makes 'the model cannot
        change what is read' a property of the code rather than a hope."""
        if not re.match(r"^\s*select\b", sql, re.I) or re.search(
            r"\b(insert|update|delete|drop|create|alter|grant|attach|union)\b", sql, re.I
        ):
            return False, "not a plain SELECT"
        if "*" in re.split(r"\bfrom\b", sql, 1, flags=re.I)[0]:
            return False, "'*' in the select list is never accepted (column scoping)"
        if template is not None:
            c_draft, c_tpl = cls._canonical(sql), cls._canonical(template)
            if c_draft is None:
                return (
                    False,
                    "draft contains a comment or a character outside the accepted SQL subset",
                )
            if c_draft != c_tpl:
                return (
                    False,
                    "draft is not canonically identical to the vetted template for this evidence class",
                )
            return True, ""
        if not catalog.tables_in_sql(sql):
            return False, "no known dexa table referenced"
        for ref in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", sql, re.I):
            if ref[0].lower() == catalog.SCHEMA and ref[1].lower() not in catalog.TABLES:
                return False, f"unknown table {ref[1]}"
        keys = [
            k
            for k in (
                task.alert_id,
                task.event_id,
                task.account_id,
                task.dataset_id,
                task.ticket_ref,
            )
            if k
        ]
        if "field_sensitivity" not in sql and not any(k in sql for k in keys):
            return False, "does not filter on this alert's keys"
        return True, ""


# ------------------------------------------------------------------ retriever ------------------


class Retriever:
    name = "retriever"

    def __init__(self, backend, trace, remaining=None):
        self.backend, self.trace = backend, trace
        self.remaining = remaining
        self.footprint: dict[str, set[str]] = {}  # table -> result columns seen

    def run(self, steps: list[Step]) -> list[Step]:
        for s in steps:
            left = _budget(self)
            if left is not None and left < 1.0:
                s.skipped = True
                self.trace.step(
                    self.name,
                    "query_skipped",
                    step=s.sid,
                    evidence=s.evidence,
                    why="time budget exhausted",
                )
                continue
            try:
                s.result = self.backend.query(s.sql, max_seconds=left)
                for t in catalog.tables_in_sql(s.sql):
                    self.footprint.setdefault(t, set()).update(
                        c for c in s.result.columns if c in catalog.TABLES[t]["columns"]
                    )
                self.trace.step(
                    self.name,
                    "query",
                    step=s.sid,
                    evidence=s.evidence,
                    backend=s.result.source,
                    query_id=s.result.query_id,
                    rows=len(s.result.rows),
                    elapsed_ms=s.result.elapsed_ms,
                    tables=catalog.tables_in_sql(s.sql),
                    sql=s.sql,
                )
            except BudgetExhausted:
                s.skipped = True
                self.trace.step(
                    self.name,
                    "query_skipped",
                    step=s.sid,
                    evidence=s.evidence,
                    why="time budget exhausted",
                )
            except DataLayerError as exc:
                s.error = str(exc)
                self.trace.step(
                    self.name,
                    "query_failed",
                    step=s.sid,
                    evidence=s.evidence,
                    error=s.error[:300],
                    sql=s.sql,
                )
        return steps


# ------------------------------------------------------------------ evaluator ------------------


@dataclass
class Fact:
    key: str
    text: str
    cites: list[dict] = field(default_factory=list)  # {"step","table","column"}
    kind: str = "evidence"  # evidence | conflict | gap


@dataclass
class Assessment:
    facts: list[Fact]
    conflicts: list[str]
    gaps: list[str]  # evidence that does not exist in the sources (-> cannot-confirm)
    missing: list[str]  # evidence classes still to fetch (-> another round)
    verdict: str | None
    reasons: list[str]
    next_steps: list[str]
    disclosure: dict
    derived: dict

    @property
    def priority(self) -> dict | None:
        return (
            self.derived.get("priority") if self.verdict in ("investigate", "needs-human") else None
        )


def _ts(s) -> dt.datetime | None:
    if s in (None, ""):
        return None
    s = str(s).replace("T", " ").split(".")[0].rstrip("Z")
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d")


def _clean(v) -> str:
    """Display form of a timestamp from any backend (Zetaris appends .0)."""
    return str(v).replace("T", " ").split(".")[0] if v not in (None, "") else ""


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "t", "yes")


def _in_window(ts: dt.datetime, a, b) -> bool:
    ta, tb = _ts(a), _ts(b)
    return (ta is None or ta <= ts) and (tb is None or ts <= tb)


def ticket_checks(t: dict, task: Task, ts: dt.datetime) -> list[str]:
    """Every way a ticket can fail to cover the export. Empty list = it covers it."""
    fails = []
    if t.get("requester_account_id") != task.account_id:
        fails.append(f"requester {t.get('requester_account_id')} is not the exporting account")
    if t.get("dataset_id") != task.dataset_id:
        fails.append(f"ticket is for dataset {t.get('dataset_id')}, not {task.dataset_id}")
    if str(t.get("status")).lower() != "approved":
        fails.append(f"ticket status is {t.get('status')}")
    if not _in_window(ts, t.get("valid_from"), t.get("valid_to")):
        fails.append(
            f"export at {ts} is outside the ticket window {_clean(t.get('valid_from'))} .. {_clean(t.get('valid_to'))}"
        )
    try:
        if task.rows is not None and int(t.get("max_rows") or 0) < int(task.rows):
            fails.append(
                f"{task.rows} rows exported exceed the ticket's max_rows {t.get('max_rows')}"
            )
    except ValueError:
        pass
    return fails


VERDICTS = ("compliant", "investigate", "cannot-confirm")
TOKEN = re.compile(r"\b(AL-\d+|T-\d+|u\d{4}|DS\d{2}|X\d{2}|P\d{3}|E\d{5}|CVE-[\d-]+|\d{3,})\b")


class Evaluator:
    name = "evaluator"

    def __init__(self, trace, llm=None, remaining=None):
        self.trace = trace
        self.llm = llm
        self.remaining = remaining

    # ---- second opinion: the model judges the same evidence independently; rules remain the floor
    def second_opinion(self, task: Task, a: "Assessment") -> dict:
        """Returns {"verdict", "reasons", "missing_evidence", "agrees", "source"}. Never raises."""
        forced, _, forced_missing = (os.environ.get("SECOND_OPINION_FORCE") or "").partition(
            ":"
        )  # test hook: "verdict[:class,...]"
        if forced in VERDICTS:
            op = {
                "verdict": forced,
                "reasons": ["forced disagreement (test)"],
                "missing_evidence": [m for m in forced_missing.split(",") if m in TEMPLATES],
                "source": "forced",
            }
        elif self.llm is None or self.llm.name == "stub":
            op = {
                "verdict": a.verdict,
                "reasons": ["stub: deterministic second opinion agrees with the rules"],
                "missing_evidence": [],
                "source": "stub",
            }
        else:
            system = (
                "You are a second security analyst reviewing a colleague's triage of a data-export alert. Judge ONLY from the facts given. "
                'Reply with JSON: {"verdict": "compliant"|"investigate"|"cannot-confirm", "reasons": [..], '
                '"missing_evidence": [names from: '
                + ", ".join(TEMPLATES)
                + "]}. Rules of the organisation: an export above the no-ticket cap "
                "needs an approved ticket covering account, dataset, rows and time, or a standing exception; an unknown account, missing policy or "
                "missing cited ticket means the question cannot be confirmed; a terminated account always needs investigation."
            )
            user = (
                f"Alert {task.alert_id}. Facts:\n"
                + "\n".join(f"- {f.text}" for f in a.facts)
                + f"\nConflicts: {a.conflicts}\nCannot confirm: {a.gaps}\n"
                f"(The colleague's verdict is withheld. Give your own.)"
            )
            try:
                text, meta = self.llm.chat(
                    system, user, json_mode=True, max_tokens=2500, max_seconds=_budget(self)
                )
                self.trace.step(self.name, "llm", **meta)
                d = extract_json(text) or {}
                v = str(d.get("verdict", "")).strip().lower()
                op = {
                    "verdict": v if v in VERDICTS else "unparseable",
                    "reasons": [str(r)[:200] for r in (d.get("reasons") or [])][:4],
                    "missing_evidence": [
                        m for m in (d.get("missing_evidence") or []) if m in TEMPLATES
                    ],
                    "source": meta.get("routed_model") or meta.get("model_served"),
                }
            except Exception as exc:  # the model is advisory; its failure never blocks a verdict
                op = {
                    "verdict": "unavailable",
                    "reasons": [f"model unavailable: {str(exc)[:120]}"],
                    "missing_evidence": [],
                    "source": "error",
                }
        op["usable"] = op["verdict"] in VERDICTS
        op["agrees"] = (
            op["verdict"] == a.verdict if op["usable"] else None
        )  # None = no usable opinion
        self.trace.step(self.name, "second_opinion", rules_verdict=a.verdict, **op)
        return op

    def assess(
        self, task: Task, done: dict[str, list[Step]], footprint: dict, rounds: int
    ) -> Assessment:
        """Apply the rules to the evidence fetched so far. Each check reads one evidence class,
        records facts (with their citations), conflicts and gaps, and either settles the verdict
        or asks for the next class."""
        r = _Review(task, done)
        if r.read_event():
            r.check_account()
            r.check_dataset()
            r.check_policy()
            r.check_authorisation()
            r.check_prior_alerts()
            r.check_destination()
            r.check_baseline()
        return self._finish(r, footprint, rounds)

    def _finish(self, r: "_Review", footprint: dict, rounds: int) -> Assessment:
        disclosure = _disclosure(footprint, r.rows, r.facts)
        r.derived["sensitivity"] = disclosure.pop("sensitivity")
        r.derived["priority"] = _priority(r.rows)
        verdict = r.derived.get("verdict")

        if r.derived.get("blocked_by_failure") and verdict != "investigate":
            # a decision-critical source was unreachable: never upgrade to "compliant" on partial evidence
            verdict = "cannot-confirm"
            r.derived["verdict"] = verdict
            r.reasons.insert(0, "a decision-critical source was unreachable; retry when it is back")
            r.next_steps.insert(0, "re-run the triage once the failing source is reachable")
        if verdict is None and (not r.missing or rounds >= MAX_ROUNDS):
            verdict = "cannot-confirm"
            r.reasons.append("evidence budget exhausted before a decision could be grounded")

        missing = list(r.missing)
        unfetched: list[str] = []
        if verdict and missing and rounds >= MAX_ROUNDS:
            # budget spent: report with what we have, and say what was not retrieved
            unfetched, missing = missing, []
        only_baseline = all(m == "account_history" for m in missing)
        if verdict and missing and only_baseline and rounds >= MAX_ROUNDS - 1:
            # the baseline is nice-to-have; never spend the last round only on it
            unfetched, missing = missing, []
        r.derived["unfetched"] = unfetched

        a = Assessment(
            r.facts,
            r.conflicts,
            r.gaps,
            missing,
            verdict if not missing else None,
            r.reasons,
            r.next_steps,
            disclosure,
            r.derived,
        )
        self.trace.step(
            self.name,
            "assess",
            round=rounds,
            facts=len(r.facts),
            conflicts=r.conflicts,
            gaps=r.gaps,
            missing=missing,
            verdict=a.verdict,
            reasons=r.reasons,
            disclosure={k: v for k, v in disclosure.items() if k != "columns_seen"},
        )
        return a


EXTERNAL_DESTINATIONS = ("email", "cloud-bucket", "usb")


def _disclosure(footprint: dict, rows: dict, facts: list[Fact]) -> dict:
    """Columns touched versus their classification; sensitive columns no fact needed are flagged."""
    sens = {(r["table_name"], r["column_name"]): r["level"] for r in rows.get("sensitivity", [])}
    used_tables = {c["table"].split(".")[1] for f in facts for c in f.cites}
    if sens:
        used_tables.add("field_sensitivity")
    touched_sensitive = sorted(
        f"{t}.{c} ({sens[(t, c)]})" for t, cols in footprint.items() for c in cols if (t, c) in sens
    )
    return {
        "tables_accessed": sorted(footprint),
        "columns_seen": {t: sorted(c) for t, c in footprint.items()},
        "tables_used_in_answer": sorted(used_tables),
        "accessed_but_unused": sorted(set(footprint) - used_tables),
        "sensitive_columns_touched": touched_sensitive,
        "pii_columns_touched": [x for x in touched_sensitive if "(pii)" in x],
        "sensitivity": sens,
    }


def _priority(rows: dict) -> dict:
    """Queue priority for the analyst (0-10, P1/P2/P3): what was exported, how much, where to, by whom."""
    ev = (rows.get("alert") or [None])[0] or {}
    ds = (rows.get("dataset") or [None])[0] or {}
    acc = (rows.get("account") or [None])[0]
    score = 0.0
    why = []

    weight = {"restricted": 4, "confidential": 3, "internal": 1, "public": 0}
    sens_w = weight.get(str(ds.get("sensitivity")).lower(), 2)
    score += sens_w
    why.append(f"dataset {ds.get('sensitivity', 'unknown')} (+{sens_w})")
    try:
        n = int(ev.get("rows_exported") or 0)
        rows_w = 0 if n < 1000 else 1 if n < 10000 else 2 if n < 50000 else 3
        score += rows_w
        why.append(f"{n} rows (+{rows_w})")
    except ValueError:
        pass
    if str(ev.get("destination_type")) in EXTERNAL_DESTINATIONS:
        score += 2
        why.append("external destination (+2)")
    if acc and str(acc.get("status")).lower() in ("terminated", "offboarding"):
        score += 2
        why.append(f"account {acc.get('status')} (+2)")
    if not acc and "account" in rows:
        score += 1
        why.append("unknown account (+1)")
    score = min(10.0, score)
    return {
        "score": score,
        "bucket": "P1" if score >= 8 else "P2" if score >= 5 else "P3",
        "factors": why,
    }


class _Review:
    """Working state of one Evaluator.assess call: the rows per evidence class, and the findings
    the checks accumulate (facts with citations, conflicts, gaps, reasons, next steps)."""

    CRITICAL = {"account", "policy", "ticket_cited", "tickets_account", "exceptions"}

    def __init__(self, task: Task, done: dict[str, list[Step]]):
        self.task = task
        self.rows = {
            cls: [r for s in steps if s.result for r in s.result.rows]
            for cls, steps in done.items()
        }
        self.fetched = {
            cls for cls, steps in done.items() if any(s.result is not None for s in steps)
        }
        self.failed = {
            cls
            for cls, steps in done.items()
            if steps
            and all(s.error for s in steps if not s.skipped)
            and any(not s.skipped for s in steps)
        }
        self.cite = {cls: steps[-1].sid for cls, steps in done.items() if steps}
        self.step_tables = {
            cls: sorted({t for s in steps for t in catalog.tables_in_sql(s.sql)})
            for cls, steps in done.items()
        }
        self.facts: list[Fact] = []
        self.conflicts: list[str] = []
        self.gaps: list[str] = []
        self.missing: list[str] = []
        self.reasons: list[str] = []
        self.next_steps: list[str] = []
        self.derived: dict = {}
        self.event: dict = {}
        self.ts: dt.datetime | None = None

    # ---- bookkeeping ---------------------------------------------------------------------

    def first(self, cls: str) -> dict | None:
        return (self.rows.get(cls) or [None])[0]

    def fact(self, key, text, cls, table, column=None, kind="evidence"):
        """Record a finding with the query step and tables it came from."""
        step = self.cite.get(cls)
        cites = [{"step": step, "table": f"{catalog.SCHEMA}.{table}", "column": column}]
        for t in self.step_tables.get(cls, []):
            if t != table:
                cites.append({"step": step, "table": f"{catalog.SCHEMA}.{t}", "column": None})
        self.facts.append(Fact(key, text, cites, kind))

    def need(self, cls: str):
        """Ask for an evidence class; if its source failed, record the gap instead of asking again."""
        if cls in self.failed:
            unreachable = self.derived.setdefault("unreachable", [])
            if cls not in unreachable:
                unreachable.append(cls)
                self.gaps.append(f"source for '{cls}' could not be queried ({TEMPLATES[cls][0]})")
                if cls in self.CRITICAL:
                    self.derived["blocked_by_failure"] = True
            return
        if cls not in self.fetched:
            self.missing.append(cls)

    def settled(self) -> bool:
        return "verdict" in self.derived

    def settle(self, verdict: str, reason: str, next_step: str | None = None, keep: bool = False):
        """Set the verdict; with `keep`, an earlier verdict wins."""
        if keep:
            self.derived.setdefault("verdict", verdict)
        else:
            self.derived["verdict"] = verdict
        self.reasons.append(reason)
        if next_step:
            self.next_steps.append(next_step)

    # ---- checks, in the order the loop needs them ------------------------------------------

    def read_event(self) -> bool:
        """The alert row and its export event. Everything else is keyed on it."""
        task = self.task
        ev = self.first("alert")
        if not ev:
            if "alert" in self.failed:
                self.gaps.append(
                    "the alert/event source (logs) could not be queried; "
                    "nothing about this export can be established"
                )
                self.settle(
                    "cannot-confirm",
                    "the logs source is unreachable",
                    "re-run the triage once the logs source is reachable",
                )
            else:
                self.settle("cannot-confirm", "alert or its export event not found")
            return False

        self.event = ev
        self.ts = _ts(ev["ts"])
        task.event_id = ev["event_id"]
        task.ts = _clean(ev["ts"])
        task.account_id = ev["account_id"]
        task.dataset_id = ev["dataset_id"]
        task.rows = int(ev["rows_exported"])
        task.ticket_ref = (ev.get("ticket_ref") or "").strip() or None
        task.rule = ev["rule"]
        self.fact(
            "event",
            f"{task.alert_id} ({ev['severity']}, rule: {ev['rule']}): account {task.account_id} "
            f"exported {task.rows} rows of {task.dataset_id} at {task.ts} via {ev['tool']} to a "
            f"{ev['destination_type']} destination; ticket cited: {task.ticket_ref or 'none'}.",
            "alert",
            "export_events",
            "rows_exported",
        )
        self.need("sensitivity")  # the classification drives redaction and the disclosure check
        return True

    def check_account(self):
        task = self.task
        acc = next(
            (r for r in self.rows.get("account", []) if r.get("account_id") == task.account_id),
            None,
        )
        if "account" in self.fetched and not acc:
            self.gaps.append(f"account {task.account_id} is not in the directory")
            self.fact(
                "account",
                f"Account {task.account_id} does not exist in the identity directory.",
                "account",
                "accounts",
                "account_id",
                kind="gap",
            )
            self.settle(
                "cannot-confirm",
                "the exporting account cannot be identified",
                "check the identity provider for a recently deleted or service account with this id",
            )
        elif acc:
            task.role = acc["role"]
            since = _ts(acc.get("status_since"))
            self.fact(
                "account",
                f"Account {task.account_id}: role {acc['role']}, department {acc['department']}, "
                f"status {acc['status']} since {_clean(acc.get('status_since'))}.",
                "account",
                "accounts",
                "status",
            )
            gone = str(acc["status"]).lower() in ("terminated", "offboarding")
            if gone and since is not None and since <= self.ts:
                self.settle(
                    "investigate",
                    f"account was {acc['status']} ({_clean(acc.get('status_since'))}) before the export",
                    "confirm with HR/IAM why the account still had export access; disable it",
                )
        else:
            self.need("account")

    def check_dataset(self):
        ds = self.first("dataset")
        if ds:
            self.fact(
                "dataset",
                f"Dataset {ds['dataset_id']} ({ds['name']}) is classified {ds['sensitivity']}, "
                f"owner {ds['owner_team']}, contains_pii={_truthy(ds.get('contains_pii'))}.",
                "dataset",
                "datasets",
                "sensitivity",
            )
        else:
            self.need("dataset")

    def check_policy(self):
        task = self.task
        if task.role:
            # no-op once fetched; records the gap and blocks "compliant" if the source failed
            self.need("policy")
            if task.ticket_ref:
                # the cited ticket is needed whenever one is cited: fetch it with the policy, not a round later
                self.need("ticket_cited")

        pol = self._policy_row()
        if pol is None:
            return
        cap = int(pol.get("max_rows_without_ticket") or 0)
        allowed = _truthy(pol.get("allowed"))
        hours_ok = (
            str(pol.get("allowed_hours_start"))
            <= self.ts.strftime("%H:%M")
            <= str(pol.get("allowed_hours_end"))
        )
        self.derived["cap"] = cap
        self.derived["hours_ok"] = hours_ok
        self.fact(
            "policy",
            f"Policy {pol['policy_id']}: role {task.role} {'may' if allowed else 'may NOT'} export "
            f"{task.dataset_id}; no-ticket cap {cap} rows; advisory hours "
            f"{pol.get('allowed_hours_start')}-{pol.get('allowed_hours_end')} "
            f"(export was {'inside' if hours_ok else 'outside'} them).",
            "policy",
            "policies",
            "max_rows_without_ticket",
        )
        if not allowed:
            self.settle(
                "investigate",
                f"policy gives role {task.role} no export rights on {task.dataset_id}",
                keep=True,
            )
        elif task.rows <= cap:
            self.settle(
                "compliant",
                f"{task.rows} rows are within the no-ticket cap of {cap}; "
                "the hours window is advisory below the cap",
                keep=True,
            )
        else:
            self.derived["needs_auth"] = True

    def _policy_row(self) -> dict | None:
        """The policy row for this role and dataset, or None (with the gap or conflict recorded)."""
        task = self.task
        if not task.role or "policy" not in self.fetched:
            return None
        rows = self.rows.get("policy", [])
        if not rows:
            self.gaps.append(f"no policy row for role {task.role} on {task.dataset_id}")
            self.fact(
                "policy",
                f"No data-access policy exists for role {task.role} on {task.dataset_id}; "
                "entitlement cannot be determined from policy.",
                "policy",
                "policies",
                "policy_id",
                kind="gap",
            )
            self.settle(
                "cannot-confirm",
                "no policy covers this role and dataset",
                "ask the dataset owner to publish a policy row for this role; "
                "treat as unauthorised until then",
                keep=True,
            )
            return None

        def mine(r) -> bool:
            return r.get("role") == task.role and r.get("dataset_id") == task.dataset_id

        if mine(rows[0]):
            return rows[0]
        # the query returned rows for other roles or datasets first: say so, use ours if it is there
        labels = [
            f"{r.get('policy_id')}({r.get('role')}/{r.get('dataset_id')})"
            for r in rows
            if not mine(r)
        ]
        self.conflicts.append(
            f"policy query returned rows for other roles/datasets ({', '.join(labels[:5])}); ignored"
        )
        own = next((r for r in rows if mine(r)), None)
        if own is not None:
            return own
        self.gaps.append(
            f"no policy row for role {task.role} on {task.dataset_id} among the returned rows"
        )
        self.settle("cannot-confirm", "no applicable policy row", keep=True)
        return None

    def check_authorisation(self):
        """Only when the export exceeds the no-ticket cap: the cited ticket first, then any other
        ticket or standing exception, then the conclusion."""
        task = self.task
        if not self.derived.get("needs_auth") or self.settled():
            return

        covering, cited_fails = self._cited_ticket()
        if covering:
            self.settle(
                "compliant",
                f"approved ticket {covering} covers the account, dataset, {task.rows} rows "
                "and the time of export",
            )
            return
        cited_pending = (
            task.ticket_ref
            and "ticket_cited" not in self.fetched
            and "ticket_cited" not in self.failed
        )
        if cited_pending:
            return  # read the cited ticket first; only widen the search if it does not cover the export

        # the cited ticket is missing or fails: look for any other authorisation before deciding
        self.need("tickets_account")
        self.need("exceptions")
        if "tickets_account" in self.fetched:
            self._other_tickets()
        if not self.settled() and "exceptions" in self.fetched:
            self._exceptions()
        if not self.settled() and {"tickets_account", "exceptions"} <= self.fetched:
            self._conclude_unauthorised(cited_fails)
            self.need("account_history")

    def _cited_ticket(self) -> tuple[str | None, list[str] | None]:
        """Returns (covering ticket id, list of checks the cited ticket failed)."""
        task = self.task
        if not task.ticket_ref:
            return None, None
        if "ticket_cited" not in self.fetched:
            self.need("ticket_cited")
            return None, None
        t = next(
            (r for r in self.rows.get("ticket_cited", []) if r.get("ticket_id") == task.ticket_ref),
            None,
        )
        if not t:
            self.gaps.append(f"cited ticket {task.ticket_ref} does not exist in the ticket system")
            self.fact(
                "ticket_cited",
                f"The exporter cited ticket {task.ticket_ref}, which does not exist in the ticket system.",
                "ticket_cited",
                "tickets",
                "ticket_id",
                kind="gap",
            )
            self.next_steps.append(
                "ask the exporter for the approval they relied on; "
                "check the ticket system's audit log for a deleted ticket"
            )
            return None, None
        fails = ticket_checks(t, task, self.ts)
        self.fact(
            "ticket_cited",
            f"Ticket {t['ticket_id']}: requester {t['requester_account_id']}, dataset {t['dataset_id']}, "
            f"max_rows {t['max_rows']}, {t['status']}, valid {_clean(t['valid_from'])} .. "
            f"{_clean(t['valid_to'])}, approver {t['approver_id']}.",
            "ticket_cited",
            "tickets",
            "valid_to",
        )
        if not fails:
            return t["ticket_id"], fails
        self.conflicts.append(
            f"ticket {t['ticket_id']} was cited but does not cover the export: " + "; ".join(fails)
        )
        return None, fails

    def _other_tickets(self):
        task = self.task
        others = [
            t for t in self.rows.get("tickets_account", []) if t.get("ticket_id") != task.ticket_ref
        ]
        good = [t for t in others if not ticket_checks(t, task, self.ts)]
        if good:
            covering = good[0]["ticket_id"]
            self.fact(
                "tickets_account",
                f"Another approved ticket {covering} covers this export (not cited by the exporter).",
                "tickets_account",
                "tickets",
                "ticket_id",
            )
            self.settle(
                "compliant",
                f"approved ticket {covering} covers the export even though it was not cited",
                "remind the exporter to cite the ticket in the export tool",
            )
        else:
            self.fact(
                "tickets_account",
                f"{len(others)} other ticket(s) for {task.account_id} on {task.dataset_id}; "
                "none covers this export.",
                "tickets_account",
                "tickets",
                "status",
            )

    def _exceptions(self):
        task = self.task

        def applies(e) -> bool:
            # the row must be for this dataset and for this role or this account, whatever the query returned
            if e.get("dataset_id") != task.dataset_id:
                return False
            scope = (e.get("scope_type"), e.get("scope_value"))
            return scope in (("role", task.role), ("account", task.account_id))

        rows = self.rows.get("exceptions", [])
        foreign = [e.get("exception_id") for e in rows if not applies(e)]
        if foreign:
            self.conflicts.append(
                "exceptions query returned rows that do not apply to this alert "
                f"({', '.join(map(str, foreign))}); ignored"
            )
        valid = [
            e
            for e in rows
            if applies(e) and _in_window(self.ts, e.get("valid_from"), e.get("valid_to"))
        ]
        if valid:
            e = valid[0]
            self.fact(
                "exceptions",
                f"Policy exception {e['exception_id']} ({e['scope_type']} {e['scope_value']}, "
                f"approved by {e['approved_by']}, valid {_clean(e['valid_from'])} .. "
                f"{_clean(e['valid_to'])}) applies: {e['clause']}",
                "exceptions",
                "policy_exceptions",
                "clause",
            )
            self.settle(
                "compliant", f"standing policy exception {e['exception_id']} covers the export"
            )
        else:
            self.fact(
                "exceptions",
                f"No policy exception covers role {task.role} or account {task.account_id} "
                f"on {task.dataset_id} at {task.ts}.",
                "exceptions",
                "policy_exceptions",
                "exception_id",
            )

    def _conclude_unauthorised(self, cited_fails):
        task = self.task
        if task.ticket_ref and not self.rows.get("ticket_cited"):
            self.settle(
                "cannot-confirm",
                "the only authorisation the exporter cites cannot be found, "
                "and nothing else covers the export",
            )
        elif cited_fails:
            self.settle(
                "investigate",
                "the cited ticket does not cover this export and no other authorisation does",
                "contact the ticket approver and the exporter's manager; preserve the exported file",
            )
        else:
            self.settle(
                "investigate",
                f"{task.rows} rows exceed the no-ticket cap and no ticket or exception "
                "authorises the export",
                "contact the exporter's manager; confirm whether the destination was internal; "
                "preserve the exported file",
            )

    def check_prior_alerts(self):
        """What the world model already knows about this account (entity consistency across alerts)."""
        prior = (self.task.prior or {}).get("prior_alerts") or []
        if prior and self.derived.get("verdict") in ("investigate", "cannot-confirm"):
            flagged = [p for p in prior if p["verdict"] in ("investigate", "cannot-confirm")]
            if flagged:
                self.reasons.append(
                    f"account has {len(flagged)} earlier alert(s) that were not cleared: "
                    + ", ".join(f"{p['alert']} ({p['verdict']})" for p in flagged)
                )
                self.next_steps.insert(
                    0, "escalate: repeat pattern on the same account across investigations"
                )
        history = (self.task.prior or {}).get("status_history") or []
        if len({h["value"] for h in history}) > 1:
            self.conflicts.append(
                "directory status for this account differs between investigations: "
                + " -> ".join(f"{h['value']} ({h['at']})" for h in history)
            )

    def check_destination(self):
        """The destination is a confidential column: read it only when the export left the
        internal share and the verdict calls for it."""
        external = str(self.event.get("destination_type")) in EXTERNAL_DESTINATIONS
        if not external or self.derived.get("verdict") not in ("investigate", "cannot-confirm"):
            return
        self.need("destination")
        dest = self.first("destination")
        if dest:
            self.fact(
                "destination",
                f"The export went to an external {dest['destination_type']} destination: "
                f"{dest['destination']}.",
                "destination",
                "export_events",
                "destination",
            )
            self.next_steps.append(
                "confirm whether the destination is a sanctioned location; request deletion if not"
            )

    def check_baseline(self):
        """Context for investigate verdicts: how this export compares with the account's history."""
        hist = self.first("account_history")
        if not hist or self.derived.get("verdict") != "investigate":
            return
        largest = int(hist.get("max_rows_before") or 0)
        comparison = "far above" if int(self.task.rows) > 3 * largest else "in line with"
        self.fact(
            "account_history",
            f"Baseline: {hist.get('prior_exports') or 0} earlier export(s) of {self.task.dataset_id} "
            f"by this account, largest {largest} rows; this export is {comparison} that baseline.",
            "account_history",
            "export_events",
            "rows_exported",
        )


# ------------------------------------------------------------------ reporter -------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+\d[\d ]{8,}\d")
RECID = re.compile(r"\bC\d{6}\b")
URLISH = re.compile(r"\b(s3|smb|gs)://[^\s,;)]+")


def redact(text: str) -> tuple[str, int]:
    n = 0
    for rx, repl in (
        (EMAIL, "<email redacted>"),
        (PHONE, "<phone redacted>"),
        (RECID, "<record id redacted>"),
        (URLISH, "<destination redacted>"),
    ):
        text, k = rx.subn(repl, text)
        n += k
    return text, n


def _supported_tokens(a: "Assessment", task: Task) -> set[str]:
    text = " ".join(
        [f.text for f in a.facts]
        + a.reasons
        + a.next_steps
        + a.conflicts
        + a.gaps
        + [str(v) for v in vars(task).values() if isinstance(v, (str, int))]
    )
    return set(TOKEN.findall(text))


class Critic:
    """Proposer + Critic pairing: scores the Reporter's summary on a 0-3 x 3 rubric (accuracy, appropriateness,
    actionability). Below 7/9 it returns ONE fix and the Reporter revises once. Model-backed when a model is
    available; a deterministic check (every id/number in the summary must appear in the evidence) otherwise."""

    name = "critic"
    PASS = 7

    def __init__(self, llm, trace, remaining=None):
        self.llm, self.trace = llm, trace
        self.remaining = remaining

    def review(self, task: Task, a: "Assessment", summary: str, attempt: int = 1) -> dict:
        if self.llm.name == "stub":
            supported = _supported_tokens(a, task)
            unsupported = sorted({t for t in TOKEN.findall(summary) if t not in supported})
            accuracy = (
                0 if unsupported else 3
            )  # a fabricated id or number is disqualifying, not a deduction
            appropriateness = 3 if a.verdict and a.verdict in summary else 1
            actionability = (
                3
                if (
                    not a.next_steps or any(n[:30].lower() in summary.lower() for n in a.next_steps)
                )
                else 2
            )
            fix = None
            if unsupported:
                fix = f"remove or ground these unsupported references: {', '.join(unsupported)}"
            elif appropriateness < 3:
                fix = f"state the verdict '{a.verdict}' explicitly"
            elif actionability < 3:
                fix = f"include the next check: {a.next_steps[0]}"
            scores = {
                "accuracy": accuracy,
                "appropriateness": appropriateness,
                "actionability": actionability,
            }
            source = "stub"
        else:
            system = (
                "You are a strict reviewer. Score a security analyst's summary against the evidence on three criteria using EXACTLY these anchors. "
                "accuracy: 3 = every id, number and claim appears in the facts; 2 = one minor claim not in the facts; 1 = one wrong id/number; 0 = a fabricated ticket, account, number or event. "
                "appropriateness: 3 = the verdict and its main reason are stated and match the facts; 2 = verdict stated, reason vague; 1 = verdict implied only; 0 = verdict missing or contradicted. "
                "actionability: 3 = a concrete next check is stated (or the verdict is compliant and no action is needed); 2 = a next step is implied; 1 = vague advice; 0 = none while one was needed. "
                "Do not deliberate at length: pick the anchor that fits and answer. Reply with JSON only: "
                '{"accuracy": n, "appropriateness": n, "actionability": n, "fix": "one sentence, the single most important change, or null"}.'
            )
            user = (
                f"Verdict: {a.verdict}\nReasons: {a.reasons}\nNext checks: {a.next_steps}\nFacts:\n"
                + "\n".join(f"- {f.text}" for f in a.facts)
                + f"\nUnconfirmed: {a.gaps}\n\nSummary under review:\n{summary}"
            )
            try:
                text, meta = self.llm.chat(
                    system, user, json_mode=True, max_tokens=3500, max_seconds=_budget(self)
                )
            except LLMError as exc:
                out = {
                    "attempt": attempt,
                    "scores": None,
                    "total": None,
                    "passed": None,
                    "fix": None,
                    "source": "error",
                    "note": f"model unavailable: {str(exc)[:160]}",
                }
                self.trace.step(self.name, "critique", **out)
                return out
            self.trace.step(self.name, "llm", **meta)
            d = extract_json(text) or {}
            source = meta.get("routed_model") or meta.get("model_served")
            if not all(k in d for k in ("accuracy", "appropriateness", "actionability")):
                out = {
                    "attempt": attempt,
                    "scores": None,
                    "total": None,
                    "passed": None,
                    "fix": None,
                    "source": source,
                    "note": "model reply unparseable; critic unavailable",
                    "raw_tail": text[-300:],
                    "usage": meta.get("usage"),
                }
                self.trace.step(self.name, "critique", **out)
                return out
            try:
                scores = {
                    k: max(0, min(3, int(float(d.get(k, 0) or 0))))
                    for k in ("accuracy", "appropriateness", "actionability")
                }
            except (TypeError, ValueError):
                out = {
                    "attempt": attempt,
                    "scores": None,
                    "total": None,
                    "passed": None,
                    "fix": None,
                    "source": source,
                    "note": "model reply had non-numeric scores; critic unavailable",
                }
                self.trace.step(self.name, "critique", **out)
                return out
            fix = d.get("fix") or None
        total = sum(scores.values())
        out = {
            "attempt": attempt,
            "scores": scores,
            "total": total,
            "passed": total >= self.PASS,
            "fix": fix if total < self.PASS else None,
            "source": source,
        }
        self.trace.step(self.name, "critique", **out)
        return out


OWNER_HINTS = [
    ("approver", "ticket approver"),
    ("manager", "exporter's manager"),
    ("HR/IAM", "IAM / HR"),
    ("human review", "security review board"),
    ("re-run", "analyst"),
    ("dataset owner", "dataset owner"),
    ("exporter", "exporter"),
    ("identity provider", "IAM"),
    ("destination", "analyst"),
]
DONE_HINTS = [
    ("preserve", "export file preserved and hash recorded"),
    ("re-run", "triage re-run with the source reachable"),
    ("human review", "decision recorded with reasons"),
    ("approval", "approval evidence attached to the alert"),
    ("policy row", "policy row published or export confirmed unauthorised"),
    ("disable", "account access confirmed disabled"),
    ("destination", "destination confirmed sanctioned, or deletion confirmed"),
]


def handoff(step: str) -> tuple[str, str]:
    """(suggested owner, done-when) for a next step, from keywords in its text."""
    owner = next((o for k, o in OWNER_HINTS if k.lower() in step.lower()), "analyst")
    done = next(
        (d for k, d in DONE_HINTS if k.lower() in step.lower()),
        "evidence attached and the verdict re-run",
    )
    return owner, done


def _handoff(step: str) -> str:
    owner, done = handoff(step)
    return f"{step}  _(suggested owner: {owner}; done when: {done})_"


class Reporter:
    name = "reporter"

    def __init__(self, llm, trace, remaining=None):
        self.llm, self.trace = llm, trace
        self.remaining = remaining

    def template_summary(self, a: Assessment) -> str:
        first = a.reasons[0] if a.reasons else "no reason recorded"
        nxt = f" Next: {a.next_steps[0]}." if a.next_steps else " No further action required."
        return f"Verdict {a.verdict}: {first}.{nxt}"

    def draft_summary(self, task: Task, a: Assessment) -> str:
        """The proposer's draft (3-5 sentences for a colleague). Deterministic template without a model,
        or when the model is unavailable or returns nothing usable."""
        if self.llm.name == "stub":
            return self.template_summary(a)
        try:
            text = self._summary(task, a)
        except LLMError as exc:
            self.trace.step(
                self.name, "model_unavailable", error=str(exc)[:200], fallback="template summary"
            )
            return self.template_summary(a)
        return text or self.template_summary(a)

    def revise(self, task: Task, a: Assessment, summary: str, fix: str) -> str:
        """One revision after the Critic's fix. Without a model: drop sentences carrying unsupported references."""
        self.trace.step(self.name, "revise", fix=fix)
        if self.llm.name == "stub":
            supported = _supported_tokens(a, task)
            kept = [
                sent
                for sent in re.split(r"(?<=[.!?])\s+", summary)
                if all(t in supported for t in TOKEN.findall(sent))
            ]
            text = " ".join(kept).strip()
            if a.verdict and a.verdict not in text:
                text = f"Verdict {a.verdict}: {a.reasons[0] if a.reasons else ''}. " + text
            if a.next_steps and not any(n[:30].lower() in text.lower() for n in a.next_steps):
                text += f" Next: {a.next_steps[0]}."
            return text.strip()
        system = (
            "Revise the summary below according to the reviewer's fix. Use ONLY the facts listed; remove anything they do not support. "
            "3-5 sentences, verdict and main reason first, name what could not be confirmed."
        )
        user = (
            f"Reviewer's fix: {fix}\nVerdict: {a.verdict}\nReasons: {a.reasons}\nFacts:\n"
            + "\n".join(f"- {f.text}" for f in a.facts)
            + f"\nUnconfirmed: {a.gaps}\n\nSummary to revise:\n{summary}"
        )
        try:
            text, meta = self.llm.chat(system, user, max_tokens=1500, max_seconds=_budget(self))
        except LLMError as exc:
            self.trace.step(
                self.name, "model_unavailable", error=str(exc)[:200], fallback="template summary"
            )
            return self.template_summary(a)
        self.trace.step(self.name, "llm", **meta)
        return text.strip() or self.template_summary(a)

    def report(
        self,
        task: Task,
        a: Assessment,
        memory_context: str,
        rounds: int,
        world_context: str = "",
        summary: str | None = None,
        critique: dict | None = None,
        second_opinion: dict | None = None,
        confidence: dict | None = None,
        timeline: list[dict] | None = None,
        memory_updates: dict | None = None,
        status: str = "complete",
    ) -> str:
        redacted = 0
        head = f"**Verdict: {a.verdict}**"
        if confidence:
            head += f" · evidence/review score {confidence['score']:.2f} ({confidence['label']}; heuristic, not a probability)"
        if a.priority:
            head += f" · priority {a.priority['bucket']} ({a.priority['score']:.0f}/10)"
        if a.verdict in ("cannot-confirm", "needs-human"):
            head += " · queue: manual review (unresolved evidence)"
        if status != "complete":
            head += f" · **{status}**"
        if critique and critique.get("review_status") and critique["review_status"] != "passed":
            head += f" · review: {critique['review_status']}"
        lines = [f"# Export-alert triage: {task.alert_id}", "", head, ""]
        if confidence and confidence.get("factors"):
            lines += ["Score factors: " + "; ".join(confidence["factors"]), ""]
        if a.priority:
            lines += ["Priority: " + "; ".join(a.priority["factors"]), ""]
        if a.reasons:
            lines += ["Why:"] + [f"- {r}" for r in a.reasons] + [""]
        if summary is None:
            summary = self.draft_summary(task, a)
        if summary:
            s, k = redact(summary)
            redacted += k
            lines += [s, ""]
        lines += ["## Evidence (table.column | query step)"]
        for f in a.facts:
            if f.kind == "gap":
                continue
            t, k = redact(f.text)
            redacted += k
            c = f.cites[0]
            lines.append(f"- {t}  [{c['table']}.{c.get('column') or '*'} | {c['step']}]")
        if a.conflicts:
            lines += ["", "## Conflicts (flagged, not silently resolved)"] + [
                f"- {c}" for c in a.conflicts
            ]
        gap_facts = [f for f in a.facts if f.kind == "gap"]
        if a.gaps or gap_facts:
            lines += ["", "## Cannot be confirmed from the sources"] + [f"- {g}" for g in a.gaps]
        unf = a.derived.get("unfetched") or a.missing
        if unf:
            lines += [
                "",
                f"## Evidence not retrieved within the {rounds}-round budget (verdict stands on what was retrieved)",
            ] + [f"- {m}: {TEMPLATES[m][0]}" for m in unf if m in TEMPLATES]
        if a.next_steps:
            lines += [
                "",
                "## Suggested next checks (owner to be assigned by the analyst; SLA not modelled)",
            ] + [f"- {_handoff(n)}" for n in a.next_steps]
        d = a.disclosure
        lines += [
            "",
            "## Access footprint (disclosure check)",
            f"- tables accessed: {', '.join(d['tables_accessed']) or 'none'}",
            f"- tables that contributed to the verdict: {', '.join(d['tables_used_in_answer']) or 'none'}",
            f"- accessed but not needed: {', '.join(d['accessed_but_unused']) or 'none'}",
            f"- sensitive columns touched: {', '.join(d['sensitive_columns_touched']) or 'none'}",
            f"- values redacted in this report: {redacted}",
        ]
        if second_opinion:
            src = f" ({second_opinion.get('source')})" if second_opinion.get("source") else ""
            if second_opinion.get("agrees") is None:
                head = f"- no usable opinion: the model reply was {second_opinion.get('verdict')}{src}; the rules-based verdict stands"
            else:
                head = f"- {'agrees' if second_opinion['agrees'] else 'DISAGREES'}: model verdict {second_opinion.get('verdict')} vs rules {second_opinion.get('rules_verdict', a.verdict)}{src}"
            title = (
                "## Second opinion (Evaluator, deterministic stub: offline mode)"
                if second_opinion.get("source") == "stub"
                else "## Second opinion (Evaluator, model-backed)"
            )
            lines += ["", title, head] + [f"  - {r}" for r in second_opinion.get("reasons", [])[:3]]
        if critique:
            sc = critique.get("scores")
            if sc is None:
                lines += [
                    "",
                    "## Review (Critic, 0-3 x 3 rubric)",
                    f"- unavailable: {critique.get('note')} ({critique.get('source')})",
                ]
            else:
                lines += [
                    "",
                    "## Review (Critic, 0-3 x 3 rubric)",
                    f"- accuracy {sc['accuracy']}, appropriateness {sc['appropriateness']}, actionability {sc['actionability']} = {critique['total']}/9 "
                    f"({'pass' if critique['passed'] else 'fail'}; attempt {critique['attempt']})"
                    + (
                        f"; fix applied: {critique.get('fix_applied')}"
                        if critique.get("fix_applied")
                        else ""
                    ),
                ]
        if timeline:
            lines += ["", "## Investigation timeline"]
            for st in timeline:
                lines.append(f"- {st['t']:>6.1f}s · {st['agent']} · {st['what']}")
        if world_context:
            lines += ["", "## World state (Meterless World Model)", world_context]
        if memory_context:
            m, k = redact(memory_context)
            lines += ["", "## Memory used (H-MEM)", m]
        if memory_updates:
            lines += ["", "## Memory updated (H-MEM, with provenance)"]
            for kind, items in memory_updates.items():
                for it in items:
                    t, _ = redact(it["content"])
                    lines.append(f"- {kind}: `{it['id']}` {t[:140]}  <{it['source']}>")
        return "\n".join(lines)

    def _summary(self, task: Task, a: Assessment) -> str:
        if self.llm.name == "stub":
            return ""
        system = (
            "You are a careful security analyst writing 3-5 sentences for a colleague who will act on them. Use ONLY the facts given; "
            "never add a claim that is not in the list; state the verdict and its main reason first; name what could not be confirmed."
        )
        user = (
            f"Verdict: {a.verdict}\nReasons: {a.reasons}\nFacts:\n"
            + "\n".join(f"- {f.text}" for f in a.facts)
            + f"\nConflicts: {a.conflicts}\nUnconfirmed: {a.gaps}"
        )
        text, meta = self.llm.chat(system, user, max_tokens=1500, max_seconds=_budget(self))
        self.trace.step(self.name, "llm", **meta)
        return text.strip()
