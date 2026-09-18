"""Inference functions for running medical nudging on patient data.

This module provides the core inference logic used by scripts/run_inference.py,
supporting both local orchestrator and AgentCore runtime modes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import hashlib
import time
from datetime import datetime
from typing import Callable, Sequence, TypeVar

from .models import InferenceReport, InferenceResult

log = logging.getLogger("inference")

T = TypeVar("T")
R = TypeVar("R")


def run_bounded_concurrently(
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    max_concurrency: int,
    on_complete: Callable[[T, R], None] | None = None,
) -> list[R]:
    """Run independent work with a fixed bound while preserving input order."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")

    if max_concurrency == 1:
        results = []
        for item in items:
            result = worker(item)
            results.append(result)
            if on_complete:
                on_complete(item, result)
        return results

    indexed_results: dict[int, R] = {}
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = {executor.submit(worker, item): (index, item) for index, item in enumerate(items)}
        for future in as_completed(futures):
            index, item = futures[future]
            result = future.result()
            indexed_results[index] = result
            if on_complete:
                on_complete(item, result)

    return [indexed_results[index] for index in range(len(items))]


def build_inference_cache_key(
    model_id: str,
    patient_identity: str | bytes,
    context_condition: str,
    visit_context: dict,
    runtime_config: dict | None = None,
) -> str:
    """Build the cache key shared by inference and experiment runners."""
    patient_bytes = (
        patient_identity.encode() if isinstance(patient_identity, str) else patient_identity
    )
    patient_hash = hashlib.md5(patient_bytes, usedforsecurity=False).hexdigest()[:8]
    visit_hash = hashlib.md5(
        json.dumps(visit_context, sort_keys=True).encode(), usedforsecurity=False
    ).hexdigest()[:8]
    model_key = model_id.replace("/", "_").replace(":", "_")
    key = f"{model_key}:{patient_hash}:{context_condition}:{visit_hash}"
    if runtime_config:
        runtime_hash = hashlib.md5(
            json.dumps(runtime_config, sort_keys=True).encode(),
            usedforsecurity=False,
        ).hexdigest()[:8]
        key = f"{key}:{runtime_hash}"
    return key


def build_request_body(patient_data: str, fmt: str, visit_context: dict) -> dict:
    """Build request body for inference.

    Args:
        patient_data: Raw patient data (CCDA XML or FHIR JSON)
        fmt: Format identifier ("ccda" or "fhir")
        visit_context: Visit context dictionary

    Returns:
        Request body dictionary
    """
    body = {"visit_context": visit_context, "config": {"max_nudges": 5}}
    body["ccda_xml" if fmt == "ccda" else "fhir_json"] = patient_data
    return body


def run_local(
    sample: dict,
    visit_context: dict,
    enable_trace: bool = False,
    model_id: str | None = None,
    context_condition: str = "full",
    extra_headers: dict | None = None,
    response_config: dict | None = None,
) -> tuple[InferenceResult, dict | None]:
    """Run inference using local orchestrator.

    Args:
        sample: Sample file info dict with keys: sample_id, file_path, format
        visit_context: Visit context for inference
        enable_trace: If True, capture and return execution trace
        model_id: Optional model ID override (default: from config)
        context_condition: Context condition for guidelines: full, summaries_only, no_guidelines
        extra_headers: Additional Bedrock request fields (e.g., anthropic-beta for 1M context)
        response_config: Response constraints such as ``max_nudges``

    Returns:
        Tuple of (InferenceResult, trace_dict or None)
    """
    from .agents.orchestrator import MedicalNudgingOrchestrator

    with open(sample["file_path"]) as f:
        patient_data = f.read()

    orchestrator = MedicalNudgingOrchestrator(
        model_id=model_id,
        context_condition=context_condition,
        extra_headers=extra_headers,
    )
    start = time.perf_counter()
    trace_dict: dict | None = None

    try:
        if enable_trace:
            response, trace = orchestrator.generate_nudges_with_trace(
                patient_data,
                visit_context,
                config=response_config,
            )
            trace_dict = trace.to_dict()
        else:
            response = orchestrator.generate_nudges(
                patient_data,
                visit_context,
                config=response_config,
            )

        return (
            InferenceResult(
                sample_id=sample["sample_id"],
                file_path=sample["file_path"],
                format=sample["format"],
                source="local",
                status=response.status,
                latency_ms=int((time.perf_counter() - start) * 1000),
                nudge_count=len(response.nudges),
                categories_used=list({n.category for n in response.nudges}),
                nudge_types_used=list({n.nudge_type for n in response.nudges}),
                error=response.error,
                warnings=response.warnings,
                nudges=[n.model_dump() for n in response.nudges],
                key_findings=response.key_findings,
                patient_summary=response.patient_summary,
            ),
            trace_dict,
        )
    except Exception as e:
        return (
            InferenceResult(
                sample_id=sample["sample_id"],
                file_path=sample["file_path"],
                format=sample["format"],
                source="local",
                status="error",
                latency_ms=int((time.perf_counter() - start) * 1000),
                nudge_count=0,
                error=str(e),
            ),
            trace_dict,
        )


