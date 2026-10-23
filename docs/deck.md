# ExportWhy — slide deck content

Open Agent Hackathon 2026 · Track 2: The Agent That Can Explain Why · solo entry (Xiao)
Target: 10 slides, 16:9. Each slide below has: title, on-slide text, visual, speaker notes (≤40 s).
Numbers are from the repository at submission time; re-run `evaluate`, `seed_sweep.py` and
`ablation_disclosure.py` before exporting and replace any figure that changed.

---

## 1. Title

**ExportWhy** — an agent that explains why a data-export alert needs investigation

Sub: three systems that never talk to each other · one verdict · every claim cited · what could not be confirmed, said out loud

Visual: the product banner (UI screenshot: the `AL-004 → INVESTIGATE` banner with its reasons, and the metrics row `evidence/review score 1.00 · queue P1 (8/10)`).

Notes: One sentence: a security analyst gets an alert that someone exported 48,000 payment records at 3 a.m. Was it authorised? The answer lives in three systems. ExportWhy investigates all three and tells the analyst what it found, what it could not confirm, and what to check next.

---

## 2. The problem and the user

**User:** a security analyst on a data-protection / insider-risk queue.
**Input:** one alert from the SIEM: *"account u1012 exported 48,000 rows of payment_transactions to an external bucket at 03:30"*.
**Why it is hard:** the evidence is fragmented
- the export audit log says *what* happened
- the data-access policy says *what is allowed* for that role and dataset
- the approval-ticket system says *whether it was approved*, for how many rows, until when
- none of them knows about the others; the analyst joins them by hand, every time

**Cost today:** the workflow must reconcile export logs, policies and approvals by hand; accepting an "approved" ticket without checking its scope and expiry risks clearing an export it does not cover (that is exactly what AL-004 shows). (Time savings have not been measured in this project.)

Visual: three disconnected boxes (logs · policy · tickets) with the analyst in the middle.

Notes: Keep it concrete: show the alert text, name the three systems, say that the failure mode is not "no data" but "data in three places with nobody checking that the ticket actually covers the export".

---

## 3. What ExportWhy does

Given an alert id, it:
1. identifies the export and the account
2. fetches only the evidence the decision needs, class by class (policy, cited ticket, other tickets, exceptions, baseline, destination)
3. decides **compliant / investigate / cannot-confirm**, with a confidence and a queue priority
4. cites the table, column and query step behind every evidence line; reasons, conflicts and next checks are listed separately
5. flags conflicts between sources instead of resolving them silently
6. says what could not be confirmed and the next checks
7. redacts personal data in the report; keeps the references in the trace

Visual: one report, annotated (verdict line, evidence line with `[dexa.tickets.valid_to | q6]`, "Conflicts", "Cannot be confirmed", "Access footprint").

Notes: Read one evidence line aloud so the judges hear the citation format. Point at "cannot be confirmed": ExportWhy explicitly separates unresolved evidence from a grounded disposition instead of forcing a yes/no.

---

## 4. How the agents work together (architecture)

```
alert id ─▶ Planner ─evidence classes─▶ Retriever ─SQL─▶ Zetaris (dexa.*: logs · policy · tickets)
               ▲                            │ rows
               │ "insufficient / conflict:  ▼
               └──── fetch X" ──────── Evaluator (rules) ──▶ second opinion (Nemotron) ──▶ needs-human on disagreement
                                            │ facts · conflicts · gaps · footprint
                                            ▼
                                        Reporter (proposer) ⇄ Critic (0-3 × 3 rubric, one revision)
                                            │
                     H-MEM ◀── findings, re-verified ──┘──▶ World Model (accounts, datasets, tickets, exceptions)
```

- not a pipeline: the Evaluator sends the Planner back, up to 3 rounds, and the next evidence class depends on what came back (a cited ticket that fails a check triggers a search for other tickets and exceptions)
- the model is advisory: drafts SQL (validator-gated), gives a second opinion (can only escalate), scores the summary (can only edit prose); the rules are the floor
- shared state: H-MEM remembers findings with provenance; the World Model carries the account across alerts

Visual: the diagram above, with the feedback arrow highlighted.

Notes: Say explicitly which agentic pairings this is (Planner+Doer, Evidence-Weaver+Synthesizer, Proposer+Critic) and the autonomy level: L2, loop with checks, analyst in the loop.

---

## 5. Zetaris: three sources, one schema

- three independent Postgres databases (logs, policy, tickets) registered as **three JDBC datasources**
- one schemastore container `dexa` with a view per table → the agent writes ordinary SQL; the policy and cited-ticket lookups are **cross-source joins executed by Zetaris** (logs ⋈ policy, logs ⋈ tickets), the rest are scoped single-source queries
- the agent never connects to a source directly; the data layer is the only tool it has
- 15 registration statements, scripted; the same schema is served offline from a SQLite twin (`SAMPLE_MODE`)

Visual: Zetaris Data Explorer screenshot showing the three datasources and the `dexa` container; one federated query with its result.

Notes: This is the Track-2 shape: the evidence is spread across systems and the agent reaches all of them through one governed access point.

---

## 6. Meterless: two engines on the critical path, Nightshift as the provider

