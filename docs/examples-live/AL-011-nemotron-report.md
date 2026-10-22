# Export-alert triage: AL-011

**Verdict: cannot-confirm** · confidence 0.60 (medium)

Confidence: evidence completeness 1.00; second opinion agrees; critic 9/9; 1 open gap(s)

Why:
- the only authorisation the exporter cites cannot be found, and nothing else covers the export

Verdict: cannot-confirm — the exporter’s only cited authorisation (ticket T‑9999) cannot be found, and no other ticket or policy exception covers this export. Account u1022, an active analyst in customer‑care, exported 7,000 rows of the confidential, PII‑containing dataset DS04 (marketing_leads) at 22:25 on 2026‑10‑22 via notebook to an external email address, exceeding the no‑ticket cap of 5,000 rows and occurring outside advisory hours (07:00–19:00). Policy P004 permits analysts to export DS04 but only within the row limit and advisory hours unless a valid ticket or exception applies; neither exists here. The cited ticket T‑9999 does not exist in the ticket system, and no other ticket for u1022 on DS04 covers this export. Next steps: ask the exporter for the approval they relied on and check the ticket system’s audit log for a deleted ticket.

## Evidence (table.column | query step)
- AL-011 (high, rule: off-hours bulk export citing a ticket): account u1022 exported 7000 rows of DS04 at 2026-10-22 22:25:00 via notebook to a email destination; ticket cited: T-9999.  [dexa.export_events.rows_exported | q1]
- Account u1022: role analyst, department customer-care, status active since 2026-01-12 00:00:00.  [dexa.accounts.status | q2]
- Dataset DS04 (marketing_leads) is classified confidential, owner marketing, contains_pii=True.  [dexa.datasets.sensitivity | q4]
- Policy P004: role analyst may export DS04; no-ticket cap 5000 rows; advisory hours 07:00-19:00 (export was outside them).  [dexa.policies.max_rows_without_ticket | q5]
- 0 other ticket(s) for u1022 on DS04; none covers this export.  [dexa.tickets.status | q7]
- No policy exception covers role analyst or account u1022 on DS04 at 2026-10-22 22:25:00.  [dexa.policy_exceptions.exception_id | q8]

## Cannot be confirmed from the sources
- cited ticket T-9999 does not exist in the ticket system

## Suggested next checks
- ask the exporter for the approval they relied on; check the ticket system's audit log for a deleted ticket

## Access footprint (disclosure check)
- tables accessed: accounts, alerts, datasets, export_events, field_sensitivity, policies, policy_exceptions, tickets
- tables that contributed to the verdict: accounts, alerts, datasets, export_events, field_sensitivity, policies, policy_exceptions, tickets
- accessed but not needed: none
- sensitive columns touched: none
- values redacted in this report: 0

## Second opinion (Evaluator, model-backed)
- agrees: model verdict cannot-confirm vs rules cannot-confirm (nvidia/nemotron-3.5-lightning-30b-a3b)
  - Export of 7000 rows exceeds the no-ticket cap of 5000 rows per policy P004
  - Export occurred outside advisory hours (22:25, outside 07:00-19:00)
  - Cited ticket T-9999 does not exist in the ticket system

## Review (Critic, 0-3 x 3 rubric)
- accuracy 3, appropriateness 3, actionability 3 = 9/9 (pass; attempt 2); fix applied: Add a concrete next step such as asking the exporter for the approval they relied on and checking the ticket system's audit log for a deleted ticket.

## Investigation timeline
-    0.3s · planner · fetch alert
-    0.9s · retriever · q1 alert: 1 rows from alerts, export_events (zetaris, 590 ms)
-    0.9s · evaluator · needs more: sensitivity, account, dataset
-    0.9s · planner · fetch account
-    1.1s · retriever · q2 account: 1 rows from accounts (zetaris, 161 ms)
-    1.1s · evaluator · needs more: sensitivity, dataset, policy
-   10.9s · planner · fetch sensitivity, dataset, policy (model drafted 2 SQL)
-   11.1s · retriever · q3 sensitivity: 9 rows from field_sensitivity (zetaris, 276 ms)
-   12.1s · retriever · q4 dataset: 1 rows from datasets (zetaris, 930 ms)
-   12.2s · retriever · q5 policy: 1 rows from policies (zetaris, 164 ms)
-   12.2s · evaluator · needs more: ticket_cited
-   14.7s · planner · fetch ticket_cited (model drafted 1 SQL)
-   14.9s · retriever · q6 ticket_cited: 0 rows from tickets (zetaris, 222 ms)
-   14.9s · evaluator · needs more: tickets_account, exceptions
-   60.0s · planner · fetch tickets_account, exceptions (model drafted 2 SQL)
-   60.3s · retriever · q7 tickets_account: 0 rows from tickets (zetaris, 320 ms)
-   60.5s · retriever · q8 exceptions: 0 rows from policy_exceptions (zetaris, 232 ms)
-   60.5s · evaluator · verdict cannot-confirm
-  132.1s · evaluator · second opinion cannot-confirm (agrees; nvidia/nemotron-3.5-lightning-30b-a3b)
-  183.2s · critic · rubric 6/9 attempt 1, fix: Add a concrete next step such as asking the exporter for the approval they relied on and checking the ticket system's audit log for a deleted ticket.
-  183.2s · reporter · revised the summary once
-  297.7s · critic · rubric 9/9 attempt 2

## Memory updated (H-MEM, with provenance)
- created: `mem-1` AL-011 (high, rule: off-hours bulk export citing a ticket): account u1022 exported 7000 rows of DS04 at 2026-10-22 22:25:00 via notebook to   <dexa.export_events via q1>
- created: `mem-2` Account u1022: role analyst, department customer-care, status active since 2026-01-12 00:00:00.  <dexa.accounts via q2>
- created: `mem-3` Dataset DS04 (marketing_leads) is classified confidential, owner marketing, contains_pii=True.  <dexa.datasets via q4>
- created: `mem-4` Policy P004: role analyst may export DS04; no-ticket cap 5000 rows; advisory hours 07:00-19:00 (export was outside them).  <dexa.policies via q5>
- created: `mem-5` The exporter cited ticket T-9999, which does not exist in the ticket system.  <dexa.tickets via q6>
- created: `mem-6` 0 other ticket(s) for u1022 on DS04; none covers this export.  <dexa.tickets via q7>
- created: `mem-7` No policy exception covers role analyst or account u1022 on DS04 at 2026-10-22 22:25:00.  <dexa.policy_exceptions via q8>
- created: `mem-8` verdict cannot-confirm  <evaluator>

_trace: traces/20261022T193903Z-0d1233.json | evaluator rounds: 3 | data: zetaris | model: meterless-nightshift_

