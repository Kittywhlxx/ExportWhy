# ExportWhy — pitch deck content

GenAI · product: ExportWhy · Open Agent Hackathon 2026, Track 2 "The Agent That Can Explain Why" · solo entry
Framing: GenAI presents ExportWhy to a prospective customer. The judges should be able to read it as that
customer: is this my problem, what does it do for me, why is it worth trying, can I trust it. Architecture,
sponsor components and results are there as the reasons to believe, not as a research chapter.
10 slides, 16:9. Each slide: title, on-slide text, visual, speaker notes (≤40 s).
The deck is read by the judges on its own; the 1–4 minute video tells the same story in its own order
(see `video-script.md`) and does not walk through these slides.
Numbers are from the repository at submission time; re-run `evaluate`, `seed_sweep.py` and
`ablation_disclosure.py` before exporting and replace any figure that changed. The organiser's checklist
(problem and users · solution · agent workflow/architecture · technical stack · how Zetaris and Meterless
were used · what was unique · product visuals · future enhancements) maps onto slides 2–10.

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

## 3. Who it is for, and what hurts

**Who buys:** security operations / data-protection teams in organisations that have a data-access policy and a ticketed approval process, and handle regulated data (payments, personal data).
**Who uses it:** the analyst on the DLP / insider-risk queue.
**The decision it supports:** does the authorisation on file cover this export? On the answer, the analyst continues the investigation, fetches what is missing, or hands the alert to human review. Closing or escalating stays their call.
**Who else benefits:** compliance and audit, who later ask "on what evidence was this alert closed?"

| pain | what it costs them |
|---|---|
| every alert needs the same three lookups, by hand | queue time; calls differ by who happens to be on shift |
| "approved" is accepted without checking scope and expiry | an uncovered export is closed as compliant |
| the evidence that is missing is not written down | "cannot confirm" is silently turned into "probably fine" or "escalate everything" |
| investigations read more sensitive columns than the decision needs | the triage itself becomes a disclosure |
| no memory across alerts on the same account | a repeat pattern looks like a first offence |

Visual: the table, one row highlighted per sentence of the voice-over.

Notes: Not the customer: the person who exported the data, and teams without a policy or a ticket process (two of the three sources would be missing). What we can show today is fit to the scenario and the function. Analyst time saved, uncovered exports no longer closed as compliant, and willingness to pay are not measured; they are the pilot targets on slide 10, not results. Do not state them as benefits achieved.

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

## 5. What you get: the product

- single-page UI: verdict banner with reasons · evidence/review score · queue · evidence with citations · conflicts · what could not be confirmed · next checks · access footprint · investigation timeline · "correct the agent" buttons · outage switch for drills
- the same investigation from the command line or over HTTP (`run.py`: JSON in, JSON out, structured errors), as a Docker image, and fully offline in sample mode
- every run leaves a readable log, a JSONL audit log and a full JSON trace: the answer to "why was this alert closed" is on file

Visual: UI screenshot with the banner, the metrics row and the timeline column; a second screenshot of the "Correct the agent" section.

Notes: Show the product before explaining how it is built. Walk the analyst's reading order on the AL-004 screen: the Conflicts line says why the ticket does not cover the export, the evidence line above it carries the table, column and query it came from, Next checks says what to do and who, and the feedback buttons are where a disagreement goes. Only then move on to how it is built. The UI is how the analyst acts; the investigation is the same with or without it.

---

## 6. How it differs from what exists

