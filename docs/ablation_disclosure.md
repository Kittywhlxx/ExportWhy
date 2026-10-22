# Disclosure ablation

Same 13 alerts (seed 7), same evidence loop and verdict rules; only how the queries are written differs. Counts are distinct sensitive columns read, summed over the 13 triages, classified by `field_sensitivity`.

| config | alerts | pii | confidential | internal | sensitive_total | unused_tables | tables_per_alert | ground_truth |
|---|---|---|---|---|---|---|---|---|
| naive (SELECT *) | 13 | 52 | 26 | 35 | 113 | 0 | 7.2 | 13/13 |
| scoped templates (this system) | 13 | 0 | 5 | 0 | 5 | 0 | 7.2 | 13/13 |

naive = every evidence query is `SELECT *`; scoped = the shipped column-scoped templates. Both rows are reproducible offline. A live-model row is only added with --live-traces on a frozen trace directory, because mixing trace versions is not a like-for-like comparison.
