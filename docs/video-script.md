# ExportWhy — demo video script (target 3:30, hard max 4:00)

Record at 1920×1080, 30 fps. Screen capture of the UI (offline mode for responsiveness, one live
run pre-recorded), the Zetaris Data Explorer, a terminal, and one Nightshift window. Voice-over in
English; captions on. No keys or tokens on screen: `.env` never appears; the Nightshift Console is
shown with the key blurred or not shown at all.

Prepare before recording:
- `rm -rf memory && SAMPLE_MODE=true python -m agent.main alert AL-001` (so the World Model knows u1022)
- a finished live run of AL-004 (`python -m agent.main alert AL-004`) with its report and trace open
- UI running (`streamlit run ui/app.py`), mode "Offline sample"
- Zetaris Data Explorer open on the three datasources and the `dexa` container
- terminal with `logs/trace-<live run id>.jsonl` ready to `grep model`

| time | screen | voice-over |
|---|---|---|
| 0:00–0:20 | Title card, then the alert text on a plain slide: *"u1012 exported 48,000 rows of payment_transactions to an external bucket at 03:30. Cited ticket T-2629."* | "A security analyst gets this alert. Was the export authorised? The export log, the data-access policy and the approval tickets each hold part of the answer, and none of them talks to the others. Today the analyst joins them by hand. ExportWhy does the investigation and explains why." |
| 0:20–0:40 | Zetaris Data Explorer: three datasources (logsdb, policydb, ticketsdb) and the `dexa` container; run one federated query in the SQL editor (alert ⋈ event ⋈ ticket ⋈ policy) | "The three sources are three separate databases. Zetaris registers them as three datasources and exposes one schema. The agent only ever writes SQL against that schema; the cross-source joins run in Zetaris." |
| 0:40–1:25 | UI: pick AL-004, press Triage. Pause on the banner: *INVESTIGATE · confidence · priority P1*. Scroll the right-hand timeline slowly: planner → retriever → evaluator "needs more: ticket_cited" → planner → retriever → evaluator "needs more: tickets_account, exceptions" → verdict | "Watch the loop. The Planner asks for the policy and the cited ticket. The Evaluator finds that ticket T-2629 allows 5,000 rows, not 48,000, so it sends the Planner back for other tickets and standing exceptions. Nothing covers the export. Verdict: investigate. Each round fetched only what the decision needed." |
| 1:25–1:50 | Report body: read one evidence line with its citation; "Conflicts" section; "Suggested next checks"; the redacted destination | "Every claim cites the table, the column and the query it came from. The conflict is stated, not resolved away: the ticket was cited but does not cover the export. The destination is a confidential column: it was read because this export left the company, and it is redacted in the report." |
| 1:50–2:15 | UI: pick AL-011, Triage. Banner: *CANNOT-CONFIRM*. "Cannot be confirmed from the sources: cited ticket T-9999 does not exist". Then the "World state" block: *earlier alert AL-001: verdict compliant* | "When the evidence is missing, the agent says so. T-9999 is not in the ticket system, so this is cannot-confirm, not a guess. And it remembers: the World Model knows this account from an earlier alert." |
| 2:15–2:40 | Pre-recorded live run of AL-004: the "Second opinion" and "Review (Critic)" sections; terminal: `grep '"model"' logs/trace-<id>.jsonl | head` showing `nvidia/nemotron-...` and receipt ids; Nightshift window (no key visible) | "With Meterless Nightshift as the provider, Nemotron drafts the SQL, gives an independent second opinion on the same facts, and scores the written summary as a critic. Every call is logged with the model that served it. The model can make a verdict more cautious; it cannot turn a block into a compliant." |
| 2:40–3:00 | UI "Correct the agent": click **wrong** on one fact; confidence drops 0.95 → 0.75, tag `review`, ledger row appears. Then **👎 disagree** on the verdict; calibration line updates | "The analyst stays in the loop. Marking a fact wrong goes into the H-MEM trust ledger: confidence drops, the record is tagged for review, and it ranks lower next time. Verdict feedback feeds a calibration tally." |
| 3:00–3:20 | Sidebar: tick "Simulate an outage: tickets", Triage AL-001 → *CANNOT-CONFIRM · the ticket source could not be queried* | "If a source is down, the agent degrades instead of guessing: the ticket system is unreachable, so an alert that was compliant a minute ago becomes cannot-confirm with a retry instruction." |
| 3:20–3:35 | Slide: the ablation table (naive 113 · this system 3 · with Nemotron 5 sensitive column reads; identical verdicts) and "156/156 across 12 seeds" | "Same verdicts, thirty-eight times less sensitive data read: minimal disclosure is a property of the planner, not a loss of accuracy. Across twelve generated worlds, every verdict matched the ground truth." |
| 3:35–3:50 | End card: repo URL, "Zetaris · Meterless H-MEM + World Model · Nightshift · Nemotron", one line: *an answer with its evidence, and an honest list of what it could not confirm* | "ExportWhy: Zetaris as the data layer, two Meterless engines on the critical path, Nemotron through Nightshift. An answer with its evidence, and an honest list of what it could not confirm." |

Recording notes:
- Offline mode makes each triage return in 2 seconds; say once (at 2:15) that the live run takes 4–6 minutes and was pre-recorded.
- Keep the mouse still while reading citations; zoom (Cmd + scroll) on the evidence line.
- If a take runs long, cut 1:50–2:15 to the World state block only.