| | Microsoft Purview DLP triage agent | Mimecast Mihra (Incydr) | general "AI SOC analyst" agents | **ExportWhy** |
|---|---|---|---|---|
| question answered | how urgent is this alert (Needs attention / Less urgent / Not categorised) | DLP alert triage | investigate any alert type | **was this export authorised, and on what evidence** |
| evidence sources | the vendor's own M365 and endpoint telemetry | the vendor's own platform | the vendor's integrations | **three independent systems, federated through Zetaris; cross-source joins run in the data layer** |
| output | a priority bucket | a triage result | an investigation report and priority | **verdict + cited evidence lines + conflicts + what could not be confirmed + next checks + disclosure footprint** |
| how the verdict is reached | not described in the public materials we read | not described in the public materials we read | varies by vendor | **rules decide; the model drafts SQL (template-gated), gives a second opinion (can only escalate) and reviews the prose (can only edit)** |
| what is not read | not stated | not stated | not stated | **column-scoped: 96% fewer distinct sensitive-column reads than a naive planner, same verdicts** |

Sources for the first two columns: Microsoft Learn (Copilot in Purview agents overview) and Mimecast's product announcement; the third column describes the category, not one vendor. "Not described" means exactly that, not a claim about how they work.

Visual: the table.

Notes: Do not say "nobody does this": alert triage automation is an industry direction and the judges will know it. Do not present citations, memory, a third verdict or several roles as inventions either; each exists elsewhere. The originality is the combination: cross-source authorisation checking, evidence fetched on demand, constrained model review and a traceable report, as one investigation workflow. Show it on AL-004: the cited ticket is checked field by field against the export log (cross-source); it fails on rows, which is what triggers the fetch of other tickets and exceptions (on demand); the model's second opinion agrees and could only have escalated (constrained); every line of the report names its query (traceable). The constrained-review choice is motivated by Alert-Bench ([NeurIPS 2026 AIWILD workshop](https://neurips.cc/virtual/2026/168137)), which found that in the agent configurations it benchmarked, a revision stage overturned more correct decisions than it repaired.

---

## 7. How it works: architecture and workflow

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

Workflow: alert id → plan the evidence classes → fetch through Zetaris → evaluate with rules → ask the model for a second opinion → draft the summary → Critic scores it (one revision) → report, memory update, trace.

- not a pipeline: the Evaluator sends the Planner back, up to 3 rounds, and the next evidence class depends on what came back (a cited ticket that fails a check triggers the search for other tickets and exceptions)
- the rules decide; the model is advisory (drafts, second opinion, prose review)
- bounded: 3 rounds, 9 model calls, a 480 s budget checked before every query and model call; a source failure or an exhausted budget is reported, never guessed around

Visual: the diagram, with the feedback arrow highlighted; the workflow as a strip underneath.

Notes: For the judges' checklist: Planner + Doer, Evaluator critiques and sends back, Proposer + Critic on the summary, shared structured memory, fallback on source failure, traceable evidence in every answer. Autonomy level L2: loop with checks, analyst in the loop.

---

## 8. What the sponsor technology gives the customer

| the customer gets | because of | proof: what the demo shows, what the repository holds |
|---|---|---|
| **one investigation checks authorisation evidence spread across three systems**, without the agent ever holding a connection to any of them | **Zetaris**: three Postgres databases as three JDBC datasources, one schemastore container `dexa`; the policy and cited-ticket lookups are cross-source joins executed in Zetaris; 15 scripted registration statements; the same schema served offline by a SQLite twin | demo 2:15: one federated query in the Data Explorer joins the export event to its ticket and policy. Repo: 13/13 alerts on the federated backend, one trace per alert in `docs/examples-live/zetaris-rules/` |
| **findings and analyst corrections stay available to later investigations**, with their source and a trust ledger | **Meterless H-MEM**: every finding stored with provenance; a re-run re-verifies (+confidence), a changed fact supersedes the old one; helpful / wrong and verdict agree / disagree go to the ledger and change ranking next time | demo 2:45: one `wrong` click drops a fact's confidence 0.90 → 0.70, tags it `review` and writes a ledger row. Repo: Python implementation of the spec; the official conformance vectors reproduced in pytest (13 cases, byte-identical vectors file) |
| **the current alert is read against the account's history** (earlier alerts, status changes) | **Meterless World Model**: accounts, datasets, tickets, exceptions as entities, alerts and events as contexts, event-time provenance; re-runs append nothing | demo 1:45: AL-011 is read against AL-001 on the same account (the World state block). Repo: the official runner's 8 cases reproduced in pytest; idempotency and persistence tests; the cross-alert test (AL-001 then AL-011 on the same account) |
| **language work done by a capable model, inside fixed limits**: SQL drafts that run only when identical to the vetted template, a second opinion that can only escalate, a summary that ships only after review | **NVIDIA Nemotron through Meterless Nightshift**: OpenAI-compatible runtime contract (bearer token, `meterless-app`, `meterless-scope`), `GET /v1/models` as the online check, bounded retries | demo 2:30: the recorded AL-011 second opinion and the Critic's 6/9 → revision → 9/9, with the model id and receipt on every call. Repo: every successful call logs `routed_model` and `receipt_id` (`nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b`, `nvidia/nemotron-3.5-lightning-30b-a3b` in the recorded runs); recorded run AL-004: five drafts accepted as template-identical, critic 6/9 → revision → 9/9 |

