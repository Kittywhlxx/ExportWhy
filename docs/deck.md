# ExportWhy — final 12-slide presentation

Product: ExportWhy · Open Agent Hackathon 2026, Track 2 "The Agent That Can Explain Why" · solo entry.
No company name on the deck: the product is the name.
Framing: ExportWhy is presented to a prospective customer. The judges should be able to read the deck as
that customer: is this my problem, what does it do for me, why is it worth trying, can I trust it.
Architecture, sponsor components and results are the reasons to believe, not a research chapter.
12 slides, 16:9. Each slide: title, on-slide copy, visual, footer where needed, presenter note (≤40 s).
The deck is read by the judges on its own; the 1–4 minute video tells the same story in its own order
(see `video-script.md`) and does not walk through these slides.
Numbers are from the repository at submission time; re-run `evaluate`, `seed_sweep.py` and
`ablation_disclosure.py` before exporting and replace any figure that changed. The organiser's checklist
(problem and users · solution · agent workflow/architecture · technical stack · how Zetaris and Meterless
were used · what was unique · product visuals · future enhancements) maps onto slides 2–12.

---

## 1. ExportWhy: evidence behind the authorisation decision

**On-slide copy**

**ExportWhy**

A data-export investigation skill for security teams.

Check logs, policy and approval records.
Deliver a verdict, inspectable evidence and suggested next checks.

**Visual**

A real AL-004 product screenshot: the INVESTIGATE banner, its reason and one cited evidence line.

**Presenter note**

Introduce the product through what the analyst receives: an investigation they can inspect and act on.

---

## 2. An approved ticket does not necessarily cover the export

**On-slide copy**

**03:30 · Account u1012**

- 48,000 payment-transaction rows exported
- Destination: an external cloud bucket
- Approval ticket cited: T-2629

**The ticket was approved, for 5,000 rows.**

The log records what happened.
The policy defines what is allowed.
The ticket records what was approved.

**Visual**

The export event and approval record side by side, highlighting **48,000 versus 5,000**.

**Footer**

Synthetic demonstration scenario: AL-004.

**Presenter note**

Authorisation requires matching the account, dataset, volume and validity window to the actual export.

---

## 3. Built for the analyst deciding what happens next

**On-slide copy**

**Target customers**
Security operations and data-protection teams with export alerts, access policies and ticketed approvals.

**Primary user**
The analyst investigating DLP or insider-risk alerts.

| The analyst's task | The question to resolve |
|---|---|
| Check authorisation | Does the approval cover this account, dataset, volume and time? |
| Assess evidence | Is the export uncovered, or is evidence still missing? |
| Choose the next action | Investigate further, collect evidence or request human review? |
| Support later review | Can another person inspect the evidence behind the finding? |

**Visual**

An analyst working across logs, policy and tickets, with the investigation report available to
subsequent reviewers.

**Presenter note**

ExportWhy supports the investigation. Closing the alert and taking action remain the analyst's
responsibility. Analyst time saved, missed uncovered exports and willingness to pay are not measured yet;
they are the pilot targets on slide 12, not results.

---

## 4. From investigation tasks to usable findings

**On-slide copy**

| Customer task | ExportWhy capability | What the analyst receives |
|---|---|---|
| Check approval scope | Compare account, dataset, volume, time and status | Specific matches and conflicts |
| Continue when the cited ticket fails | Search other tickets and standing exceptions | A broader authorisation check |
| Inspect the reasoning | Cite table, column and query step on each evidence line | Traceable findings |
| Understand uncertainty | List gaps and evidence not retrieved | Suggested next checks and owners |
| Retain context and corrections | Cross-alert history and analyst feedback records | Information available to later investigations |

**Footer**

Queries are column-scoped and the report records the access footprint. Suggested owners are assigned by
the analyst.

**Visual**

A compact table alongside actual evidence, conflict and next-check excerpts.

---

## 5. The product: verdict, evidence, next action

**On-slide copy**

**One alert ID produces an investigation the analyst can inspect.**

1. **Read the finding:** verdict, reason and queue priority
2. **Check the evidence:** citations, approval scope and conflicts
3. **Choose the next step:** further investigation, evidence collection or review
4. **Correct the record:** feedback on facts and verdicts

