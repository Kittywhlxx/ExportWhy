"""What the agent is allowed to know about the data layer: the `dexa` schema.

Identical in Zetaris mode (schemastore container `dexa` over three JDBC datasources, see
data/zetaris/register.py) and SAMPLE_MODE (SQLite attached as `dexa`).
"""

from __future__ import annotations

import re

SCHEMA = "dexa"

TABLES: dict[str, dict] = {
    "alerts": {
        "system": "logs (SIEM alert queue)",
        "columns": ["alert_id", "event_id", "raised_at", "rule", "severity", "status"],
        "about": "alerts raised on export events",
    },
    "export_events": {
        "system": "logs (data-export audit log)",
        "columns": [
            "event_id",
            "ts",
            "account_id",
            "dataset_id",
            "rows_exported",
            "destination_type",
            "destination",
            "client_ip",
            "tool",
            "ticket_ref",
            "sample_record_ids",
        ],
        "about": "every export: who, when, which dataset, how many rows, where to, and the ticket the exporter cited",
    },
    "accounts": {
        "system": "logs (identity directory extract)",
        "columns": [
            "account_id",
            "display_name",
            "role",
            "department",
            "manager_id",
            "status",
            "status_since",
            "email",
            "phone",
        ],
        "about": "accounts, their role and employment status",
    },
    "datasets": {
        "system": "policy (data catalog)",
        "columns": [
            "dataset_id",
            "name",
            "sensitivity",
            "owner_team",
            "record_count",
            "contains_pii",
        ],
        "about": "datasets and their sensitivity class",
    },
    "policies": {
        "system": "policy (data-access policy)",
        "columns": [
            "policy_id",
            "role",
            "dataset_id",
            "allowed",
            "allowed_hours_start",
            "allowed_hours_end",
            "max_rows_without_ticket",
            "requires_ticket",
            "notes",
        ],
        "about": "per role x dataset: may export, the no-ticket row cap, the advisory hours window",
    },
    "policy_exceptions": {
        "system": "policy (standing exceptions)",
        "columns": [
            "exception_id",
            "scope_type",
            "scope_value",
            "dataset_id",
            "clause",
            "valid_from",
            "valid_to",
            "approved_by",
        ],
        "about": "time-boxed exception clauses for a role or a named account on a dataset",
    },
    "field_sensitivity": {
        "system": "policy (data classification)",
        "columns": ["table_name", "column_name", "level"],
        "about": "which columns are pii / confidential / internal; drives redaction and the disclosure check",
    },
    "tickets": {
        "system": "tickets (approval workflow)",
        "columns": [
            "ticket_id",
            "requester_account_id",
            "dataset_id",
            "purpose",
            "max_rows",
            "valid_from",
            "valid_to",
            "status",
            "approver_id",
            "created_at",
        ],
        "about": "export approvals: who may export which dataset, up to how many rows, in which window",
    },
}


def schema_card() -> str:
    lines = [
        f"Schema `{SCHEMA}` (query tables as {SCHEMA}.<table>; read-only SQL; ANSI SELECT/JOIN only; select only the columns you need):"
    ]
    for name, t in TABLES.items():
        lines.append(
            f"- {SCHEMA}.{name} [{t['system']}]: {t['about']}. Columns: {', '.join(t['columns'])}"
        )
    return "\n".join(lines)


def tables_in_sql(sql: str) -> list[str]:
    found = re.findall(rf"\b{SCHEMA}\.([a-z_]+)", sql, flags=re.I)
    return sorted({f.lower() for f in found if f.lower() in TABLES})
