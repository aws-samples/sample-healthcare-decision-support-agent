---
status: accepted
date: 2026-08-18
---

# Use isolated Strands Shell for calculations

The nudging agent uses executable code only for small arithmetic, date, percentage, and trend calculations. A live benchmark in a development account measured AgentCore Code Interpreter at 2.25 seconds for the first execution in a new session and 0.20 seconds for a second execution, while the baseline used it in only 3 of 36 patient requests. On the same development machine, 50 isolated Strands Shell samples had median construction and execution times of 0.414 ms and 0.136 ms respectively. We will replace AgentCore Code Interpreter with a request-scoped `calculate(script)` tool backed by embedded Lua 5.4. Each tool invocation creates a fresh shell on the executing thread.

## Considered Options

- Reusing AgentCore sessions reduced startup but caused stale process-global session mappings and cross-request lifecycle failures.
- A fresh AgentCore session per request fixed isolation but retained a remote service dependency, IAM permissions, and avoidable startup for a narrow calculation use case.
- Retaining one Strands Shell per request would preserve temporary state, but the Python binding is thread-affine while Strands schedules synchronous tool calls on worker threads.
- A generic shell tool would expose more capability than the clinical agent needs.

## Consequences

Each calculation has an empty in-memory VFS, no host binds, no network allowlist, no credentials, a five-second timeout, and bounded resources. Calculations cannot exchange temporary files or other shell state, which matches the tool's arithmetic-only contract and prevents cross-patient state leakage. This is an in-process mediation layer rather than microVM isolation, so it is appropriate only for the current single-owner calculation workload. If the agent later requires Python packages, arbitrary binaries, persistent execution state, or adversarial multi-tenant execution, introduce a separate hardened execution adapter rather than widening `calculate`.
