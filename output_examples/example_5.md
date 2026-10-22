# Export-alert triage: AL-013

**Verdict: compliant**

Why:
- 2000 rows are within the no-ticket cap of 50000; the hours window is advisory below the cap

## Evidence (table.column | query step)
- AL-013 (low, rule: off-hours export): account u1030 exported 2000 rows of DS06 at 2026-10-11 02:31:00 via sql-console to a internal-share destination; ticket cited: none.  [dexa.export_events.rows_exported | q1]
- Account u1030: role analyst, department customer-care, status active since 2024-07-15 00:00:00.  [dexa.accounts.status | q2]
- Dataset DS06 (product_catalog) is classified internal, owner sales, contains_pii=False.  [dexa.datasets.sensitivity | q4]
- Policy P006: role analyst may export DS06; no-ticket cap 50000 rows; advisory hours 07:00-19:00 (export was outside them).  [dexa.policies.max_rows_without_ticket | q5]

## Access footprint (disclosure check)
- tables accessed: accounts, alerts, datasets, export_events, field_sensitivity, policies
- tables that contributed to the verdict: accounts, alerts, datasets, export_events, field_sensitivity, policies
- accessed but not needed: none
- sensitive columns touched: none
- values redacted in this report: 0

_trace: traces/example-5.json | evaluator rounds: 1 | data: sqlite | model: stub_
