#!/usr/bin/env python3
"""Fetch OTEL traces from AgentCore for inference results.

This script retrieves traces that may have been missed during inference runs
due to OTEL propagation delays. Traces typically need 30-120 seconds to be
indexed in X-Ray after an invocation completes.

Usage:
    # Fetch traces for a specific inference result file
    uv run scripts/fetch_traces.py results/inference_agentcore_20260108_010125.json

    # Fetch all traces in a time window (last N minutes)
    uv run scripts/fetch_traces.py --window 60

    # Fetch trace for a specific session ID
    uv run scripts/fetch_traces.py --session-id c75a8ce4-5444-4c59-a4ef-0794776379e5

    # List available sessions in time window
    uv run scripts/fetch_traces.py --list --window 120
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from medical_nudging.config import get_value, load_config
from medical_nudging.tracing.agentcore_observability import AgentCoreTraceRetriever

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("fetch_traces")
console = Console()


def fetch_traces_for_results(
    results_file: Path,
    retriever: AgentCoreTraceRetriever,
    output_dir: Path | None = None,
) -> list[tuple[str, dict | None]]:
    """Fetch traces for all samples in an inference results file.

    Args:
        results_file: Path to inference results JSON file
        retriever: AgentCoreTraceRetriever instance
        output_dir: Optional output directory for traces

    Returns:
        List of (sample_id, trace_dict) tuples
    """
    with open(results_file) as f:
        data = json.load(f)

    results = data.get("results", [])
    if not results:
        log.warning("No results found in %s", results_file)
        return []

    # Determine time window from results
    start_times = []
    end_times = []
    for r in results:
        if r.get("invocation_start"):
            start_times.append(datetime.fromisoformat(r["invocation_start"]))
        if r.get("invocation_end"):
            end_times.append(datetime.fromisoformat(r["invocation_end"]))

    if not start_times or not end_times:
        log.warning("No invocation timing data in results, using 2-hour window")
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=2)
    else:
        start_time = min(start_times) - timedelta(minutes=5)
        end_time = max(end_times) + timedelta(minutes=5)

    log.info(
        "Time window: %s to %s",
        start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time.strftime("%H:%M:%S"),
    )

    traces = []
    found_count = 0

    for r in results:
        sample_id = r.get("sample_id", "unknown")
        session_id = r.get("session_id")

        if not session_id:
            log.warning("No session_id for sample %s", sample_id)
            traces.append((sample_id, None))
            continue

        log.info("Fetching trace for session %s (%s)...", session_id[:12], sample_id[:30])

        trace_data = retriever.get_traces_for_session(
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
        )

        if trace_data:
            found_count += 1
            span_count = trace_data.get("total_span_count", 0)
            log.info("  [green]Found[/green] - %d spans", span_count)

            trace_dict = {
                "sample_id": sample_id,
                "session_id": session_id,
                "source": "agentcore_otel",
                **trace_data,
            }
            traces.append((sample_id, trace_dict))
        else:
            log.warning("  [yellow]Not found[/yellow]")
            traces.append((sample_id, None))

    log.info("Retrieved %d/%d traces", found_count, len(results))

    # Save traces if output directory specified
    if output_dir and traces:
        save_traces(traces, output_dir, results_file.stem)

    return traces


def fetch_trace_for_session(
    session_id: str,
    retriever: AgentCoreTraceRetriever,
    window_minutes: int = 120,
) -> dict | None:
    """Fetch trace for a specific session ID.

    Args:
        session_id: Session ID to fetch trace for
        retriever: AgentCoreTraceRetriever instance
        window_minutes: Time window to search (minutes ago to now)

    Returns:
        Trace data dict or None
    """
    end_time = datetime.now()
    start_time = end_time - timedelta(minutes=window_minutes)

    log.info("Fetching trace for session: %s", session_id)
    log.info("Time window: last %d minutes", window_minutes)

    trace_data = retriever.get_traces_for_session(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
    )

    if trace_data:
        span_count = trace_data.get("total_span_count", 0)
        log.info("[green]Found trace with %d spans[/green]", span_count)

        # Print trace summary
        traces_dict = trace_data.get("traces", {})
        for trace_id, trace_info in traces_dict.items():
            console.print(f"  Trace ID: {trace_id}")
            console.print(f"  Root spans: {len(trace_info.get('root_spans', []))}")
            console.print(f"  Error count: {trace_info.get('error_count', 0)}")
    else:
        log.warning("[yellow]No trace found for session[/yellow]")

    return trace_data


def list_sessions_in_window(
    retriever: AgentCoreTraceRetriever,
    window_minutes: int = 60,
) -> list[dict]:
    """List all sessions/traces in a time window.

    Args:
        retriever: AgentCoreTraceRetriever instance
        window_minutes: Time window to search (minutes ago to now)

    Returns:
        List of trace dicts
    """
    end_time = datetime.now()
    start_time = end_time - timedelta(minutes=window_minutes)

    log.info("Listing traces in last %d minutes", window_minutes)

    traces = retriever.get_traces_in_window(
        start_time=start_time,
        end_time=end_time,
        limit=50,
    )

    if not traces:
        log.info("No traces found")
        return []

    # Display as table
    table = Table(title=f"Traces (last {window_minutes} minutes)")
    table.add_column("#", style="dim")
    table.add_column("Session ID", style="cyan")
    table.add_column("Spans", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Trace ID", style="dim")

    for i, trace in enumerate(traces, 1):
        session_id = trace.get("session_id", "unknown")
        span_count = str(trace.get("total_span_count", 0))
        traces_dict = trace.get("traces", {})
        error_count = sum(t.get("error_count", 0) for t in traces_dict.values())
        trace_id = list(traces_dict.keys())[0] if traces_dict else "N/A"

        table.add_row(
            str(i),
            session_id[:36] if session_id else "N/A",
            span_count,
            str(error_count) if error_count else "-",
            trace_id[:20] + "..." if trace_id and len(trace_id) > 20 else trace_id,
        )

    console.print(table)
    return traces


def save_traces(
    traces: list[tuple[str, dict | None]],
    output_dir: Path,
    prefix: str,
) -> Path:
    """Save traces to output directory.

    Args:
        traces: List of (sample_id, trace_dict) tuples
        output_dir: Output directory
        prefix: Filename prefix

    Returns:
        Path to trace directory
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_dir = output_dir / "traces" / f"{prefix}_refetch_{timestamp}"
    trace_dir.mkdir(parents=True, exist_ok=True)

    index_entries = []
    for i, (sample_id, trace_dict) in enumerate(traces, 1):
        if trace_dict is None:
            continue

        trace_filename = f"sample_{i:03d}_{sample_id[:50]}.json"
        trace_path = trace_dir / trace_filename

        with open(trace_path, "w") as f:
            json.dump(trace_dict, f, indent=2, default=str)

        index_entries.append(
            {
                "sample_id": sample_id,
                "file": trace_filename,
                "span_count": trace_dict.get("total_span_count", 0),
            }
        )

    # Write index
    index_path = trace_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "total_samples": len(index_entries),
                "traces": index_entries,
            },
            f,
            indent=2,
        )

    log.info("Saved %d traces to %s", len(index_entries), trace_dir)
    return trace_dir


