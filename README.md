# ExportWhy — an agent that explains why a data-export alert needs investigation

Open Agent Hackathon 2026 · Track 2 *The Agent That Can Explain Why* · solo entry

A security analyst gets an alert: *"account u1023 exported 30,000 rows of payment_transactions at
01:05"*. Was it authorised? The answer is spread across three systems that never talk to each
other: the export audit log, the data-access policy, and the approval-ticket workflow. ExportWhy
investigates the alert across all three, reaches a verdict (**compliant / investigate /
cannot-confirm**), and backs every claim with the table, column and query it came from. It says
plainly what it could *not* confirm, flags where sources disagree, and redacts personal data in
the report while keeping the references in the trace.

```
$ python -m agent.main alert AL-003

# Export-alert triage: AL-003
**Verdict: investigate**
Why:
- the cited ticket does not cover this export and no other authorisation does
## Evidence (table.column | query step)
- AL-003 (high, rule: off-hours bulk export of restricted data): account u1023 exported 30000 rows of DS02 ...  [dexa.export_events.rows_exported | q1]
- Ticket T-2628: requester u1023, dataset DS02, max_rows 50000, approved, valid 2026-10-06 01:05:00 .. 2026-10-11 01:05:00 ...  [dexa.tickets.valid_to | q6]
## Conflicts (flagged, not silently resolved)
- ticket T-2628 was cited but does not cover the export: export at 2026-10-18 01:05:00 is outside the ticket window ...
## Access footprint (disclosure check)
- tables accessed: accounts, alerts, datasets, export_events, field_sensitivity, policies, policy_exceptions, tickets
- sensitive columns touched: none
```

## How it works

```
                 ┌────────────┐   evidence classes   ┌───────────┐   SQL (read-only)   ┌──────────────────────────┐
  alert id ────▶ │  Planner   │ ───────────────────▶ │ Retriever │ ──────────────────▶ │  Zetaris  (one schema:   │
                 └────────────┘                       └───────────┘                     │  dexa.*  over 3 sources) │
                       ▲                                    │ rows                       │  logsdb · policydb ·     │
      "evidence insufficient /                              ▼                            │  ticketsdb  (Postgres)   │
       source conflict: fetch X"                      ┌───────────┐                     └──────────────────────────┘
                       └──────────────────────────── │ Evaluator │
                                                      └───────────┘
                                                            │ facts, conflicts, gaps, disclosure
                                                            ▼
                                                      ┌───────────┐     ┌──────────────────────────────┐
                                                      │ Reporter  │ ──▶ │ verdict + cited evidence +   │
                                                      └───────────┘     │ redacted report + JSON trace │
         Meterless H-MEM  ◀── findings, re-verified ──┘     │           └──────────────────────────────┘
         Meterless World Model ◀── entities, edges, facts ──┘
```

* **Planner** decides which *evidence classes* the verdict still lacks (the policy for this role,
  the ticket the exporter cited, other tickets, standing exceptions, the export baseline...) and
  drafts the SQL for them. Drafts come from the model (Nemotron through Nightshift) and are kept
  only if they are read-only, touch known tables, and stay on this alert's keys; otherwise the
  rule-based template runs.
* **Retriever** runs the SQL against Zetaris and records which tables and columns were touched.
* **Evaluator** applies the decision rules (does the ticket cover account, dataset, rows and time?
  does an exception clause apply? is the account still employed?) and sends the Planner back for
  more evidence when the picture is incomplete or sources disagree. At most three rounds. A source
  that cannot be queried becomes an explicit gap, never a silent "compliant".
* **Evaluator, second opinion**: once the rules have a verdict, the model (Nemotron through
  Nightshift) judges the same facts independently, without seeing the rules' answer. Agreement is
  recorded; a disagreement buys one more evidence round for whatever the model says is missing,
  and if they still disagree the verdict becomes **needs-human** with both positions in the report.
  The model can never silently turn a block or a cannot-confirm into a compliant.
