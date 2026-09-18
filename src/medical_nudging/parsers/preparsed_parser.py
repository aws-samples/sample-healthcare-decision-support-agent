"""Pre-parsed JSON parser.

This module handles JSON data that has already been parsed into a structured format.
It validates against existing schemas (ParsedCCDA, ParsedFHIR) when possible,
otherwise passes through as-is.
"""

import json
from typing import Any

from pydantic import ValidationError

from medical_nudging.parsers.ccda_parser import ParsedCCDA
from medical_nudging.parsers.fhir_parser import ParsedFHIR


class PreParsedJSONError(Exception):
    """Raised when pre-parsed JSON parsing fails."""

    pass


def parse_preparsed_json(json_str: str) -> ParsedCCDA | ParsedFHIR | dict[str, Any]:
    """Parse pre-parsed JSON. Validates if schema matches, else passthrough.

    This function handles JSON data that has already been parsed/structured
    (e.g., from a separate preprocessing step or external system).

    Detection logic:
    - If data has "encounters" or "procedures" keys, try ParsedFHIR schema
    - If data has "demographics" key, try ParsedCCDA schema
    - If validation fails or no schema matches, return as-is dict (passthrough)

    Args:
        json_str: Pre-parsed JSON document as string

    Returns:
        ParsedCCDA if data matches CCDA schema,
        ParsedFHIR if data matches FHIR schema,
        dict if no schema matches (generic passthrough)

    Raises:
        PreParsedJSONError: If JSON is malformed
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise PreParsedJSONError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise PreParsedJSONError(f"Expected JSON object, got {type(data).__name__}")

    # Try FHIR schema (has encounters/procedures which are FHIR-specific)
    if "encounters" in data or "procedures" in data:
        try:
            return ParsedFHIR(**data)
        except ValidationError:
            pass  # Fall through to try CCDA or passthrough

    # Try CCDA schema (has demographics but no FHIR-specific fields)
    if "demographics" in data:
        try:
            return ParsedCCDA(**data)
        except ValidationError:
            pass  # Fall through to passthrough

    # Passthrough: return as-is
    return data
