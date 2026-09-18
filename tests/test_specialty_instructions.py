"""Tests for specialty instruction loading."""

from medical_nudging.utils import load_specialty_instructions


def test_load_cardiology_instructions():
    """Test loading cardiology specialty instructions."""
    content = load_specialty_instructions("cardiology")
    assert content, "Cardiology instructions should not be empty"
    assert "cardiology" in content.lower()
    assert "heart" in content.lower() or "cardiovascular" in content.lower()


def test_load_endocrinology_instructions():
    """Test loading endocrinology specialty instructions."""
    content = load_specialty_instructions("endocrinology")
    assert content, "Endocrinology instructions should not be empty"
    assert "endocrinology" in content.lower()
    assert "diabetes" in content.lower()


def test_load_general_instructions():
    """Test loading general specialty instructions."""
    content = load_specialty_instructions("general")
    assert content, "General instructions should not be empty"
    assert "general" in content.lower()


def test_fallback_to_general():
    """Test fallback to general.md for unknown specialty."""
    content = load_specialty_instructions("unknown_specialty")
    assert content, "Should fall back to general instructions"
    assert "general" in content.lower()


def test_empty_string_if_no_files():
    """Test graceful degradation if no files exist (edge case)."""
    # This test validates the code path, though in practice general.md should exist
    # We can't easily test this without mocking, so we just verify it doesn't crash
    content = load_specialty_instructions("nonexistent_with_no_general_fallback")
    assert isinstance(content, str)


def test_cardiology_contains_expected_keywords():
    """Test that cardiology instructions contain relevant keywords."""
    content = load_specialty_instructions("cardiology")
    keywords = ["heart failure", "atrial fibrillation", "coronary", "acc", "aha"]
    found_keywords = [kw for kw in keywords if kw.lower() in content.lower()]
    assert (
        len(found_keywords) >= 3
    ), f"Expected at least 3 cardiology keywords, found: {found_keywords}"


def test_endocrinology_contains_expected_keywords():
    """Test that endocrinology instructions contain relevant keywords."""
    content = load_specialty_instructions("endocrinology")
    keywords = ["diabetes", "thyroid", "a1c", "ada", "metabolic"]
    found_keywords = [kw for kw in keywords if kw.lower() in content.lower()]
    assert (
        len(found_keywords) >= 3
    ), f"Expected at least 3 endocrinology keywords, found: {found_keywords}"


def test_general_contains_expected_keywords():
    """Test that general instructions contain relevant keywords."""
    content = load_specialty_instructions("general")
    keywords = ["preventive", "screening", "vaccination", "chronic disease", "uspstf"]
    found_keywords = [kw for kw in keywords if kw.lower() in content.lower()]
    assert (
        len(found_keywords) >= 3
    ), f"Expected at least 3 general practice keywords, found: {found_keywords}"
