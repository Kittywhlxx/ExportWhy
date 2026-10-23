# Live Zetaris verification: 13/13 (rule verdicts, stub model)

One `LLM_STUB=true python -m agent.main evaluate` run against the local Zetaris platform (three Postgres
datasources federated through the `dexa` container), recorded 23 Oct 2026 03:25 UTC. The model is stubbed so
the verdicts are the rules' alone; `backend` in every trace is `zetaris`. One full JSON trace per alert is in
this folder (`AL-0NN.json`); each holds every SQL statement, the Zetaris query ids, row counts and the evaluation
rounds. No secrets appear in these files.

| alert | scenario | expected | verdict | rounds | status | run id |
|---|---|---|---|---|---|---|
| AL-001 | S1 valid_ticket | compliant | compliant | 1 | complete | `20261023T032524Z-1c72c7` |
| AL-002 | S1 valid_ticket | compliant | compliant | 1 | complete | `20261023T032526Z-6d2165` |
| AL-003 | S2a expired_ticket | investigate | investigate | 2 | complete | `20261023T032529Z-69f959` |
| AL-004 | S2b scope_mismatch | investigate | investigate | 3 | complete | `20261023T032532Z-8a0908` |
| AL-005 | S3 policy_exception | compliant | compliant | 2 | complete | `20261023T032534Z-279764` |
| AL-006 | S3 policy_exception | compliant | compliant | 2 | complete | `20261023T032536Z-8dbd6e` |
| AL-007 | S4 no_authorization | investigate | investigate | 3 | complete | `20261023T032538Z-0338fe` |
| AL-008 | S4 no_authorization | investigate | investigate | 3 | complete | `20261023T032540Z-118a67` |
| AL-009 | S5a unknown_account | cannot-confirm | cannot-confirm | 1 | complete | `20261023T032543Z-805a53` |
| AL-010 | S5b missing_policy | cannot-confirm | cannot-confirm | 1 | complete | `20261023T032544Z-092e9c` |
| AL-011 | S5c ticket_not_found | cannot-confirm | cannot-confirm | 3 | complete | `20261023T032545Z-bd1d59` |
| AL-012 | S6 terminated_account | investigate | investigate | 1 | complete | `20261023T032548Z-80c548` |
| AL-013 | S7 within_policy | compliant | compliant | 1 | complete | `20261023T032550Z-1e1d79` |

13/13 verdicts match `data/sample/ground_truth.csv`.