* **Reporter + Critic** (proposer/critic pairing): the Reporter drafts a 3-5 sentence summary; the
  Critic scores it 0-3 on accuracy (every claim backed by a listed fact), appropriateness (verdict
  and reason stated and consistent) and actionability (next check concrete). Below 7/9 it returns
  one fix and the Reporter revises once. Scores, fix and revision are in the trace and the UI.
* **Reporter** writes the verdict, the per-claim citations, the conflicts, what could not be
  confirmed, next checks, and the **disclosure footprint**: which tables contributed, which did
  not, and which sensitive columns were read. Personal data is redacted in the report.
* **Memory across investigations**: H-MEM keeps each finding with its provenance (a re-run
  re-verifies and raises confidence; a changed fact supersedes the old one, never overwrites). The
  World Model keeps accounts, datasets, tickets and exceptions as shared entities, so the second
  alert on the same account knows about the first.

## Agent loop spec (the eight lines)

| | |
|---|---|
| **Goal** | Decide whether one export alert is compliant, needs investigation, or cannot be confirmed, and say why with evidence |
| **Plan (≤5 steps)** | identify alert+account → fetch policy, dataset, cited ticket → if not covered: other tickets, exceptions → baseline/destination → second opinion, critique, report |
| **Tools** | `run_sql(select) → rows` via Zetaris REST (fail: timeout/unreachable → gap, never a guess); H-MEM `recall/remember/feedback`; World Model `dossier/ingest`; Nightshift `chat` (fail: template SQL, stub summary) |
| **Memory** | scratchpad = evidence rows per class; decisions = H-MEM facts with provenance (re-verified, superseded); knowledge = World Model entities and verdicts |
| **Rubric (0-3 × 3)** | Critic: accuracy · appropriateness · actionability; pass at 7/9 |
| **Stop conditions** | verdict grounded → report; evidence budget (3 rounds) spent → cannot-confirm; rules vs model disagree → needs-human; critic < 7/9 → one revision |
| **Run log** | `logs/trace-<run_id>.jsonl`: step · decision · result · next agent, plus `traces/<run_id>.json` |
| **Guardrails** | ≤3 evaluator rounds; ≤9 logical model calls per alert, enforced by a counter (up to three planning rounds, up to two second opinions, one summary, up to two critiques, one revision; retries inside a call excluded); read-only SQL on known tables only; every evidence lookup runs its vetted template unless the model's draft is canonically identical to it (comments, non-ASCII whitespace, extra columns, widened joins all fall back to the template) and the Evaluator re-checks that returned rows belong to this alert; a 480 s triage budget enforced before every query and every model call, inside retries and as the request timeout of each call (zero budget = no call; an in-flight request can overrun by at most its own timeout, no new query or call starts after the budget, and a query that cannot be sent for lack of time is reported as 'not retrieved within the budget', never as a source failure), model timeout ≤180 s; every model failure degrades to a template or 'unavailable', a summary that fails review is replaced by the deterministic one; secrets only from env; redaction before output |

Autonomy level L2 (loop with checks): the agent iterates on its own but must pass the rules, the
second opinion and the critic; the analyst stays in the loop through the feedback buttons.

## Sponsor technology

| | Where | What it does in this project |
|---|---|---|
| **Zetaris** (required) | `data/zetaris/register.py`, `agent/zetaris.py` | Three Postgres databases are registered as three JDBC datasources and exposed as one schemastore container `dexa`. Every agent query is a Lightning SQL `SELECT` over that schema; Zetaris executes the cross-source joins. The agent never connects to a source directly. |
| **Meterless H-MEM** (engine) | `agent/hmem/` (Python implementation of the spec), `agent/memory.py` | Findings are memory records with mandatory source and provenance; feedback deltas, supersede-on-change and the trust ledger follow the spec. `tests/test_hmem_conformance.py` reproduces the official conformance vectors (`agent/hmem/conformance_vectors.json`, a copy of the upstream file). |
| **Meterless World Model** (engine) | `agent/worldmodel/` (Python implementation of the spec), `agent/world.py` | Stable content-addressed ids, provenance on every write, append-only event log, bitemporal edges. Accounts/datasets/tickets/exceptions are entities, alerts/events are contexts; the account dossier feeds the Planner and the Evaluator. `tests/test_worldmodel_conformance.py` reproduces the official runner's cases. |
| **Meterless Nightshift** (primary AI provider) | `agent/llm.py` | OpenAI-compatible calls to the local runtime with the provider contract v4 headers (`Authorization: Bearer`, `meterless-app`, `meterless-scope: user`), `GET /v1/models` as the online check, 429 back-off. |
| **NVIDIA Nemotron** | via Nightshift | The NVIDIA key lives in Nightshift's Providers tab; requests are routed to Nemotron models and the compute receipt (requested vs routed model) is written into every trace under `routed_model`. |

