# Evaluating the agent: four layers

This document describes how the sample evaluates its own output. It is the design
behind the `evals/` package. The code in that package is the reference; this
document explains what each part establishes and what it does not.

The comparison permits up to five nudges per patient. A nudge names a proposed action,
the patient evidence behind it, and, when it rests on a guideline, the guideline
passage it cites. Every check below reads the nudge together with the **tool
ledger**: the record of every FHIR query, every guideline search, and every result
the agent saw during the run. Nothing is evaluated against evidence the agent did
not retrieve.

## The principle

Start with what you can verify. Use error analysis to define what you must judge.

Most evaluation failures in this project were not clinical errors. They were
evidence errors: a citation that did not resolve, a trend asserted from one data
point, a medication called active because a dispense record existed, an absence
claimed without a query. Code can catch all of these. A model judge is needed only
for what code cannot check, and a model judge must itself be tested before its
scores are trusted. Clinical appropriateness is judged by clinicians.

## The four layers

| Layer | Question | Method | Pass means | Does not mean |
|---|---|---|---|---|
| 1. Operational reliability | Did the run work? | Deterministic gates on the run record | The output is well formed and inside budget | Anything about content |
| 2. Evidence contract | Is each nudge verifiable against its retrieved evidence? | Deterministic verifier over nudge + tool ledger | The nudge is honest about its evidence | The nudge is clinically correct |
| 3. Evaluator validity | Can a model judge detect a known violation? | Negative control: break one evidence link, run the judge | The judge is trusted for that violation type | The judge is trusted for other types |
| 4. Clinical quality | Should a clinician see this nudge? | Human review with a fixed rubric | A clinician found it appropriate | Anything generalizable beyond the reviewed set |

Layers 1 to 3 are automated. Layer 4 is human. A higher layer presupposes the ones
below it: there is no point judging the evidence of a run that failed, or reviewing
the clinical quality of a nudge whose citation does not exist. Layers 3 and 4 are
independent of each other. Layer 3 validates evaluators, not nudges. A clinician can
review a run whose judge was never tested; the review is then not comparable to any
automated score, and that is the only consequence.

No layer's pass is evidence of clinical validity. Passing all four is necessary for
a trustworthy nudge. It is not sufficient.

## Layer 1: operational reliability

All gates are deterministic and read only the run record and trace. They ship in
`evals/gates.py`.

| Gate | Checks |
|---|---|
| Output format success | The structured-output stage produced a valid `NudgeResponse` |
| Execution health | Run status is success or partial; no tool call returned an error |
| Reviewer ready | Trace present; output within compact-output limits; tool-call budgets respected; no exact duplicate nudges |
| Latency budget | End-to-end latency under a configured ceiling |
| Token budget | Input and output token counts from the trace under a configured ceiling |

Latency and token ceilings are configuration. This document asserts no default. Set
them from your own baseline run before you use them as a gate.

## Layer 2: evidence contract

The **evidence contract** is the set of links a nudge must carry: to patient
evidence, to guideline evidence, to citation provenance, to temporal conditions, and
to its proposed action. The **deterministic verifier** checks each link against the
tool ledger. It makes no model calls.

Every check returns one of three verdicts:

- `satisfies_contract`: the evidence supports the claim.
- `violates_contract`: the evidence contradicts the claim, the cited passage does not
  support it, or the claim asserts something the ledger shows to be false.
- `abstain_insufficient_evidence`: the ledger does not contain what would be needed
  to decide.

A nudge passes Layer 2 when no check returns `violates_contract`. Abstentions are
counted and reported separately. They are never folded into pass or fail. A high
abstention rate is a retrieval problem or a coverage problem, and it is useful to
see it as such.

### Checks shown in the blog post

| Check | What it asks | Where it came from |
|---|---|---|
| Citation resolution | Does the cited source exist in the guideline catalog? | Unresolvable citations in early runs |
| Cited-passage identity | Does the cited text appear in a `search_guidelines` result in the ledger? | Citations to sources that were never retrieved |
| Medication status | When the nudge says a medication is active, does the record support that under the versioned rule? A dispense record alone does not | Dispense-implies-active inference, the most common audit failure |
| Absence within coverage | When the nudge says something is absent, did the agent query for it, and was the result untruncated? | Absence claims made under a truncated result window |
| Chart-state precondition: redundant order | When the nudge proposes an order, is that order already present in the retrieved record? | Clinician review: a redundant alert costs trust |

### Checks in the repository only

