# Export-alert triage: AL-004

**Verdict: investigate** · evidence/review score 1.00 (high; heuristic, not a probability) · priority P1 (8/10)

Score factors: evidence completeness 1.00; second opinion agrees; critic 9/9

Priority: dataset restricted (+4); 48000 rows (+2); external destination (+2)

Why:
- the cited ticket does not cover this export and no other authorisation does

Verdict: investigate — the cited ticket T‑2629 authorizes only 5,000 rows for DS02, but the export moved 48,000 rows to an external cloud bucket, and no other authorization covers this action. The export occurred outside advisory hours (03:30 vs. 07:00–19:00) and far exceeds the no‑ticket cap of 500 rows for the finance role. Account u1012 has no prior DS02 exports, making this a significant baseline deviation. No policy exception applies to this role or account at the time of export. Nothing remains unconfirmed; next steps: contact ticket approver u1005 and the exporter's manager, and verify the sanction status of the destination bucket <destination redacted>

## Evidence (table.column | query step)
- AL-004 (high, rule: rows exported exceed ticket scope): account u1012 exported 48000 rows of DS02 at 2026-10-19 03:30:00 via sql-console to a cloud-bucket destination; ticket cited: T-2629.  [dexa.export_events.rows_exported | q1]
- Account u1012: role finance, department marketing, status active since 2025-07-03 00:00:00.  [dexa.accounts.status | q2]
- Dataset DS02 (payment_transactions) is classified restricted, owner finance, contains_pii=True.  [dexa.datasets.sensitivity | q4]
- Policy P041: role finance may export DS02; no-ticket cap 500 rows; advisory hours 07:00-19:00 (export was outside them).  [dexa.policies.max_rows_without_ticket | q5]
- Ticket T-2629: requester u1012, dataset DS02, max_rows 5000, approved, valid 2026-10-18 03:30:00 .. 2026-10-25 03:30:00, approver u1005.  [dexa.tickets.valid_to | q6]
- 0 other ticket(s) for u1012 on DS02; none covers this export.  [dexa.tickets.status | q7]
- No policy exception covers role finance or account u1012 on DS02 at 2026-10-19 03:30:00.  [dexa.policy_exceptions.exception_id | q8]
- The export went to an external cloud-bucket destination: <destination redacted>  [dexa.export_events.destination | q10]
- Baseline: 0 earlier export(s) of DS02 by this account, largest 0 rows; this export is far above that baseline.  [dexa.export_events.rows_exported | q9]

## Conflicts (flagged, not silently resolved)
- ticket T-2629 was cited but does not cover the export: 48000 rows exported exceed the ticket's max_rows 5000

## Suggested next checks (owner to be assigned by the analyst; SLA not modelled)
- contact the ticket approver and the exporter's manager; preserve the exported file  _(suggested owner: ticket approver; done when: export file preserved and hash recorded)_
- confirm whether the destination is a sanctioned location; request deletion if not  _(suggested owner: analyst; done when: destination confirmed sanctioned, or deletion confirmed)_

## Access footprint (disclosure check)
- tables accessed: accounts, alerts, datasets, export_events, field_sensitivity, policies, policy_exceptions, tickets
- tables that contributed to the verdict: accounts, alerts, datasets, export_events, field_sensitivity, policies, policy_exceptions, tickets
- accessed but not needed: none
- sensitive columns touched: export_events.destination (confidential)
- values redacted in this report: 2

## Second opinion (Evaluator, model-backed)
- agrees: model verdict investigate vs rules investigate (nvidia/nemotron-3.5-super-vl-120b-a12b)
  - Export of 48,000 rows exceeds the no-ticket cap of 500 rows for role finance on DS02
  - Cited ticket T-2629 is approved and valid at the export time, but its max_rows of 5,000 does not cover the 48,000 rows exported
  - No other ticket for u1012 on DS02 covers this export

## Review (Critic, 0-3 x 3 rubric)
- accuracy 3, appropriateness 3, actionability 3 = 9/9 (pass; attempt 2); fix applied: Include a concrete next step (e.g., contact the ticket approver and exporter's manager, and verify the destination bucket's sanction status).

