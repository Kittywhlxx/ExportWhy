# Disclosure ablation

Same 13 alerts (seed 7), same evidence loop and verdict rules; only how the queries are written differs. Counts are distinct sensitive columns read, summed over the 13 triages, classified by `field_sensitivity`.

| config | alerts | pii | confidential | internal | sensitive_total | unused_tables | tables_per_alert | ground_truth |
|---|---|---|---|---|---|---|---|---|
| naive (SELECT *) | 13 | 52 | 26 | 35 | 113 | 0 | 7.2 | 13/13 |
| scoped templates (this system) | 13 | 0 | 3 | 0 | 3 | 0 | 7.2 | 13/13 |
| scoped + Nemotron-drafted SQL (validated) | 13 | 0 | 5 | 0 | 5 | 2 | 7.3 | 13/13 |

naive = every evidence query is `SELECT *`; scoped = the shipped column-scoped templates; the third row is the live run where Nemotron drafted the SQL and the validator kept only drafts that stay read-only, on known tables, on this alert's keys.

Note: the live-run row was recorded before the planner fix that reads the cited ticket before widening the search (78cc844), hence its two unused-table reads; the other two rows are after it.
