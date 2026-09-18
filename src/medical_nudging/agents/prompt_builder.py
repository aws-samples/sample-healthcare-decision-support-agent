"""Prompt construction functions for the medical nudging agent.

Builds system and user prompts with custom instructions,
specialty guidance, and source filtering.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from medical_nudging.config import get_project_root
from medical_nudging.config_bundle import effective_base_prompt

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _clinical_time_context(visit_context: dict[str, Any]) -> str:
    """Describe the time anchor without misreading deidentified shifted dates."""
    if visit_context.get("clinical_date_mode") == "deidentified_shifted":
        return """<clinical_time_context mode="deidentified_shifted">
Chart calendar dates are deidentified and shifted. Use the latest retrieved encounter or clinical event as the chart-time anchor. Interpret recency, trends, and intervals only relative to other chart dates. Do not compare chart years to the wall clock or flag shifted future years as a data-integrity error.
</clinical_time_context>"""

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"<current_datetime>{now_utc}</current_datetime>"


def build_system_prompt(
    visit_context: dict,
    enabled_sources: list[str] | None = None,
    runtime_config: dict[str, Any] | None = None,
) -> str:
    """Build the full system prompt for the medical nudging agent.

    The base prompt is the active configuration bundle's ``system_prompt`` when a
    request named one, else ``prompts/orchestrator.md``
    (see :mod:`medical_nudging.config_bundle`). Source filtering and runtime limits
    are appended either way.

    Args:
        visit_context: Visit context dict (reserved for future extensions)
        enabled_sources: Optional list of guideline source names to restrict search
        runtime_config: Output and request-scoped tool limits

    Returns:
        Complete system prompt string
    """
    base_prompt = effective_base_prompt()

    if enabled_sources:
        source_list = ", ".join(enabled_sources)
        base_prompt += f"""

SOURCE FILTERING:
Only search these guideline sources: {source_list}
When calling search_guidelines, pass them as the 'sources' parameter."""

    config = runtime_config or {}
    max_nudges = config.get("max_nudges")
    tool_limits = config.get("tool_limits", {})
    search_limits = tool_limits.get("guideline_search", {})
    fhir_limits = tool_limits.get("fhir_query", {})
    constraints = []
    if max_nudges is not None:
        constraints.append(f"- Final nudge maximum: {max_nudges}")
    if search_limits.get("max_calls") is not None:
        constraints.append(
            f"- Guideline-search external call maximum: {search_limits['max_calls']}"
        )
    if search_limits.get("max_results_per_call") is not None:
        constraints.append(
            "- Guideline passages per search maximum: " f"{search_limits['max_results_per_call']}"
        )
    if fhir_limits.get("max_calls") is not None:
        constraints.append(f"- FHIR query-round maximum: {fhir_limits['max_calls']}")
    if fhir_limits.get("max_resource_types_per_call") is not None:
        constraints.append(
            "- FHIR resource types per round maximum: "
            f"{fhir_limits['max_resource_types_per_call']}"
        )
    if fhir_limits.get("max_results_per_resource") is not None:
        constraints.append(
            "- FHIR results per resource maximum: " f"{fhir_limits['max_results_per_resource']}"
        )
    if fhir_limits.get("recent_history_days") is not None:
        anchor = fhir_limits.get("history_anchor", "wall_clock")
        anchor_text = (
            "relative to the latest dated resource returned"
            if anchor == "latest_resource"
            else "relative to the current date"
        )
        constraints.append(
            f"- Default dated-history window: {fhir_limits['recent_history_days']} days. "
            f"Apply it {anchor_text}. Request older history only for a specific "
            "clinically relevant question."
        )
    if constraints:
        base_prompt += "\n\n# RUNTIME LIMITS\n\n" + "\n".join(constraints)

    return base_prompt


def load_custom_instructions(visit_context: dict) -> str | None:
    """Load custom instructions from preset file or direct text.

    Direct text in ``visit_context["custom_instructions"]`` takes precedence
    over a preset name in ``visit_context["custom_instructions_preset"]``.

    Args:
        visit_context: Dict with optional ``custom_instructions`` or
            ``custom_instructions_preset`` keys

    Returns:
        Custom instructions string, or ``None`` if not configured
    """
    # Direct text takes precedence
    if visit_context.get("custom_instructions"):
        return visit_context["custom_instructions"]

    # Load from preset file
    preset_name = visit_context.get("custom_instructions_preset")
    if preset_name and yaml is not None:
        preset_path = get_project_root() / "config" / "custom_instructions" / f"{preset_name}.yaml"
        if preset_path.exists():
            try:
                with open(preset_path) as f:
                    preset = yaml.safe_load(f)
                    return preset.get("instructions", "")
            except Exception as e:
                logger.warning(f"Failed to load custom instructions preset {preset_name}: {e}")
    return None


def _visit_preamble(
    visit_context: dict,
    specialty: str,
    custom_instructions: str | None,
    specialty_instructions: str | None,
) -> str:
    """The user-prompt opening shared by every data source: task, time, visit, instructions."""
    eff_specialty = visit_context.get("specialty", specialty)
    time_context = _clinical_time_context(visit_context)

    prompt = f"""Generate clinical nudges for this patient visit.

