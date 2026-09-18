#!/usr/bin/env python3
"""Verify OpenSearch connection and index status.

Usage:
    uv run python scripts/verify_opensearch.py
    uv run python scripts/verify_opensearch.py --queries
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

load_dotenv()

# Configure rich logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("verify")

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Verify OpenSearch connection and index status")
    parser.add_argument("--queries", action="store_true", help="Run test queries")
    args = parser.parse_args()

    from medical_nudging.search.opensearch_backend import OpenSearchBackend

    backend = OpenSearchBackend()

    log.info("[bold cyan]OpenSearch Verification[/bold cyan]")

    # Check availability
    log.info("[bold]1. Availability Check[/bold]")
    available = backend.is_available()
    if available:
        log.info("   Status: [green]Available[/green]")
    else:
        log.error("   Status: [red]Not Available[/red]")
        log.error("OpenSearch not available. Check:")
        log.error("   - OPENSEARCH_ENDPOINT environment variable is set")
        log.error("   - AWS credentials are configured")
        log.error("   - Network connectivity to OpenSearch endpoint")
        return 1

    # Health check
    log.info("[bold]2. Health Check[/bold]")
    health = backend.health_check()

    if health.get("healthy"):
        log.info("   Healthy: [green]Yes[/green]")
        log.info("   Latency: [cyan]%.1fms[/cyan]", health.get("latency_ms", 0))
        log.info("   Index Name: [cyan]%s[/cyan]", health.get("index_name", "N/A"))
        log.info("   Index Exists: [cyan]%s[/cyan]", health.get("index_exists", "N/A"))
        log.info("   Document Count: [cyan]%s[/cyan]", health.get("index_doc_count", "N/A"))
        log.info("   Cluster: [cyan]%s[/cyan]", health.get("cluster_name", "N/A"))
        log.info("   Version: [cyan]%s[/cyan]", health.get("version", "N/A"))
    else:
        log.error("   Healthy: [red]No[/red]")
        log.error("   Error: [red]%s[/red]", health.get("error", "Unknown error"))
        return 1

    # Check if index has documents
    if health.get("index_doc_count", 0) == 0:
        log.warning("Index exists but has no documents.")
        log.warning("   Run the ingestion script to index guidelines:")
        log.warning("   uv run scripts/ingest_opensearch_docling.py guidelines/pdfs/ --recursive")

    # Run test queries if requested
    if args.queries:
        log.info("[bold]3. Test Queries[/bold]")

        test_queries = [
            "diabetes HbA1c target",
            "blood pressure hypertension treatment",
            "statin therapy cardiovascular",
            "GLP-1 receptor agonist",
            "heart failure GDMT",
        ]

        table = Table(title="Search Results")
        table.add_column("Query", style="cyan")
        table.add_column("Results", justify="right")
        table.add_column("Top Source")
        table.add_column("Top Score", justify="right")

        for query in test_queries:
            results = backend.search(query, limit=3)
            result_count = len(results)
            top_source = results[0].source if results else "N/A"
            top_score = f"{results[0].relevance:.2f}" if results else "N/A"
            table.add_row(query[:30], str(result_count), top_source[:30], top_score)

        console.print(table)

        # Show sample result content
        if test_queries:
            log.info("[bold]Sample Result Content[/bold]")
            sample_results = backend.search(test_queries[0], limit=1)
            if sample_results:
                r = sample_results[0]
                log.info("   Query: [cyan]%s[/cyan]", test_queries[0])
                log.info("   Source: [cyan]%s[/cyan]", r.source)
                log.info("   Section: [cyan]%s[/cyan]", r.section or "N/A")
                log.info("   Content preview:")
                log.info("   [dim]%s...[/dim]", r.content[:200])

    log.info("[green]Verification complete.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
