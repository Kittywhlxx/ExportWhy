# Export-alert triage: AL-013

**Verdict: compliant** · evidence/review score 1.00 (high; heuristic, not a probability)

Score factors: evidence completeness 1.00; second opinion agrees; critic 9/9

Why:
- 2000 rows are within the no-ticket cap of 50000; the hours window is advisory below the cap

Verdict compliant: 2000 rows are within the no-ticket cap of 50000; the hours window is advisory below the cap. No further action required.

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

## Second opinion (Evaluator, deterministic stub: offline mode)
- agrees: model verdict compliant vs rules compliant (stub)
  - stub: deterministic second opinion agrees with the rules

## Review (Critic, 0-3 x 3 rubric)
- accuracy 3, appropriateness 3, actionability 3 = 9/9 (pass; attempt 1)

## Investigation timeline
-    0.0s · planner · fetch alert
-    0.0s · retriever · q1 alert: 1 rows from alerts, export_events (sqlite, 0 ms)
-    0.0s · evaluator · needs more: sensitivity, account, dataset
-    0.0s · planner · fetch account
-    0.0s · retriever · q2 account: 1 rows from accounts (sqlite, 0 ms)
-    0.0s · evaluator · needs more: sensitivity, dataset, policy
-    0.0s · planner · fetch sensitivity, dataset, policy
-    0.0s · retriever · q3 sensitivity: 9 rows from field_sensitivity (sqlite, 0 ms)
-    0.0s · retriever · q4 dataset: 1 rows from datasets (sqlite, 0 ms)
-    0.0s · retriever · q5 policy: 1 rows from accounts, export_events, policies (sqlite, 0 ms)
-    0.0s · evaluator · verdict compliant
-    0.0s · evaluator · second opinion compliant (agrees; stub)
-    0.0s · critic · rubric 9/9 attempt 1

## World state (Meterless World Model)
Account u1030 is known to the world model: 1 recorded export(s) across DS06; 0 ticket citation(s).

## Memory updated (H-MEM, with provenance)
- reconfirmed: `mem-99` AL-013 (low, rule: off-hours export): account u1030 exported 2000 rows of DS06 at 2026-10-11 02:31:00 via sql-console to a internal-share de  <dexa.export_events via q1>
- reconfirmed: `mem-100` Account u1030: role analyst, department customer-care, status active since 2024-07-15 00:00:00.  <dexa.accounts via q2>
- reconfirmed: `mem-101` Dataset DS06 (product_catalog) is classified internal, owner sales, contains_pii=False.  <dexa.datasets via q4>
- reconfirmed: `mem-102` Policy P006: role analyst may export DS06; no-ticket cap 50000 rows; advisory hours 07:00-19:00 (export was outside them).  <dexa.policies via q5>
- reconfirmed: `mem-103` verdict compliant  <evaluator>

_trace: traces/example-5.json | evaluator rounds: 1 | data: sqlite | model: stub_
