"""World Model adapter: the persistent entity graph the investigations share.

Entities  : account, dataset, ticket, exception           (externally keyed: logs/policy/tickets ids)
Contexts  : alert (parent) > export_event                  (externally keyed)
Edges     : account -exported-> dataset (in the event context), account -cited-> ticket,
            ticket -covers/-does_not_cover-> export_event, exception -covers-> account
Facts     : account.status (from the directory), account.verdict:<alert> (from the evaluator)

Every write carries provenance whose `at` is the alert's own timestamp, never the wall clock, so
re-running the same alert produces the same world (content-addressed ids, no duplicate events).
Entity consistency: the same account seen in two alerts resolves to one entity; a changed directory
status supersedes the earlier fact instead of overwriting it, and the dossier shows both.
"""
from __future__ import annotations

from . import config

WORLD_DIR = config.ROOT / "memory"
NS = "exportwhy"


def _d(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class World:
    def __init__(self, persist: bool = True):
        from .worldmodel import WorldModel  # Python port of the Meterless World Model spec (agent/worldmodel/)
        if persist:
            WORLD_DIR.mkdir(exist_ok=True)
            self.w = WorldModel(NS, storage="local", directory=str(WORLD_DIR))
        else:
            self.w = WorldModel(NS, storage="memory")

    # ------------------------------------------------------------------ ids
    @staticmethod
    def account_id(acc: str) -> str:
        return f"account:logs:{acc}"

    # ------------------------------------------------------------------ write
    def _upsert_entity(self, type_, key, attrs, source, state="active"):
        """The reference appends an event on every upsert; skip the write when nothing changed so a
        re-run of the same alert leaves the event log untouched (state is identical either way)."""
        eid = f"{type_}:{key['system']}:{key['id']}"
        cur = self.w.entity(eid)
        if cur is not None and dict(_d(cur, "attrs") or {}) == dict(attrs) and _d(cur, "state") == state:
            return cur
        return self.w.upsert_entity(type_, external_key=key, attrs=attrs, state=state, source=source)

    def _upsert_context(self, type_, key, attrs, source, parent=None):
        cid = f"{type_}:{key['system']}:{key['id']}"
        cur = next((c for c in self.w.contexts(type_) if _d(c, "id") == cid), None)
        if cur is not None and dict(_d(cur, "attrs") or {}) == dict(attrs) and _d(cur, "parent") == parent:
            return cur
        return self.w.upsert_context(type_, external_key=key, attrs=attrs, parent=parent, source=source)

    def ingest_run(self, task, assessment, rows: dict, run_id: str) -> dict:
        """Fold what this investigation established into the world. Returns counts for the trace."""
        before = len(self.w.events())
        at = (task.ts or "1970-01-01 00:00:00").replace(" ", "T")
        src = lambda kind, by="exportwhy": {"kind": kind, "by": by, "at": at, "runId": run_id}

        acc_row = (rows.get("account") or [None])[0]
        ds_row = (rows.get("dataset") or [None])[0]
        ev_row = (rows.get("alert") or [None])[0]
        account = self._upsert_entity("account", {"system": "logs", "id": task.account_id},
                                      {k: acc_row[k] for k in ("role", "department") if acc_row and k in acc_row},
                                      src("directory" if acc_row else "agent"),
                                      state="active" if not acc_row else ("archived" if str(acc_row.get("status")).lower() == "terminated" else "active"))
        dataset = self._upsert_entity("dataset", {"system": "policy", "id": task.dataset_id},
                                      {k: ds_row[k] for k in ("name", "sensitivity") if ds_row and k in ds_row}, src("catalog" if ds_row else "agent"))
        alert = self._upsert_context("alert", {"system": "logs", "id": task.alert_id}, {"rule": task.rule, "severity": _d(ev_row, "severity")}, src("siem"))
        event = self._upsert_context("export_event", {"system": "logs", "id": task.event_id},
                                     {"ts": task.ts, "rows": task.rows, "destination_type": _d(ev_row, "destination_type")}, src("audit-log"), parent=alert.id)
        self.w.relate(account.id, "exported", dataset.id, context=event.id, attrs={"rows": task.rows}, valid_from=at, source=src("audit-log"))

        if acc_row:
            self.w.assert_fact(account.id, "status", str(acc_row.get("status")), source=src("directory"))
        t = (rows.get("ticket_cited") or [None])[0]
        if task.ticket_ref:
            ticket = self._upsert_entity("ticket", {"system": "tickets", "id": task.ticket_ref},
                                         {k: t[k] for k in ("status", "max_rows") if t and k in t}, src("tickets" if t else "agent"), state="active" if t else "emerging")
            self.w.relate(account.id, "cited", ticket.id, context=event.id, source=src("audit-log"))
            if t:
                covers = not any(c.startswith("ticket ") and task.ticket_ref in c for c in assessment.conflicts)
                self.w.relate(ticket.id, "covers" if covers else "does_not_cover", event.id, context=alert.id, source=src("agent"))
            else:
                self.w.assert_fact(ticket.id, "exists_in_ticket_system", False, source=src("tickets"))
        for e in rows.get("exceptions", []) or []:
            if any(e.get("exception_id") in r for r in assessment.reasons):
                exc = self._upsert_entity("exception", {"system": "policy", "id": e["exception_id"]},
                                          {"scope_type": e.get("scope_type"), "scope_value": e.get("scope_value")}, src("policy"))
                self.w.relate(exc.id, "covers", account.id, context=event.id, source=src("agent"))
        self.w.assert_fact(account.id, f"verdict:{task.alert_id}", assessment.verdict, confidence=0.9 if not assessment.gaps else 0.6,
                           context=alert.id, source=src("agent"))
        return {"events_before": before, "events_after": len(self.w.events()), "appended": len(self.w.events()) - before}

    # ------------------------------------------------------------------ read
    def dossier(self, account_id: str, current_alert: str) -> tuple[str, dict]:
        """What the world already knows about this account: prior alerts and verdicts, status history,
        datasets exported, tickets cited. Used as context for the planner and the evaluator."""
        aid = self.account_id(account_id)
        ent = self.w.entity(aid)
        if ent is None:
            return "", {"known": False, "prior_alerts": [], "status_history": []}
        # The reference implementation does not replay supersede marks from the log, so "current" is
        # derived here from the append order: the last fact asserted per predicate wins.
        facts_all = self.w.facts(aid, current_only=False)
        statuses = [f for f in facts_all if _d(f, "predicate") == "status"]
        status_history = [{"value": _d(f, "value"), "at": str(_d(f, "assertedAt"))[:19]} for f in statuses]
        latest: dict[str, object] = {}
        for f in facts_all:
            latest[str(_d(f, "predicate"))] = f
        prior = [{"alert": pred.split(":", 1)[1], "verdict": _d(f, "value"), "at": str(_d(f, "assertedAt"))[:19]}
                 for pred, f in latest.items() if pred.startswith("verdict:") and pred.split(":", 1)[1] != current_alert]
        exported = self.w.relationships(from_=aid, type="exported")
        cited = self.w.relationships(from_=aid, type="cited")
        datasets = sorted({_d(r, "to").split(":")[-1] for r in exported})
        lines = [f"Account {account_id} is known to the world model: {len(exported)} recorded export(s) across {', '.join(datasets) or 'no datasets'}; {len(cited)} ticket citation(s)."]
        for p in prior:
            lines.append(f"- earlier alert {p['alert']}: verdict {p['verdict']} (recorded {p['at']})")
        if len({s['value'] for s in status_history}) > 1:
            lines.append("- directory status changed across investigations: " + " -> ".join(f"{s['value']} ({s['at']})" for s in sorted(status_history, key=lambda s: s['at'])))
        return "\n".join(lines), {"known": True, "prior_alerts": prior, "status_history": status_history, "exports": len(exported), "datasets": datasets}

    def stats(self) -> dict:
        return {"entities": len(self.w.entities()), "events": len(self.w.events()), "merge_queue": len(self.w.merge_queue())}