Nothing here is a wrapper around a prompt: the data layer, the two engines and the provider are
each on the critical path, and `SAMPLE_MODE` exists so the loop can be exercised without them.

## Setup

Tested on macOS (Apple Silicon). Linux works the same; Windows via WSL.

### 0. Prerequisites

* Python 3.10+, Docker Desktop (8 GB memory, 4 CPUs) and `git`.
* A Zetaris Cloud account: download `zetaris-platform.zip` and the registry sign-in command from
  https://cloud.enterprise.zetaris.com/dashboard/download (not redistributed in this repo).
* Meterless Nightshift for your machine from https://meterless.ai, and an NVIDIA API key from
  https://build.nvidia.com/models (free endpoints).

### 1. This repository

```bash
git clone <this repo> exportwhy && cd exportwhy
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then edit: see step 4
```

### 2. Offline check (no Zetaris, no model: 30 seconds)

```bash
.venv/bin/python data/gen_data.py --seed 7      # synthetic three-source world + ground truth
.venv/bin/python data/build_sample.py           # SQLite twin of the Zetaris schema
SAMPLE_MODE=true .venv/bin/python -m agent.main evaluate   # expect 13/13 verdicts match ground truth
.venv/bin/pytest -q                             # H-MEM + World Model conformance, end-to-end loop
```

### 3. Zetaris (the data layer)

```bash
unzip zetaris-platform.zip && cd zetaris-platform
docker login zetarishackathon.azurecr.io        # paste the command from the download page
./preflight.sh && docker compose pull && docker compose up -d && ./verify.sh     # 9/9
cd ..
```

