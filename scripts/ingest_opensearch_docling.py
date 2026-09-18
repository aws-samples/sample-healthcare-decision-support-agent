#!/usr/bin/env python3
"""Ingest PDF guidelines into OpenSearch using Docling for high-quality text extraction.

Docling provides superior handling of multi-column layouts, tables, and document structure
compared to pdfplumber. Uses HierarchicalChunker for semantic chunking.

The parent directory of each PDF is used as the guideline source name (e.g., ADA/, CDC/).

Usage:
    # Single local PDF (dry run to inspect extraction quality)
    uv run scripts/ingest_opensearch_docling.py /path/to/guidelines.pdf --dry-run --verbose

    # Directory of PDFs (recursive) to new index
    uv run scripts/ingest_opensearch_docling.py /path/to/guidelines/ --recursive --index guidelines-docling

    # From S3
    uv run scripts/ingest_opensearch_docling.py s3://bucket/ADA/ada-standards-2026.pdf

    # Specify endpoint and region explicitly
    uv run scripts/ingest_opensearch_docling.py file.pdf --endpoint https://xxx.us-east-1.aoss.amazonaws.com --region us-east-1

    # Use a specific config file
    uv run scripts/ingest_opensearch_docling.py file.pdf --config /path/to/settings.yaml

    # Options
    uv run scripts/ingest_opensearch_docling.py file.pdf --source "ADA 2026"  # Override source name
    uv run scripts/ingest_opensearch_docling.py file.pdf --clear-existing     # Delete existing docs first
    uv run scripts/ingest_opensearch_docling.py file.pdf --dry-run --verbose  # Inspect extraction quality

Configuration:
    The script reads settings from a config file (default: config/settings.yaml).
    CLI arguments take precedence over config file values.

    Config file keys used:
        opensearch_endpoint - OpenSearch Serverless endpoint URL
        opensearch_index - Index name (default: guidelines)
        aws_region - AWS region (default: us-east-1)
"""

import argparse
import hashlib
import logging
import os
import json
from functools import lru_cache
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.chunker import HierarchicalChunker
from opensearchpy import AWSV4SignerAuth, OpenSearch, OpenSearchException, RequestsHttpConnection
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Configure rich logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("ingest_docling")
console = Console()


def slugify_source(source: str) -> str:
    """Convert source name to OpenSearch index slug.

    Examples:
        "ADA 2026" -> "ada"
        "AHA_ACC 2025" -> "aha-acc"
    """
    org = source.split()[0].lower()
    return re.sub(r"[^a-z0-9]+", "-", org).strip("-")


def upload_to_s3(local_path: str, bucket: str, prefix: str = "pdfs") -> str:
    """Upload a local file to S3 using parent directory as the org folder.

    Returns:
        S3 URI of the uploaded file (s3://bucket/prefix/parent/filename).
    """
    s3 = boto3.client("s3")
    path_obj = Path(local_path)
    parent_dir = path_obj.parent.name

    if parent_dir and parent_dir != ".":
        s3_key = f"{prefix}/{parent_dir}/{path_obj.name}"
    else:
        s3_key = f"{prefix}/{path_obj.name}"

    s3_uri = f"s3://{bucket}/{s3_key}"
    log.info("  Uploading to %s", s3_uri)
    s3.upload_file(local_path, bucket, s3_key)
    return s3_uri


@lru_cache(maxsize=1)
def _manifest_source_keys() -> frozenset[str]:
    """Source ids declared in guidelines/sources.json (empty when the manifest is absent)."""
    manifest = Path(__file__).resolve().parent.parent / "guidelines" / "sources.json"
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return frozenset()
    return frozenset(
        entry.get("source", "")
        for entry in payload.get("guidelines", [])
        if isinstance(entry, dict)
    )


def derive_source_name(s3_key: str) -> str:
    """Derive source name from file path.

    Uses the parent directory as the guideline name, plus any year found in the filename.

    Returns:
        Source name like "ADA 2026" or "AHA_ACC 2025"
    """
    path = Path(s3_key)

    # scripts/fetch_guideline_pdfs.py names each download after its manifest key
    # (guidelines/pdfs/<SOURCE_KEY>.pdf). That key is the source id the catalog builder
    # and the agent's list_guidelines tool expect, so it wins over the directory name.
    if path.stem in _manifest_source_keys():
        return path.stem

    parent = path.parent.name
    org = parent if parent and parent != "." else None

    year_match = re.search(r"\b(19|20)\d{2}\b", path.stem)
    year = year_match.group(0) if year_match else None

    if org and year:
        return f"{org} {year}"
    elif org:
        return org
    elif year:
        return f"Guideline {year}"
    else:
        return path.stem


