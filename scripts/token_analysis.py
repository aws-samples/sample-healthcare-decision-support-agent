#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "tiktoken", "rich", "lxml", "pydantic"]
# ///
"""Token size analysis for CCDA/FHIR data - raw vs parsed comparison.

Usage:
    uv run scripts/token_analysis.py --samples 10
    uv run scripts/token_analysis.py --samples 100 --seed 42
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from medical_nudging.parsers.ccda_parser import parse_ccda
from medical_nudging.parsers.fhir_parser import parse_fhir

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("token_analysis")
console = Console()

MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"
AWS_PROFILE = os.environ.get("AWS_PROFILE")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


@dataclass
class TokenResult:
    file_name: str
    format: str
    file_size_bytes: int
    raw_tokens: int
    parsed_tokens: int
    compression_ratio: float
    count_source: str  # "bedrock" or "tiktoken"


def count_tokens_bedrock(text: str, client) -> int | None:
    """Count tokens using Bedrock count_tokens API."""
    try:
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": text}],
            }
        )
        response = client.count_tokens(modelId=MODEL_ID, input={"invokeModel": {"body": body}})
        return response["inputTokens"]
    except Exception as e:
        log.warning("Bedrock count_tokens failed: %s", e)
        return None


def count_tokens_tiktoken(text: str, encoding) -> int:
    """Count tokens using tiktoken cl100k_base encoding."""
    return len(encoding.encode(text))


def get_sample_files(n_samples: int, seed: int, ccda_dir: Path, fhir_dir: Path) -> list[dict]:
    """Get random sample of files, split evenly between CCDA and FHIR."""
    random.seed(seed)
    samples = []
    n_each = n_samples // 2

    for directory, ext, fmt in [(ccda_dir, "*.xml", "ccda"), (fhir_dir, "*.json", "fhir")]:
        if not directory.exists():
            log.error("Directory not found: %s", directory)
            continue
        files = [f for f in directory.glob(ext) if f.is_file() and not f.name.endswith(".tar.gz")]
        selected = random.sample(files, min(n_each, len(files)))
        for f in selected:
            samples.append({"path": f, "format": fmt})

    return samples


def process_file(sample: dict, bedrock_client, tiktoken_encoding) -> TokenResult | None:
    """Process a single file and return token counts."""
    path = sample["path"]
    fmt = sample["format"]

    try:
        raw_content = path.read_text()
        file_size = path.stat().st_size

        # Parse the file
        if fmt == "ccda":
            parsed = parse_ccda(raw_content)
        else:
            parsed = parse_fhir(raw_content)

        parsed_json = json.dumps(parsed.model_dump(), indent=2)

        # Try Bedrock first, fallback to tiktoken
        count_source = "bedrock"
        raw_tokens = count_tokens_bedrock(raw_content, bedrock_client) if bedrock_client else None
        parsed_tokens = (
            count_tokens_bedrock(parsed_json, bedrock_client) if bedrock_client else None
        )

        if raw_tokens is None or parsed_tokens is None:
            count_source = "tiktoken"
            raw_tokens = count_tokens_tiktoken(raw_content, tiktoken_encoding)
            parsed_tokens = count_tokens_tiktoken(parsed_json, tiktoken_encoding)

        compression_ratio = raw_tokens / parsed_tokens if parsed_tokens > 0 else 0

        return TokenResult(
            file_name=path.name,
            format=fmt,
            file_size_bytes=file_size,
            raw_tokens=raw_tokens,
            parsed_tokens=parsed_tokens,
            compression_ratio=compression_ratio,
            count_source=count_source,
        )
    except Exception as e:
        log.error("Error processing %s: %s", path.name, e)
        return None


def compute_stats(values: list[float]) -> dict:
    """Compute summary statistics."""
    if not values:
        return {"mean": 0, "median": 0, "min": 0, "max": 0, "stdev": 0}
    return {
        "mean": mean(values),
        "median": median(values),
        "min": min(values),
        "max": max(values),
        "stdev": stdev(values) if len(values) > 1 else 0,
    }


def display_summary(results: list[TokenResult]):
    """Display summary statistics table."""
    ccda_results = [r for r in results if r.format == "ccda"]
    fhir_results = [r for r in results if r.format == "fhir"]

    table = Table(title="Token Analysis Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("CCDA (XML)", justify="right")
    table.add_column("FHIR (JSON)", justify="right")
    table.add_column("All", justify="right")

    def fmt_stats(data: list[TokenResult], attr: str) -> str:
        values = [getattr(r, attr) for r in data]
        if not values:
            return "-"
        s = compute_stats(values)
        if attr == "file_size_bytes":
            return f"{s['mean']/1024:.0f}KB (±{s['stdev']/1024:.0f})"
        elif attr == "compression_ratio":
            return f"{s['mean']:.1f}x (±{s['stdev']:.1f})"
        return f"{s['mean']:,.0f} (±{s['stdev']:,.0f})"

    table.add_row("Count", str(len(ccda_results)), str(len(fhir_results)), str(len(results)))
    table.add_row(
        "File Size",
        fmt_stats(ccda_results, "file_size_bytes"),
        fmt_stats(fhir_results, "file_size_bytes"),
        fmt_stats(results, "file_size_bytes"),
    )
    table.add_row(
        "Raw Tokens",
        fmt_stats(ccda_results, "raw_tokens"),
        fmt_stats(fhir_results, "raw_tokens"),
        fmt_stats(results, "raw_tokens"),
    )
    table.add_row(
        "Parsed Tokens",
        fmt_stats(ccda_results, "parsed_tokens"),
        fmt_stats(fhir_results, "parsed_tokens"),
        fmt_stats(results, "parsed_tokens"),
    )
    table.add_row(
        "Compression",
        fmt_stats(ccda_results, "compression_ratio"),
        fmt_stats(fhir_results, "compression_ratio"),
        fmt_stats(results, "compression_ratio"),
    )

    console.print(table)

    sources = set(r.count_source for r in results)
    log.info("Token source: %s", ", ".join(sources))


def save_csv(results: list[TokenResult], output_path: Path):
    """Save detailed results to CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "file_name",
                "format",
                "file_size_bytes",
                "raw_tokens",
                "parsed_tokens",
                "compression_ratio",
                "count_source",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    r.file_name,
                    r.format,
                    r.file_size_bytes,
                    r.raw_tokens,
                    r.parsed_tokens,
                    f"{r.compression_ratio:.2f}",
                    r.count_source,
                ]
            )