Then create an API token in the Zetaris web UI (http://localhost:3000 → Management → API Tokens)
and put it in `.env` as `ZETARIS_API_KEY`. Start the three synthetic sources next to Zetaris and
register them:

```bash
docker compose --env-file .env -f infra/sources/docker-compose.yml up -d   # logsdb / policydb / ticketsdb
.venv/bin/python data/zetaris/register.py                                 # 15 statements: 3 datasources + dexa views
LLM_STUB=true .venv/bin/python -m agent.main evaluate                     # 13/13 on the federated backend
```

### 4. Nightshift and Nemotron (the model)

Install and open Nightshift. In **Setup ▸ Providers** add your NVIDIA key. Then copy the runtime
settings into `.env`:

```
METERLESS_BASE_URL=http://127.0.0.1:8088/v1          # Console ▸ Runtime URL
METERLESS_RUNTIME_TOKEN=<Console ▸ API key>          # never the NVIDIA key
METERLESS_MODEL=meterless/fast                       # a routing mode; the receipt records the Nemotron model used
```

```bash
.venv/bin/python -m agent.main health      # data layer: zetaris OK · model provider: online, meterless/* ids
.venv/bin/python -m agent.main alert AL-004
```

## Usage

Programmatic entry point (JSON in, JSON out; `input_examples/*.json` → `output_examples/*.json`):

```bash
python run.py --input input_examples/example_2.json     # one triage, response on stdout
python run.py --alert AL-003 --sample                     # shorthand, offline
python run.py --serve                                     # HTTP on 0.0.0.0:$PORT: POST /run, GET /health, GET /alerts
curl -X POST localhost:8000/run -H 'Content-Type: application/json' -d @input_examples/example_2.json
```

Docker (Zetaris and Nightshift stay on the host; the image builds the SAMPLE_MODE twin, so
`-e SAMPLE_MODE=true` runs with nothing else):

```bash
docker build -t exportwhy .
docker run --rm -p 8000:8000 -e SAMPLE_MODE=true exportwhy                       # offline
docker run --rm -p 8000:8000 --env-file .env exportwhy                           # live: use host.docker.internal in .env
```

Logs: readable `[INFO]` lines on stderr (`docker logs`), one JSON object per step in
`logs/trace-<run_id>.jsonl` (agent, action, input/output summary, model and receipt for every model
call, status success/recovered/escalated/error), and the full nested trace in `traces/<run_id>.json`.

CLI:

```
python -m agent.main alert <ALERT-ID>        triage one alert (report to stdout, JSON trace to traces/)
python -m agent.main alert <ALERT-ID> --simulate-outage tickets   fail a source (logs | policy | tickets): the verdict degrades to cannot-confirm, never to a guess
python -m agent.main evaluate [--write-examples]   triage every alert, score against data/sample/ground_truth.csv
python -m agent.main memory                  what H-MEM remembers
python -m agent.main feedback <mem-id> wrong correct a memory (helpful | not_helpful | wrong); logged in the trust ledger
python -m agent.main feedback-verdict <ALERT-ID> agree|disagree [--note ...]   analyst feedback on the verdict; calibration tally
python -m agent.main health                  data layer + model provider status
```

Environment switches: `SAMPLE_MODE=true` (SQLite twin + deterministic stub, fully offline),
`LLM_STUB=true` (real Zetaris, stub model), `SIMULATE_OUTAGE=tickets,policy` (fail those sources), `PLANNER=naive` (ablation: `SELECT *` queries). Without any, the real Zetaris and Nightshift are used.

UI: `.venv/bin/streamlit run ui/app.py` (same loop, with per-fact feedback into the H-MEM ledger and an outage switch).

`input_examples/` and `output_examples/` hold one command and one report per alert; `traces/`
gets one JSON file per run (every plan, query, rejection, evaluation round and model receipt).

## Data

All three sources are synthetic and generated with a seed; judges can pick a seed the author never
saw and the ground truth comes with it. See [data/README.md](data/README.md) for the schema, the ten
injected scenarios and the regeneration commands.

## What a triage returns

Every verdict carries an **evidence/review score** (a heuristic in [0, 1], not a calibrated probability:
evidence completeness, second-opinion agreement, critic score, open gaps, unretrieved evidence), a **priority** for the analyst's queue when it needs work (dataset sensitivity, rows,
external destination, account status → P1/P2/P3), a **status** (complete, partial, timed-out: the
whole triage has a wall-clock budget, `TRIAGE_TIMEOUT_S`, default 480 s, and returns what it has with
"manual review" instead of hanging; any evidence skipped for lack of budget makes the run timed-out and is
named in the report, so a verdict never reads as complete on evidence it did not see), the **investigation timeline** (every plan, query with row counts,
evaluation round, second opinion and critique, in order), and the **memory updates** it made, each with
its source. Analysts can agree or disagree with the verdict itself (`feedback-verdict`, or the buttons in
the UI); the trust ledger keeps it and the calibration tally is shown per verdict type.

`evaluate` also prints a trajectory table (rounds, queries, zero-row queries, sensitive columns,
evidence/review score per alert, and the count of compliant verdicts that would rest on a zero-row authorisation
query, which must be 0). Alert-Bench (NeurIPS 2026) found that revision stages overturn more correct
decisions than they repair; here the second opinion and the critic can only make a verdict more
conservative (needs-human) or edit prose, never flip it to compliant.

## Accuracy across seeds

The generator writes the ground truth with every seed, so the agent can be scored on worlds its author
never looked at (`scripts/seed_sweep.py`, full tables in [docs/seed_sweep.md](docs/seed_sweep.md)).

| | |
|---|---|
| seeds | 1, 2, 3, 5, 7, 11, 13, 19, 23, 42, 99, 2026 |
| triages | 12 x 13 = 156, offline (SQLite twin, rule verdicts, stub model) |
| verdicts matching ground truth | **156 / 156** |
| live backend (Zetaris, seed 7) | 13 / 13 with the stub model and 13 / 13 with Nemotron drafting the SQL |