def main():
    config = load_config()

    if config.get("aws_profile"):
        os.environ["AWS_PROFILE"] = config["aws_profile"]

    parser = argparse.ArgumentParser(
        description="Fetch OTEL traces from AgentCore",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "results_file",
        type=Path,
        nargs="?",
        help="Inference results JSON file to fetch traces for",
    )
    parser.add_argument(
        "--session-id",
        help="Fetch trace for a specific session ID",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all sessions in time window",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=120,
        help="Time window in minutes (default: 120)",
    )
    parser.add_argument(
        "--agent-arn",
        default=get_value("agent_arn", "AGENT_ARN"),
        help="AgentCore ARN (default: from config)",
    )
    parser.add_argument(
        "--region",
        default=config.get("aws_region", "us-east-1"),
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results"),
        help="Output directory for traces (default: results/)",
    )
    parser.add_argument(
        "--profile",
        help="AWS profile override (default: from config/settings.yaml)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Don't save traces to disk",
    )
    args = parser.parse_args()

    if args.profile:
        config["aws_profile"] = args.profile

    if not args.agent_arn:
        log.error("--agent-arn required (set in config/settings.yaml or via CLI)")
        return 1

    # Extract agent_id from ARN
    agent_id = args.agent_arn.split("/")[-1]

    retriever = AgentCoreTraceRetriever(
        agent_id=agent_id,
        region=args.region,
        profile=config.get("aws_profile"),
    )

    if args.list:
        list_sessions_in_window(retriever, args.window)
    elif args.session_id:
        trace = fetch_trace_for_session(args.session_id, retriever, args.window)
        if trace and not args.no_save:
            traces = [(args.session_id, {"session_id": args.session_id, **trace})]
            save_traces(traces, args.output_dir, f"session_{args.session_id[:8]}")
    elif args.results_file:
        if not args.results_file.exists():
            log.error("File not found: %s", args.results_file)
            return 1
        output_dir = None if args.no_save else args.output_dir
        fetch_traces_for_results(args.results_file, retriever, output_dir)
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
