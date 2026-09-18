"""Tests for guideline search tool."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from medical_nudging.tools import search_guidelines
from medical_nudging.tools.guideline_search import create_search_guidelines_tool

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guidelines"


@pytest.fixture(autouse=True)
def setup_guidelines_path():
    """Set GUIDELINES_PATH to test fixtures for all tests."""
    from medical_nudging.search import factory

    # Clear any cached backends
    factory._backends.clear()

    # Set environment to use test fixtures
    with patch.dict(
        os.environ, {"GUIDELINES_PATH": str(FIXTURES_DIR), "SEARCH_BACKEND": "ripgrep"}
    ):
        yield
        # Clean up cached backends after test
        factory._backends.clear()


def _extract_text(result):
    """Helper to extract text from tool response."""
    if isinstance(result, dict):
        return result["content"][0]["text"]
    return result


def test_exact_match_returns_correct_passage():
    """Test that exact match returns correct passage with citation."""
    result = search_guidelines("HbA1c target should be <7%")
    text = _extract_text(result)

    assert "ADA_2024_diabetes.txt" in text or "diabetes" in text.lower()
    assert (
        "HbA1c target should be <7% for most adults with diabetes" in text or "HbA1c target" in text
    )


def test_case_insensitive_matching():
    """Test that case-insensitive matching works."""
    result = search_guidelines("hba1c target")
    text = _extract_text(result)

    assert "diabetes" in text.lower()
    assert "HbA1c target" in text or "hba1c target" in text.lower()


def test_no_results_returns_appropriate_message():
    """Test that no results returns appropriate message."""
    result = search_guidelines("xyznonexistentterm123")
    text = _extract_text(result)

    assert "No matching guidelines found" in text


def test_filter_to_specific_guideline():
    """Test that filtering to specific guideline works."""
    result = search_guidelines("target", sources=["diabetes"])
    text = _extract_text(result)

    assert "diabetes" in text.lower()
    assert "HbA1c target" in text or "target" in text.lower()
    # Should not include cardiovascular guidelines
    assert "cardiovascular" not in text.lower() or "ACC_AHA" not in text


def test_multiple_matches_return_all_results():
    """Test that multiple matches return all results."""
    result = search_guidelines("recommended for patients")
    text = _extract_text(result)

    # Should find matches in guidelines
    assert "recommended" in text.lower() or "patients" in text.lower()
    # Should have multiple line citations or sections
    assert text.count("===") >= 1 or text.count("Line") >= 2


def test_cardiovascular_guideline_search():
    """Test searching cardiovascular guidelines."""
    result = search_guidelines("blood pressure target")
    text = _extract_text(result)

    assert "cardiovascular" in text.lower() or "blood pressure" in text.lower()
    assert "130/80 mmHg" in text or "blood pressure" in text.lower()


def test_context_lines_included():
    """Test that context lines are included in results."""
    result = search_guidelines("HbA1c target should be <7%")
    text = _extract_text(result)

    # Should have context lines around the match
    lines = text.split("\n")
    # Should have more than just the matching line
    assert len([line for line in lines if line.strip() and not line.startswith("===")]) > 1


def test_fallback_to_file_read_when_ripgrep_not_available():
    """Test that fallback to Python search works when ripgrep is not available."""
    # Force ripgrep backend and disable ripgrep binary to trigger Python fallback
    from medical_nudging.search import factory

    factory._backends.clear()

    with patch("shutil.which", return_value=None):
        result = search_guidelines("HbA1c target")
        text = _extract_text(result)

        assert "ADA_2024_diabetes" in text or "diabetes" in text.lower()
        assert "HbA1c target" in text or "hba1c target" in text.lower()
        assert "Line" in text


def test_fallback_no_results():
    """Test that fallback returns appropriate message when no results found."""
    # Force ripgrep backend with Python fallback
    from medical_nudging.search import factory

    factory._backends.clear()

    with patch("shutil.which", return_value=None):
        result = search_guidelines("xyznonexistentterm123")
        text = _extract_text(result)

        assert "No matching guidelines found" in text


def test_fallback_filter_by_guideline():
    """Test that fallback filtering by guideline name works."""
    # Force ripgrep backend with Python fallback
    from medical_nudging.search import factory

    factory._backends.clear()

    with patch("shutil.which", return_value=None):
        result = search_guidelines("target", sources=["diabetes"])
        text = _extract_text(result)

        assert "ADA_2024_diabetes" in text or "diabetes" in text.lower()
        assert "cardiovascular" not in text.lower() or "ACC_AHA" not in text


class FakeSearchBackend:
    name = "fake"

    def __init__(self):
        self.limits = []

    def search(self, query, sources=None, limit=10):
        self.limits.append(limit)
        return []


def test_request_scoped_search_tool_enforces_call_and_result_limits():
    backend = FakeSearchBackend()
    bounded_search = create_search_guidelines_tool(
        backend,
        max_calls=1,
        max_results_per_call=3,
    )

    first = _extract_text(bounded_search("sepsis", limit=10))
    second = _extract_text(bounded_search("shock", limit=10))

    assert bounded_search.tool_name == "search_guidelines"
    assert first == "No matching guidelines found."
    assert "BUDGET_EXHAUSTED" in second
    assert backend.limits == [3]


def test_python_fallback_searches_markdown_summaries(tmp_path):
    """Without rg, the pure-Python path must find the markdown summaries the SOP produces."""
    from medical_nudging.search.ripgrep_backend import RipgrepBackend

    summaries = tmp_path / "summaries" / "ADA"
    summaries.mkdir(parents=True)
    (summaries / "ADA_2024_diabetes.md").write_text(
        "# ADA 2024\n\nHbA1c target should be <7% for most adults.\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("blood pressure target <130/80\n", encoding="utf-8")

    backend = RipgrepBackend(guidelines_dir=tmp_path)
    with patch("medical_nudging.search.ripgrep_backend.shutil.which", return_value=None):
        md_hits = backend.search("HbA1c target")
        txt_hits = backend.search("blood pressure")

    assert [r.source for r in md_hits] == ["ADA_2024_diabetes"]
    assert "HbA1c target" in md_hits[0].content
    assert [r.source for r in txt_hits] == ["notes"]