**Base verdicts**
compliant · investigate · cannot-confirm

**Unresolved rules/model disagreement**
needs-human

**Visual**

A large, real AL-004 UI screenshot with three callouts:

- Verdict and reason
- Ticket-scope conflict and cited evidence
- Suggested next checks and feedback

**Footer**

The evidence/review score is a heuristic, not a probability of correctness.

**Presenter note**

Follow the analyst's reading order. Show how the ticket evidence supports the conflict and how the
suggested next check follows from it. Do not tour the controls.

---

## 6. Existing products and our focus

**On-slide copy**

| Product | Publicly described capability or current implementation |
|---|---|
| **Microsoft Purview DLP Triage Agent** | Triages supported DLP alerts into categories including Needs attention, Less urgent and Not categorized. |
| **Mimecast Mihra Investigation Agent** | According to Mimecast's announcement (beta, with enhancements to follow), provides alert triage, contextual summaries, explanations and recommended next steps in Incydr. |
| **ExportWhy** | Focuses on whether authorisation covers a specific export, combining logs, policy and tickets through Zetaris into cited findings, conflicts, gaps and an access footprint. |

**Our design focus**

Field-level authorisation checks
Federated evidence retrieval
Constrained model review

**Footer**

A comparison of documented capabilities and product focus, not a head-to-head performance evaluation or
a claim of exclusive features.

**Sources**