Source and passage identity, exact values, dates, units, arithmetic and trend
derivations, temporal conditions, derivation-chain integrity, action evidence chain.
These are correct and necessary. They are not shown in the post because each is a
one-line idea.

### Chart-state preconditions

A **chart-state precondition** checks that the nudge's action is not already
satisfied or made inapplicable by the record as retrieved. The redundant-order check
ships. Two more are documented extension points with the same trace-only posture:

- Discharge context: no discharge date in the chart, no discharge-medication nudge.
- Setting: an inpatient recommendation must not be stated in home-medication terms.

### A worked example of an evidence failure

An early run produced a nudge about ongoing blood loss for a patient whose record
held four days of stable hemoglobin. The agent had queried once, hit a result
truncation it did not report, and wrote a confident single-timepoint rationale. The
first clinician reviewer asked the same question the verifier now asks: what was the
last hemoglobin? After the tool layer began reporting truncation and the agent could
retrieve the series, it withdrew the framing on its own.

This is a Layer 2 failure, not a Layer 3 or Layer 4 one. No judge was needed to find
it, and the fix was in retrieval, not in the prompt.

### Generation-time enforcement

The same verifier runs inside the agent as a Strands steering handler. After the
model drafts a nudge, the handler replays the verifier over the draft and the ledger.
On failure it returns the failures to the model and asks for a retry, up to a
configured cap. Nudges that still fail are marked `contract_failures_unresolved` and
excluded from the output. The evaluator and the steering handler are the same code,
so a nudge that reaches the output has already passed the checks the evaluator will
run.

## Layer 3: evaluator validity

Readers will want a model judge that reads a nudge and its evidence and says
supported or not. Before trusting one, test it with a **negative control**.

Procedure:

1. Take every nudge from your run that passed Layer 2 and carries a resolvable
   citation, up to a configured cap.
2. Apply the fabrication operator: replace the citation with a plausible source that
   is not in the retrieved results. Exactly one link is broken. The right answer is
   now known: `violates_contract`.
3. Run the deterministic verifier. It must flag every control. This is true by
   construction and confirms the pipeline.
4. Run the holistic judge on the same controls.
5. If it misses a control, inspect the instruction responsible for the blind spot.
6. Make one targeted instruction change and give the prompt a new version.
7. Re-test on a fresh control set built from different eligible nudges, excluding
   every nudge in the first set. Keep the all-or-nothing criterion fixed.
8. Freeze and report both prompt/set rows, including misses and abstentions.

This is the intended Layer 3 loop: **control → blind spot → one-instruction fix →
held-out re-test**. A prompt edited after inspecting set A must not be evaluated
again on set A as evidence of improvement. The holdout is at nudge level; report
any patient overlap separately and do not imply a patient-disjoint study.

Pass condition, fixed before running: the judge returns `violates_contract` on every
control. One miss means the judge is not trusted for citation support, and any
downstream use of its citation-support score says so. A rate on five cases is noise;
all-or-nothing is the honest bar at this scale.

Rules:

- The judge must be from a different model family than the generator. Same-family
  judging is a known self-preference bias. If you do it anyway, disclose it.
- No control set ships in the repository. Controls are built from your own run, so
  no patient evidence is stored. The procedure works identically on MIMIC-IV demo and
  on Synthea data.
- Layer 3 runs once per judge version, not on every commit.

The research version of this layer tests four violation types across dev and
held-out splits and reports paired verdict transitions with layer attribution. The
sample shows one type so the procedure is clear.

## Layer 4: clinical quality

Only a clinician can judge whether a nudge is appropriate to show. The repository
ships a generator, not a judgment: `make_review_html` turns a run into a
self-contained HTML review page. Each nudge is shown with the FHIR results and
guideline excerpts behind it, because the first reviewer could not judge
appropriateness without seeing the evidence. The reviewer fills a CSV.

Rubric, one row per nudge:

| Column | Values | Asks |
|---|---|---|
| `acceptable_to_show` | yes / no | Would you accept seeing this in a clinical workflow? |
| `potentially_unsafe_or_misleading` | yes / no | Could this lead to harm or a wrong belief? |
| `actionable` | yes / no | Is there a clear action a clinician could take? |
| `redundant_or_low_value` | yes / no | Does the chart already cover this, or is it noise? |
| `priority_appropriate` | yes / no | Does the urgency label match the content? |
| `nudge_type` | clinical / administrative | Is this a care decision or a coding or documentation item? |
| `comment` | free text | Wording problems, including directive language and imprecise absence claims |

