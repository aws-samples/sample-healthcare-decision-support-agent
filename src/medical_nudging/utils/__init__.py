"""Utility modules for medical nudging."""

from pathlib import Path

from .summary_loader import load_guideline_summaries, format_summaries_for_prompt


def load_specialty_instructions(specialty: str) -> str:
    """Load specialty-specific instructions, falling back to general.

    Args:
        specialty: Specialty name (e.g., "cardiology", "endocrinology", "general")

    Returns:
        Markdown instructions content, or empty string if not found
    """
    prompts_dir = Path(__file__).parent.parent.parent.parent / "prompts" / "specialties"

    # Try specialty-specific file
    specialty_file = prompts_dir / f"{specialty}.md"
    if specialty_file.exists():
        return specialty_file.read_text()

    # Fall back to general
    general_file = prompts_dir / "general.md"
    if general_file.exists():
        return general_file.read_text()

    # Graceful degradation
    return ""


__all__ = [
    "load_guideline_summaries",
    "format_summaries_for_prompt",
    "load_specialty_instructions",
]