Visual: Zetaris Data Explorer with the three datasources and one federated query; the UI "Correct the agent" section with the ledger; the World state block; a JSONL excerpt with `model` and `receipt_id`.

Notes: The ten points are not for naming the sponsors often; each row is a contribution the judges watch happen on screen, then the repository evidence behind it. Lead each row with the benefit, then name the component. Both Meterless engines are on the critical path: remove either and a feature stops working (cross-alert context, feedback with an audit trail). Nemotron does three jobs and every one of them is receipted.

---

## 9. Can you trust it? Validation

What a customer needs to believe: it runs, the evidence is checkable, failures are reported, it does not read more than it needs.

| check | result |
|---|---|
| live Zetaris, rule verdicts (one trace per alert in docs/examples-live/zetaris-rules) | 13/13 alerts match ground truth |
| live Zetaris + Nemotron, recorded run AL-004 (docs/examples-live) | verdict matches; all five drafts accepted because identical to the templates; critic 6/9 → revision → 9/9. (AL-011 in the same folder is the earlier recording of the critic loop) |
| 12 seeds × 13 alerts, offline | 156/156 |
| disclosure ablation (distinct sensitive-column reads over 13 synthetic triages) | naive `SELECT *` 113 · this system 5 (96% fewer), identical verdicts |
| failure path | a critical source down never yields compliant: cannot-confirm, or needs-human if the second opinion also disagrees (3 outage tests); budget exhaustion stops retrieval and names the unfetched evidence |
| tests | 64 passed (two engine conformance suites + end-to-end + review regressions) |

Visual: the confusion table (10 scenarios, every row in its expected column) and the ablation bar (113 vs 5).

Notes: Say which is which: offline runs use a deterministic stub for the second opinion and the Critic, and the report labels it `(stub)`; the model's part is shown only on the recorded live runs, with model ids and receipts. The division of labour, stated precisely: business evidence comes from the queries, the rules form the base verdict, the model drafts SQL, gives the second opinion and reviews the summary, and the trace records what actually ran. Be honest: ground truth is generated with the data, so 156/156 measures consistency on the ten scenarios the generator injects, not real-world detection quality. The ablation is the differentiator: 96% fewer distinct sensitive-column accesses on these 13 synthetic alerts, for the same verdicts.

---

## 10. Limits and phase two

**Limits (stated in the README):** synthetic sources and policies; rules cover the ten injected scenarios; the recorded live runs took 231 s (AL-004, 7 calls) and 298 s (AL-011, 8 recorded calls), with Nightshift routing to a 550B reasoning model for most calls, so latency varies by routing; the official TypeScript conformance runners were ported to pytest, not executed.

**Phase two:**
- a pilot on a customer's real queue, measuring analyst minutes per alert, uncovered exports closed as compliant, and whether the team keeps using it: the validation targets, none measured today
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
