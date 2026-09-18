"""Canonical text formatting for guideline search results.

One formatter for every producer of the ``=== SOURCE ===`` passage block format: the
guideline search tools and the single-pass retrieval injection. The evidence-contract
ledger parser (``parse_guideline_passages``) round-trips exactly this format, so all
producers must share this function — a drifted copy degrades the parse silently.
"""

from __future__ import annotations

from typing import Any


def format_search_results(results: list[Any]) -> str:
    """Render search results as ``=== SOURCE ===`` passage blocks.

    Accepts any objects with ``source``, ``section``, ``section_number``,
    ``page_number``, ``relevance``, and ``content`` attributes
    (:class:`~medical_nudging.search.backend.SearchResult`).
    """
    lines: list[str] = []
    for result in results:
        lines.append(f"=== {result.source} ===")
        if result.section:
            lines.append(f"Section: {result.section}")
        if result.section_number:
            lines.append(f"Section Number: {result.section_number}")
        lines.append(f"Page: {result.page_number if result.page_number is not None else 'N/A'}")
        lines.append(f"Relevance: {result.relevance:.3f}")
        lines.append(f"\n{result.content}\n")
    return "\n".join(lines)