def load_config(config_path: str | None) -> dict:
    """Load configuration from YAML file. Returns empty dict if not found."""
    if config_path is None:
        script_dir = Path(__file__).parent.parent
        config_path = str(script_dir / "config" / "settings.yaml")

    config_file = Path(config_path)
    if config_file.exists():
        log.debug("Loading config from: %s", config_path)
        with open(config_file) as f:
            return yaml.safe_load(f) or {}

    log.debug("Config file not found: %s", config_path)
    return {}


def get_opensearch_client(endpoint: str, region: str) -> OpenSearch:
    """Get OpenSearch client with AWS SigV4 auth."""
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")
    host = endpoint.replace("https://", "").replace("http://", "")

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=120,
    )


def ensure_index(client: OpenSearch, index_name: str) -> None:
    """Create index with mapping if it doesn't exist."""
    if client.indices.exists(index=index_name):
        return

    mapping = {
        "mappings": {
            "properties": {
                "source": {"type": "keyword"},
                "s3_uri": {"type": "keyword"},
                "section": {"type": "text", "analyzer": "english"},
                "section_number": {"type": "keyword"},
                "page_number": {"type": "integer"},
                "content": {
                    "type": "text",
                    "analyzer": "english",
                    "term_vector": "with_positions_offsets",
                },
                "hierarchy": {"type": "keyword"},
                "chunk_type": {"type": "keyword"},
                "ingested_at": {"type": "date"},
            }
        }
    }

    client.indices.create(index=index_name, body=mapping)
    log.info("Created index: %s", index_name)


def delete_source(client: OpenSearch, index_name: str, source: str) -> int:
    """Delete all documents for a source."""
    try:
        response = client.delete_by_query(
            index=index_name,
            body={"query": {"term": {"source": source}}},
            refresh=True,
        )
        return response.get("deleted", 0)
    except OpenSearchException as e:
        log.warning("Failed to delete existing docs for %s: %s", source, e)
        return 0


def generate_doc_id(source: str, page: int, section: str | None, chunk_index: int) -> str:
    """Generate deterministic document ID to prevent duplicates on re-ingestion."""
    key = f"{source}|{page}|{section}|{chunk_index}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def parse_s3_url(path: str) -> tuple[str, str]:
    """Parse s3://bucket/key into (bucket, key)."""
    path_without_scheme = path[5:]
    parts = path_without_scheme.split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def download_to_temp(path: str) -> str:
    """Download S3 file to temp location, or return local path as-is."""
    if path.startswith("s3://"):
        bucket, key = parse_s3_url(path)
        s3 = boto3.client("s3")
        suffix = Path(key).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            s3.download_fileobj(bucket, key, f)
            return f.name
    return path


def list_pdfs(path: str, recursive: bool = False) -> list[str]:
    """List PDF files from local directory or S3 prefix."""
    pdfs = []

    if path.startswith("s3://"):
        bucket, prefix = parse_s3_url(path)
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".pdf") and "/parsed/" not in key:
                    if recursive or "/" not in key[len(prefix) :].lstrip("/"):
                        pdfs.append(f"s3://{bucket}/{key}")
    else:
        local_path = Path(path)
        if local_path.is_file() and path.endswith(".pdf"):
            return [path]

        pattern = "**/*.pdf" if recursive else "*.pdf"
        for pdf_path in local_path.glob(pattern):
            pdfs.append(str(pdf_path))

    return sorted(pdfs)


def _resolve_s3_uri(path: str, bucket: str | None, dry_run: bool) -> str:
    """S3 location recorded on every chunk; local files are uploaded unless dry-running."""
    if path.startswith("s3://"):
        return path
    if bucket:
        return upload_to_s3(path, bucket)
    if dry_run:
        return f"s3://dry-run/{Path(path).name}"
    raise ValueError(f"No S3 bucket provided for local file: {path}")