The CSV schema is the interface. Column names are stable; the rubric questions are
configurable in the generator.

Rules for this layer:

- No model score replaces a rubric column. A narrowly scoped model evaluator may sit
  beside a column only if it has a clinician-approved rubric and has passed its own
  Layer 3 control.
- Results describe the reviewed set. They are not a validation claim.
- Non-directive language is a boundary condition for the product, not only a style
  preference. The comment column is where a reviewer names a directive phrasing.

The rubric CSV is also the raw material for a later step this sample does not take.
With enough clinician reviews, a model judge can be shaped to agree with the
clinicians on these columns, and its agreement can be measured on held-out reviews.
That judge would then sit beside Layer 4 as a screening signal, subject to its own
Layer 3 control. Building it needs review volume and a clinician-owned rubric that
are out of scope here. The layer is designed so the data to do it accumulates from
the first review.

## Where each layer runs

| Setting | Layers | Cases | Output is |
|---|---|---|---|
| Regression dataset, CI/CD gate | 1 and 2, all checks | Fixed patient ids on a frozen corpus and generator config | Pass / fail per commit |
| Online evaluation, sampled traces | 1, and the Layer 2 checks that need no reference: citation resolution, cited-passage identity, budget exhaustion, redundant order | Production traces | A trend signal, not clinical-safety evidence |
| Offline, per judge version | 3 | Controls built from the latest passing run | Trusted / not trusted per violation type |
| Offline, per review cycle | 4 | A run bundle handed to a clinician | Rubric CSV |

Medication status and absence-within-coverage stay offline because they need the
versioned rule artifacts and full coverage records, not just the trace.

