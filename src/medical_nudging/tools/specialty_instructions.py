"""Tool for loading specialty-specific clinical guidance."""

import logging
from pathlib import Path

from strands import tool

logger = logging.getLogger(__name__)

# Maximum age in months for pediatric guidance (18 years)
PEDIATRIC_AGE_THRESHOLD_MONTHS = 216


def _get_specialties_dir() -> Path:
    """Get the specialties directory path."""
    return Path(__file__).parent.parent.parent.parent / "prompts" / "specialties"


def _load_specialty_file(specialty: str) -> str | None:
    """Load a single specialty file.

    Args:
        specialty: Specialty name (e.g., "cardiology", "pediatrics")

    Returns:
        File contents or None if not found
    """
    specialties_dir = _get_specialties_dir()
    specialty_file = specialties_dir / f"{specialty}.md"

    if specialty_file.exists():
        try:
            return specialty_file.read_text()
        except OSError as e:
            logger.warning(f"Failed to read specialty file {specialty_file}: {e}")
            return None
    return None


def _list_available_specialties() -> list[str]:
    """List all available specialty instruction files.

    Returns:
        List of specialty names (without .md extension)
    """
    specialties_dir = _get_specialties_dir()
    if not specialties_dir.exists():
        return []

    return [f.stem for f in specialties_dir.glob("*.md")]


def load_specialty_guidance(
    specialty: str,
    patient_age_months: int | None = None,
) -> str | None:
    """Load specialty instructions for embedding in user prompt.

    Non-tool variant for pre-loading specialty guidance at prompt build time.
    Same logic as the ``@tool`` version but returns ``None`` when no files found.

    Args:
        specialty: Medical specialty for this visit
        patient_age_months: Patient age in months for pediatric auto-include

    Returns:
        Specialty instructions string, or ``None`` if nothing found
    """
    specialties_loaded: list[str] = []
    guidance_sections: list[str] = []

    specialty_lower = specialty.lower()
    specialty_content = _load_specialty_file(specialty_lower)

    if specialty_content:
        specialties_loaded.append(specialty_lower)
        guidance_sections.append(specialty_content)
    else:
        general_content = _load_specialty_file("general")
        if general_content:
            specialties_loaded.append("general")
            guidance_sections.append(general_content)
            logger.info(
                f"Specialty '{specialty}' not found, using general guidance. "
                f"Available: {_list_available_specialties()}"
            )

    if (
        patient_age_months is not None
        and patient_age_months <= PEDIATRIC_AGE_THRESHOLD_MONTHS
        and "pediatrics" not in specialties_loaded
    ):
        pediatric_content = _load_specialty_file("pediatrics")
        if pediatric_content:
            specialties_loaded.append("pediatrics")
            guidance_sections.append(
                "\n\n---\n\n"
                "# Additional Pediatric Guidance\n"
                "(Auto-included because patient age ≤18 years)\n\n" + pediatric_content
            )

    if not guidance_sections:
        return None

    header = f"# Specialty Guidance Loaded: {', '.join(specialties_loaded)}\n\n"
    return header + "\n\n".join(guidance_sections)


@tool
def load_specialty_instructions(
    specialty: str,
    patient_age_months: int | None = None,
) -> str:
    """Load specialty-specific clinical guidance for nudge generation.

    Call this tool EARLY in the nudge generation process to get specialty-specific
    clinical guidance. The guidance will inform how you interpret clinical data
    and what nudges are appropriate.

    Args:
        specialty: Medical specialty for this visit (e.g., "cardiology",
                   "endocrinology", "pediatrics", "general")
        patient_age_months: Patient age in months. If provided and patient is
                           ≤18 years (216 months), pediatric guidance will be
                           automatically included alongside the requested specialty.

    Returns:
        Specialty instructions as markdown text. May include multiple specialty
        guidance sections if the patient characteristics warrant it (e.g.,
        cardiology visit for a 10-year-old will include both cardiology AND
        pediatric guidance).

    Examples:
        # Adult cardiology patient
        load_specialty_instructions("cardiology", patient_age_months=600)
        # Returns: cardiology guidance only

        # Pediatric cardiology patient (6 months old)
        load_specialty_instructions("cardiology", patient_age_months=6)
        # Returns: cardiology guidance + pediatric guidance

        # Well-child visit
        load_specialty_instructions("pediatrics", patient_age_months=24)
        # Returns: pediatric guidance
    """
    specialties_loaded = []
    guidance_sections = []

    # Load the requested specialty
    specialty_lower = specialty.lower()
    specialty_content = _load_specialty_file(specialty_lower)

    if specialty_content:
        specialties_loaded.append(specialty_lower)
        guidance_sections.append(specialty_content)
    else:
        # Fall back to general if specialty not found
        general_content = _load_specialty_file("general")
        if general_content:
            specialties_loaded.append("general")
            guidance_sections.append(general_content)
            logger.info(
                f"Specialty '{specialty}' not found, using general guidance. "
                f"Available specialties: {_list_available_specialties()}"
            )

    # Auto-include pediatric guidance for young patients (unless already loading pediatrics)
    if (
        patient_age_months is not None
        and patient_age_months <= PEDIATRIC_AGE_THRESHOLD_MONTHS
        and "pediatrics" not in specialties_loaded
    ):
        pediatric_content = _load_specialty_file("pediatrics")
        if pediatric_content:
            specialties_loaded.append("pediatrics")
            guidance_sections.append(
                "\n\n---\n\n"
                "# Additional Pediatric Guidance\n"
                "(Auto-included because patient age ≤18 years)\n\n" + pediatric_content
            )
            logger.info(
                f"Auto-included pediatric guidance for patient age {patient_age_months} months"
            )

    if not guidance_sections:
        available = _list_available_specialties()
        return (
            f"No specialty instructions found for '{specialty}'.\n\n"
            f"Available specialties: {', '.join(available) if available else 'none'}\n\n"
            "Proceeding with general clinical knowledge."
        )

    result = "\n\n".join(guidance_sections)

    # Add header indicating what was loaded
    header = f"# Specialty Guidance Loaded: {', '.join(specialties_loaded)}\n\n"

    return header + result