def _build_converter(ocr: bool) -> DocumentConverter:
    """Docling converter; ``ocr=False`` skips OCR of embedded images.

    OCR is the memory-hungry step. On a 16 GB host, native-text PDFs with large
    embedded images (several NICE guidelines, the BTF handbook) were OOM-killed with OCR
    on and converted in a few minutes with it off; their text does not come from OCR.
    """
    if ocr:
        return DocumentConverter()
    options = PdfPipelineOptions(do_ocr=False)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def _chunk_document(local_path: str, ocr: bool = True) -> list:
    """Convert a PDF with Docling and split it with the HierarchicalChunker."""
    log.info("  Converting with Docling%s...", "" if ocr else " (OCR off)")
    converter = _build_converter(ocr)
    result = converter.convert(local_path)

    log.info("  Chunking with HierarchicalChunker...")
    chunker = HierarchicalChunker()
    return list(chunker.chunk(result.document))


def _chunk_metadata(chunk) -> tuple[str, list[str], set[int]]:
    """Return (first heading, heading hierarchy, page numbers) for one chunk.

    Page numbers come from doc_items provenance; HierarchicalChunker keeps no
    page-level metadata of its own.
    """
    meta = getattr(chunk, "meta", None)
    if not meta:
        return "", [], set()
    headings = getattr(meta, "headings", None) or []
    if not isinstance(headings, list):
        headings = [str(headings)]
    pages = {
        pv.page_no for di in getattr(meta, "doc_items", []) for pv in getattr(di, "prov", []) or []
    }
    return (headings[0] if headings else ""), list(headings), pages


def _print_chunk_preview(chunks: list, limit: int = 5) -> None:
    """Show the first chunks so extraction quality can be inspected."""
    table = Table(title=f"Sample Chunks (first {limit})")
    table.add_column("#", style="dim")
    table.add_column("Type")
    table.add_column("Page")
    table.add_column("Heading")
    table.add_column("Len")
    table.add_column("Preview", max_width=60)

    for i, chunk in enumerate(chunks[:limit]):
        heading, _hierarchy, pages = _chunk_metadata(chunk)
        page_str = ",".join(str(p) for p in sorted(pages)) if pages else "?"
        preview = chunk.text[:100].replace("\n", " ")
        if len(chunk.text) > 100:
            preview += "..."
        table.add_row(
            str(i + 1), type(chunk).__name__, page_str, heading[:30], str(len(chunk.text)), preview
        )
    console.print(table)


