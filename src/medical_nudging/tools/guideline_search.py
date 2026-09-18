"""Guideline search tool using pluggable search backends."""

import logging
from typing import Callable, Optional

from strands import tool

from medical_nudging.search import get_search_backend
from medical_nudging.search.backend import SearchBackend
from medical_nudging.search.factory import SearchBackendFactory
from medical_nudging.search.formatting import format_search_results
from medical_nudging.tools.tool_budget import CallBudget
from medical_nudging.utils.summary_loader import DEFAULT_SUMMARIES_DIR

logger = logging.getLogger(__name__)


def _search(backend: SearchBackend, query: str, sources: Optional[list[str]], limit: int) -> str:
    """Run one search and format it for the model; errors come back as a message string."""
    try:
        logger.debug(f"Using {backend.name} backend for guideline search")
        results = backend.search(query=query, sources=sources, limit=limit)
        if not results:
            return "No matching guidelines found."
        return format_search_results(results)
    except Exception as e:
        error_msg = f"ERROR: Error searching guidelines: {str(e)}"
        logger.error(error_msg)
        return error_msg


@tool
def search_guidelines(
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 10,
) -> str:
    """
    Search clinical guideline files for matching passages.

    Uses configurable backend (OpenSearch Serverless or ripgrep fallback).
    Backend selection via agent.search_backend in config/settings.yaml.

    Args:
        query: Search terms (supports natural language for OpenSearch)
        sources: Optional list of guideline sources to filter (e.g., ["ADA 2026"])
        limit: Maximum results to return (default 10)

    Returns:
        Formatted string with search results and citations.
        On error, returns an error message string.
    """
    try:
        backend = get_search_backend()
    except Exception as e:
        error_msg = f"ERROR: Error searching guidelines: {str(e)}"
        logger.error(error_msg)
        return error_msg
    return _search(backend, query, sources, limit)


def create_search_guidelines_tool(
    backend: SearchBackend,
    *,
    max_calls: int | None = None,
    max_results_per_call: int | None = None,
) -> Callable:
    """Create a search_guidelines tool with a specific backend.

    This allows creating search tools that use different backends or directories,
    useful for experiment conditions like searching summaries vs full PDFs.

    Args:
        backend: The search backend to use
        max_calls: Maximum external searches for this request
        max_results_per_call: Maximum passages returned by one search

    Returns:
        A decorated tool function that uses the specified backend
    """

    budget = CallBudget("search_guidelines", max_calls)

    def search_guidelines_custom(
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 10,
    ) -> str:
        """
        Search clinical guideline files for matching passages.

        Args:
            query: Search terms (supports natural language)
            sources: Optional list of guideline sources to filter (e.g., ["ADA 2026"])
            limit: Maximum results to return (default 10)

        Returns:
            Formatted string with search results and citations.
        """
        allowed, _remaining = budget.consume()
        if not allowed:
            return budget.exhausted_message()

        effective_limit = limit
        if max_results_per_call is not None:
            effective_limit = min(limit, max_results_per_call)
        effective_limit = max(1, effective_limit)
        return _search(backend, query, sources, effective_limit)

    # Strands captures the Python function name when @tool is applied.
    search_guidelines_custom.__name__ = "search_guidelines"
    return tool(search_guidelines_custom)


def create_summaries_search_tool(
    *,
    max_calls: int | None = None,
    max_results_per_call: int | None = None,
) -> Callable:
    """Create a search tool configured for guideline summaries.

    Returns:
        A search_guidelines tool that uses ripgrep on the summaries directory
    """
    backend = SearchBackendFactory.create_ripgrep_backend(DEFAULT_SUMMARIES_DIR)
    logger.info(f"Created summaries search tool for {DEFAULT_SUMMARIES_DIR}")
    return create_search_guidelines_tool(
        backend,
        max_calls=max_calls,
        max_results_per_call=max_results_per_call,
    )