def run_fhir_api(
    sample: dict,
    visit_context: dict,
    enable_trace: bool = False,
    model_id: str | None = None,
    context_condition: str = "full",
    extra_headers: dict | None = None,
    response_config: dict | None = None,
) -> tuple[InferenceResult, dict | None]:
    """Run inference using local orchestrator in FHIR API mode.

    Instead of reading patient data from a file, the agent queries
    the FHIR server on demand via the query_patient_fhir tool.

    Args:
        sample: Sample info dict with keys: sample_id, patient_id.
            file_path and format are optional (set to placeholders).
        visit_context: Visit context for inference. Should NOT already
            contain data_source or patient_id — these are set from the sample.
        enable_trace: If True, capture and return execution trace
        model_id: Optional model ID override (default: from config)
        context_condition: Context condition for guidelines
        extra_headers: Additional Bedrock request fields
        response_config: Response constraints such as ``max_nudges``

    Returns:
        Tuple of (InferenceResult, trace_dict or None)
    """
    from .agents.orchestrator import MedicalNudgingOrchestrator

    patient_id = sample["patient_id"]
    file_path = sample.get("file_path", f"fhir://{patient_id}")

    # Merge FHIR API routing into visit context
    fhir_visit_context = {
        **visit_context,
        "data_source": "fhir_api",
        "patient_id": patient_id,
    }

    orchestrator = MedicalNudgingOrchestrator(
        model_id=model_id,
        context_condition=context_condition,
        extra_headers=extra_headers,
    )
    start = time.perf_counter()
    trace_dict: dict | None = None

    try:
        if enable_trace:
            response, trace = orchestrator.generate_nudges_with_trace(
                patient_data=None,
                visit_context=fhir_visit_context,
                config=response_config,
            )
            trace_dict = trace.to_dict()
        else:
            response = orchestrator.generate_nudges(
                patient_data=None,
                visit_context=fhir_visit_context,
                config=response_config,
            )

        return (
            InferenceResult(
                sample_id=sample["sample_id"],
                file_path=file_path,
                format="fhir_api",
                source="local",
                status=response.status,
                latency_ms=int((time.perf_counter() - start) * 1000),
                nudge_count=len(response.nudges),
                categories_used=list({n.category for n in response.nudges}),
                nudge_types_used=list({n.nudge_type for n in response.nudges}),
                error=response.error,
                warnings=response.warnings,
                nudges=[n.model_dump() for n in response.nudges],
                key_findings=response.key_findings,
                patient_summary=response.patient_summary,
            ),
            trace_dict,
        )
    except Exception as e:
        return (
            InferenceResult(
                sample_id=sample["sample_id"],
                file_path=file_path,
                format="fhir_api",
                source="local",
                status="error",
                latency_ms=int((time.perf_counter() - start) * 1000),
                nudge_count=0,
                error=str(e),
            ),
            trace_dict,
        )


def run_agentcore(
    sample: dict, visit_context: dict, agent_arn: str, region: str, profile: str | None
) -> InferenceResult:
    """Run inference using AgentCore.

    Tracks invocation timing for CloudWatch trace correlation.

    Args:
        sample: Sample file info dict with keys: sample_id, file_path, format
        visit_context: Visit context for inference
        agent_arn: AgentCore runtime ARN
        region: AWS region
        profile: AWS profile name (optional)

    Returns:
        InferenceResult with timing info for trace correlation
    """
    import boto3
    from botocore.config import Config

    with open(sample["file_path"]) as f:
        patient_data = f.read()

    request_body = build_request_body(patient_data, sample["format"], visit_context)
    start = time.perf_counter()
    invocation_start = datetime.now()

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        # Increase read timeout for long-running agent invocations (default is 60s)
        client = session.client(
            "bedrock-agentcore",
            config=Config(read_timeout=600, retries={"max_attempts": 0}),
        )
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            qualifier="DEFAULT",
            payload=json.dumps(request_body),
        )

        # Extract session ID for OTEL trace correlation
        # Session ID can be in HTTP headers or response body
        session_id = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get(
            "x-amzn-bedrock-agentcore-runtime-session-id"
        ) or response.get("runtimeSessionId")
        if session_id:
            log.debug("Captured session ID: %s", session_id)

        # Parse response based on content type
        content_type = response.get("contentType", "")
        raw_result = {}

        if "text/event-stream" in content_type:
            # Handle SSE streaming response
            content = []
            for line in response["response"].iter_lines(chunk_size=1024):
                if line:
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line.startswith("data: "):
                        content.append(line[6:])
            if content:
                raw_result = json.loads("".join(content))

        elif content_type == "application/json":
            # Handle standard JSON response
            chunks = []
            for chunk in response.get("response", []):
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
            if chunks:
                raw_result = json.loads("".join(chunks))

        invocation_end = datetime.now()
        output = raw_result.get("output", raw_result)
        nudges = output.get("nudges", [])

        return InferenceResult(
            sample_id=sample["sample_id"],
            file_path=sample["file_path"],
            format=sample["format"],
            source="agentcore",
            status=output.get("status", "unknown"),
            latency_ms=int((time.perf_counter() - start) * 1000),
            nudge_count=len(nudges),
            categories_used=list({n.get("category", "") for n in nudges if n.get("category")}),
            nudge_types_used=list({n.get("nudge_type", "") for n in nudges if n.get("nudge_type")}),
            error=output.get("error"),
            warnings=output.get("warnings", []),
            nudges=nudges,
            key_findings=output.get("key_findings", []),
            patient_summary=output.get("patient_summary"),
            invocation_start=invocation_start,
            invocation_end=invocation_end,
            session_id=session_id,
        )
    except Exception as e:
        log.error("AgentCore error: %s", e)
        return InferenceResult(
            sample_id=sample["sample_id"],
            file_path=sample["file_path"],
            format=sample["format"],
            source="agentcore",
            status="error",
            latency_ms=int((time.perf_counter() - start) * 1000),
            nudge_count=0,
            error=str(e),
            invocation_start=invocation_start,
            invocation_end=datetime.now(),
        )