def _index_chunks(
    client: OpenSearch, target_index: str, chunks: list, source: str, s3_uri: str
) -> int:
    """Write one document per chunk and return how many were indexed."""
    indexed_count = 0
    for chunk_idx, chunk in enumerate(chunks):
        heading, hierarchy, pages = _chunk_metadata(chunk)
        page = min(pages) if pages else 0
        doc = {
            "source": source,
            "s3_uri": s3_uri,
            "section": heading,
            "section_number": "",
            "page_number": page,
            "content": chunk.text,
            "hierarchy": hierarchy,
            "chunk_type": type(chunk).__name__,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        doc_id = generate_doc_id(source, page, heading, chunk_idx)
        client.index(index=target_index, id=doc_id, body=doc)
        indexed_count += 1
    return indexed_count


def ingest_pdf_docling(
    path: str,
    client: OpenSearch | None,
    index_name: str,
    source_override: str | None = None,
    clear_existing: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    per_source_index: bool = False,
    bucket: str | None = None,
    ocr: bool = True,
) -> dict:
    """Ingest a single PDF into OpenSearch using Docling.

    Returns:
        Stats dict with source, s3_uri, chunks, chars
    """
    source = source_override or derive_source_name(
        path if not path.startswith("s3://") else parse_s3_url(path)[1]
    )
    log.info("Processing: %s (source: %s)", path, source)

    s3_uri = _resolve_s3_uri(path, bucket, dry_run)

    target_index = f"guidelines-{slugify_source(source)}" if per_source_index else index_name
    if per_source_index:
        log.info("  Target index: %s (per-source)", target_index)

    # Download S3 files to temp location for Docling
    local_path = download_to_temp(path)
    try:
        if local_path != path:
            log.info("  Downloaded to: %s", local_path)

        chunks = _chunk_document(local_path, ocr=ocr)
        total_chars = sum(len(chunk.text) for chunk in chunks)
        log.info("  Chunks: %d, Characters: %s", len(chunks), f"{total_chars:,}")

        if verbose:
            _print_chunk_preview(chunks)

        if dry_run:
            log.info("  [dim][dry-run] Skipping indexing[/dim]")
            return {"source": source, "s3_uri": s3_uri, "chunks": len(chunks), "chars": total_chars}

        ensure_index(client, target_index)
        if clear_existing:
            deleted = delete_source(client, target_index, source)
            log.info("  Deleted %d existing documents", deleted)

        indexed_count = _index_chunks(client, target_index, chunks, source, s3_uri)
        log.info("  Indexed: %d chunks to %s", indexed_count, target_index)
        return {"source": source, "s3_uri": s3_uri, "chunks": indexed_count, "chars": total_chars}
    finally:
        if local_path != path:
            os.unlink(local_path)


def main():
    parser = argparse.ArgumentParser(
        description="Ingest PDF guidelines into OpenSearch using Docling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", help="PDF file, directory, or S3 path (s3://bucket/key)")
    parser.add_argument(
        "--config", "-c", help="Path to config file (default: config/settings.yaml)"
    )
    parser.add_argument("--endpoint", help="OpenSearch Serverless endpoint URL")
    parser.add_argument("--region", default=None, help="AWS region (default: us-east-1)")
    parser.add_argument("--source", help="Override auto-derived source name")
    parser.add_argument("--index", default=None, help="OpenSearch index name (default: guidelines)")
    parser.add_argument(
        "--recursive", "-r", action="store_true", help="Recursively process directories"
    )
    parser.add_argument(
        "--clear-existing",
        action="store_true",
        help="Delete existing documents for source before ingesting",
    )
    parser.add_argument(
        "--per-source-index",
        action="store_true",
        help="Create per-source indices (e.g., guidelines-ada)",
    )
    parser.add_argument(
        "--bucket",
        help="S3 bucket for uploading local files. Default from GUIDELINES_BUCKET env var or config.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse PDFs and show stats without indexing"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed chunk info")
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip OCR of embedded images (much lower memory; fine for native-text PDFs)",
    )

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    config = load_config(args.config)

    # Resolve settings: CLI arg > config file > default
    endpoint = args.endpoint or config.get("opensearch_endpoint", "")
    if not endpoint and not args.dry_run:
        log.error("OpenSearch endpoint required. Provide via --endpoint or config file.")
        sys.exit(1)

    region = args.region or config.get("aws_region", "us-east-1")
    index_name = args.index or config.get("opensearch_index", "guidelines")
    bucket = args.bucket or os.environ.get("GUIDELINES_BUCKET") or config.get("guidelines_bucket")

    pdfs = list_pdfs(args.path, args.recursive)
    if not pdfs:
        log.error("No PDF files found at: %s", args.path)
        sys.exit(1)

    has_local_files = any(not p.startswith("s3://") for p in pdfs)
    if has_local_files and not bucket and not args.dry_run:
        log.error("Local files require --bucket or GUIDELINES_BUCKET env var to upload to S3")
        sys.exit(1)

    log.info("Found %d PDF(s) to process", len(pdfs))

    # Initialize OpenSearch client (unless dry run)
    client = None
    if not args.dry_run:
        log.info("Connecting to OpenSearch: %s (region: %s)", endpoint, region)
        client = get_opensearch_client(endpoint, region)
        if not args.per_source_index:
            ensure_index(client, index_name)

    total_chunks = 0
    total_chars = 0
    total_pdfs = 0

    for pdf_path in pdfs:
        source_override = args.source if len(pdfs) == 1 else None
        try:
            stats = ingest_pdf_docling(
                path=pdf_path,
                client=client,
                index_name=index_name,
                source_override=source_override,
                clear_existing=args.clear_existing,
                dry_run=args.dry_run,
                verbose=args.verbose,
                per_source_index=args.per_source_index,
                bucket=bucket,
                ocr=not args.no_ocr,
            )
            total_pdfs += 1
            total_chunks += stats["chunks"]
            total_chars += stats["chars"]
        except Exception:
            log.error("Failed to process %s", pdf_path, exc_info=args.verbose)

    log.info("=" * 50)
    log.info(
        "Summary: %d PDFs, %d chunks, %s characters", total_pdfs, total_chunks, f"{total_chars:,}"
    )


if __name__ == "__main__":
    main()
