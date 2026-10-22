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
from .zetaris import DataLayerError, QueryResult

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
    role: str | None = None          # known after the account row is read
    prior: dict | None = None        # world-model dossier for the account (prior alerts, status history)

    def describe(self) -> str:
        return f"Triage alert {self.alert_id}"


@dataclass
class Step:
    sid: str
    evidence: str
    purpose: str
    sql: str
    origin: str = "rules"            # "rules" | "llm"
    result: QueryResult | None = None
    error: str | None = None


# evidence class -> (purpose, SQL template). ANSI; identical on Zetaris Lightning SQL and SQLite.
# Templates select only the columns the decision needs: no email/phone/destination/sample ids.
TEMPLATES: dict[str, tuple[str, str]] = {
    "alert": ("the alert and the export event it points at",
        "SELECT a.alert_id, a.rule, a.severity, a.raised_at, e.event_id, e.ts, e.account_id, e.dataset_id, e.rows_exported, e.destination_type, e.tool, e.ticket_ref "
        "FROM dexa.alerts a JOIN dexa.export_events e ON e.event_id = a.event_id WHERE a.alert_id = '{alert_id}'"),
    "account": ("who the exporting account is: role and employment status",
        "SELECT account_id, role, department, status, status_since FROM dexa.accounts WHERE account_id = '{account_id}'"),
    "dataset": ("what was exported: dataset sensitivity",
        "SELECT dataset_id, name, sensitivity, owner_team, contains_pii FROM dexa.datasets WHERE dataset_id = '{dataset_id}'"),
    "policy": ("what the role may export from that dataset without a ticket",
        "SELECT policy_id, role, dataset_id, allowed, allowed_hours_start, allowed_hours_end, max_rows_without_ticket, requires_ticket, notes "
        "FROM dexa.policies WHERE role = '{role}' AND dataset_id = '{dataset_id}'"),
    "sensitivity": ("which columns are sensitive (for redaction and the disclosure check)",
        "SELECT table_name, column_name, level FROM dexa.field_sensitivity"),
    "ticket_cited": ("the ticket the exporter cited",
        "SELECT ticket_id, requester_account_id, dataset_id, max_rows, valid_from, valid_to, status, approver_id FROM dexa.tickets WHERE ticket_id = '{ticket_ref}'"),
    "tickets_account": ("any other ticket for this account and dataset",
        "SELECT ticket_id, requester_account_id, dataset_id, max_rows, valid_from, valid_to, status, approver_id FROM dexa.tickets "
        "WHERE requester_account_id = '{account_id}' AND dataset_id = '{dataset_id}' ORDER BY valid_to DESC"),
    "exceptions": ("standing policy exceptions for the role or the account on that dataset",
        "SELECT exception_id, scope_type, scope_value, dataset_id, clause, valid_from, valid_to, approved_by FROM dexa.policy_exceptions "
        "WHERE dataset_id = '{dataset_id}' AND ((scope_type = 'role' AND scope_value = '{role}') OR (scope_type = 'account' AND scope_value = '{account_id}'))"),
    "destination": ("where the data went (sensitive column, fetched only for external destinations)",
        "SELECT event_id, destination_type, destination FROM dexa.export_events WHERE event_id = '{event_id}'"),
    "account_history": ("the account's export baseline on that dataset before this event",
        "SELECT COUNT(*) AS prior_exports, MAX(rows_exported) AS max_rows_before, SUM(rows_exported) AS total_rows_before FROM dexa.export_events "
        "WHERE account_id = '{account_id}' AND dataset_id = '{dataset_id}' AND ts < '{ts}'"),
}

# Classes whose SQL is fully determined by the alert's keys: the model adds nothing, so they are never
# sent for drafting (keeps a triage at <= 3 model calls: two planning rounds and one summary).
NO_DRAFT = {"alert", "account", "sensitivity", "destination", "account_history"}


# ------------------------------------------------------------------ planner --------------------

