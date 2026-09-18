#!/usr/bin/env python3
"""Build the runtime guideline catalog from an OpenSearch index.

The generated catalog is the contract between the indexed corpus and the
agent's ``list_guidelines`` tool. It includes only sources that are present in
the selected index and verifies that each has non-reference content available
for retrieval.

Usage:
    uv run --extra ingest \
      python scripts/build_guideline_catalog.py \
      --index guidelines-icu-baseline-v2 \
      --expected-sources 18
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import yaml
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

REFERENCE_SECTION_EXCLUSIONS = [
    {"wildcard": {"hierarchy": {"value": "reference*", "case_insensitive": True}}},
    {"wildcard": {"hierarchy": {"value": "bibliograph*", "case_insensitive": True}}},
]

CRITICAL_CARE_METADATA = {
    "target_population": "Hospitalized and critically ill adults (ICU, ED, inpatient acute care)",
    "specialties": [
        "critical_care",
        "emergency_medicine",
        "hospital_medicine",
        "infectious_disease",
    ],
}


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON object from disk."""
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    """Return the SHA-256 checksum of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_filename(s3_uri: str) -> str:
    """Extract the original filename from an indexed S3 URI."""
    parsed = urlparse(s3_uri)
    return Path(parsed.path).name


def build_catalog(
    *,
    index_name: str,
    inventory: list[dict[str, Any]],
    source_manifest: dict[str, Any],
    pdf_dir: Path,
    metadata_catalog: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate a runtime catalog from an index inventory."""
    manifest_entries = {entry["source"]: entry for entry in source_manifest.get("guidelines", [])}
    metadata_entries = {
        entry["source"]: entry for entry in (metadata_catalog or {}).get("guidelines", [])
    }

    catalog_entries: list[dict[str, Any]] = []
    for indexed in sorted(inventory, key=lambda entry: entry["source"]):
        source = indexed["source"]
        if source not in manifest_entries:
            raise ValueError(f"Indexed source is missing from sources.json: {source}")
        if indexed["retrievable_chunks"] < 1:
            raise ValueError(f"Source has no retrievable non-reference chunks: {source}")

        s3_uri = indexed.get("s3_uri", "")
        filename = source_filename(s3_uri)
        pdf_path = pdf_dir / filename
        if not filename or not pdf_path.is_file():
            raise ValueError(f"Local PDF for indexed source {source} was not found: {filename}")

        source_entry = dict(manifest_entries[source])
        prior_metadata = metadata_entries.get(source, {})
        for key in (
            "target_population",
            "specialties",
            "conditions_covered",
            "summary_path",
        ):
            if key in prior_metadata:
                source_entry[key] = prior_metadata[key]
        if source_entry.get("category") == "critical_care":
            for key, value in CRITICAL_CARE_METADATA.items():
                source_entry.setdefault(key, value)

        source_entry.update(
            {
                "document_filename": filename,
                "document_sha256": sha256_file(pdf_path),
                "document_bytes": pdf_path.stat().st_size,
                "indexed_chunks": indexed["indexed_chunks"],
                "retrievable_chunks": indexed["retrievable_chunks"],
                "ingestion_result": "success",
                "ingested_at": indexed.get("ingested_at"),
            }
        )
        catalog_entries.append(source_entry)

    inventory_sources = {entry["source"] for entry in inventory}
    if len(inventory_sources) != len(inventory):
        raise ValueError("OpenSearch inventory contains duplicate source identifiers")

    return {
        "version": "3.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "index": index_name,
        "source_count": len(catalog_entries),
        "indexed_chunks": sum(entry["indexed_chunks"] for entry in catalog_entries),
        "retrievable_chunks": sum(entry["retrievable_chunks"] for entry in catalog_entries),
        "retrieval": {
            "backend": "opensearch_serverless",
            "index": index_name,
            "query": "BM25 match with AUTO fuzziness",
            "default_result_limit": 10,
            "reference_section_policy": (
                "Exclude Docling hierarchy headings beginning with "
                "'reference' or 'bibliograph', case-insensitive."
            ),
        },
        "guidelines": catalog_entries,
    }


def get_opensearch_client(endpoint: str, region: str) -> OpenSearch:
    """Create an authenticated OpenSearch Serverless client."""
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")
    host = endpoint.removeprefix("https://").removeprefix("http://")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=120,
    )


def fetch_inventory(client: OpenSearch, index_name: str) -> tuple[int, list[dict[str, Any]]]:
    """Return total and per-source counts plus one indexed document identity."""
    response = client.search(
        index=index_name,
        body={
            "size": 0,
            "track_total_hits": True,
            "aggs": {
                "sources": {
                    "terms": {"field": "source", "size": 100},
                    "aggs": {
                        "retrievable": {
                            "filter": {"bool": {"must_not": REFERENCE_SECTION_EXCLUSIONS}}
                        },
                        "latest_ingestion": {"max": {"field": "ingested_at"}},
                        "identity": {
                            "top_hits": {
                                "size": 1,
                                "_source": ["s3_uri"],
                            }
                        },
                    },
                }
            },
        },
    )

    inventory = []
    for bucket in response["aggregations"]["sources"]["buckets"]:
        hits = bucket["identity"]["hits"]["hits"]
        s3_uri = hits[0]["_source"].get("s3_uri", "") if hits else ""
        inventory.append(
            {
                "source": bucket["key"],
                "indexed_chunks": bucket["doc_count"],
                "retrievable_chunks": bucket["retrievable"]["doc_count"],
                "ingested_at": bucket["latest_ingestion"].get("value_as_string"),
                "s3_uri": s3_uri,
            }
        )

    total = response["hits"]["total"]["value"]
    return total, inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, help="Exact OpenSearch index to catalog")
    parser.add_argument("--expected-sources", type=int, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--sources", type=Path, default=Path("guidelines/sources.json"))
    parser.add_argument("--pdf-dir", type=Path, default=Path("guidelines/pdfs"))
    parser.add_argument(
        "--metadata-catalog",
        type=Path,
        default=None,
        help="Optional catalog JSON from an annotation run whose metadata should be merged",
    )
    parser.add_argument("--output", type=Path, default=Path("guidelines/catalog.json"))
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text()) or {}
    endpoint = config.get("opensearch_endpoint")
    if not endpoint:
        raise ValueError(f"{args.config}: opensearch_endpoint is required")
    region = config.get("aws_region", "us-east-1")

    client = get_opensearch_client(endpoint, region)
    total, inventory = fetch_inventory(client, args.index)
    if len(inventory) != args.expected_sources:
        raise ValueError(
            f"{args.index}: expected {args.expected_sources} sources, found {len(inventory)}"
        )

    metadata_catalog = (
        load_json_object(args.metadata_catalog)
        if args.metadata_catalog is not None and args.metadata_catalog.is_file()
        else None
    )
    catalog = build_catalog(
        index_name=args.index,
        inventory=inventory,
        source_manifest=load_json_object(args.sources),
        pdf_dir=args.pdf_dir,
        metadata_catalog=metadata_catalog,
    )
    if catalog["indexed_chunks"] != total:
        raise ValueError(
            f"{args.index}: aggregated {catalog['indexed_chunks']} chunks but index reports {total}"
        )

    args.output.write_text(json.dumps(catalog, indent=2) + "\n")
    print(
        f"Wrote {args.output}: {catalog['source_count']} sources, "
        f"{catalog['indexed_chunks']} chunks, "
        f"{catalog['retrievable_chunks']} retrievable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