| scenario | expected | compliant | investigate | cannot-confirm |
|---|---|---:|---:|---:|
| valid_ticket (x2 per seed) | compliant | 24 | 0 | 0 |
| policy_exception (x2) | compliant | 24 | 0 | 0 |
| within_policy | compliant | 12 | 0 | 0 |
| expired_ticket | investigate | 0 | 12 | 0 |
| scope_mismatch | investigate | 0 | 12 | 0 |
| no_authorization (x2) | investigate | 0 | 24 | 0 |
| terminated_account | investigate | 0 | 12 | 0 |
| unknown_account | cannot-confirm | 0 | 0 | 12 |
| missing_policy | cannot-confirm | 0 | 0 | 12 |
| ticket_not_found | cannot-confirm | 0 | 0 | 12 |

Two generator leaks were found by exactly this sweep and fixed (a background ticket that happened to
authorise an injected alert on seed 11; an account with a named exception reused by another scenario on
seed 3). Pick any other seed: `python data/gen_data.py --seed N && python data/build_sample.py && SAMPLE_MODE=true python -m agent.main evaluate`.

## Does the disclosure controller change anything? (ablation)

Same 13 alerts, same evidence loop, same verdict rules. Only how the queries are written differs.
Counts are distinct sensitive columns read, summed over the 13 triages (`scripts/ablation_disclosure.py`,
full table in [docs/ablation_disclosure.md](docs/ablation_disclosure.md)).

| planner | pii | confidential | internal | sensitive total | verdicts vs ground truth |
|---|---:|---:|---:|---:|---|
| naive: every evidence query is `SELECT *` | 52 | 26 | 35 | 113 | 13/13 |
| this system: column-scoped evidence queries | 0 | 5 | 0 | 5 | 13/13 |

The five confidential reads are the `destination` column, fetched only for the alerts whose export left
the internal share and whose verdict was not "compliant". Identical verdicts, 96% fewer distinct
sensitive-column accesses on these 13 synthetic alerts: minimal disclosure is a policy of the planner, not a
loss of accuracy. With Nemotron drafting the SQL, the validator now keeps every filter and the column list of
an authorisation template, so a draft cannot widen what is read.

## Tests

`pytest -q` runs the H-MEM conformance vectors, the World Model conformance cases, and the
end-to-end loop in `SAMPLE_MODE` (ground truth, cross-alert world state, outage injection per source,
redaction, second-opinion escalation, critic-driven revision, confidence/priority/timeline, time budget,
verdict feedback, review-found regressions). 64 tests, one expected-fail that pins a known reference-implementation behaviour.

## Limits and honest notes

* Verdict logic is rule-based by design; the model drafts SQL and writes the summary. The rules
  cover the ten scenarios the generator produces, not every real-world policy.
* The reference engines are implemented in Python from the Meterless specs; Node is not required.
  The official TypeScript runners were not executed here, their cases were ported to pytest.
* Nightshift chooses the Nemotron model per request and currently prefers Nemotron 3 Ultra (550B), a
  reasoning model that spends 400–1400 tokens thinking before it answers; a live triage therefore takes
  4–5 minutes for five model calls, within the 5–10 minute budget. The stub path takes 2 seconds.
* Pre-existing components: no pre-existing product code. The Meterless engines are Python ports of the
  published reference implementations (Apache-2.0, see NOTICE); everything else was written during the build
  window (22–27 Oct 2026). Project licence: Apache-2.0.

## Repository layout

```
agent/            the agent: roles.py (four roles), zetaris.py (data layer), llm.py (Nightshift), memory.py + hmem/ (H-MEM),
                  world.py + worldmodel/ (World Model), trace.py, catalog.py, main.py (CLI)
data/             gen_data.py (synthetic world + ground truth), build_sample.py (SQLite twin), zetaris/register.py, sample/
infra/sources/    the three synthetic source databases (one Postgres server) on the Zetaris network
tests/            conformance ports + end-to-end
input_examples/ output_examples/ traces/
docs/             the official rules (markdown)
KICKOFF.md        the project's control page (scope, schedule, red lines)
```