class Planner:
    name = "planner"

    def __init__(self, llm, trace):
        self.llm, self.trace = llm, trace
        self.counter = 0

    def plan(self, task: Task, classes: list[str], memory_context: str, reason: str) -> list[Step]:
        params = {k: (v if v is not None else "") for k, v in vars(task).items()}
        steps = [self._template(c, params) for c in classes]
        draftable = [st for st in steps if st.evidence not in NO_DRAFT]
        drafted = self._llm_refine(task, draftable, memory_context, reason) if self.llm.name != "stub" and draftable else 0
        self.trace.step(self.name, "plan", reason=reason, evidence=classes, llm_drafted=drafted,
                        steps=[{"id": s.sid, "evidence": s.evidence, "origin": s.origin, "sql": s.sql} for s in steps])
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
        system = ("You write ANSI SQL for a security analyst triaging a data-export alert. Only SELECT statements, only the "
                  "tables below, only the columns needed for the stated purpose. Reply with a JSON array of {\"id\": step id, \"sql\": \"...\"}.\n"
                  + catalog.schema_card())
        user = (f"Alert {task.alert_id} (event={task.event_id}, account={task.account_id}, role={task.role}, dataset={task.dataset_id}, ts={task.ts}, ticket_ref={task.ticket_ref}).\n"
                f"Why we are planning now: {reason}\nMemory from earlier investigations:\n{memory_context or '(none)'}\n\nSteps:\n"
                + "\n".join(f"- {s.sid} [{s.evidence}]: {s.purpose}. Reference SQL: {s.sql}" for s in steps))
        try:
            text, meta = self.llm.chat(system, user, json_mode=True)
        except LLMError as exc:
            self.trace.step(self.name, "model_unavailable", error=str(exc)[:200], fallback="rule-based SQL templates")
            return 0
        drafts = extract_json(text) or []
        by_id = {s.sid: s for s in steps}
        kept = 0
        for d in drafts if isinstance(drafts, list) else []:
            s = by_id.get(str(d.get("id"))); sql = str(d.get("sql", "")).strip().rstrip(";")
            if not s or not sql:
                continue
            ok, why = self._validate(sql, task)
            if ok:
                s.sql, s.origin = sql, "llm"; kept += 1
            else:
                self.trace.step(self.name, "rejected_llm_sql", step=s.sid, why=why, sql=sql[:300])
        self.trace.step(self.name, "llm", **meta)
        return kept

    @staticmethod
    def _validate(sql: str, task: Task) -> tuple[bool, str]:
        if not re.match(r"^\s*select\b", sql, re.I) or re.search(r"\b(insert|update|delete|drop|create|alter|grant|attach)\b", sql, re.I):
            return False, "not a plain SELECT"
        if not catalog.tables_in_sql(sql):
            return False, "no known dexa table referenced"
        for ref in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", sql, re.I):
            if ref[0].lower() == catalog.SCHEMA and ref[1].lower() not in catalog.TABLES:
                return False, f"unknown table {ref[1]}"
        keys = [k for k in (task.alert_id, task.event_id, task.account_id, task.dataset_id, task.ticket_ref) if k]
        if "field_sensitivity" not in sql and not any(k in sql for k in keys):
            return False, "does not filter on this alert's keys"
        return True, ""


# ------------------------------------------------------------------ retriever ------------------

class Retriever:
    name = "retriever"

    def __init__(self, backend, trace):
        self.backend, self.trace = backend, trace
        self.footprint: dict[str, set[str]] = {}      # table -> result columns seen

    def run(self, steps: list[Step]) -> list[Step]:
        for s in steps:
            try:
                s.result = self.backend.query(s.sql)
                for t in catalog.tables_in_sql(s.sql):
                    self.footprint.setdefault(t, set()).update(c for c in s.result.columns if c in catalog.TABLES[t]["columns"])
                self.trace.step(self.name, "query", step=s.sid, evidence=s.evidence, backend=s.result.source, query_id=s.result.query_id,
                                rows=len(s.result.rows), elapsed_ms=s.result.elapsed_ms, tables=catalog.tables_in_sql(s.sql), sql=s.sql)
            except DataLayerError as exc:
                s.error = str(exc)
                self.trace.step(self.name, "query_failed", step=s.sid, evidence=s.evidence, error=s.error[:300], sql=s.sql)
        return steps


# ------------------------------------------------------------------ evaluator ------------------

@dataclass
class Fact:
    key: str
    text: str
    cites: list[dict] = field(default_factory=list)   # {"step","table","column"}
    kind: str = "evidence"                            # evidence | conflict | gap


@dataclass
class Assessment:
    facts: list[Fact]
    conflicts: list[str]
    gaps: list[str]                 # evidence that does not exist in the sources (-> cannot-confirm)
    missing: list[str]              # evidence classes still to fetch (-> another round)
    verdict: str | None
    reasons: list[str]
    next_steps: list[str]
    disclosure: dict
    derived: dict

    @property
    def priority(self) -> dict | None:
        return self.derived.get("priority") if self.verdict in ("investigate", "needs-human") else None


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
        fails.append(f"export at {ts} is outside the ticket window {_clean(t.get('valid_from'))} .. {_clean(t.get('valid_to'))}")
    try:
        if task.rows is not None and int(t.get("max_rows") or 0) < int(task.rows):
            fails.append(f"{task.rows} rows exported exceed the ticket's max_rows {t.get('max_rows')}")
    except ValueError:
        pass
    return fails


VERDICTS = ("compliant", "investigate", "cannot-confirm")
TOKEN = re.compile(r"\b(AL-\d+|T-\d+|u\d{4}|DS\d{2}|X\d{2}|P\d{3}|E\d{5}|CVE-[\d-]+|\d{3,})\b")