def main():
    parser = argparse.ArgumentParser(description="Token size analysis for CCDA/FHIR data")
    parser.add_argument("--samples", type=int, default=10, help="Total samples (split evenly)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--ccda-dir", type=Path, default=Path("data/synthea-100-latest"))
    parser.add_argument("--fhir-dir", type=Path, default=Path("data/syntheticmedicare10k"))
    parser.add_argument("--output", type=Path, default=None, help="Output CSV path")
    args = parser.parse_args()

    # Initialize Bedrock client
    bedrock_client = None
    try:
        import boto3

        session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
        bedrock_client = session.client("bedrock-runtime")
        log.info("[green]Using Bedrock count_tokens API (model: %s)[/green]", MODEL_ID)
    except Exception as e:
        log.warning("[yellow]Bedrock unavailable (%s), falling back to tiktoken[/yellow]", e)

    # Initialize tiktoken as fallback
    import tiktoken

    tiktoken_encoding = tiktoken.get_encoding("cl100k_base")

    # Get sample files
    samples = get_sample_files(args.samples, args.seed, args.ccda_dir, args.fhir_dir)
    if not samples:
        log.error("No sample files found")
        return 1

    ccda_count = sum(1 for s in samples if s["format"] == "ccda")
    fhir_count = sum(1 for s in samples if s["format"] == "fhir")
    log.info(
        "[bold]Processing %d files (%d CCDA, %d FHIR)...[/bold]",
        len(samples),
        ccda_count,
        fhir_count,
    )

    # Process files
    results = []
    for i, sample in enumerate(samples, 1):
        log.info("[%d/%d] %s", i, len(samples), sample["path"].name)
        result = process_file(sample, bedrock_client, tiktoken_encoding)
        if result:
            results.append(result)

    if not results:
        log.error("No results to display")
        return 1

    # Display summary
    print()
    display_summary(results)

    # Save CSV
    output_path = args.output or Path(f"scripts/token_analysis_{datetime.now():%Y%m%d_%H%M%S}.csv")
    save_csv(results, output_path)
    log.info("[green]Results saved to %s[/green]", output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
