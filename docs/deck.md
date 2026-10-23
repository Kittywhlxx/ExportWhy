# ExportWhy — pitch deck content

GenAI · product: ExportWhy · Open Agent Hackathon 2026, Track 2 "The Agent That Can Explain Why" · solo entry
Framing: a business-plan style pitch. GenAI is the company; ExportWhy is the skill it sells to security
teams. 12 slides, 16:9. Each slide: title, on-slide text, visual, speaker notes (≤40 s).
The deck is read by the judges on its own; the 1–4 minute video does not walk through it (see
`video-script.md`: 30 s of problem framing, then the system running).
Numbers are from the repository at submission time; re-run `evaluate`, `seed_sweep.py` and
`ablation_disclosure.py` before exporting and replace any figure that changed. The organiser's checklist
(problem and users · solution · agent workflow/architecture · technical stack · how Zetaris and Meterless
were used · what was unique · product visuals · future enhancements) maps onto slides 2–12.

---

## 1. Title

**GenAI · ExportWhy** — the agent that explains why a data-export alert needs investigation

Sub: three systems that never talk to each other · one verdict · every evidence line cited · what could not be confirmed, said out loud

Visual: the product banner (UI screenshot: the `AL-004 → INVESTIGATE` banner with its reasons, and the metrics row `evidence/review score 1.00 · queue P1 (8/10)`).

Notes: One sentence: a security analyst gets an alert that someone exported 48,000 payment records at 3 a.m. Was it authorised? The answer lives in three systems. ExportWhy investigates all three and tells the analyst what it found, what it could not confirm, and what to check next.

---

## 2. The problem

One alert from the SIEM: *"account u1012 exported 48,000 rows of payment_transactions to an external bucket at 03:30, citing ticket T-2629."*

Was it authorised? The evidence is in three places that do not know about each other:
- the **export audit log** says what happened (who, what, how much, where to, which ticket was cited)
- the **data-access policy** says what is allowed (role, dataset, no-ticket cap, hours)
- the **approval-ticket system** says what was approved (requester, dataset, rows, validity, approver)

The failure mode is not "no data". It is "approved" taken at face value: T-2629 is approved, but for 5,000 rows, not 48,000.

Visual: the alert text on a plain slide; three disconnected boxes (logs · policy · tickets) with the analyst in the middle.

Notes: Keep it concrete. Say the ticket is real and approved, and still does not cover the export. That is the whole product in one example.

---

## 3. Target customers and their pain

**Who buys:** security operations / data-protection teams in organisations that have a data-access policy and a ticketed approval process, and handle regulated data (payments, personal data).
**Who uses it:** the analyst on the DLP / insider-risk queue. Closing or escalating the alert stays their decision.
**Who else benefits:** compliance and audit, who later ask "on what evidence was this alert closed?"

| pain | what it costs them |
|---|---|
| every alert needs the same three lookups, by hand | queue time; calls differ by who happens to be on shift |
| "approved" is accepted without checking scope and expiry | an uncovered export is closed as compliant |
| the evidence that is missing is not written down | "cannot confirm" is silently turned into "probably fine" or "escalate everything" |
| investigations read more sensitive columns than the decision needs | the triage itself becomes a disclosure |
| no memory across alerts on the same account | a repeat pattern looks like a first offence |

Visual: the table, one row highlighted per sentence of the voice-over.

Notes: Not the customer: the person who exported the data, and teams without a policy or a ticket process (two of the three sources would be missing).

---

## 4. What ExportWhy does about each pain

| pain | feature |
|---|---|
| three lookups by hand | one alert id in; the agent fetches evidence class by class (policy, cited ticket, other tickets, standing exceptions, baseline, destination), through one SQL schema |
| "approved" at face value | the cited ticket is checked field by field: account, dataset, rows, validity window, status; a ticket that does not cover the export is a **conflict**, written as such |
| missing evidence disappears | three verdicts, not two: **compliant / investigate / cannot-confirm**; what could not be confirmed is listed, with the next checks and an owner |
| over-reading | column-scoped queries; a disclosure footprint on every report (tables accessed, sensitive columns touched, tables fetched but unused) |
| no memory | the account's earlier alerts and status changes are read before planning; repeat patterns and status conflicts are reported |
| trust | every evidence line cites the table, column and query step; personal data is redacted in the report and kept in the trace; analysts correct facts and verdicts, and the correction is logged |

Visual: one report, annotated (verdict line, an evidence line with `[dexa.tickets.valid_to | q6]`, "Conflicts", "Cannot be confirmed", "Access footprint").

Notes: Read one evidence line aloud so the judges hear the citation format. Point at "cannot be confirmed": unresolved evidence is separated from a grounded disposition instead of being forced into yes/no.

