# Generator comparison: Opus 5 and Sonnet 5

The final comparison uses fresh, complete Opus 5 and Sonnet 5 arms on the same 45 audited MIMIC-IV FHIR demo patients, v4 generator configuration and `guidelines-icu-baseline-v3` corpus. Both capture full responses and execution traces. The entailment checker stays Opus 5. Other generators are a reader config swap at `config/evals/blog_*.json`; see `docs/evaluation.md`.

Contains information from MIMIC-IV Clinical Database Demo on FHIR, which is made available here under the Open Database License (ODbL).

Data references supplied in `scripts/mimic/fetch_demo.py`: Bennett et al. (2025), *MIMIC-IV Clinical Database Demo on FHIR*, version 2.1.0, PhysioNet, doi:10.13026/vphg-y548; Bennett et al. (2023), *MIMIC-IV on FHIR: converting a decade of in-patient data into an exchangeable, interoperable format*, JAMIA 30(4):718–725; Pollard et al. (2026), *PhysioNet as a global platform for biomedical research*, Nature Health.

## Operational measurements and steering yield

| Metric | Opus 5 | Sonnet 5 |
| --- | --- | --- |
| Successful requests | 45/45 | 45/45 |
| Median latency (seconds) | 266.2 | 303.5 |
| Patients with measured latency | 45 | 45 |
| Input tokens | 17,405,934 | 20,395,980 |
| Output tokens | 805,116 | 1,239,090 |
| Cache-read input tokens | 2,134,776 | 2,546,500 |
| Cache-write input tokens | 10,864 | 11,000 |
| Patients with measured token usage | 45/45 | 45/45 |
| Estimated generator cost (USD) | 119.12 | 59.09 |
| Candidate nudges | 112 | 113 |
| Source-recorded exclusions | 35 | 16 |
| Recovered missing-citation exclusions | 0 | 60 |
| Total exclusions | 35 | 76 |
| Retained nudges | 77 | 37 |
| Patients with retained nudges | 40 | 23 |
| Guided retries | 111 | 125 |

Completion is request status, not all operational gates passing. Medians include recorded failed-request latency. Retained counts do not establish evidence completeness or clinical superiority.

Costs are configured rate-card estimates of measured generator usage, including separately priced cache reads/writes. They exclude entailment, judge calls, infrastructure, discarded attempts and unmeasured failures. They are not the total bill. Per-million rates (input/output/cache read/cache write) are $5.50/$27.50/$0.55/$6.875 for Opus and $2.20/$11/$0.22/$2.75 for Sonnet, recorded on 2026-09-09.

Fresh Opus uses the corrected serializer. Replay reconciles the earlier Sonnet serializer’s missing-citation exclusions from recorded final steering failures, while preserving original files and source counts. These are generation requirements, not new deterministic Layer 2 violations. Neither prompts nor steering decisions are changed during replay.

## Layer 1: measured operational gates

Cells are pass / fail / not recorded. All three detailed Opus gates now have real results from captured responses and traces; none is reconstructed from default fields. Missing token measurements cannot pass a budget gate.

| Gate | Opus 5 | Sonnet 5 |
| --- | --- | --- |
| completion | 45 / 0 / 0 | 45 / 0 / 0 |
| OutputFormatSuccess | 45 / 0 / 0 | 45 / 0 / 0 |
| ExecutionHealth | 44 / 1 / 0 | 27 / 18 / 0 |
| ReviewerReady | 45 / 0 / 0 | 35 / 10 / 0 |
| latency_budget | 45 / 0 / 0 | 45 / 0 / 0 |
| token_budget | 45 / 0 / 0 | 45 / 0 / 0 |

ExecutionHealth includes tool errors even when a request eventually succeeds. ReviewerReady checks output-length bounds, trace availability, configured tool budgets, exact duplicates and retrieved citation sources. Sonnet’s 18 ExecutionHealth failures include 73 structured-output schema errors; its ten ReviewerReady failures concern word-count bounds. Detailed gate results for both arms remain in the saved Experiment reports.

## Layer 2: five evidence-contract checks

Cells are satisfies / violates / abstains / not applicable over all retained nudges. These diagnostic denominators include patients that fail Layer 1; clinician-review export separately requires current Layer 1 gates.

| Check | Opus 5 | Sonnet 5 |
| --- | --- | --- |
| Citation resolution | 77 / 0 / 0 / 0 | 37 / 0 / 0 / 0 |
| Cited-passage identity | 77 / 0 / 0 / 0 | 37 / 0 / 0 / 0 |
| Medication status | 4 / 0 / 21 / 52 | 2 / 0 / 9 / 26 |
| Absence within coverage | 0 / 0 / 0 / 77 | 0 / 0 / 0 / 37 |
| Redundant order | 0 / 0 / 45 / 32 | 0 / 0 / 20 / 17 |

| Whole-nudge verdict | Opus 5 | Sonnet 5 |
| --- | --- | --- |
| satisfies_contract | 0 | 0 |
| violates_contract | 0 | 0 |
| abstain_insufficient_evidence | 77 | 37 |

Whole-nudge verdicts preserve failures and abstentions from repository checks beyond the five highlighted here. Resolved citation links and zero established violations do not establish complete support or clinical validity.

## Layer 3: control, instruction fix, held-out re-test

| Prompt / set | Source | Controls | Verifier detected | Sol detected | All-controls criterion |
| --- | --- | --- | --- | --- | --- |
| v1 / A | Sonnet 5 | 5 | 5 | 2 | Failed |
| v2 / B | Sonnet 5 | 5 | 5 | 5 | Passed |