class Evaluator:
    name = "evaluator"

    def __init__(self, trace, llm=None):
        self.trace = trace
        self.llm = llm

    # ---- second opinion: the model judges the same evidence independently; rules remain the floor
    def second_opinion(self, task: Task, a: "Assessment") -> dict:
        """Returns {"verdict", "reasons", "missing_evidence", "agrees", "source"}. Never raises."""
        forced = os.environ.get("SECOND_OPINION_FORCE")          # test hook: simulate a disagreeing model
        if forced in VERDICTS:
            op = {"verdict": forced, "reasons": ["forced disagreement (test)"], "missing_evidence": [], "source": "forced"}
        elif self.llm is None or self.llm.name == "stub":
            op = {"verdict": a.verdict, "reasons": ["stub: deterministic second opinion agrees with the rules"], "missing_evidence": [], "source": "stub"}
        else:
            system = ("You are a second security analyst reviewing a colleague's triage of a data-export alert. Judge ONLY from the facts given. "
                      "Reply with JSON: {\"verdict\": \"compliant\"|\"investigate\"|\"cannot-confirm\", \"reasons\": [..], "
                      "\"missing_evidence\": [names from: " + ", ".join(TEMPLATES) + "]}. Rules of the organisation: an export above the no-ticket cap "
                      "needs an approved ticket covering account, dataset, rows and time, or a standing exception; an unknown account, missing policy or "
                      "missing cited ticket means the question cannot be confirmed; a terminated account always needs investigation.")
            user = (f"Alert {task.alert_id}. Facts:\n" + "\n".join(f"- {f.text}" for f in a.facts) + f"\nConflicts: {a.conflicts}\nCannot confirm: {a.gaps}\n"
                    f"(The colleague's verdict is withheld. Give your own.)")
            try:
                text, meta = self.llm.chat(system, user, json_mode=True, max_tokens=1500)
                self.trace.step(self.name, "llm", **meta)
                d = extract_json(text) or {}
                v = str(d.get("verdict", "")).strip().lower()
                op = {"verdict": v if v in VERDICTS else "unparseable", "reasons": [str(r)[:200] for r in (d.get("reasons") or [])][:4],
                      "missing_evidence": [m for m in (d.get("missing_evidence") or []) if m in TEMPLATES], "source": meta.get("routed_model") or meta.get("model_served")}
            except Exception as exc:  # noqa: BLE001 - the model is advisory; its failure never blocks a verdict
                op = {"verdict": "unavailable", "reasons": [f"model unavailable: {str(exc)[:120]}"], "missing_evidence": [], "source": "error"}
        op["usable"] = op["verdict"] in VERDICTS
        op["agrees"] = op["verdict"] == a.verdict if op["usable"] else None   # None = no usable opinion
        self.trace.step(self.name, "second_opinion", rules_verdict=a.verdict, **op)
        return op

    def assess(self, task: Task, done: dict[str, list[Step]], footprint: dict, rounds: int) -> Assessment:
        rows = {cls: [r for s in steps if s.result for r in s.result.rows] for cls, steps in done.items()}
        fetched = {cls for cls, steps in done.items() if any(s.result is not None for s in steps)}
        failed = {cls for cls, steps in done.items() if steps and all(s.error for s in steps)}
        cite = {cls: steps[-1].sid for cls, steps in done.items() if steps}
        step_tables = {cls: sorted({t for s in steps for t in catalog.tables_in_sql(s.sql)}) for cls, steps in done.items()}
        facts: list[Fact] = []; conflicts: list[str] = []; gaps: list[str] = []; missing: list[str] = []
        reasons: list[str] = []; next_steps: list[str] = []; derived: dict = {}

        def fact(key, text, cls, table, column=None, kind="evidence"):
            cites = [{"step": cite.get(cls), "table": f"{catalog.SCHEMA}.{table}", "column": column}]
            cites += [{"step": cite.get(cls), "table": f"{catalog.SCHEMA}.{t}", "column": None} for t in step_tables.get(cls, []) if t != table]
            facts.append(Fact(key, text, cites, kind))

        CRITICAL = {"account", "policy", "ticket_cited", "tickets_account", "exceptions"}

        def need(cls):
            if cls in failed:
                if cls not in derived.setdefault("unreachable", []):
                    derived["unreachable"].append(cls)
                    gaps.append(f"source for '{cls}' could not be queried ({TEMPLATES[cls][0]})")
                    if cls in CRITICAL:
                        derived["blocked_by_failure"] = True
                return
            if cls not in fetched:
                missing.append(cls)

        # ---- alert + event
        ev = (rows.get("alert") or [None])[0]
        if not ev:
            if "alert" in failed:
                gaps.append("the alert/event source (logs) could not be queried; nothing about this export can be established")
                reasons.append("the logs source is unreachable"); next_steps.append("re-run the triage once the logs source is reachable")
            else:
                reasons.append("alert or its export event not found")
            derived["verdict"] = "cannot-confirm"
            return self._finish(task, facts, conflicts, gaps, missing, reasons, next_steps, derived, footprint, rows, rounds)
        ts = _ts(ev["ts"])
        task.event_id, task.ts, task.account_id, task.dataset_id = ev["event_id"], _clean(ev["ts"]), ev["account_id"], ev["dataset_id"]
        task.rows, task.ticket_ref, task.rule = int(ev["rows_exported"]), (ev.get("ticket_ref") or "").strip() or None, ev["rule"]
        fact("event", f"{task.alert_id} ({ev['severity']}, rule: {ev['rule']}): account {task.account_id} exported {task.rows} rows of {task.dataset_id} at {task.ts} "
             f"via {ev['tool']} to a {ev['destination_type']} destination; ticket cited: {task.ticket_ref or 'none'}.", "alert", "export_events", "rows_exported")

        need("sensitivity")   # the classification drives redaction and the disclosure check

        # ---- account
        acc = (rows.get("account") or [None])[0]
        if "account" in fetched and not acc:
            gaps.append(f"account {task.account_id} is not in the directory")
            fact("account", f"Account {task.account_id} does not exist in the identity directory.", "account", "accounts", "account_id", kind="gap")
            derived["verdict"] = "cannot-confirm"; reasons.append("the exporting account cannot be identified")
            next_steps.append("check the identity provider for a recently deleted or service account with this id")
        elif acc:
            task.role = acc["role"]
            since = _ts(acc.get("status_since"))
            fact("account", f"Account {task.account_id}: role {acc['role']}, department {acc['department']}, status {acc['status']} since {_clean(acc.get('status_since'))}.", "account", "accounts", "status")
            if str(acc["status"]).lower() in ("terminated", "offboarding") and since is not None and since <= ts:
                derived["verdict"] = "investigate"; reasons.append(f"account was {acc['status']} ({_clean(acc.get('status_since'))}) before the export")
                next_steps.append("confirm with HR/IAM why the account still had export access; disable it")
        else:
            need("account")

        # ---- dataset + policy
        ds = (rows.get("dataset") or [None])[0]
        if ds:
            fact("dataset", f"Dataset {ds['dataset_id']} ({ds['name']}) is classified {ds['sensitivity']}, owner {ds['owner_team']}, contains_pii={_truthy(ds.get('contains_pii'))}.",
                 "dataset", "datasets", "sensitivity")
        else:
            need("dataset")
        pol = (rows.get("policy") or [None])[0]
        if task.role:
            need("policy")          # no-op once fetched; records the gap and blocks "compliant" if the source failed
        if task.role and "policy" in fetched and not pol:
            gaps.append(f"no policy row for role {task.role} on {task.dataset_id}")
            fact("policy", f"No data-access policy exists for role {task.role} on {task.dataset_id}; entitlement cannot be determined from policy.", "policy", "policies", "policy_id", kind="gap")
            derived.setdefault("verdict", "cannot-confirm"); reasons.append("no policy covers this role and dataset")
            next_steps.append("ask the dataset owner to publish a policy row for this role; treat as unauthorised until then")
        elif pol:
            cap = int(pol.get("max_rows_without_ticket") or 0); allowed = _truthy(pol.get("allowed"))
            derived["cap"] = cap
            hours_ok = str(pol.get("allowed_hours_start")) <= ts.strftime("%H:%M") <= str(pol.get("allowed_hours_end"))
            derived["hours_ok"] = hours_ok
            fact("policy", f"Policy {pol['policy_id']}: role {task.role} {'may' if allowed else 'may NOT'} export {task.dataset_id}; no-ticket cap {cap} rows; "
                 f"advisory hours {pol.get('allowed_hours_start')}-{pol.get('allowed_hours_end')} (export was {'inside' if hours_ok else 'outside'} them).", "policy", "policies", "max_rows_without_ticket")
            if not allowed:
                derived.setdefault("verdict", "investigate"); reasons.append(f"policy gives role {task.role} no export rights on {task.dataset_id}")
            elif task.rows <= cap:
                derived.setdefault("verdict", "compliant"); reasons.append(f"{task.rows} rows are within the no-ticket cap of {cap}; the hours window is advisory below the cap")
            else:
                derived["needs_auth"] = True

        # ---- authorisation search (only when the export exceeds the cap)
        if derived.get("needs_auth") and "verdict" not in derived:
            covering = None; cited_fails = None
            if task.ticket_ref:
                if "ticket_cited" not in fetched:
                    need("ticket_cited")
                else:
                    t = (rows.get("ticket_cited") or [None])[0]
                    if not t:
                        gaps.append(f"cited ticket {task.ticket_ref} does not exist in the ticket system")
                        fact("ticket_cited", f"The exporter cited ticket {task.ticket_ref}, which does not exist in the ticket system.", "ticket_cited", "tickets", "ticket_id", kind="gap")
                        next_steps.append("ask the exporter for the approval they relied on; check the ticket system's audit log for a deleted ticket")
                    else:
                        cited_fails = ticket_checks(t, task, ts)
                        fact("ticket_cited", f"Ticket {t['ticket_id']}: requester {t['requester_account_id']}, dataset {t['dataset_id']}, max_rows {t['max_rows']}, "
                             f"{t['status']}, valid {_clean(t['valid_from'])} .. {_clean(t['valid_to'])}, approver {t['approver_id']}.", "ticket_cited", "tickets", "valid_to")
                        if not cited_fails:
                            covering = t["ticket_id"]
                        else:
                            conflicts.append(f"ticket {t['ticket_id']} was cited but does not cover the export: " + "; ".join(cited_fails))
            if covering:
                derived["verdict"] = "compliant"; reasons.append(f"approved ticket {covering} covers the account, dataset, {task.rows} rows and the time of export")
            elif task.ticket_ref and "ticket_cited" not in fetched and "ticket_cited" not in failed:
                pass   # read the cited ticket first; only widen the search if it does not cover the export
            else:
                # the cited ticket is missing or fails: look for any other authorisation before deciding
                need("tickets_account"); need("exceptions")
                if "tickets_account" in fetched:
                    others = [t for t in rows.get("tickets_account", []) if t.get("ticket_id") != task.ticket_ref]
                    good = [t for t in others if not ticket_checks(t, task, ts)]
                    if good:
                        covering = good[0]["ticket_id"]
                        fact("tickets_account", f"Another approved ticket {covering} covers this export (not cited by the exporter).", "tickets_account", "tickets", "ticket_id")
                        derived["verdict"] = "compliant"; reasons.append(f"approved ticket {covering} covers the export even though it was not cited")
                        next_steps.append("remind the exporter to cite the ticket in the export tool")
                    else:
                        fact("tickets_account", f"{len(others)} other ticket(s) for {task.account_id} on {task.dataset_id}; none covers this export.", "tickets_account", "tickets", "status")
                if "verdict" not in derived and "exceptions" in fetched:
                    exc = [e for e in rows.get("exceptions", []) if _in_window(ts, e.get("valid_from"), e.get("valid_to"))]
                    if exc:
                        e = exc[0]
                        fact("exceptions", f"Policy exception {e['exception_id']} ({e['scope_type']} {e['scope_value']}, approved by {e['approved_by']}, valid {_clean(e['valid_from'])} .. {_clean(e['valid_to'])}) applies: {e['clause']}",
                             "exceptions", "policy_exceptions", "clause")
                        derived["verdict"] = "compliant"; reasons.append(f"standing policy exception {e['exception_id']} covers the export")
                    else:
                        fact("exceptions", f"No policy exception covers role {task.role} or account {task.account_id} on {task.dataset_id} at {task.ts}.", "exceptions", "policy_exceptions", "exception_id")
                if "verdict" not in derived and "tickets_account" in fetched and "exceptions" in fetched:
                    if task.ticket_ref and not (rows.get("ticket_cited") or []):
                        derived["verdict"] = "cannot-confirm"; reasons.append("the only authorisation the exporter cites cannot be found, and nothing else covers the export")
                    elif cited_fails:
                        derived["verdict"] = "investigate"; reasons.append("the cited ticket does not cover this export and no other authorisation does")
                        next_steps.append("contact the ticket approver and the exporter's manager; preserve the exported file")
                    else:
                        derived["verdict"] = "investigate"; reasons.append(f"{task.rows} rows exceed the no-ticket cap and no ticket or exception authorises the export")
                        next_steps.append("contact the exporter's manager; confirm whether the destination was internal; preserve the exported file")
                    need("account_history")

        # ---- what the world model already knows about this account (entity consistency across alerts)
        prior = (task.prior or {}).get("prior_alerts") or []
        if prior and derived.get("verdict") in ("investigate", "cannot-confirm"):
            flagged = [p for p in prior if p["verdict"] in ("investigate", "cannot-confirm")]
            if flagged:
                reasons.append(f"account has {len(flagged)} earlier alert(s) that were not cleared: " + ", ".join(f"{p['alert']} ({p['verdict']})" for p in flagged))
                next_steps.insert(0, "escalate: repeat pattern on the same account across investigations")
        hist = (task.prior or {}).get("status_history") or []
        if len({h["value"] for h in hist}) > 1:
            conflicts.append("directory status for this account differs between investigations: " + " -> ".join(f"{h['value']} ({h['at']})" for h in hist))

        # ---- destination: a confidential column, read only when the export left the internal share
        external = str(ev.get("destination_type")) in ("email", "cloud-bucket", "usb")
        if external and derived.get("verdict") in ("investigate", "cannot-confirm"):
            need("destination")
            dest = (rows.get("destination") or [None])[0]
            if dest:
                fact("destination", f"The export went to an external {dest['destination_type']} destination: {dest['destination']}.", "destination", "export_events", "destination")
                next_steps.append("confirm whether the destination is a sanctioned location; request deletion if not")

        # ---- baseline (context for investigate verdicts)
        hist = (rows.get("account_history") or [None])[0]
        if hist and derived.get("verdict") == "investigate":
            fact("account_history", f"Baseline: {hist.get('prior_exports') or 0} earlier export(s) of {task.dataset_id} by this account, largest {hist.get('max_rows_before') or 0} rows; "
                 f"this export is {'far above' if int(task.rows) > 3 * int(hist.get('max_rows_before') or 0) else 'in line with'} that baseline.", "account_history", "export_events", "rows_exported")

        return self._finish(task, facts, conflicts, gaps, missing, reasons, next_steps, derived, footprint, rows, rounds)

    def _finish(self, task, facts, conflicts, gaps, missing, reasons, next_steps, derived, footprint, rows, rounds) -> Assessment:
        # disclosure: columns touched vs classification; sensitive columns that no fact needed are flagged
        sens = {(r["table_name"], r["column_name"]): r["level"] for r in rows.get("sensitivity", [])}
        used_tables = {c["table"].split(".")[1] for f in facts for c in f.cites} | ({"field_sensitivity"} if sens else set())
        touched_sensitive = sorted(f"{t}.{c} ({sens[(t, c)]})" for t, cols in footprint.items() for c in cols if (t, c) in sens)
        disclosure = {"tables_accessed": sorted(footprint), "columns_seen": {t: sorted(c) for t, c in footprint.items()},
                      "tables_used_in_answer": sorted(used_tables), "accessed_but_unused": sorted(set(footprint) - used_tables),
                      "sensitive_columns_touched": touched_sensitive,
                      "pii_columns_touched": [x for x in touched_sensitive if "(pii)" in x]}
        derived["sensitivity"] = sens
        verdict = derived.get("verdict")
        # priority for the analyst's queue: what was exported, how much, where to, by whom (0-10, P1/P2/P3)
        ev = (rows.get("alert") or [None])[0]; ds = (rows.get("dataset") or [None])[0]; acc = (rows.get("account") or [None])[0]
        score = 0.0; why = []
        sens_w = {"restricted": 4, "confidential": 3, "internal": 1, "public": 0}.get(str((ds or {}).get("sensitivity")).lower(), 2)
        score += sens_w; why.append(f"dataset {(ds or {}).get('sensitivity', 'unknown')} (+{sens_w})")
        try:
            r = int((ev or {}).get("rows_exported") or 0)
            rw = 0 if r < 1000 else 1 if r < 10000 else 2 if r < 50000 else 3
            score += rw; why.append(f"{r} rows (+{rw})")
        except ValueError:
            pass
        if str((ev or {}).get("destination_type")) in ("email", "cloud-bucket", "usb"):
            score += 2; why.append("external destination (+2)")
        if acc and str(acc.get("status")).lower() in ("terminated", "offboarding"):
            score += 2; why.append(f"account {acc.get('status')} (+2)")
        if not acc and "account" in rows:
            score += 1; why.append("unknown account (+1)")
        score = min(10.0, score)
        derived["priority"] = {"score": score, "bucket": "P1" if score >= 8 else "P2" if score >= 5 else "P3", "factors": why}
        if derived.get("blocked_by_failure") and verdict != "investigate":
            # a decision-critical source was unreachable: never upgrade to "compliant" on partial evidence
            verdict = "cannot-confirm"; derived["verdict"] = verdict
            reasons.insert(0, "a decision-critical source was unreachable; retry when it is back")
            next_steps.insert(0, "re-run the triage once the failing source is reachable")
        if verdict is None and (not missing or rounds >= MAX_ROUNDS):
            verdict = "cannot-confirm"; reasons.append("evidence budget exhausted before a decision could be grounded")
        if verdict and missing and rounds >= MAX_ROUNDS:
            missing = []   # budget spent: report with what we have
        if verdict and missing and all(m == "account_history" for m in missing) and rounds >= MAX_ROUNDS - 1:
            missing = []   # baseline is nice-to-have; never spend the last round only on it
        a = Assessment(facts, conflicts, gaps, missing, verdict if not missing else None, reasons, next_steps, disclosure, derived)
        self.trace.step(self.name, "assess", round=rounds, facts=len(facts), conflicts=conflicts, gaps=gaps, missing=missing,
                        verdict=a.verdict, reasons=reasons, disclosure={k: v for k, v in disclosure.items() if k != "columns_seen"})
        return a