---

## 5. How it differs from what exists

| | Microsoft Purview DLP triage agent | Mimecast Mihra (Incydr) | general "AI SOC analyst" agents | **ExportWhy** |
|---|---|---|---|---|
| question answered | how urgent is this alert (Needs attention / Less urgent / Not categorised) | DLP alert triage | investigate any alert type | **was this export authorised, and on what evidence** |
| evidence sources | the vendor's own M365 and endpoint telemetry | the vendor's own platform | the vendor's integrations | **three independent systems, federated through Zetaris; cross-source joins run in the data layer** |
| output | a priority bucket | a triage result | an investigation report and priority | **verdict + cited evidence lines + conflicts + what could not be confirmed + next checks + disclosure footprint** |
| role of the model | the model decides | the model decides | the model decides | **rules are the floor; the model drafts SQL (template-gated), gives a second opinion (can only escalate) and reviews the prose (can only edit)** |
| what is not read | not stated | not stated | not stated | **column-scoped: 96% fewer distinct sensitive-column reads than a naive planner, same verdicts** |

Sources for the first two columns: Microsoft Learn (Copilot in Purview agents overview) and Mimecast's product announcement; the third column describes the category, not one vendor.

Visual: the table.

Notes: Do not say "nobody does this": alert triage automation is an industry direction and the judges will know it. Say what is different: the question (authorised or not, not just urgent), the evidence granularity, independent sources through a federated layer, and a model that cannot loosen a conclusion. The last point is what Alert-Bench ([NeurIPS 2026 AIWILD workshop](https://neurips.cc/virtual/2026/168137)) found commercial revision stages get wrong: they overturn more correct decisions than they repair.

---

## 6. System architecture

```
                     logsdb        policydb        ticketsdb        (three Postgres databases)
                        └──────────────┼──────────────┘
                                   Zetaris  ·  one schema `dexa`, cross-source joins
                                       │
   ┌───────────────────────────────────┼─────────────────────────────────────────┐
   │  agent loop (≤3 evidence rounds, ≤9 model calls, 480 s budget)              │
   │  Planner ─▶ Retriever ─▶ Evaluator (rules) ─▶ second opinion ─▶ needs-human │
   │     ▲            │                                                          │
   │     └── "insufficient / conflict: fetch X" ◀────────────────────────────────┘
   │  Reporter (proposer) ⇄ Critic (0-3 × 3 rubric, one revision)                │
   └──────┬──────────────────────┬─────────────────────────┬──────────────────────┘
       H-MEM                World Model              Nightshift ─▶ Nemotron
   memory + trust ledger   account entities, history   model provider with receipts
                                       │
                    report · JSON trace · JSONL log · CLI / HTTP / Streamlit UI
```

- not a pipeline: the Evaluator sends the Planner back, up to 3 rounds, and the next evidence class depends on what came back (a cited ticket that fails a check triggers the search for other tickets and exceptions)
- the model is advisory; the rules are the floor
- shared state: H-MEM remembers findings with provenance; the World Model carries the account across alerts

Visual: the diagram, with the feedback arrow highlighted.

Notes: Name the pairings for the judges' checklist: Planner + Doer, Evaluator critiques and sends back, Proposer + Critic on the summary, shared structured memory, fallback on source failure, traceable evidence in every answer. Autonomy level L2: loop with checks, analyst in the loop.

---

## 7. Workflow and technical stack: why each piece

| step | component | why this choice |
|---|---|---|
| reach three systems with one query language | **Zetaris** (JDBC datasources, schemastore container, Lightning SQL) | the agent never opens a source connection; governance and the cross-source joins live in the data layer, not in agent code |
| remember findings with provenance and let analysts correct them | **Meterless H-MEM** | memory records carry source, confidence and a trust ledger; feedback changes ranking next time instead of being lost |
| know the account across alerts | **Meterless World Model** | entities and contexts with event-time provenance; the second alert on an account sees the first |
| draft SQL, second-opinion the verdict, review the prose | **NVIDIA Nemotron through Meterless Nightshift** | one OpenAI-compatible provider with routing receipts; every call shows which model served it |
| decide | **rules in Python** | auditable, testable, deterministic; the model can tighten, never loosen |
| run anywhere | Python 3.11, SQLite twin for offline mode, Docker image, Streamlit UI | judges can run the whole loop with no network in 30 seconds |

Visual: the workflow as a horizontal strip (alert → plan → fetch → evaluate → second opinion → summarise → review → report) with the component logo under each step.

Notes: This slide sells the sponsors: Zetaris is the only tool the agent has for data; both Meterless engines are on the critical path (remove either and a feature stops working); Nemotron does three jobs with receipts.

---

## 8. Zetaris: what it provides here

- three independent Postgres databases (logs, policy, tickets) registered as **three JDBC datasources**
- one schemastore container `dexa` with a view per table: the agent writes ordinary SQL; the policy and cited-ticket lookups are **cross-source joins executed by Zetaris** (logs ⋈ policy, logs ⋈ tickets), the rest are scoped single-source queries
- the agent never connects to a source directly; the data layer is its only tool
- 15 registration statements, scripted; the same schema is served offline from a SQLite twin (`SAMPLE_MODE`)
- verified: 13/13 alerts on the federated backend, one trace per alert in `docs/examples-live/zetaris-rules/`

Visual: Zetaris Data Explorer screenshot showing the three datasources and the `dexa` container; one federated query with its result.

Notes: This is the Track-2 shape: the evidence is spread across systems and the agent reaches all of them through one governed access point.

---

## 9. Meterless: two engines on the critical path, Nightshift as the provider

| | what it does here | proof |
|---|---|---|
| **H-MEM** | every finding stored with source and provenance; a re-run re-verifies (+confidence), a changed fact supersedes the old one; analyst feedback (helpful / wrong, verdict agree / disagree) goes to the trust ledger | Python implementation of the spec; the official conformance vectors reproduced in pytest (13 cases, byte-identical vectors file) |
| **World Model** | accounts, datasets, tickets, exceptions as entities; alerts and events as contexts; the second alert on an account knows about the first; re-runs append nothing | official runner's 8 cases reproduced in pytest; idempotency and persistence tests |
| **Nightshift → Nemotron** | primary AI provider via the OpenAI-compatible runtime contract (bearer token, `meterless-app`, `meterless-scope`), `GET /v1/models` as the online check, bounded retries; Nemotron drafts the evidence SQL (a draft runs only if canonically identical to the vetted template), gives the second opinion blind to the rules' answer, scores the summary as the Critic | every successful call logs `routed_model` and `receipt_id` (`nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3.5-lightning-30b-a3b` in the recorded runs) |

Visual: the UI "Correct the agent" section (feedback buttons + ledger history), the World state block, and a JSONL excerpt with `model` and `receipt_id`.

Notes: Stress "critical path": remove either engine and a feature stops working (cross-alert context, feedback with audit trail). Name the design rule: the model can make the verdict more conservative or improve the prose; it cannot flip a block to compliant.

---

## 10. Does it work? Validation

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

## 11. The product

- single-page UI: verdict banner with reasons · evidence/review score · queue · evidence · conflicts · footprint · investigation timeline · feedback buttons · outage switch
- CLI and HTTP entry point (`run.py`: JSON in, JSON out, structured errors), Docker image, sample mode with no network
- every run: readable log on stderr, structured CLI output on stdout, `logs/trace-<run_id>.jsonl`, full trace JSON

Visual: UI screenshot with the banner, the metrics row and the timeline column; a second screenshot of the "Correct the agent" section.

Notes: 10 seconds. The UI exists so the analyst can act; the system is the same with or without it.

---

## 12. Limits and phase two

**Limits (stated in the README):** synthetic sources and policies; rules cover the ten injected scenarios; the recorded live runs took 231 s (AL-004, 7 calls) and 298 s (AL-011, 8 recorded calls), with Nightshift routing to a 550B reasoning model for most calls, so latency varies by routing; the official TypeScript conformance runners were ported to pytest, not executed.

**Phase two:**
- real connectors (SIEM, IAM, ticketing) behind the same Zetaris schema
- richer context: device, geo, identity risk, prior incidents on the account
- write-back: open the case, assign, attach the evidence pack
- evaluate on production investigations (CORTEX-style trajectories), not only generated truth
- more alert families on the same loop (privilege grants, share-link exposure), each with its own evidence classes and templates

Visual: roadmap strip.

Notes: End on the one-liner: an analyst can read the verdict, see each evidence line's source, and knows exactly what the agent could not confirm.

---

## Appendix slide (optional): the eight-line agent loop spec

Goal · Plan (≤5 steps) · Tools with fail modes · Memory (scratchpad / decisions / knowledge) · Rubric 0-3×3 · Stop conditions (grounded → report; time budget spent → partial/timed-out result with any unfetched evidence named (cannot-confirm when evidence was skipped); round budget spent → verdict on what was retrieved, unfetched named; unresolved disagreement → needs-human; critic <7/9 → one revision, still failing → deterministic summary) · Run log (JSONL) · Guardrails (3 rounds, ≤9 logical model calls by counter, authorisation lookups run the fixed template, column-scoped, 480 s budget enforced before every query and model call and inside retries, redaction).