The deployment half of this table ships in the repository. `evals.agentcore_dataset`
publishes `config/evals/regression_dataset_v1.json` as an immutable AgentCore dataset
version. `evals.agentcore_gate` runs that version against a deployed candidate runtime
through the AgentCore on-demand dataset runner: the runtime returns its trace, tool
ledger, and verifier reports next to the response, so the gate applies the same Layer 1
gates and every Layer 2 check as the local runner, plus the frozen thresholds in
`config/evals/regression_thresholds_v1.json`, and exits nonzero on a regression. The
AgentCore built-in evaluators run over the collected CloudWatch spans as trend signals.
`terraform/evaluation.tf` wraps the gate in a CodeBuild project and adds an opt-in
AgentCore online evaluation configuration, disabled by default, with a configurable
sampling percentage. Commands, the known-bad fixture, cost, sampling guidance, and the
review-then-publish feedback loop are in the
[deployment guide](deployment-guide.md#candidate-acceptance-gate-and-online-evaluation).

The prompt under evaluation is part of a result's identity. Local runs record the
SHA-256 of every file under `prompts/`. Runtime runs and gate results additionally
record a `prompt` block: whether the base prompt came from the repository or from an
AgentCore configuration bundle, the base prompt's SHA-256, and the bundle id and
immutable version id. A gate run pins one bundle version and fails on any mismatch
between the version it pinned and the version the runtime reports, so a verdict is
never attributed to the wrong prompt ([deployment guide](deployment-guide.md#7-version-the-prompt-without-redeploying)).

## Case identity and caching

Every evaluation case has a name that carries the dataset version, the patient short
id, the nudge index, and the evaluator id. Every result is stored under a path that
carries the generator configuration hash, the corpus version, and the rule artifact
hashes. A change to any of these produces a different path, so a stale result can
never be read as current.

The regression dataset file holds patient ids and the run configuration. It holds no
patient data. Evidence is fetched at evaluation time from the configured FHIR
source.

## Concurrency

Deterministic evaluators run in parallel without restriction. Model judge calls take
a `max_concurrency` setting that defaults to 1: Bedrock throttling and reproducible
output order both favor serial by default. Generation keeps its existing thread pool.

## Vocabulary

Terms in bold above are defined in the project glossary. The ones that matter most
here: evidence contract, evidence span, tool ledger, deterministic verifier,
evidence contract verdict, chart-state precondition, negative control, evaluation
layer, clinical validity.

## Running the evaluations

Install the checkout with `uv sync --extra dev --extra evals`. Configure the FHIR
and OpenSearch endpoints in the ignored `config/settings.yaml`. The published
cohort uses the open-access MIMIC-IV FHIR demo, 45 audited patients, and the frozen
`guidelines-icu-baseline-v3` index. Each arm config contains identifiers and settings,
without patient records or retrieved guideline passages. Import the demo and build
the corpus using the repository's data preparation instructions first.

The executable entry point is `evals.runner`. It uses Strands `Experiment` for
inference, caching, and evaluation; `evals.compare` uses that same runner to replay
saved outputs without invoking the generator. The old DSPy `evaluation/` package
has been removed, along with its `run_experiment.py` entry points and their YAML
experiment files. The two `scripts/*/run_inference.py` scripts remain the quick way
to exercise the agent over local files or HealthLake; they share one batch loop and
write `InferenceReport` artifacts but are not part of the evaluation.

```bash
# Optionally verify one patient before your full run, in a separate directory.
uv run python -m evals.runner \
  --config config/evals/blog_sonnet5.json --limit 1 \
  --output-dir ~/nudge-evaluations/sonnet-smoke --profile YOUR_PROFILE

# Generate a complete arm after verifying the smoke's structured output.
uv run python -m evals.runner \
  --config config/evals/blog_sonnet5.json --concurrency 2 \
  --output-dir ~/nudge-evaluations/sonnet5 --profile YOUR_PROFILE

# Re-score the result directory printed by the runner. This makes no generator calls.
uv run python -m evals.compare \
  --config config/evals/blog_sonnet5.json --runs /absolute/path/printed/by/runner \
  --output-dir ~/nudge-evaluations/sonnet5-replay \
  --validate-judge --export-review --profile YOUR_PROFILE
```

After inspecting set A, test the single-rule v2 correction in a separate output
store. `--exclude-controls` points to set A's result directory containing `tasks/`:

```bash
uv run python -m evals.compare \
  --config config/evals/blog_sonnet5.json --runs /absolute/path/printed/by/runner \
  --output-dir ~/nudge-evaluations/sonnet5-judge-v2 \
  --validate-judge --judge-prompt-version v2 \
  --exclude-controls /absolute/path/to/set-a-control-result \
  --profile YOUR_PROFILE
```

V2 requires an exclusion set, and the runner rejects an undersized held-out set
before judge calls. Prompt content and excluded-control hashes enter provenance;
prompt hashes separate cached judgments. Preserve both completed result directories
and report them together. Do not retry misses or malformed judgments for a better
result, and do not overwrite the original prompt or set A.


The runner exits nonzero if any gate fails. Failed patient runs remain in the
cohort and cache, so restarting a command does not replace failures with favorable
samples. `compare` writes a descriptive report even when gates fail. It rejects
patient-set and generator-provenance mismatches. `--allow-partial` explicitly
permits an incomplete cohort for diagnostics; it does not produce a complete-arm
comparison. Failed runs may lack token usage, so reports include the number of
patients with measured tokens; a partial cost estimate is not a zero-cost failure.
Zero-filled token counters on a failed request are treated as unmeasured by both
the summary and the token-budget gate. They cannot pass that gate as zero usage.

To use all eligible demo patients, pass `--eligible-cohort /path/to/eligibility.json`
to `evals.runner`. The file must contain `eligible_patient_ids_anchored`, the list
from the demo eligibility audit. The published audit has 97 eligible patients;
the runner uses the supplied audit's actual distinct identifiers and records its
hash. This changes the dataset identity. Use the resulting manifest's `config`
when replaying a cohort override or smoke run.

### Comparison settings and provenance

The published comparison has two generators: **Opus 5 and Sonnet 5**.
`config/evals/blog_*.json` is the config swap point for readers who want to try
other generators. Copy an arm config, change both generator model-id fields
(`model.model_id` and `generator_config.model_id`), and record any required
provider-specific reasoning settings and pricing. Keep the entailment checker
fixed if you want to compare only generators. Other generators are not measured
in the published table. The results are in [the comparison report](generator-comparison.md).

The saved Opus system baseline uses adaptive thinking, high effort, 32,000 maximum
output tokens, prompt caching, no explicit temperature or thinking token budget,
five nudges, unlimited tool calls, backend-default result limits, and a 50-page
FHIR cap. `search_backend=auto` resolved to OpenSearch in that run; the comparison
pins the resolved OpenSearch backend. All arms retain the same custom instructions,
visit contexts, corpus, retry cap of three, and citation requirement.

The saved run's model/agent settings were recovered from the unchanged endpoint-only
settings file and the frozen runtime defaults. Its generator provenance is stored
with every patient. This is the paper's `p08-steered-generator-v4`; an older
eight-patient tool-budget experiment with the same name prefix is not the
reference for this comparison. The historical Opus baseline files could not
faithfully reconstruct all three detailed operational gates, so the complete Opus
arm was regenerated with the current runner. The [reconstruction audit](opus-arm-reconstruction.md)
records the missing evidence and route. That run remains historical; the comparison
never mixes its patients with fresh generations. Readers generate their own Opus
arm with `evals.runner` and `config/evals/blog_opus5.json`.

The generator model changes between arms. The steering entailment checker remains
`us.anthropic.claude-opus-5` in both, keeping that component constant. Both use
adaptive thinking and high effort. For reader-supplied generators, Anthropic-only
thinking/output-config fields are sent only to Anthropic models; equal effort names
do not establish equal compute. New generator clients use a 600-second per-call Bedrock read timeout and
`retries={"max_attempts": 2}`. `model.read_timeout` configures the ceiling and is
recorded in run identity and actual client provenance. This matches the repository's
600-second AgentCore-client precedent.

The citation-control judge is `us.openai.gpt-5.6-sol`, high reasoning effort,
maximum concurrency one. Completed valid judgments are cached by the exact evidence
view, prompt hash, model, effort, and output-token limit. Changing any of these
invalidates the judgment. Transport errors and malformed JSON count as control
misses. Zero admitted controls never establishes trust. Control reports preserve
the original three-state verdict; admission requires a resolved cited source and
passage and no established contract violation, while unrelated abstentions remain
visible. Passing these controls establishes detection of this citation corruption,
not clinical quality or general judge validity.

Set A contains five Sonnet citation controls. GPT-5.6 Sol with
`holistic_judge_v1.md` detected two and abstained on three, failing the fixed bar.
The abstentions expose an instruction gap: v1 treats an absent cited source as
missing support. `holistic_judge_v2.md` adds one rule: a cited source/section absent
from the retrieved passages is a citation-provenance violation. V2 is evaluated
only on set B, five different eligible Sonnet nudges; v1/set A stays frozen.
The frozen outcome is **v1 / A: 2 of 5, failed; v2 / B: 5 of 5, passed**. Set B
shares neither nudges nor patients with A. The [validation record](judge-validation.md)
reports selection and provenance. V2 passes only for this corruption on this set,
not general citation entailment. If a held-out re-test fails, the deterministic
verifier remains the only trusted citation-provenance check on those controls.

The five highlighted Layer 2 checks are counted independently. An established
failure from any other repository verifier check also fails the nudge and gate,
and is counted in `additional_contract_failures`. Repository-only abstentions also
remain abstentions in the aggregate nudge verdict, even when the highlighted checks
satisfy their narrower contracts. The redundant-order check is
conservative: an unqualified imperative must name exactly the active order in an
active retrieved encounter. Conditional/repeat orders, missing encounter evidence,
and fuzzy name matches abstain. This new check runs during evaluation; it was not
part of the frozen baseline's generation-time steering.

The historical Opus artifacts omit the full response and execution trace.
Completion, latency, tokens, steering outcomes and saved evidence can be replayed,
but detailed operational gates cannot all be reconstructed. The fresh Opus arm
captures those fields; historical missing gates are not silently relabeled as passes.

The legacy per-nudge serializer omitted missing-citation generation requirements
from exclusion flags even when the final steering attempt recorded those failures.
The corrected serializer retains them. Replay applies the same correction to saved
arms only when their provenance requires a citation and their final unresolved
attempt records a generation-requirement failure. It preserves the source files,
reports original exclusion counts separately, and filters the replayed output
consistently. These are generation-requirement exclusions, not new deterministic
Layer 2 violations. The preserved historical Opus replay has 32 source-recorded exclusions
plus nine recovered exclusions, leaving 66 eligible nudges; its original paper
table retains 75. Fresh runs record the generation-requirement exclusions directly. Generation guidance, retry decisions, and model calls are unchanged.

Cost is a rate-card estimate of measured generator usage, including separately
priced cache reads and writes. Arm configs record rates and retrieval date. The
estimate excludes steering entailment calls, the evaluator, infrastructure, and
unmeasured failed requests. It is not the total bill. The comparison uses US-geo
rates from the [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/),
including the 5-minute Anthropic cache-write rate.

### Review and regression artifacts

`--export-review` produces a CSV, trace files, and a standalone vC HTML page with
FHIR and guideline evidence panels. It first applies the current Layer 1 gates and
excludes nudges with current Layer 2 violations. `attrition.json` records omissions;
no HTML is created when nothing is eligible. Keep the directory outside all worktrees.
To regenerate it with different scenario labels or rubric wording:

```bash
uv run python -m evals.review.make_review_html \
  --csv ~/nudge-evaluations/review/nudges_review.csv \
  --trace-root ~/nudge-evaluations/review \
  --scenario-labels /path/to/labels.json \
  --rubric-questions /path/to/questions.json \
  --out ~/nudge-evaluations/review/review.html
```

Both JSON options are optional mappings: scenario id to display label, and rubric
column to question text. Column names and allowed answers remain stable. The
human `nudge_type` column accepts `clinical`/`administrative`; the generator's
original subtype lives in `generated_nudge_type`. Answers persist locally under a
bundle-specific browser storage key and export through the page's CSV button.
Rebuilding from an answered CSV preserves imported answers before browser overrides.
The template loads no external fonts or scripts. No cross-arm clinician review is
implied by the generator comparison.

The completed Sonnet export contains 28 nudges across 17 patients. Nineteen patients
fail at least one Layer 1 gate, omitting nine otherwise-retained nudges. The bundle
includes its attrition and source manifest; all rubric answers remain blank.
Producing this bundle does not constitute clinician review.

`--export-regression` on the frozen Opus replay writes predefined AgentCore
scenarios, following the [AgentCore dataset schema](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations-schema.html).
The initial checked-in `config/evals/regression_dataset_v1.json` includes the six
readiness-cohort patients that overlap the audited 45 plus two low-retry controls.
It contains only identifiers, run configuration, and behavioral assertions.
Historical readiness defects motivate these cases; a patient's inclusion is not
a new clinician judgment. Positive expectations require completion and citation
provenance. Negative expectations prohibit unsupported medication status,
uncovered absence claims, and provably redundant direct orders. Unresolved steering
failures are retained as exclusions, never handed to the reviewer as clean output.

```bash
uv run python -m evals.runner \
  --config config/evals/blog_opus5.json \
  --regression-dataset config/evals/regression_dataset_v1.json \
  --output-dir ~/nudge-evaluations/regression --profile YOUR_PROFILE
```

The regression dataset must match the model, agent, and generator configuration.
Its hash and version enter the run identity. Frozen ceilings in
`config/evals/regression_thresholds_v1.json` are 632,967 ms and 1,094,842 total tokens
per patient (1.25 times the saved Opus maxima), zero established violations, and
100% completion. These are conservative regression limits, not clinical or
production service-level targets. Latency/token ceilings are also pinned in the
arm configs. CI/CD wiring and online sampling belong to the deployment work.

The regression JSON was validated through AgentCore SDK 1.22.0
`FileDatasetProvider`. This schema check used an isolated environment; generation
retains the saved run's dependency versions (Strands 1.51.0, AgentCore 1.1.1,
boto3/botocore 1.42.10, Pydantic 2.12.5). The generation prompts and specialty
instructions match the frozen source.

For the browser integration test, install Playwright and Chromium outside all
worktrees in the durable artifact store. Set `NODE_PATH` and
`PLAYWRIGHT_BROWSERS_PATH` to those installations, and
run `uv run pytest tests/test_review_browser.py`. It exercises the full review → CSV
→ regenerated HTML → CSV workflow with synthetic data.

The annotation HTTP API remains available. It stores its JSON outside the checkout
at `~/.local/share/medical-nudging/annotations.json` by default; set
`NUDGE_ANNOTATIONS_FILE` before starting the API to use another external path. If
you have a legacy `evaluation/data/error_taxonomy.json`, move it outside all
worktrees and point that variable at it. It is user data and is not migrated into
the source tree.


### Transport and artifact provenance

Both comparison arm configs use a 600-second Bedrock read timeout and
concurrency two. The historical Opus baseline run used 120-second clients; its explicit
retention exception belongs to the preserved historical config. The active Opus
config removes that exception for the fresh replacement arm. Sonnet's completed
run also uses 600-second clients throughout. Only one generator arm runs at a time.

All run outputs, logs, controls, and populated review files live outside worktrees
in one durable artifact store with one directory per generation effort. Never
write run artifacts under a worktree or `/tmp`. Historical Opus artifacts remain
unchanged in their original directory; the measured replacement is in
`opus5-regen/`. Frozen regression thresholds do not change on export.

The FHIR client normalizes each next-page query component independently, preserving
existing escapes and literal `+` characters. This fixes a signature mismatch when
encoded query values and raw page-token padding occur together. The real-client
pagination integration test covers this mixed case, and live two-page checks pass.
Neither completed arm has a recorded HTTP 403. This transport fix leaves prompts,
steering rules, and evaluation criteria unchanged.
