# Opus operational-gate reconstruction audit

The saved historical Opus baseline arm cannot establish all three detailed Layer 1 gates faithfully.
The replacement arm therefore ran through `evals.runner` with
`config/evals/blog_opus5.json`, concurrency two, writing to the external
artifact store's `opus5-regen/` directory. The historical run remains the historical record.

| Gate | Saved evidence | Missing evidence |
| --- | --- | --- |
| OutputFormatSuccess | 44 successful statuses and one error status | The failed request’s error/stage and original full response |
| ExecutionHealth | Run status and aggregate tool-call counts | Per-call completion/error flags and tool-result error status |
| ReviewerReady | Nudge text, retrieved evidence and call counts | Patient summary, key findings and full execution trace |

The audit inspected `patient_runs.json`, `source_nudges.json`, the run log and the
historical run's serializer. Its metrics summarize token usage, latency and tool-call totals;
the tool ledgers preserve evidence spans. Neither substitutes for the missing
response or per-call error history. The log contains no summaries, key findings,
tool-call records or tool-result blocks. Silence in the log cannot establish
execution health. Filling these fields with defaults would manufacture results.

Regeneration keeps the frozen model/agent/generator/corpus/visit settings and uses
the current 600-second transport setting. The historical retention exception is
removed from the active config because the fresh run does not use old clients.
Frozen regression thresholds remain unchanged. The measured table uses this
complete replacement arm, without mixing historical and fresh patients.

The replacement completed on 2026-09-10: all 45 requests succeeded, with full
responses and execution traces saved. Deterministic replay and a task-level audit
established these results:

| Gate | Pass | Fail | Not recorded |
| --- | ---: | ---: | ---: |
| OutputFormatSuccess | 45 | 0 | 0 |
| ExecutionHealth | 44 | 1 | 0 |
| ReviewerReady | 45 | 0 | 0 |

The ExecutionHealth failure is a `GeneratedNudgeOutput` validation error recorded
as a tool result in an otherwise successful request; it remains in the comparison.
All 45 requests pass the frozen latency and token budgets. The audit confirmed
the exact cohort and settings,
600-second clients, and preservation of every captured field through replay.
No authentication errors or read timeouts were recorded.

The [final comparison](generator-comparison.md) uses these replacement results throughout.
The external store retains the generation in `opus5-regen/`, the deterministic
replay in `opus5-regen-evaluation/`, and the content-free checks in
`operations/evaluation-followups/fresh-opus-audit.json`.
