# Citation-provenance judge validation

The intended Layer 3 loop found an instruction gap, applied one instruction fix,
and tested it on held-out controls. Both outcomes are frozen.

| Prompt / set | Sonnet source nudges | Verifier detected | Sol detected | Sol abstained | All-controls criterion |
| --- | --- | --- | --- | --- | --- |
| v1 / A | 5 | 5 | 2 | 3 | Failed |
| v2 / B | 5 | 5 | 5 | 0 | Passed |

Sol is `us.openai.gpt-5.6-sol`, high effort, maximum concurrency one and 2,048
output tokens, unchanged between tests. Every admitted control must receive
`violates_contract`; abstentions and errors count as misses.

V1 maps missing support to abstention. Its three abstentions exposed ambiguity
when a claimed citation is absent from the supplied passages. V2 adds one rule:
if the cited source/section does not appear among retrieved guideline passages,
that is a citation-provenance failure and the verdict is `violates_contract`.
The original v1 prompt is unchanged.

Set B was frozen before any v2 call: the next five eligible nudges in deterministic
patient-id/nudge-index order, excluding every set A identity. Each original is a
retained Sonnet nudge with resolved source/passage links and no established Layer 2
violation. Unrelated original abstentions remain visible. The fabrication operator
breaks a citation link, and the verifier detects all five corruptions.

Sets A and B share **zero nudges and zero patients**: A spans three patients, B four.
V2 was never run on set A. The v1 tasks, report, cached judgments and prompt remain
byte-for-byte unchanged. No missed judgment was retried. The tested sets differ,
so these small counts do not estimate a paired effect or general judge accuracy.

V2 passes for this citation-provenance corruption on the held-out set. This does
not establish general citation entailment, full evidence support or clinical
validity. Layer 4 remains independent clinician review with its fixed rubric.

Both sets come from the completed Sonnet 5 arm; entailment during generation stays
Opus 5. The external artifact store records the source summary, selection, exact
case inputs, prompt/config hashes, model settings and results. Set A is indexed under
`controls/sonnet5/` in that store; set B is under
`controls/sonnet5-v2-set-b/`, with `freeze-before-judging.json` and
`freeze-completed.json`. No patient evidence ships in the repository.

Reproduction and held-out exclusion checks are documented in `docs/evaluation.md`.
The evaluation integration suite and the full test suite passed after provenance
hardening, including the headless review round trip.

Contains information from MIMIC-IV Clinical Database Demo on FHIR, which is made
available here under the Open Database License (ODbL).