def generate_report(results: list[InferenceResult], mode: str) -> InferenceReport:
    """Generate inference report from results.

    Args:
        results: List of InferenceResult objects
        mode: Inference mode ("local" or "agentcore")

    Returns:
        InferenceReport with aggregated statistics
    """
    latencies = sorted(r.latency_ms for r in results if r.status != "error")

    category_dist: dict[str, int] = {}
    nudge_type_dist: dict[str, int] = {}
    error_types: dict[str, int] = {}

    for r in results:
        for cat in r.categories_used:
            category_dist[cat] = category_dist.get(cat, 0) + 1
        for nt in r.nudge_types_used:
            nudge_type_dist[nt] = nudge_type_dist.get(nt, 0) + 1
        if r.status == "error" and r.error:
            # Extract error type from error message (first line or truncated)
            error_key = r.error.split("\n")[0][:100]
            error_types[error_key] = error_types.get(error_key, 0) + 1

    return InferenceReport(
        timestamp=datetime.now().isoformat(),
        mode=mode,
        total_samples=len(results),
        success_count=sum(1 for r in results if r.status == "success"),
        partial_count=sum(1 for r in results if r.status == "partial"),
        error_count=sum(1 for r in results if r.status == "error"),
        avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0,
        p50_latency_ms=latencies[len(latencies) // 2] if latencies else 0,
        p95_latency_ms=latencies[int(len(latencies) * 0.95)] if latencies else 0,
        category_distribution=category_dist,
        nudge_type_distribution=nudge_type_dist,
        error_types=error_types,
        results=results,
    )


def fetch_agentcore_traces(
    results: list[InferenceResult],
    log_group: str,
    region: str,
    profile: str | None,
    agent_arn: str | None = None,
) -> list[tuple[str, dict | None]]:
    """Fetch traces from AgentCore using OTEL Observability API.

    Uses the AgentCore Observability API (via the bedrock_agentcore_starter_toolkit)
    to retrieve rich OTEL traces with full span data including tool calls,
    model invocations, and timing.

    Requires Transaction Search to be enabled in the AWS account.
    See README.md for setup instructions.

    Args:
        results: List of InferenceResults with timing info
        log_group: CloudWatch log group name (unused, kept for API compatibility)
        region: AWS region
        profile: AWS profile name
        agent_arn: AgentCore ARN (required for OTEL trace retrieval)

    Returns:
        List of (sample_id, trace_dict) tuples
    """
    if not agent_arn:
        log.warning("[yellow]No agent ARN provided, cannot retrieve OTEL traces[/yellow]")
        return [(r.sample_id, None) for r in results]

    try:
        from .tracing.agentcore_observability import fetch_agentcore_traces_otel

        log.info("[cyan]Fetching OTEL traces from AgentCore...[/cyan]")
        traces = fetch_agentcore_traces_otel(results, agent_arn, region, profile)

        traces_found = sum(1 for _, t in traces if t)
        log.info(
            "[green]Retrieved %d/%d OTEL traces[/green]",
            traces_found,
            len(results),
        )
        return traces

    except ImportError:
        log.warning(
            "[yellow]bedrock_agentcore_starter_toolkit not available. "
            "Install with: pip install bedrock-agentcore-starter-toolkit[/yellow]"
        )
        return [(r.sample_id, None) for r in results]
    except Exception as e:
        log.warning("[yellow]OTEL trace retrieval failed: %s[/yellow]", e)
        return [(r.sample_id, None) for r in results]