| | what it does here | proof |
|---|---|---|
| **H-MEM** | every finding stored with source and provenance; a re-run re-verifies (+confidence), a changed fact supersedes the old one; analyst feedback (helpful / wrong, verdict agree / disagree) goes to the trust ledger | Python implementation of the spec; the official conformance vectors reproduced in pytest (13 cases, byte-identical vectors file) |
| **World Model** | accounts, datasets, tickets, exceptions as entities; alerts and events as contexts; the second alert on an account knows about the first; re-runs append nothing | official runner's 8 cases reproduced in pytest; idempotency and persistence tests |
| **Nightshift** | primary AI provider via the OpenAI-compatible runtime contract (bearer token, `meterless-app`, `meterless-scope`), `GET /v1/models` as the online check, bounded retries | every model call logged with the routed model and compute receipt |

Visual: the UI "Correct the agent" section (feedback buttons + ledger history) and the World state block.

Notes: Stress "critical path": remove either engine and a feature stops working (cross-alert context, feedback with audit trail).

---

## 7. NVIDIA Nemotron: where the model contributes

- proposes the evidence SQL; a draft runs only if it is canonically identical to the vetted template for its evidence class, otherwise the template runs (model proposes, template disposes). In the recorded AL-004 run all five drafts were accepted on those terms
- gives the Evaluator's **second opinion** on the same facts, blind to the rules' answer
- writes the analyst summary and **scores it as the Critic**
- every successful call: `routed_model` and `receipt_id` in the trace (`nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3.5-lightning-30b-a3b` in the recorded runs)

Visual: a JSONL log excerpt with `model` and `receipt_id`; the "Second opinion" and "Review" sections of a report.

Notes: Name the design rule: the model can make the verdict more conservative or improve the prose; it cannot flip a block to compliant. Alert-Bench ([Benchmarking LLM Agents for SOC Alert Triage, NeurIPS 2026 listing](https://neurips.cc/virtual/2026/168137)) reports revision stages that overturn more correct decisions than they repair; this design avoids that.

---

## 8. Does it work? Evidence

| check | result |
|---|---|
| live Zetaris, rule verdicts (one trace per alert in docs/examples-live/zetaris-rules) | 13/13 alerts match ground truth |
| live Zetaris + Nemotron, recorded run AL-004 (docs/examples-live) | verdict matches; all five drafts accepted because identical to the templates; critic 6/9 → revision → 9/9. (AL-011 in the same folder is the earlier recording of the critic loop) |
| 12 seeds × 13 alerts, offline | 156/156 |
| disclosure ablation (distinct sensitive-column reads over 13 synthetic triages) | naive `SELECT *` 113 · this system 5 (96% fewer), identical verdicts |
| failure path | a critical source down never yields compliant: cannot-confirm, or needs-human if the second opinion also disagrees (3 outage tests); budget exhaustion stops retrieval and names the unfetched evidence |
| tests | 64 passed (two engine conformance suites + end-to-end + review regressions) |

Visual: the confusion table (10 scenarios, every row in its expected column) and the ablation bar (113 vs 5).

Notes: Be honest: ground truth is generated with the data, so 156/156 measures consistency on the ten scenarios the generator injects, not real-world detection quality. The ablation is the differentiator: 96% fewer distinct sensitive-column accesses on these 13 synthetic alerts, for the same verdicts.

---

## 9. Product

- CLI and HTTP entry point (`run.py`: JSON in, JSON out, structured errors), Docker image, sample mode with no network
- single-page UI: verdict · confidence · priority · evidence · conflicts · footprint · timeline · feedback buttons · outage switch
- every run: readable log on stderr, structured CLI output on stdout, `logs/trace-<run_id>.jsonl`, full trace JSON

Visual: UI screenshot with the banner, the metrics row and the timeline column.

Notes: 10 seconds. The UI exists so the analyst can act; the system is the same with or without it.

---

## 10. Limits and what comes next

**Limits (stated in the README):** synthetic sources and policies; rules cover the ten injected scenarios; the recorded live runs took 231 s (AL-004, 7 calls) and 298 s (AL-011, 8 recorded calls), with Nightshift routing to a 550B reasoning model for most calls, so latency varies by routing; the official TypeScript conformance runners were ported to pytest, not executed.

**Next:**
- real connectors (SIEM, IAM, ticketing) behind the same Zetaris schema
- richer context: device, geo, identity risk, prior incidents on the account
- write-back: open the case, assign, attach the evidence pack
- evaluate on production investigations (CORTEX-style trajectories), not only generated truth

Visual: roadmap strip.

Notes: End on the one-liner: an analyst can read the verdict, see every claim's source, and knows exactly what the agent could not confirm.

---

## Appendix slide (optional): the eight-line agent loop spec

Goal · Plan (≤5 steps) · Tools with fail modes · Memory (scratchpad / decisions / knowledge) · Rubric 0-3×3 · Stop conditions (grounded → report; time budget spent → cannot-confirm with the unfetched evidence named, round budget spent → verdict on what was retrieved, unfetched named; disagreement → needs-human; critic <7/9 → one revision, still failing → deterministic summary) · Run log (JSONL) · Guardrails (3 rounds, ≤9 logical model calls by counter, authorisation lookups run the fixed template, column-scoped, 480 s budget enforced before every query and model call and inside retries, redaction).
