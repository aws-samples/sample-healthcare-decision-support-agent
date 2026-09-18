"""Tests for pre-parsed JSON parser."""

import json
import pytest
from pathlib import Path

from medical_nudging.parsers.preparsed_parser import (
    parse_preparsed_json,
    PreParsedJSONError,
)
from medical_nudging.parsers.ccda_parser import ParsedCCDA
from medical_nudging.parsers.fhir_parser import ParsedFHIR


# Path to sample preparsed data
PREPARSED_DATA_PATH = Path(__file__).parent.parent / "data" / "preparsed"
GENERIC_DATA_PATH = Path(__file__).parent.parent / "data" / "generic"


class TestParsePreparsedJSON:
    """Tests for parse_preparsed_json function."""

    def test_parse_ccda_schema(self):
        """Test parsing JSON that matches ParsedCCDA schema."""
        ccda_like = json.dumps(
            {
                "demographics": {
                    "name": {"given": "John", "family": "Doe"},
                    "dob": "1980-01-15",
                    "gender": "male",
                },
                "problems": [
                    {
                        "description": "Diabetes",
                        "code": "44054006",
                        "status": "active",
                    }
                ],
                "medications": [],
                "allergies": [],
                "vitals": [],
                "labs": [],
                "immunizations": [],
                "clinical_notes": [],
            }
        )

        result = parse_preparsed_json(ccda_like)

        assert isinstance(result, ParsedCCDA)
        assert result.demographics.name["given"] == "John"
        assert result.demographics.name["family"] == "Doe"
        assert len(result.problems) == 1
        assert result.problems[0].description == "Diabetes"

    def test_parse_fhir_schema(self):
        """Test parsing JSON that matches ParsedFHIR schema (has encounters)."""
        fhir_like = json.dumps(
            {
                "demographics": {
                    "name": {"given": "Jane", "family": "Smith"},
                    "dob": "1990-06-15",
                    "gender": "female",
                },
                "problems": [],
                "medications": [],
                "allergies": [],
                "vitals": [],
                "labs": [],
                "immunizations": [],
                "encounters": [
                    {
                        "encounter_type": "Office Visit",
                        "status": "finished",
                    }
                ],
                "procedures": [],
            }
        )

        result = parse_preparsed_json(fhir_like)

        assert isinstance(result, ParsedFHIR)
        assert result.demographics.name["given"] == "Jane"
        assert len(result.encounters) == 1
        assert result.encounters[0].encounter_type == "Office Visit"

    def test_parse_fhir_schema_with_procedures(self):
        """Test parsing JSON that matches ParsedFHIR schema (has procedures)."""
        fhir_like = json.dumps(
            {
                "demographics": {
                    "name": {"given": "Bob", "family": "Jones"},
                    "dob": "1970-03-20",
                    "gender": "male",
                },
                "problems": [],
                "medications": [],
                "allergies": [],
                "vitals": [],
                "labs": [],
                "immunizations": [],
                "encounters": [],
                "procedures": [
                    {
                        "description": "Blood draw",
                        "status": "completed",
                    }
                ],
            }
        )

        result = parse_preparsed_json(fhir_like)

        assert isinstance(result, ParsedFHIR)
        assert len(result.procedures) == 1
        assert result.procedures[0].description == "Blood draw"

    def test_parse_generic_passthrough(self):
        """Test parsing JSON that doesn't match any schema (passthrough)."""
        generic = json.dumps(
            {
                "custom_field": "value",
                "patient_info": {"name": "Custom Patient"},
                "data": [1, 2, 3],
            }
        )

        result = parse_preparsed_json(generic)

        assert isinstance(result, dict)
        assert result["custom_field"] == "value"
        assert result["patient_info"]["name"] == "Custom Patient"

    def test_parse_demographics_but_invalid_schema(self):
        """Test parsing JSON with demographics but failing schema validation."""
        # Has demographics but invalid types that won't pass validation
        invalid_schema = json.dumps(
            {
                "demographics": "not a dict",  # Wrong type
                "problems": [],
            }
        )

        result = parse_preparsed_json(invalid_schema)

        # Should fall through to passthrough since validation fails
        assert isinstance(result, dict)
        assert result["demographics"] == "not a dict"

    def test_parse_invalid_json(self):
        """Test that invalid JSON raises PreParsedJSONError."""
        with pytest.raises(PreParsedJSONError, match="Invalid JSON"):
            parse_preparsed_json("not valid json")

    def test_parse_non_object(self):
        """Test that non-object JSON raises PreParsedJSONError."""
        with pytest.raises(PreParsedJSONError, match="Expected JSON object"):
            parse_preparsed_json("[1, 2, 3]")

    def test_parse_real_preparsed_file(self):
        """Test parsing a real preparsed file."""
        preparsed_file = PREPARSED_DATA_PATH / "preparsed_01.json"
        if not preparsed_file.exists():
            pytest.skip(f"Test file not found: {preparsed_file}")

        content = preparsed_file.read_text()
        result = parse_preparsed_json(content)

        # preparsed_01.json has CCDA-like structure (no encounters/procedures)
        assert isinstance(result, ParsedCCDA)
        assert result.demographics.name["given"] == "Maria"
        assert len(result.problems) >= 1

    def test_parse_real_preparsed_file_fhir(self):
        """Test parsing a real preparsed file with FHIR structure."""
        preparsed_file = PREPARSED_DATA_PATH / "preparsed_02.json"
        if not preparsed_file.exists():
            pytest.skip(f"Test file not found: {preparsed_file}")

        content = preparsed_file.read_text()
        result = parse_preparsed_json(content)

        # preparsed_02.json has encounters and procedures (FHIR-like)
        assert isinstance(result, ParsedFHIR)
        assert result.demographics.name["given"] == "Robert"
        assert len(result.encounters) >= 1
        assert len(result.procedures) >= 1

    def test_parse_real_generic_file(self):
        """Test parsing a real generic file (passthrough)."""
        generic_file = GENERIC_DATA_PATH / "generic_01.json"
        if not generic_file.exists():
            pytest.skip(f"Test file not found: {generic_file}")

        content = generic_file.read_text()
        result = parse_preparsed_json(content)

        # generic_01.json doesn't have demographics, so passthrough
        assert isinstance(result, dict)
        assert "patient_id" in result
        assert "custom_field" in result


class TestModelDump:
    """Tests for model serialization."""

    def test_ccda_result_model_dump(self):
        """Test that ParsedCCDA result can be serialized."""
        ccda_like = json.dumps(
            {
                "demographics": {
                    "name": {"given": "Test", "family": "User"},
                    "dob": "1980-01-01",
                    "gender": "male",
                },
                "problems": [],
                "medications": [],
                "allergies": [],
                "vitals": [],
                "labs": [],
                "immunizations": [],
                "clinical_notes": [],
            }
        )

        result = parse_preparsed_json(ccda_like)

        assert isinstance(result, ParsedCCDA)
        dumped = result.model_dump()
        assert isinstance(dumped, dict)
        assert "demographics" in dumped

    def test_passthrough_result_json_serializable(self):
        """Test that passthrough result is JSON serializable."""
        generic = json.dumps({"custom": "data", "nested": {"key": "value"}})

        result = parse_preparsed_json(generic)

        assert isinstance(result, dict)
        json_str = json.dumps(result)
        assert "custom" in json_str
