"""Tests for runtime guideline catalog generation."""

import hashlib

import pytest

from scripts.build_guideline_catalog import build_catalog


def test_build_catalog_combines_index_inventory_metadata_and_checksums(tmp_path):
    pdf_path = tmp_path / "sepsis.pdf"
    pdf_path.write_bytes(b"licensed guideline bytes")
    inventory = [
        {
            "source": "SEPSIS",
            "indexed_chunks": 10,
            "retrievable_chunks": 8,
            "ingested_at": "2026-08-18T12:00:00Z",
            "s3_uri": "s3://guidelines/pdfs/sepsis.pdf",
        }
    ]
    source_manifest = {
        "guidelines": [
            {
                "source": "SEPSIS",
                "title": "Sepsis Guideline",
                "organization": "Example",
                "year": 2026,
                "category": "critical_care",
            }
        ]
    }

    catalog = build_catalog(
        index_name="guidelines-v2",
        inventory=inventory,
        source_manifest=source_manifest,
        pdf_dir=tmp_path,
        generated_at="2026-08-18T12:30:00Z",
    )

    entry = catalog["guidelines"][0]
    assert catalog["index"] == "guidelines-v2"
    assert catalog["source_count"] == 1
    assert catalog["indexed_chunks"] == 10
    assert catalog["retrievable_chunks"] == 8
    assert entry["document_filename"] == "sepsis.pdf"
    assert entry["document_sha256"] == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    assert entry["ingestion_result"] == "success"
    assert entry["target_population"].startswith("Hospitalized")


def test_build_catalog_rejects_unknown_indexed_source(tmp_path):
    with pytest.raises(ValueError, match="missing from sources.json"):
        build_catalog(
            index_name="guidelines-v2",
            inventory=[
                {
                    "source": "UNKNOWN",
                    "indexed_chunks": 1,
                    "retrievable_chunks": 1,
                    "s3_uri": "s3://guidelines/unknown.pdf",
                }
            ],
            source_manifest={"guidelines": []},
            pdf_dir=tmp_path,
        )


def test_build_catalog_rejects_source_with_only_reference_chunks(tmp_path):
    with pytest.raises(ValueError, match="no retrievable non-reference chunks"):
        build_catalog(
            index_name="guidelines-v2",
            inventory=[
                {
                    "source": "SEPSIS",
                    "indexed_chunks": 2,
                    "retrievable_chunks": 0,
                    "s3_uri": "s3://guidelines/sepsis.pdf",
                }
            ],
            source_manifest={"guidelines": [{"source": "SEPSIS"}]},
            pdf_dir=tmp_path,
        )
