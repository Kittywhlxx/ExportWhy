# Live run examples (real Zetaris + Nightshift + Nemotron)

**AL-004** (recorded after the second review round, commit 0781267): 231 s, 7 logical model calls, status
complete. Nemotron drafted the SQL for five evidence classes; the authorisation lookups (policy, cited
ticket, other tickets, exceptions) were accepted because the drafts were canonically identical to the
fixed templates, the dataset lookup passed the column and predicate checks; two of them (policy, cited
ticket) are cross-source joins executed by Zetaris (`tables` in the trace: export_events + accounts +
policies, export_events + tickets). The second opinion (`nvidia/nemotron-3.5-super-vl-120b-a12b`) agreed with
the rules' *investigate* and named the scope mismatch; the Critic scored the first summary 6/9 (no next
step), the Reporter revised once, the second review scored 9/9.

**AL-011** (recorded before that round, 298 s): the same Proposer + Critic loop on a cannot-confirm case
(cited ticket does not exist); the second opinion agreed and gave three reasons. Kept as the historical
demonstration of the loop; it predates the stricter authorisation-query rule.

Every successful model call carries its routed model and receipt id. No secrets appear in these files.

**zetaris-rules/**: one trace per alert from the 13/13 live Zetaris verification with the stub model (rule verdicts only); see its README.
