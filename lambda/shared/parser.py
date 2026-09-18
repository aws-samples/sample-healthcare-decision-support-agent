"""Shared PDF parsing utilities for guideline ingestion."""

import re
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass
class GuidelineSection:
    """Parsed section from a guideline PDF."""

    source: str
    section: Optional[str]
    section_number: Optional[str]
    page_number: int
    content: str


def derive_source_name(s3_key: str) -> str:
    """Derive guideline source name from S3 key.

    Extracts organization from directory and year from filename.

    Examples:
        Guidelines-from-vendor/ADA/ada-standards-of-care-2026.pdf → ADA 2026
        Guidelines-from-vendor/AHA_ACC/2023-aha-acc-guidelines.pdf → AHA_ACC 2023
        ADA/ada-standards-of-care-2026.pdf → ADA 2026
        CDC/infection-control.pdf → CDC (no year)
    """
    parts = s3_key.strip("/").split("/")
    if not parts:
        return "Unknown"

    # Skip common prefix directories to find the org folder
    skip_prefixes = {"Guidelines-from-vendor", "guidelines", "test", "data"}
    org_idx = 0
    for i, part in enumerate(parts[:-1]):  # Exclude filename
        if part.lower() not in {p.lower() for p in skip_prefixes}:
            org_idx = i
            break

    # Get organization from the org directory (ADA, AHA_ACC, CDC, MISC)
    org = parts[org_idx] if org_idx < len(parts) - 1 else parts[0]

    # Extract year from filename using regex
    filename = parts[-1] if parts else ""
    year_match = re.search(r"\b(19|20)\d{2}\b", filename)
    year = year_match.group(0) if year_match else ""

    return f"{org} {year}".strip()


def parse_pdf(pdf_bytes: bytes, source: str) -> Iterator[GuidelineSection]:
    """Parse PDF bytes into guideline sections.

    Uses pdfplumber for text extraction with section detection.

    Args:
        pdf_bytes: Raw PDF file content
        source: Source name for the guideline

    Yields:
        GuidelineSection for each detected section
    """
    import io

    import pdfplumber

    # Section header patterns (numbered sections like "9.2 Pharmacologic Approaches")
    section_pattern = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$", re.MULTILINE)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        current_section: Optional[str] = None
        current_section_number: Optional[str] = None
        current_content: list[str] = []
        current_page: int = 1

        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""

            # Check for section headers
            for match in section_pattern.finditer(text):
                # Yield previous section if exists
                if current_content:
                    yield GuidelineSection(
                        source=source,
                        section=current_section,
                        section_number=current_section_number,
                        page_number=current_page,
                        content="\n".join(current_content).strip(),
                    )
                    current_content = []

                current_section_number = match.group(1)
                current_section = match.group(2).strip()
                current_page = page_num

            # Add page content
            if text.strip():
                current_content.append(text)

        # Yield final section
        if current_content:
            yield GuidelineSection(
                source=source,
                section=current_section,
                section_number=current_section_number,
                page_number=current_page,
                content="\n".join(current_content).strip(),
            )


def chunk_content(content: str, max_chars: int = 4000) -> Iterator[str]:
    """Split content into chunks for indexing.

    Tries to split on paragraph boundaries.
    """
    if len(content) <= max_chars:
        yield content
        return

    paragraphs = content.split("\n\n")
    current_chunk: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for \n\n
        if current_len + para_len > max_chars and current_chunk:
            yield "\n\n".join(current_chunk)
            current_chunk = []
            current_len = 0
        current_chunk.append(para)
        current_len += para_len

    if current_chunk:
        yield "\n\n".join(current_chunk)