[Microsoft Learn: Purview DLP Triage Agent](https://learn.microsoft.com/en-us/purview/copilot-in-purview-triage-dlp-agent-get-started)

[Mimecast: Mihra Investigation Agent announcement](https://www.mimecast.com/blog/mimecast-extends-data-protection-to-the-agentic-enterprise/)

**Presenter note**

None of the parts is new on its own; the combination, running as one investigation, is the point. The
constrained-review choice is motivated by Alert-Bench (NeurIPS 2026 AIWILD workshop,
https://neurips.cc/virtual/2026/168137): in the agent configurations it benchmarked, a revision stage
overturned more correct decisions than it repaired.

---

## 7. Architecture: business records become traceable findings

**On-slide copy**

**Business records**
Export logs · Access policy · Approval tickets

↓

**Zetaris**
One SQL schema · Cross-source queries

↓

**Investigation roles**
Planner · Retriever · Evaluator · Reporter · Critic

↓

**Deliverables**
Report · UI · Query and model-call traces

**Supporting services**

- H-MEM: findings, provenance and feedback
- World Model: entities and historical context
- Nightshift → Nemotron: drafting, second opinion and review

**Technical stack**

Python · PostgreSQL ×3 · Zetaris · Streamlit · CLI/HTTP · pytest · Docker

**Visual**

A single architecture diagram. Place memory and model services beside the investigation loop.

**Footer**

Live investigations use Zetaris over three independent sample databases. Offline demonstrations use a
SQLite twin and deterministic model stubs.

**Presenter note**

Business evidence enters through Zetaris. Web information is not used to determine export authorisation.

---

## 8. Workflow: the evidence determines what to retrieve next

**On-slide copy**

**The AL-004 investigation**

Identify alert and account
→ Retrieve dataset information, policy and cited ticket
→ Detect a scope mismatch
→ Search other tickets and standing exceptions
→ Add account history and destination context
→ Produce verdict, second opinion and reviewed summary

**Feedback loops**

**Evaluator → Planner**
Request the evidence still needed.

**Critic → Reporter**
Request a revision and review it again.

**Control boundaries**

- Rules establish the base verdict
- SQL drafts execute only when canonically identical to vetted templates
- Unresolved model disagreement leads to human review
- Failed or unavailable summary review triggers a deterministic fallback
- Bounded: 3 evidence rounds, 9 model calls, a 480 s budget checked before every query and model call

**Visual**

The real AL-004 timeline beside a simplified workflow with both feedback arrows visible.

**Presenter note**

Show the combination working in one investigation: cross-source checks, evidence requests, constrained
model review and traceable reporting.

---

## 9. Zetaris: authorisation evidence across three systems

**On-slide copy**

| Capability used | Contribution to the product |
|---|---|
| Three databases registered as JDBC data sources | One access point for the investigation |
| One schema for logs, policy and tickets | A consistent query interface for the agent |
| Cross-source policy and cited-ticket joins | Export events and authorisation records checked together |
| Query results returned to the agent | The agent records query steps and attaches evidence references |

**Recorded verification**

13 alerts investigated on the federated backend, with one saved trace per alert
(`docs/examples-live/zetaris-rules/`).

**Visual**

The three registered data sources and an actual policy or cited-ticket cross-source query with its result.

**Presenter note**

This implements the organiser's grounding pattern: query Zetaris for the facts first, then produce an
answer that can be traced back to the evidence.

---

## 10. Meterless and Nemotron: context, corrections and review

**On-slide copy**

| Technology | Contribution to the product | Demonstrable evidence |
|---|---|---|
| **Meterless H-MEM** | Retains findings, provenance and analyst corrections for later investigations | A fact correction, confidence change and ledger entry |
| **Meterless World Model** | Connects the current alert to the account's earlier activity | Previous alerts visible in the current account context |
| **Meterless Nightshift → NVIDIA Nemotron** | Supports SQL drafting, a second opinion, summary drafting and Critic review | AL-004 model-call records and the 6/9 → 9/9 summary revision |

**Model boundaries**

The model cannot directly turn a rule-based block into compliant.
Model-written summaries must pass review; otherwise, a deterministic summary is delivered.

**Footer**

H-MEM and World Model are Python implementations of the specifications, checked with conformance cases.
Successful model calls retain routing and receipt information.

**Visual**

Three real excerpts: feedback ledger, account history and AL-004 model-call/review records.

**Presenter note**

Show the full model IDs in the recorded traces. AL-004: `nvidia/nemotron-3-ultra-550b-a55b` and
`nvidia/nemotron-3.5-super-vl-120b-a12b`. AL-011 (the earlier recording of the critic loop):
`nvidia/nemotron-3-ultra-550b-a55b` and `nvidia/nemotron-3.5-lightning-30b-a3b`. The records show which
model handled each successful call. Offline runs use a deterministic stub for the second opinion and the
Critic, labelled `(stub)` in the report.

---

## 11. What we have validated

**On-slide copy**

| Verification | Recorded result |
|---|---|
| Live Zetaris backend, stub model | **13/13** verdicts match generated ground truth |
| Live Zetaris backend, Nemotron drafting the SQL | **13/13** |
| Twelve offline seeds | **156/156** verdicts match |
| Sensitive-column reads across the same 13 synthetic alerts | **113 → 5: approximately 96% fewer**, with unchanged verdicts |
| Recorded AL-004 Nemotron run | **231 seconds · 7 model calls · Critic 6/9 → 9/9** |
| Tests | **64 passed** (engine conformance suites, end-to-end, review regressions) |

**Failure behaviour is visible**

Unavailable critical sources are reported.
Budget-limited investigations identify evidence not retrieved.
Unresolved model disagreement goes to human review.

**Visual**

A 113-versus-5 bar chart, with the live-run record and one source-outage result alongside it.

**Footer**

Synthetic sources and policies; ten generated scenarios. Production accuracy, analyst time savings and
customer ROI have not yet been measured. Model routing and latency vary between runs.

**Presenter note**

The results establish behaviour within the tested scenarios. Customer impact is the next validation step.

---

## 12. Next: validate the workflow with a security team

**On-slide copy**

**Demonstrable today**

Cross-source authorisation checks, cited findings, investigation history, analyst feedback and failure
fallbacks.

**Phase two**

1. Connect real logs, identity records and approval systems
2. Review de-identified historical investigations
3. Measure evidence quality, actionable guidance and analyst effort
4. Add case creation, assignment and evidence-pack write-back

**Pilot invitation**

Choose one export-alert workflow and evaluate:

**Can the evidence be found?
Can the finding be checked?
Does the next step help the analyst?**

**Visual**

Current prototype → Historical-case pilot → Workflow integration

**Closing line**

**ExportWhy**
Authorisation decisions with evidence and visible gaps.