# ------------------------------------------------------------------ reporter -------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+\d[\d ]{8,}\d")
RECID = re.compile(r"\bC\d{6}\b")
URLISH = re.compile(r"\b(s3|smb|gs)://[^\s,;)]+")


def redact(text: str) -> tuple[str, int]:
    n = 0
    for rx, repl in ((EMAIL, "<email redacted>"), (PHONE, "<phone redacted>"), (RECID, "<record id redacted>"), (URLISH, "<destination redacted>")):
        text, k = rx.subn(repl, text); n += k
    return text, n


def _supported_tokens(a: "Assessment", task: Task) -> set[str]:
    text = " ".join([f.text for f in a.facts] + a.reasons + a.next_steps + a.conflicts + a.gaps + [str(v) for v in vars(task).values() if isinstance(v, (str, int))])
    return set(TOKEN.findall(text))


class Critic:
    """Proposer + Critic pairing: scores the Reporter's summary on a 0-3 x 3 rubric (accuracy, appropriateness,
    actionability). Below 7/9 it returns ONE fix and the Reporter revises once. Model-backed when a model is
    available; a deterministic check (every id/number in the summary must appear in the evidence) otherwise."""
    name = "critic"
    PASS = 7

    def __init__(self, llm, trace):
        self.llm, self.trace = llm, trace

    def review(self, task: Task, a: "Assessment", summary: str, attempt: int = 1) -> dict:
        if self.llm.name == "stub":
            supported = _supported_tokens(a, task)
            unsupported = sorted({t for t in TOKEN.findall(summary) if t not in supported})
            accuracy = 0 if unsupported else 3          # a fabricated id or number is disqualifying, not a deduction
            appropriateness = 3 if a.verdict and a.verdict in summary else 1
            actionability = 3 if (not a.next_steps or any(n[:30].lower() in summary.lower() for n in a.next_steps)) else 2
            fix = None
            if unsupported:
                fix = f"remove or ground these unsupported references: {', '.join(unsupported)}"
            elif appropriateness < 3:
                fix = f"state the verdict '{a.verdict}' explicitly"
            elif actionability < 3:
                fix = f"include the next check: {a.next_steps[0]}"
            scores = {"accuracy": accuracy, "appropriateness": appropriateness, "actionability": actionability}
            source = "stub"
        else:
            system = ("You are a strict reviewer. Score a security analyst's summary against the evidence on three criteria, 0-3 each: "
                      "accuracy (every claim is supported by a listed fact; any unsupported id, number or claim costs points), "
                      "appropriateness (the verdict and its main reason are stated and consistent with the facts), "
                      "actionability (the next check is concrete). Reply with JSON {\"accuracy\": n, \"appropriateness\": n, \"actionability\": n, "
                      "\"fix\": \"one sentence, the single most important change, or null\"}.")
            user = (f"Verdict: {a.verdict}\nReasons: {a.reasons}\nNext checks: {a.next_steps}\nFacts:\n" + "\n".join(f"- {f.text}" for f in a.facts)
                    + f"\nUnconfirmed: {a.gaps}\n\nSummary under review:\n{summary}")
            try:
                text, meta = self.llm.chat(system, user, json_mode=True, max_tokens=2000)
            except LLMError as exc:
                out = {"attempt": attempt, "scores": None, "total": None, "passed": None, "fix": None, "source": "error", "note": f"model unavailable: {str(exc)[:160]}"}
                self.trace.step(self.name, "critique", **out)
                return out
            self.trace.step(self.name, "llm", **meta)
            d = extract_json(text) or {}
            source = meta.get("routed_model") or meta.get("model_served")
            if not all(k in d for k in ("accuracy", "appropriateness", "actionability")):
                out = {"attempt": attempt, "scores": None, "total": None, "passed": None, "fix": None, "source": source,
                       "note": "model reply unparseable; critic unavailable", "raw_tail": text[-300:], "usage": meta.get("usage")}
                self.trace.step(self.name, "critique", **out)
                return out
            scores = {k: max(0, min(3, int(d.get(k, 0) or 0))) for k in ("accuracy", "appropriateness", "actionability")}
            fix = d.get("fix") or None
        total = sum(scores.values())
        out = {"attempt": attempt, "scores": scores, "total": total, "passed": total >= self.PASS, "fix": fix if total < self.PASS else None, "source": source}
        self.trace.step(self.name, "critique", **out)
        return out


