# Sponsor technology explanation (submission form)

## Zetaris — the data layer

Three synthetic sources (an export audit log with accounts and alerts; the data-access policy with
datasets, exceptions and field classification; the approval-ticket system) run as three separate
Postgres databases. `data/zetaris/register.py` registers each as its own JDBC datasource
(`logsdb`, `policydb`, `ticketsdb`) and builds one schemastore container, `dexa`, with a view per
table. The agent's only tool is read-only Lightning SQL against `dexa.*` through the Zetaris REST
API (`agent/zetaris.py`). The two decision-critical lookups are cross-source joins executed by Zetaris
(the policy for this export: `export_events ⋈ accounts ⋈ policies`, logs ⋈ policy; the cited ticket:
`export_events ⋈ tickets`, logs ⋈ tickets); the remaining lookups are scoped single-source queries that
the Evaluator correlates. The agent never opens a connection to a source. The same schema is served
offline by a SQLite twin so judges can run everything without Zetaris (`SAMPLE_MODE=true`).
Verified: 13/13 alerts on the federated backend; registration script and schema in the README.

## Meterless Engines — H-MEM and World Model, both in working features

**H-MEM** (`agent/hmem/`, a Python implementation of the published spec; `agent/memory.py`):
every fact the Evaluator establishes is stored as a memory record with mandatory `source` and
`provenance` (the Zetaris table, query step and run). A re-run of the same alert re-verifies the
record (`feedback("helpful")`, +0.05 confidence) instead of duplicating it; a changed fact creates a
new record that `supersedes` the old one, which is kept and ranked down (−0.20 penalty). Analysts
mark facts helpful or wrong and agree or disagree with verdicts from the UI or CLI; each action is a
trust-ledger entry with previous and new state, and the calibration tally per verdict type is shown.
Recall before each investigation re-injects earlier findings on the account/dataset.
Test: `tests/test_hmem_conformance.py` reproduces the official conformance vectors
(`agent/hmem/conformance_vectors.json`, a copy of the upstream file; record validation, feedback deltas, ranking
order, sleep guardrails, conflict gate, ledger audit, dream boundary) plus persistence and scoping
tests. 17 tests pass.

**World Model** (`agent/worldmodel/`, Python implementation of the spec; `agent/world.py`):
accounts, datasets, tickets and exceptions are entities with external keys; alerts and export
events are contexts; `exported`, `cited`, `covers` are edges; directory status and the verdict per
alert are facts. Provenance on every write uses the event's own timestamp, so re-running an alert
appends zero events (content-addressed ids). The account dossier (prior alerts and verdicts, status
history, exports) is read before planning and used by the Evaluator: repeat alerts on an account
raise an escalation; a status that changed between investigations is reported as a conflict.
Test: `tests/test_worldmodel_conformance.py` reproduces the official runner's cases (stable ids,
merge bands at 0.85/0.82, provenance rejection, idempotency, bitemporal edges, audit) plus
persistence and supersede tests; the end-to-end test checks that the second alert on the same
account sees the first. 12 tests pass, one strict expected-fail documents a reference behaviour.

## Meterless Nightshift — the primary AI provider

`agent/llm.py` implements the Meterless Runtime provider contract: OpenAI-compatible calls to the
local runtime (`METERLESS_BASE_URL`, default `http://127.0.0.1:8088/v1`), `Authorization: Bearer
<runtime token>`, `meterless-app: exportwhy` on every call and `meterless-scope: user` on chat,
`GET /v1/models` as the online check, a 180 s timeout and bounded retries on 429/5xx. Model ids are
routing modes (`meterless/fast`); the compute receipt returned by Nightshift (`requested_model`,
`routed_model`, `receipt_id`) is written into every trace and JSONL log line. The model is used in
four places: drafting evidence SQL (validator-gated), the Evaluator's second opinion, the Reporter's
summary and the Critic's rubric score. Every model failure degrades to a template or an explicit
"unavailable" rather than aborting a triage. `python -m agent.main health` shows the provider online.

## NVIDIA — Nemotron through Nightshift

With the NVIDIA key stored in Nightshift's Providers tab (never in the app), requests are routed to
Nemotron models (`nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-super-vl-120b-a12b` in
the live runs). Nemotron does the work that needs language: it writes the SQL for each evidence
class, judges the facts independently as a second opinion, writes the analyst summary and scores it.
The logs show which Nemotron model handled each request. In the live run over 13 alerts, 48 of 48
Nemotron-drafted queries passed the validator and 13/13 verdicts matched the ground truth.

## What is unique about this use of the sponsor technologies

- The three sources are genuinely federated by Zetaris at query time; the agent's whole knowledge of
  the world is SQL over one governed schema, which is what makes the disclosure footprint
  measurable: 113 sensitive-column reads with naive `SELECT *` queries versus 5 with this planner,
  identical verdicts.
- The two Meterless engines are implemented from the specs in Python and both sit on the critical
  path: without H-MEM there is no re-verification, no feedback with an audit trail; without the
  World Model the second alert on an account starts from nothing.
- Nemotron is advisory by design: it can make a verdict more conservative (needs-human) or improve
  the prose; it can never flip a block or a cannot-confirm to compliant. Alert-Bench (NeurIPS 2026)
  reports revision stages that overturn correct decisions; this design rules that failure mode out
  while still using the model on every alert.

Pre-existing components: no pre-existing product code. The two Meterless engines are Python
implementations adapted from the published reference implementations and conformance suites
(Meterless repository, Apache-2.0; see NOTICE), and `agent/hmem/conformance_vectors.json` is a copy of
the upstream vectors file. The application, its integration of the engines, the synthetic data
generator, scenarios and ground truth were written during the build window (22–27 October 2026).
