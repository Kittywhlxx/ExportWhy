# Accuracy across seeds

12 seeds x 13 alerts = 156 triages, offline (SQLite twin, rule verdicts, stub model). **156/156 verdicts match the generator's ground truth.**

| seed | alerts | correct | misses |
|---|---|---|---|
| 1 | 13 | 13 | - |
| 2 | 13 | 13 | - |
| 3 | 13 | 13 | - |
| 5 | 13 | 13 | - |
| 7 | 13 | 13 | - |
| 11 | 13 | 13 | - |
| 13 | 13 | 13 | - |
| 19 | 13 | 13 | - |
| 23 | 13 | 13 | - |
| 42 | 13 | 13 | - |
| 99 | 13 | 13 | - |
| 2026 | 13 | 13 | - |

## Confusion by injected scenario (rows: scenario, columns: predicted verdict)

| scenario | expected | compliant | investigate | cannot-confirm |
|---|---|---|---|---|
| expired_ticket | investigate | 0 | 12 | 0 |
| missing_policy | cannot-confirm | 0 | 0 | 12 |
| no_authorization | investigate | 0 | 24 | 0 |
| policy_exception | compliant | 24 | 0 | 0 |
| scope_mismatch | investigate | 0 | 12 | 0 |
| terminated_account | investigate | 0 | 12 | 0 |
| ticket_not_found | cannot-confirm | 0 | 0 | 12 |
| unknown_account | cannot-confirm | 0 | 0 | 12 |
| valid_ticket | compliant | 24 | 0 | 0 |
| within_policy | compliant | 12 | 0 | 0 |

Every row should have its whole count in the expected column. The generator guarantees that a background ticket never accidentally authorises an injected alert and that accounts with a named exception are reserved for the exception scenario; both guards were added after the sweep caught exactly those two leaks on seeds 11 and 3.