class Reporter:
    name = "reporter"

    def __init__(self, llm, trace):
        self.llm, self.trace = llm, trace

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
            self.trace.step(self.name, "model_unavailable", error=str(exc)[:200], fallback="template summary")
            return self.template_summary(a)
        return text or self.template_summary(a)

    def revise(self, task: Task, a: Assessment, summary: str, fix: str) -> str:
        """One revision after the Critic's fix. Without a model: drop sentences carrying unsupported references."""
        self.trace.step(self.name, "revise", fix=fix)
        if self.llm.name == "stub":
            supported = _supported_tokens(a, task)
            kept = [sent for sent in re.split(r"(?<=[.!?])\s+", summary) if all(t in supported for t in TOKEN.findall(sent))]
            text = " ".join(kept).strip()
            if a.verdict and a.verdict not in text:
                text = f"Verdict {a.verdict}: {a.reasons[0] if a.reasons else ''}. " + text
            if a.next_steps and not any(n[:30].lower() in text.lower() for n in a.next_steps):
                text += f" Next: {a.next_steps[0]}."
            return text.strip()
        system = ("Revise the summary below according to the reviewer's fix. Use ONLY the facts listed; remove anything they do not support. "
                  "3-5 sentences, verdict and main reason first, name what could not be confirmed.")
        user = (f"Reviewer's fix: {fix}\nVerdict: {a.verdict}\nReasons: {a.reasons}\nFacts:\n" + "\n".join(f"- {f.text}" for f in a.facts)
                + f"\nUnconfirmed: {a.gaps}\n\nSummary to revise:\n{summary}")
        try:
            text, meta = self.llm.chat(system, user, max_tokens=1500)
        except LLMError as exc:
            self.trace.step(self.name, "model_unavailable", error=str(exc)[:200], fallback="template summary")
            return self.template_summary(a)
        self.trace.step(self.name, "llm", **meta)
        return text.strip() or self.template_summary(a)

    def report(self, task: Task, a: Assessment, memory_context: str, rounds: int, world_context: str = "",
               summary: str | None = None, critique: dict | None = None, second_opinion: dict | None = None,
               confidence: dict | None = None, timeline: list[dict] | None = None, memory_updates: dict | None = None,
               status: str = "complete") -> str:
        redacted = 0
        head = f"**Verdict: {a.verdict}**"
        if confidence:
            head += f" · confidence {confidence['score']:.2f} ({confidence['label']})"
        if a.priority:
            head += f" · priority {a.priority['bucket']} ({a.priority['score']:.0f}/10)"
        if status != "complete":
            head += f" · **{status}**"
        lines = [f"# Export-alert triage: {task.alert_id}", "", head, ""]
        if confidence and confidence.get("factors"):
            lines += ["Confidence: " + "; ".join(confidence["factors"]), ""]
        if a.priority:
            lines += ["Priority: " + "; ".join(a.priority["factors"]), ""]
        if a.reasons:
            lines += ["Why:"] + [f"- {r}" for r in a.reasons] + [""]
        if summary is None:
            summary = self.draft_summary(task, a)
        if summary:
            s, k = redact(summary); redacted += k; lines += [s, ""]
        lines += ["## Evidence (table.column | query step)"]
        for f in a.facts:
            if f.kind == "gap":
                continue
            t, k = redact(f.text); redacted += k
            c = f.cites[0]
            lines.append(f"- {t}  [{c['table']}.{c.get('column') or '*'} | {c['step']}]")
        if a.conflicts:
            lines += ["", "## Conflicts (flagged, not silently resolved)"] + [f"- {c}" for c in a.conflicts]
        gap_facts = [f for f in a.facts if f.kind == "gap"]
        if a.gaps or gap_facts:
            lines += ["", "## Cannot be confirmed from the sources"] + [f"- {g}" for g in a.gaps]
        if a.missing:
            lines += ["", f"## Evidence still unfetched after {rounds} round(s)"] + [f"- {m}" for m in a.missing]
        if a.next_steps:
            lines += ["", "## Suggested next checks"] + [f"- {n}" for n in a.next_steps]
        d = a.disclosure
        lines += ["", "## Access footprint (disclosure check)",
                  f"- tables accessed: {', '.join(d['tables_accessed']) or 'none'}",
                  f"- tables that contributed to the verdict: {', '.join(d['tables_used_in_answer']) or 'none'}",
                  f"- accessed but not needed: {', '.join(d['accessed_but_unused']) or 'none'}",
                  f"- sensitive columns touched: {', '.join(d['sensitive_columns_touched']) or 'none'}",
                  f"- values redacted in this report: {redacted}"]
        if second_opinion:
            src = f" ({second_opinion.get('source')})" if second_opinion.get("source") else ""
            if second_opinion.get("agrees") is None:
                head = f"- no usable opinion: the model reply was {second_opinion.get('verdict')}{src}; the rules-based verdict stands"
            else:
                head = f"- {'agrees' if second_opinion['agrees'] else 'DISAGREES'}: model verdict {second_opinion.get('verdict')} vs rules {second_opinion.get('rules_verdict', a.verdict)}{src}"
            lines += ["", "## Second opinion (Evaluator, model-backed)", head] + [f"  - {r}" for r in second_opinion.get("reasons", [])[:3]]
        if critique:
            sc = critique.get("scores")
            if sc is None:
                lines += ["", "## Review (Critic, 0-3 x 3 rubric)", f"- unavailable: {critique.get('note')} ({critique.get('source')})"]
            else:
                lines += ["", "## Review (Critic, 0-3 x 3 rubric)",
                          f"- accuracy {sc['accuracy']}, appropriateness {sc['appropriateness']}, actionability {sc['actionability']} = {critique['total']}/9 "
                          f"({'pass' if critique['passed'] else 'fail'}; attempt {critique['attempt']})"
                          + (f"; fix applied: {critique.get('fix_applied')}" if critique.get("fix_applied") else "")]
        if timeline:
            lines += ["", "## Investigation timeline"]
            for st in timeline:
                lines.append(f"- {st['t']:>6.1f}s · {st['agent']} · {st['what']}")
        if world_context:
            lines += ["", "## World state (Meterless World Model)", world_context]
        if memory_context:
            m, k = redact(memory_context); lines += ["", "## Memory used (H-MEM)", m]
        if memory_updates:
            lines += ["", "## Memory updated (H-MEM, with provenance)"]
            for kind, items in memory_updates.items():
                for it in items:
                    t, _ = redact(it["content"]); lines.append(f"- {kind}: `{it['id']}` {t[:140]}  <{it['source']}>")
        return "\n".join(lines)

    def _summary(self, task: Task, a: Assessment) -> str:
        if self.llm.name == "stub":
            return ""
        system = ("You are a careful security analyst writing 3-5 sentences for a colleague who will act on them. Use ONLY the facts given; "
                  "never add a claim that is not in the list; state the verdict and its main reason first; name what could not be confirmed.")
        user = (f"Verdict: {a.verdict}\nReasons: {a.reasons}\nFacts:\n" + "\n".join(f"- {f.text}" for f in a.facts)
                + f"\nConflicts: {a.conflicts}\nUnconfirmed: {a.gaps}")
        text, meta = self.llm.chat(system, user, max_tokens=1500)
        self.trace.step(self.name, "llm", **meta)
        return text.strip()
