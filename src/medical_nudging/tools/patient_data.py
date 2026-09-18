"""Patient data extraction tool with auto-detection for CCDA and FHIR formats."""

import json
import tempfile
from pathlib import Path

from strands import tool

from medical_nudging.parsers.ccda_parser import CCDAParseError, parse_ccda
from medical_nudging.parsers.fhir_parser import FHIRParseError, parse_fhir
from medical_nudging.parsers.preparsed_parser import PreParsedJSONError, parse_preparsed_json

# Allowed directories for file access (security: prevent path traversal)
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
ALLOWED_DIRS = [
    Path(tempfile.gettempdir()),
    _PROJECT_ROOT / "tests" / "fixtures",
]


def _validate_file_path(file_path: str) -> Path:
    """Validate file path is within allowed directories.

    Security measure to prevent path traversal attacks where an attacker
    could manipulate the LLM to read arbitrary files.

    Args:
        file_path: Path to validate

    Returns:
        Resolved Path object if valid

    Raises:
        ValueError: If path is outside allowed directories
        FileNotFoundError: If file does not exist
    """
    path = Path(file_path).resolve()

    # Check if path is within any allowed directory
    for allowed_dir in ALLOWED_DIRS:
        try:
            allowed_resolved = allowed_dir.resolve()
            if path.is_relative_to(allowed_resolved):
                if not path.exists():
                    raise FileNotFoundError(f"File not found: {file_path}")
                return path
        except ValueError:
            # is_relative_to raises ValueError if paths are on different drives (Windows)
            continue

    raise ValueError(f"Access denied: path outside allowed directories: {file_path}")


@tool
def get_patient_data(patient_data: str = "", file_path: str = "") -> str:
    """
    Extract structured patient data from CCDA XML, FHIR JSON, or pre-parsed JSON.

    Automatically detects the format:
    - If input starts with '<' or '<?xml' → CCDA parser
    - If input starts with '{':
      - If "resourceType": "Bundle" → FHIR parser
      - If has "demographics" key → Pre-parsed JSON (validates against schema or passthrough)
      - Otherwise → Generic JSON passthrough

    This is a deterministic tool that parses patient data without LLM involvement.

    Args:
        patient_data: Patient data document as string (CCDA XML, FHIR JSON, or pre-parsed JSON) - optional if file_path provided
        file_path: Path to patient data file - optional if patient_data provided

    Returns:
        JSON string containing parsed patient data in a structured format.
        On error, returns an error message string.
    """
    try:
        # Determine source of patient data
        if file_path:
            # Read from file (with path validation for security)
            try:
                validated_path = _validate_file_path(file_path)
                data = validated_path.read_text()
            except FileNotFoundError:
                return f"ERROR: File not found: {file_path}"
            except ValueError as e:
                # Path validation failed - security violation
                return f"ERROR: {str(e)}"
            except Exception as e:
                return f"ERROR: Failed to read file {file_path}: {str(e)}"
        else:
            # Use provided string (even if empty for backward compatibility)
            data = patient_data

        data = data.strip()

        # Check if data is empty after stripping
        if not data:
            return "ERROR: Unknown format. Expected CCDA XML (starts with '<') or FHIR JSON (starts with '{')"

        # Auto-detect format based on first character
        if data.startswith("<") or data.startswith("<?xml"):
            # CCDA XML format
            parsed_ccda = parse_ccda(data)
            # Convert to JSON string for LLM consumption
            return json.dumps(parsed_ccda.model_dump(), indent=2)

        elif data.startswith("{"):
            # JSON format - auto-detect FHIR Bundle vs pre-parsed vs generic
            parsed = json.loads(data)

            if parsed.get("resourceType") == "Bundle":
                # FHIR Bundle format
                parsed_fhir = parse_fhir(data)
                return json.dumps(parsed_fhir.model_dump(), indent=2)

            if "demographics" in parsed:
                # Pre-parsed JSON (has demographics key - try schema validation)
                result = parse_preparsed_json(data)
                if hasattr(result, "model_dump"):
                    return json.dumps(result.model_dump(), indent=2)
                return json.dumps(result, indent=2)

            # Generic JSON passthrough
            return json.dumps(parsed, indent=2)

        else:
            return "ERROR: Unknown format. Expected CCDA XML (starts with '<') or JSON (starts with '{')"

    except CCDAParseError as e:
        return f"ERROR: CCDA parsing failed: {str(e)}"

    except FHIRParseError as e:
        return f"ERROR: FHIR parsing failed: {str(e)}"

    except PreParsedJSONError as e:
        return f"ERROR: Pre-parsed JSON parsing failed: {str(e)}"

    except Exception as e:
        return f"ERROR: Unexpected error: {str(e)}"