## Investigation timeline
-    0.4s · planner · fetch alert
-    0.8s · retriever · q1 alert: 1 rows from alerts, export_events (zetaris, 372 ms)
-    0.8s · evaluator · needs more: sensitivity, account, dataset
-    0.8s · planner · fetch account
-    0.9s · retriever · q2 account: 1 rows from accounts (zetaris, 139 ms)
-    0.9s · evaluator · needs more: sensitivity, dataset, policy, ticket_cited
-   11.9s · planner · fetch sensitivity, dataset, policy, ticket_cited (model drafted 3 SQL)
-   12.2s · retriever · q3 sensitivity: 9 rows from field_sensitivity (zetaris, 286 ms)
-   12.3s · retriever · q4 dataset: 1 rows from datasets (zetaris, 148 ms)
-   12.7s · retriever · q5 policy: 1 rows from accounts, export_events, policies (zetaris, 403 ms)
-   13.1s · retriever · q6 ticket_cited: 1 rows from export_events, tickets (zetaris, 346 ms)
-   13.1s · evaluator · needs more: tickets_account, exceptions
-   29.4s · planner · fetch tickets_account, exceptions (model drafted 2 SQL)
-   29.7s · retriever · q7 tickets_account: 1 rows from tickets (zetaris, 296 ms)
-   30.0s · retriever · q8 exceptions: 1 rows from policy_exceptions (zetaris, 255 ms)
-   30.0s · evaluator · needs more: account_history, destination
-   30.0s · planner · fetch account_history, destination
-   30.9s · retriever · q9 account_history: 1 rows from export_events (zetaris, 926 ms)
-   31.1s · retriever · q10 destination: 1 rows from export_events (zetaris, 155 ms)
-   31.1s · evaluator · verdict investigate
-   42.1s · evaluator · second opinion investigate (agrees; nvidia/nemotron-3.5-super-vl-120b-a12b)
-  118.4s · critic · rubric 6/9 attempt 1, fix: Include a concrete next step (e.g., contact the ticket approver and exporter's manager, and verify the destination bucket's sanction status).
-  118.4s · reporter · revised the summary once
-  231.1s · critic · rubric 9/9 attempt 2

## Memory updated (H-MEM, with provenance)
- created: `mem-1` AL-004 (high, rule: rows exported exceed ticket scope): account u1012 exported 48000 rows of DS02 at 2026-10-19 03:30:00 via sql-console to   <dexa.export_events via q1>
- created: `mem-2` Account u1012: role finance, department marketing, status active since 2025-07-03 00:00:00.  <dexa.accounts via q2>
- created: `mem-3` Dataset DS02 (payment_transactions) is classified restricted, owner finance, contains_pii=True.  <dexa.datasets via q4>
- created: `mem-4` Policy P041: role finance may export DS02; no-ticket cap 500 rows; advisory hours 07:00-19:00 (export was outside them).  <dexa.policies via q5>
- created: `mem-5` Ticket T-2629: requester u1012, dataset DS02, max_rows 5000, approved, valid 2026-10-18 03:30:00 .. 2026-10-25 03:30:00, approver u1005.  <dexa.tickets via q6>
- created: `mem-6` 0 other ticket(s) for u1012 on DS02; none covers this export.  <dexa.tickets via q7>
- created: `mem-7` No policy exception covers role finance or account u1012 on DS02 at 2026-10-19 03:30:00.  <dexa.policy_exceptions via q8>
- created: `mem-8` The export went to an external cloud-bucket destination: <destination redacted>  <dexa.export_events via q10>
- created: `mem-9` Baseline: 0 earlier export(s) of DS02 by this account, largest 0 rows; this export is far above that baseline.  <dexa.export_events via q9>
- created: `mem-10` verdict investigate  <evaluator>

_trace: traces/20261023T004337Z-288dc3.json | evaluator rounds: 3 | data: zetaris | model: meterless-nightshift_

