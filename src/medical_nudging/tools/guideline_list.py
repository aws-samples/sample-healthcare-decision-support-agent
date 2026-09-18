"""Tool for listing available clinical guidelines."""

import json
import logging
from glob import glob
from pathlib import Path

from strands import tool

from medical_nudging.config import get_guidelines_path
from medical_nudging.search import get_search_backend

logger = logging.getLogger(__name__)


def _list_from_catalog() -> list[dict]:
    """List guidelines from catalog.json (preferred, rich metadata)."""
    guidelines_dir = get_guidelines_path()
    catalog_path = guidelines_dir / "catalog.json"

    if not catalog_path.exists():
        return []

    try:
        with open(catalog_path) as f:
            catalog = json.load(f)

        guidelines = []
        for entry in catalog.get("guidelines", []):
            guidelines.append(
                {
                    "source": entry.get("source", ""),
                    "title": entry.get("title", ""),
                    "organization": entry.get("organization", ""),
                    "year": entry.get("year"),
                    "target_population": entry.get("target_population", ""),
                    "specialties": entry.get("specialties", []),
                    "conditions_covered": entry.get("conditions_covered", []),
                    "summary_path": entry.get("summary_path", ""),
                    "type": "catalog",
                }
            )
        return guidelines
    except Exception as e:
        logger.warning(f"Failed to load catalog.json: {e}")
        return []


def _list_from_files() -> list[dict]:
    """List guidelines from file system (fallback when no catalog)."""
    guidelines_dir = get_guidelines_path()

    if not guidelines_dir.exists():
        return []

    result = []

    # Look for text files
    txt_files = glob(f"{guidelines_dir}/**/*.txt", recursive=True)
    for txt_file in txt_files:
        path = Path(txt_file)
        result.append(
            {
                "source": path.stem.replace("_", " "),
                "file": str(path.relative_to(guidelines_dir)),
                "type": "text",
            }
        )

    # Look for PDF files (check pdfs/ subdirectory first, then root)
    pdf_dirs = [guidelines_dir / "pdfs", guidelines_dir]
    for pdf_dir in pdf_dirs:
        if not pdf_dir.exists():
            continue
        pdf_files = glob(f"{pdf_dir}/**/*.pdf", recursive=True)
        for pdf_file in pdf_files:
            path = Path(pdf_file)
            result.append(
                {
                    "source": path.stem.replace("-", " ").replace("_", " "),
                    "file": str(path.relative_to(guidelines_dir)),
                    "type": "pdf",
                }
            )

    return result


@tool
def list_sources() -> str:
    """List available guideline sources in the search index with document counts.

    Returns a formatted list showing what guideline sources are actually indexed
    and searchable, with the number of text chunks per source.

    Use this to discover available sources before searching, or to validate
    that requested sources exist in the index.
    """
    try:
        backend = get_search_backend()
        sources = backend.list_sources()

        if not sources:
            return "No sources found in the search index."

        lines = ["Available sources in index:"]
        for s in sources:
            lines.append(f"- {s['source']} ({s['doc_count']} chunks)")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Error listing sources from index")
        return f"ERROR: Failed to list sources: {e}"


@tool
def list_guidelines() -> str:
    """List all available clinical guidelines with metadata.

    Returns a formatted string listing guideline sources with:
    - source: Name of the guideline (e.g., "ADA 2026")
    - title: Full title of the guideline
    - organization: Publishing organization
    - year: Publication year
    - target_population: Who the guideline applies to
    - specialties: Relevant medical specialties
    - conditions_covered: Medical conditions addressed

    When a catalog.json is available, rich metadata is provided.
    Otherwise, falls back to listing PDF/text files.

    Use this tool to understand what guidelines are available before
    searching or retrieving specific content.
    """
    try:
        # Try catalog first (rich metadata)
        guidelines = _list_from_catalog()

        if guidelines:
            # Format with rich metadata
            formatted = ["Available guidelines (from catalog):\n"]
            for g in guidelines:
                formatted.append(f"## {g['source']}")
                if g.get("title"):
                    formatted.append(f"**Title:** {g['title']}")
                if g.get("organization"):
                    formatted.append(f"**Organization:** {g['organization']}")
                if g.get("year"):
                    formatted.append(f"**Year:** {g['year']}")
                if g.get("target_population"):
                    formatted.append(f"**Target Population:** {g['target_population']}")
                if g.get("specialties"):
                    formatted.append(f"**Specialties:** {', '.join(g['specialties'])}")
                if g.get("conditions_covered"):
                    formatted.append(
                        f"**Conditions:** {', '.join(g['conditions_covered'][:5])}"
                        + ("..." if len(g.get("conditions_covered", [])) > 5 else "")
                    )
                formatted.append("")  # Blank line between entries
            return "\n".join(formatted)

        # Fall back to file listing
        guidelines = _list_from_files()

        if not guidelines:
            return (
                "No guidelines found. Upload guidelines to the S3 bucket "
                "or place them in the guidelines/ directory.\n\n"
                "To generate rich metadata, run the guidelines-summarizer SOP "
                "to create a catalog.json file."
            )

        # Format simple file listing
        formatted = ["Available guidelines (from files):"]
        for g in guidelines:
            formatted.append(f"• {g['source']} ({g.get('type', 'unknown')}): {g.get('file', '')}")

        return "\n".join(formatted)

    except Exception as e:
        logger.exception("Error listing guidelines")
        return f"ERROR: Error listing guidelines: {str(e)}"