GPT-5.6 Sol uses high effort, maximum concurrency one and 2,048 output tokens in both tests. V1 detected two controls and abstained on three. Its missing-support rule left an ambiguity when a claimed citation was absent from retrieved passages. V2 adds one instruction: an absent cited source/section is a citation-provenance violation.

Set B was frozen before judging: the next five eligible cited Sonnet nudges in deterministic patient-id/nudge-index order, excluding set A. Each original has resolved source/passage links and no established Layer 2 violation; unrelated abstentions remain visible. Sets A and B share zero nudges and zero patients (three patients in A, four in B). The verifier detects every fabricated citation. V2 was never tested on A; A’s prompt, tasks, report and judgments remain unchanged. No miss was retried.

V2 passes the fixed all-controls criterion for this citation corruption on the held-out set. This is not a general citation-entailment or clinical-quality result.

Both prompt/set rows are frozen and reported without pooling. Because the sets differ, these small counts do not estimate a paired effect or general judge accuracy. The intended loop and reproducible exclusion checks are in `docs/evaluation.md`; `docs/judge-validation.md` records the detailed validation.

## Layer 4: clinician-review bundle

The Sonnet bundle contains **28 nudges across 17 patients**, with FHIR and guideline evidence panels, six rubric columns and a comment field. All ratings remain blank. No clinician review or cross-arm clinical comparison has been conducted.

| Review attrition | Count |
| --- | --- |
| Source patients | 45 |
| Patients failing at least one Layer 1 gate | 19 |
| Patients passing all Layer 1 gates | 26 |
| Retained nudges before Layer 1 filtering | 37 |
| Retained nudges omitted by Layer 1 | 9 |
| Steering exclusions among Layer 1 survivors | 52 |
| Additional nudges excluded by Layer 2 | 0 |
| Review rows | 28 |
| Patients represented in review | 17 |

The standalone HTML, `nudges_review.csv`, traces, attrition and source manifest remain in the external artifact store under `review/`. `priority_appropriate` and human `nudge_type` are rubric columns; the generator’s subtype stays in `generated_nudge_type`. Clinician review is independent of the model judge’s citation-provenance validation. Abstentions remain unresolved evidence.

## Opus route and fixed settings

The initial table reused the historical Opus baseline run. Before accepting its missing operational measurements, we inspected `patient_runs.json`, `source_nudges.json`, the run log and that run's serializer. They retain status, aggregate tool metrics and evidence, but not patient summaries/key findings or per-call error/completion records. The failed request also lacks its error/stage detail. All three gates therefore could not be faithfully reconstructed; `docs/opus-arm-reconstruction.md` records the audit.

The authorized replacement Opus arm ran all 45 patients through the current runner, into the artifact store's `opus5-regen/` directory, and was deterministically replayed into `opus5-regen-evaluation/`. Every main-table Opus result comes from that replacement. The historical baseline and the earlier reused-arm report remain historical; fresh and historical patients are never mixed.

Both arms retain adaptive thinking, high effort, 32,000 maximum output tokens, prompt caching, five nudges, unlimited tool calls, OpenSearch, a 50-page FHIR cap, retry cap three and the same frozen instructions and visit contexts. No explicit temperature or thinking budget is sent. Both actual clients use a 600-second read timeout and two configured retries. Generation runs one arm at a time at concurrency two; credentials and bounded HealthLake pagination were checked before replacement. Source manifests preserve the generation implementation identity; the later judge-prompt change does not alter the generator prompt.

## Historical retrieval context

The historical Opus baseline supplies retrieval context without new ablation runs. These are the original, unreconciled 2026-09-01 results using Opus 5, not the fresh generator arm above.

| Metric | Agentic retrieval | Single-pass retrieval | No retrieval |
| --- | --- | --- | --- |
| Successful requests | 44/45 | 45/45 | 45/45 |
| Candidate nudges | 107 | 54 | 167 |
| Original exclusions | 32 | 7 | 3 |
| Original retained nudges | 75 | 47 | 164 |
| Retained nudges with citations | 66 | 43 | 0 |
| Guided retries | 113 | 82 | 31 |
| Median latency (seconds) | 262.6 | 167.7 | 103.7 |
| Historical generator estimate (USD) | 99.93 | 69.79 | 38.67 |

Single-pass retrieval used 22 passages; no retrieval disabled citation and entailment requirements. Retention therefore does not establish clinical quality or evidence superiority. Historical estimates used $5/$25 per million input/output tokens and excluded cache usage; do not compare them directly with current estimates. Source: the frozen historical baseline table.

## Regression handoff and validation

The eight-scenario AgentCore dataset and thresholds remain frozen from the original handoff: six readiness-cohort cases and two low-retry controls, 632,967ms, 1,094,842 input-plus-output tokens, zero established violations and 100% completion. The replacement run does not select a new regression subset or recalibrate limits. These are regression criteria, not clinical or production service-level targets.

The full test suite passed at the time of the comparison, including the headless review round trip, and the evaluation integration tests passed after stricter holdout provenance validation. The package builds. The populated review’s 28 rows were navigated in headless Chromium without page errors or network requests; its blank CSV export exactly matches its input. Frozen Opus source/regression and both control-set hashes are audited before publication.

Reproduction commands are in `docs/evaluation.md`. Published generator measurements use the audited 45-patient cohort; readers can use the eligibility audit to run all 97 eligible patients. Populated outputs stay outside every Git worktree. Only aggregate reports, source and configs enter Git.