{time_context}

<visit_context>
<specialty>{eff_specialty}</specialty>"""

    if visit_context.get("visit_type"):
        prompt += f"\n<visit_type>{visit_context['visit_type']}</visit_type>"

    if visit_context.get("chief_complaint"):
        prompt += f"\n<chief_complaint>{visit_context['chief_complaint']}</chief_complaint>"

    prompt += "\n</visit_context>"

    if custom_instructions:
        prompt += f"""

<custom_instructions>
{custom_instructions}
</custom_instructions>"""

    if specialty_instructions:
        prompt += f"""

<specialty_instructions>
{specialty_instructions}
</specialty_instructions>"""

    return prompt


def build_user_prompt(
    parsed_data: dict[str, Any],
    visit_context: dict,
    temp_path: str,
    specialty: str,
    custom_instructions: str | None = None,
    specialty_instructions: str | None = None,
) -> str:
    """Build the user prompt with XML-tagged visit context and patient data.

    Args:
        parsed_data: Pre-parsed patient data dict
        visit_context: Visit context (specialty, visit_type, chief_complaint, etc.)
        temp_path: Path to temp file containing the raw document
        specialty: Default specialty fallback
        custom_instructions: Optional custom instructions text
        specialty_instructions: Optional pre-loaded specialty guidance text

    Returns:
        Complete user prompt string
    """
    prompt = _visit_preamble(visit_context, specialty, custom_instructions, specialty_instructions)

    prompt += f"""

<patient_data>
{json.dumps(parsed_data, indent=2)}
</patient_data>

<raw_document_path>{temp_path}</raw_document_path>

Begin your analysis now."""

    return prompt


def build_user_prompt_fhir(
    patient_id: str,
    visit_context: dict,
    specialty: str,
    custom_instructions: str | None = None,
    specialty_instructions: str | None = None,
    injected_guideline_context: str | None = None,
) -> str:
    """Build the user prompt for FHIR API data source mode.

    Instead of embedding pre-parsed data, instructs the agent to query
    patient data on demand from the FHIR API using the patient_id.

    Args:
        patient_id: FHIR Patient resource ID
        visit_context: Visit context dict
        specialty: Specialty for this visit
        custom_instructions: Optional custom instructions text
        specialty_instructions: Optional pre-loaded specialty guidance text
        injected_guideline_context: Pre-retrieved guideline passages to embed in the
            prompt (single-pass RAG condition — no guideline search tools are exposed,
            so these passages are the only guideline evidence available)

    Returns:
        Complete user prompt string
    """
    prompt = _visit_preamble(visit_context, specialty, custom_instructions, specialty_instructions)

    if injected_guideline_context:
        prompt += f"""

<retrieved_guideline_passages>
The following clinical guideline passages were retrieved in a single retrieval round
before generation. They are the ONLY guideline evidence available for this visit: you
have no guideline search tool, and you must not cite any guideline content that does
not appear verbatim below.

{injected_guideline_context}
</retrieved_guideline_passages>"""

    prompt += f"""

<fhir_patient_id>{patient_id}</fhir_patient_id>

Patient data is NOT pre-loaded. You MUST query the FHIR API to retrieve patient data.

FHIR Query Tips:
- If a filtered query returns empty, retry without the status filter
- MedicationRequest statuses include "completed" for historical medications
- Use _count parameter to limit large result sets
- Use date parameter for temporal filtering
- Query Encounter resources to understand visit history

Begin your analysis now."""

    return prompt
