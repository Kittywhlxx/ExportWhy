"""Synthetic three-source world for export-alert triage, with ground truth.

    .venv/bin/python data/gen_data.py --seed 7 [--out data/sample]

Sources (each becomes its own database, federated by Zetaris):
  logs    : accounts, export_events, alerts
  policy  : datasets, policies, policy_exceptions, field_sensitivity
  tickets : tickets
Ground truth (never loaded into the data layer): ground_truth.csv with the expected verdict per alert.

Scenarios injected per alert (the agent must tell them apart from evidence, not from labels):
  S1 valid_ticket        approved ticket covers account, dataset, rows and time   -> compliant
  S2a expired_ticket     ticket cited but validity window ended before the export -> investigate
  S2b scope_mismatch     ticket cited but rows exceed its max_rows / other dataset -> investigate
  S3 policy_exception    no ticket, but a policy exception clause covers the export -> compliant
  S4 no_authorization    no ticket, no exception, policy requires one             -> investigate
  S5a unknown_account    exporting account is not in the directory               -> cannot-confirm
  S5b missing_policy     no policy row for the role x dataset                     -> cannot-confirm
  S5c ticket_not_found   event cites a ticket id that the ticket system lacks     -> cannot-confirm
  S6 terminated_account  account was terminated before the export                 -> investigate
  S7 within_policy       alert fired on hours, but rows are under the no-ticket cap -> compliant
No real people, systems or customer data: every name, address and id is generated.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from pathlib import Path

ROLES = ["analyst", "data-steward", "engineer", "contractor", "support", "finance"]
DEPTS = ["risk", "marketing", "platform", "customer-care", "finance", "sales"]
DATASETS = [  # id, name, sensitivity, owner_team, record_count, contains_pii
    ("DS01", "customer_master", "restricted", "risk", 1_250_000, True),
    ("DS02", "payment_transactions", "restricted", "finance", 9_800_000, True),
    ("DS03", "support_tickets", "confidential", "customer-care", 640_000, True),
    ("DS04", "marketing_leads", "confidential", "marketing", 310_000, True),
    ("DS05", "hr_directory", "restricted", "platform", 2_400, True),
    ("DS06", "product_catalog", "internal", "sales", 12_000, False),
    ("DS07", "sales_pipeline", "confidential", "sales", 58_000, False),
    ("DS08", "public_pricing", "public", "sales", 900, False),
]
SYL = ["ka", "mi", "ro", "ta", "ne", "lu", "si", "vo", "da", "ri", "en", "po", "ja", "li", "mo", "sa"]
TOOLS = ["bi-export", "sql-console", "api-client", "notebook"]
T0 = dt.datetime(2026, 10, 1, 0, 0)


def name(r):
    return "".join(r.choice(SYL) for _ in range(r.randint(2, 3))).capitalize() + " " + "".join(r.choice(SYL) for _ in range(2)).capitalize()


def iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def gen(seed: int):
    r = random.Random(seed)
    out: dict[str, list[dict]] = {}

    # ---- accounts
    accounts = []
    for i in range(40):
        role = ROLES[i % len(ROLES)] if i < 12 else r.choice(ROLES)
        status = "active"
        since = T0 - dt.timedelta(days=r.randint(30, 900))
        if i in (37, 38):
            status, since = "terminated", dt.datetime(2026, 10, 9, 17, 0)
        if i == 39:
            status, since = "offboarding", dt.datetime(2026, 10, 15, 9, 0)
        accounts.append({"account_id": f"u{1001 + i}", "display_name": name(r), "role": role, "department": r.choice(DEPTS),
                         "manager_id": f"u{1001 + r.randint(0, 5)}", "status": status, "status_since": iso(since),
                         "email": f"user{1001 + i}@example.com", "phone": f"+61 4{r.randint(10, 99)} {r.randint(100, 999)} {r.randint(100, 999)}"})
    out["accounts"] = accounts
    by_id = {a["account_id"]: a for a in accounts}

    # ---- datasets / policies / exceptions / field sensitivity
    out["datasets"] = [dict(zip(["dataset_id", "name", "sensitivity", "owner_team", "record_count", "contains_pii"], d)) for d in DATASETS]
    policies = []
    pid = 0
    for role in ROLES:
        for d in DATASETS:
            ds, sens = d[0], d[2]
            if role == "data-steward" and ds == "DS05" and seed % 2 == 0:
                continue  # deliberate gap: no policy row (S5b) -- parity keeps it present on odd seeds for variety
            if role == "contractor" and ds == "DS02":
                continue  # a second deliberate gap
            allowed = not (role in ("contractor", "support") and sens == "restricted")
            cap = {"public": 1_000_000, "internal": 50_000, "confidential": 5_000, "restricted": 500}[sens]
            if role in ("contractor", "support"):
                cap = min(cap, 200)
            pid += 1
            policies.append({"policy_id": f"P{pid:03d}", "role": role, "dataset_id": ds, "allowed": allowed,
                             "allowed_hours_start": "00:00" if sens == "public" else "07:00", "allowed_hours_end": "23:59" if sens == "public" else "19:00",
                             "max_rows_without_ticket": cap if allowed else 0, "requires_ticket": sens in ("restricted", "confidential"),
                             "notes": "exports above the cap or outside hours need an approved ticket" if allowed else "no export rights"})
    out["policies"] = policies
    pol = {(p["role"], p["dataset_id"]): p for p in policies}
    exceptions = [
        {"exception_id": "X01", "scope_type": "role", "scope_value": "data-steward", "dataset_id": "DS01", "clause": "incident-response window INC-2026-311: stewards may export customer_master for breach-scope analysis",
         "valid_from": "2026-10-18 00:00:00", "valid_to": "2026-10-25 23:59:59", "approved_by": "u1003"},
        {"exception_id": "X02", "scope_type": "role", "scope_value": "finance", "dataset_id": "DS02", "clause": "quarter-close reconciliation: finance may export payment_transactions outside hours",
         "valid_from": "2026-09-28 00:00:00", "valid_to": "2026-10-10 23:59:59", "approved_by": "u1002"},
        {"exception_id": "X03", "scope_type": "account", "scope_value": "u1014", "dataset_id": "DS04", "clause": "campaign migration: named account may export marketing_leads up to 60000 rows",
         "valid_from": "2026-10-12 00:00:00", "valid_to": "2026-10-31 23:59:59", "approved_by": "u1004"},
    ]
    out["policy_exceptions"] = exceptions
    out["field_sensitivity"] = [
        {"table_name": "accounts", "column_name": "email", "level": "pii"}, {"table_name": "accounts", "column_name": "phone", "level": "pii"},
        {"table_name": "accounts", "column_name": "display_name", "level": "pii"}, {"table_name": "accounts", "column_name": "manager_id", "level": "internal"},
        {"table_name": "export_events", "column_name": "destination", "level": "confidential"}, {"table_name": "export_events", "column_name": "client_ip", "level": "confidential"},
        {"table_name": "export_events", "column_name": "sample_record_ids", "level": "pii"}, {"table_name": "tickets", "column_name": "purpose", "level": "internal"},
        {"table_name": "datasets", "column_name": "record_count", "level": "internal"},
    ]

    # ---- tickets (background) and events (background)
    tickets, events = [], []
    tid = [0]

    def ticket(acc, ds, max_rows, start: dt.datetime, days: int, status="approved"):
        tid[0] += 1
        t = {"ticket_id": f"T-{2600 + tid[0]}", "requester_account_id": acc, "dataset_id": ds, "purpose": r.choice(["model refresh", "regulator request", "migration", "audit sample", "campaign analysis"]),
             "max_rows": max_rows, "valid_from": iso(start), "valid_to": iso(start + dt.timedelta(days=days)), "status": status,
             "approver_id": f"u{1001 + r.randint(0, 5)}", "created_at": iso(start - dt.timedelta(days=r.randint(1, 5)))}
        tickets.append(t)
        return t

    eid = [0]

    def event(acc, ds, rows, ts: dt.datetime, ticket_ref=None, dest_type=None):
        eid[0] += 1
        dest_type = dest_type or r.choice(["internal-share", "internal-share", "internal-share", "email", "cloud-bucket"])
        dest = {"internal-share": f"smb://files.corp.example/exports/{acc}/", "email": f"{acc}@example.com", "cloud-bucket": f"s3://exports-{r.randint(100, 999)}.example/", "usb": "removable:/E:"}[dest_type]
        e = {"event_id": f"E{eid[0]:05d}", "ts": iso(ts), "account_id": acc, "dataset_id": ds, "rows_exported": rows, "destination_type": dest_type,
             "destination": dest, "client_ip": f"10.{r.randint(0, 20)}.{r.randint(0, 255)}.{r.randint(1, 254)}", "tool": r.choice(TOOLS),
             "ticket_ref": ticket_ref or "", "sample_record_ids": ",".join(f"C{r.randint(100000, 999999)}" for _ in range(3))}
        events.append(e)
        return e

    for _ in range(25):
        a = r.choice(accounts[:36]); ds = r.choice(DATASETS)[0]
        ticket(a["account_id"], ds, r.choice([1000, 5000, 20000]), T0 + dt.timedelta(days=r.randint(0, 20)), r.randint(3, 14), r.choice(["approved"] * 6 + ["expired", "rejected", "pending"]))
    for _ in range(260):  # benign background: in hours, under cap
        a = r.choice(accounts[:36]); d = r.choice(DATASETS)
        p = pol.get((a["role"], d[0]))
        if not p or not p["allowed"]:
            continue
        rows = r.randint(1, max(1, int(p["max_rows_without_ticket"] * 0.6)))
        ts = T0 + dt.timedelta(days=r.randint(0, 21), hours=r.randint(8, 18), minutes=r.randint(0, 59))
        event(a["account_id"], d[0], rows, ts)

    # ---- alerts with injected scenarios
    alerts, truth = [], []
    aid = [0]

    def alert(e, rule, scenario, verdict, reason, severity="high"):
        aid[0] += 1
        alerts.append({"alert_id": f"AL-{aid[0]:03d}", "event_id": e["event_id"], "raised_at": iso(dt.datetime.fromisoformat(e["ts"]) + dt.timedelta(minutes=7)),
                       "rule": rule, "severity": severity, "status": "open"})
        truth.append({"alert_id": f"AL-{aid[0]:03d}", "scenario": scenario, "expected_verdict": verdict, "expected_reason": reason})

    named = {e["scope_value"] for e in exceptions if e["scope_type"] == "account"}

    def pick(role, ds, status="active"):
        # accounts with a named exception are reserved for the exception scenario, so other scenarios stay unambiguous
        return r.choice([a for a in accounts if a["role"] == role and a["status"] == status and a["account_id"] not in named
                         and (role, ds) in pol and pol[(role, ds)]["allowed"]])

    night = lambda day: T0 + dt.timedelta(days=day, hours=r.choice([1, 2, 3, 22, 23]), minutes=r.randint(0, 59))

    # S1 x2
    for day, (role, ds) in [(14, ("analyst", "DS01")), (16, ("engineer", "DS03"))]:
        a = pick(role, ds); ts = night(day); t = ticket(a["account_id"], ds, 20000, ts - dt.timedelta(days=2), 7)
        e = event(a["account_id"], ds, r.randint(6000, 15000), ts, ticket_ref=t["ticket_id"])
        alert(e, "off-hours bulk export of restricted/confidential data", "S1 valid_ticket", "compliant", "approved ticket covers account, dataset, rows and time")
    # S2a expired
    a = pick("analyst", "DS02"); ts = night(17); t = ticket(a["account_id"], "DS02", 50000, ts - dt.timedelta(days=12), 5, status="approved")
    e = event(a["account_id"], "DS02", 30000, ts, ticket_ref=t["ticket_id"])
    alert(e, "off-hours bulk export of restricted data", "S2a expired_ticket", "investigate", "cited ticket expired before the export")
    # S2b scope mismatch (rows)
    a = pick("finance", "DS02"); ts = night(18); t = ticket(a["account_id"], "DS02", 5000, ts - dt.timedelta(days=1), 7)
    e = event(a["account_id"], "DS02", 48000, ts, ticket_ref=t["ticket_id"])
    alert(e, "rows exported exceed ticket scope", "S2b scope_mismatch", "investigate", "rows exported exceed the ticket's max_rows")
    # S3 x2 exception clause
    a = pick("data-steward", "DS01"); ts = T0 + dt.timedelta(days=19, hours=2)
    e = event(a["account_id"], "DS01", 42000, ts)
    alert(e, "off-hours bulk export of restricted data", "S3 policy_exception", "compliant", "policy exception X01 (incident-response window) covers role and dataset at that time")
    a = by_id["u1014"]; a["role"] = "analyst"; pol.setdefault(("analyst", "DS04"), pol[("analyst", "DS04")]); ts = T0 + dt.timedelta(days=15, hours=23)
    e = event("u1014", "DS04", 55000, ts, dest_type="cloud-bucket")
    alert(e, "bulk export to external bucket", "S3 policy_exception", "compliant", "account-scoped exception X03 covers the export")
    # S4 x2 no authorization
    a = pick("support", "DS03"); ts = night(20)
    e = event(a["account_id"], "DS03", 4800, ts, dest_type="email")
    alert(e, "off-hours export emailed outside hours", "S4 no_authorization", "investigate", "no ticket, no exception, policy requires a ticket above the cap")
    a = pick("engineer", "DS01"); ts = night(12)
    e = event(a["account_id"], "DS01", 9000, ts)
    alert(e, "off-hours bulk export of restricted data", "S4 no_authorization", "investigate", "no ticket and no exception clause")
    # S5a unknown account
    ts = night(13); e = event("u2099", "DS02", 12000, ts)
    alert(e, "export by account not in directory", "S5a unknown_account", "cannot-confirm", "account not found in the directory; identity cannot be established")
    # S5b missing policy (data-steward x DS05 on even seeds; contractor x DS02 otherwise)
    if seed % 2 == 0:
        a = r.choice([x for x in accounts if x["role"] == "data-steward" and x["status"] == "active"]); ds = "DS05"
    else:
        a = r.choice([x for x in accounts if x["role"] == "contractor" and x["status"] == "active"]); ds = "DS02"
    ts = night(11); e = event(a["account_id"], ds, 900, ts)
    alert(e, "off-hours export of restricted data", "S5b missing_policy", "cannot-confirm", "no policy row for the role and dataset; entitlement cannot be determined")
    # S5c ticket reference not found
    a = pick("analyst", "DS04"); ts = night(21); e = event(a["account_id"], "DS04", 7000, ts, ticket_ref="T-9999")
    alert(e, "off-hours bulk export citing a ticket", "S5c ticket_not_found", "cannot-confirm", "the cited ticket does not exist in the ticket system")
    # S6 terminated account
    a = accounts[37]; ts = T0 + dt.timedelta(days=12, hours=14)
    e = event(a["account_id"], "DS01", 3000, ts, dest_type="cloud-bucket")
    alert(e, "export by terminated account", "S6 terminated_account", "investigate", "account terminated before the export")
    # S7 within policy (false positive on hours)
    a = pick("analyst", "DS06"); ts = night(10); e = event(a["account_id"], "DS06", 2000, ts)
    alert(e, "off-hours export", "S7 within_policy", "compliant", "internal dataset, rows under the no-ticket cap; hours rule does not apply below the cap", severity="low")

    # ground-truth guard: a background ticket must not accidentally authorise an injected alert
    injected = {(e["account_id"], e["dataset_id"]) for e in events if any(al["event_id"] == e["event_id"] for al in alerts)}
    cited = {e["ticket_ref"] for e in events if e.get("ticket_ref")}
    tickets = [t for t in tickets if t["ticket_id"] in cited or (t["requester_account_id"], t["dataset_id"]) not in injected]
    out["tickets"] = sorted(tickets, key=lambda t: t["ticket_id"])
    out["export_events"] = sorted(events, key=lambda e: e["ts"])
    out["alerts"] = alerts
    out["ground_truth"] = truth
    return out


SOURCE_OF = {"accounts": "logs", "export_events": "logs", "alerts": "logs", "datasets": "policy", "policies": "policy",
             "policy_exceptions": "policy", "field_sensitivity": "policy", "tickets": "tickets"}


def write(out: dict[str, list[dict]], dest: Path, seed: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, rows in out.items():
        with open(dest / f"{name}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    (dest / "SEED").write_text(f"{seed}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--seed", type=int, default=7); ap.add_argument("--out", default=str(Path(__file__).parent / "sample"))
    a = ap.parse_args()
    data = gen(a.seed); write(data, Path(a.out), a.seed)
    for k, v in data.items():
        print(f"{k:18s} {len(v):5d} rows  -> {SOURCE_OF.get(k, 'ground truth, not loaded')}")
