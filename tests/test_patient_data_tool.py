"""Tests for get_patient_data tool with auto-detection."""

import json
import pytest

from medical_nudging.tools.patient_data import get_patient_data


@pytest.fixture
def valid_ccda_xml() -> str:
    """Minimal valid CCDA XML."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>John</given>
          <family>Doe</family>
        </name>
        <administrativeGenderCode code="M"/>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="11450-4" codeSystem="2.16.840.1.113883.6.1" displayName="Problem List"/>
          <entry>
            <act classCode="ACT" moodCode="EVN">
              <entryRelationship typeCode="SUBJ">
                <observation classCode="OBS" moodCode="EVN">
                  <value xsi:type="CD" code="44054006" displayName="Diabetes"/>
                </observation>
              </entryRelationship>
            </act>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>"""


@pytest.fixture
def malformed_xml() -> str:
    """Malformed XML string."""
    return "<ClinicalDocument>Not closed properly"


def test_get_patient_data_success(valid_ccda_xml: str) -> None:
    """Test successful patient data extraction from CCDA."""
    result = get_patient_data(valid_ccda_xml)

    # Tool returns JSON string on success
    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    # Parse the JSON response
    content = json.loads(result)

    # Check parsed data
    assert "demographics" in content
    assert content["demographics"]["name"]["given"] == "John"
    assert content["demographics"]["name"]["family"] == "Doe"
    assert content["demographics"]["gender"] == "M"
    assert content["demographics"]["dob"] == "19800101"

    # Check problems
    assert "problems" in content
    assert len(content["problems"]) == 1
    assert content["problems"][0]["description"] == "Diabetes"

    # Check other sections exist (even if empty)
    assert "medications" in content
    assert "allergies" in content
    assert "vitals" in content
    assert "labs" in content
    assert "immunizations" in content
    assert "clinical_notes" in content


def test_get_patient_data_malformed_xml(malformed_xml: str) -> None:
    """Test handling of malformed XML."""
    result = get_patient_data(malformed_xml)

    # Should return error string
    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "CCDA parsing failed" in result


def test_get_patient_data_empty_string() -> None:
    """Test handling of empty string."""
    result = get_patient_data("")

    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "Unknown format" in result


def test_get_patient_data_non_xml_string() -> None:
    """Test handling of non-XML/JSON input."""
    result = get_patient_data("This is not XML or JSON")

    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "Unknown format" in result


def test_get_patient_data_returns_string() -> None:
    """Test that tool always returns a string."""
    # Test with minimal valid XML
    minimal_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Test</given><family>User</family></name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    result = get_patient_data(minimal_xml)

    assert isinstance(result, str)
    # Success case returns valid JSON
    content = json.loads(result)
    assert "demographics" in content


def test_get_patient_data_preserves_all_sections(valid_ccda_xml: str) -> None:
    """Test that all CCDA sections are preserved in the output."""
    result = get_patient_data(valid_ccda_xml)

    assert not result.startswith("ERROR:")
    content = json.loads(result)

    # All sections should be present in the output
    expected_sections = [
        "demographics",
        "problems",
        "medications",
        "allergies",
        "vitals",
        "labs",
        "immunizations",
        "clinical_notes",
    ]

    for section in expected_sections:
        assert section in content, f"Missing section: {section}"


# --- FHIR Auto-Detection Tests ---


@pytest.fixture
def valid_fhir_json() -> str:
    """Minimal valid FHIR Bundle JSON."""
    return json.dumps(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": "test-patient",
                        "name": [{"given": ["Jane"], "family": "Smith"}],
                        "birthDate": "1990-06-15",
                        "gender": "female",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Condition",
                        "id": "test-condition",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "code": {
                            "coding": [{"code": "38341003", "display": "Hypertension"}],
                            "text": "Hypertension",
                        },
                        "onsetDateTime": "2022-01-15",
                    }
                },
            ],
        }
    )


def test_get_patient_data_fhir_success(valid_fhir_json: str) -> None:
    """Test successful patient data extraction from FHIR."""
    result = get_patient_data(valid_fhir_json)

    # Tool returns JSON string on success
    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    # Parse the JSON response
    content = json.loads(result)

    # Check parsed data
    assert "demographics" in content
    assert content["demographics"]["name"]["given"] == "Jane"
    assert content["demographics"]["name"]["family"] == "Smith"
    assert content["demographics"]["gender"] == "female"
    assert content["demographics"]["dob"] == "1990-06-15"

    # Check problems
    assert "problems" in content
    assert len(content["problems"]) == 1
    assert content["problems"][0]["description"] == "Hypertension"


def test_get_patient_data_fhir_with_whitespace() -> None:
    """Test FHIR detection with leading whitespace."""
    fhir_json = """
    {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": []
    }
    """
    result = get_patient_data(fhir_json)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")
    # Should parse as valid JSON
    content = json.loads(result)
    assert "demographics" in content


def test_get_patient_data_fhir_invalid_json() -> None:
    """Test handling of invalid JSON."""
    invalid_json = '{"resourceType": "Bundle", "entry": [}'  # Missing closing bracket
    result = get_patient_data(invalid_json)

    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    # Now catches as generic JSON error since parse happens early
    assert "error" in result.lower()


def test_get_patient_data_fhir_non_bundle() -> None:
    """Test handling of FHIR non-Bundle resource.

    Non-Bundle FHIR resources are now treated as generic JSON passthrough
    since they don't match "resourceType": "Bundle".
    """
    patient_only = json.dumps({"resourceType": "Patient", "id": "test"})
    result = get_patient_data(patient_only)

    assert isinstance(result, str)
    # Now passes through as generic JSON (no demographics, no Bundle)
    assert not result.startswith("ERROR:")
    content = json.loads(result)
    assert content["resourceType"] == "Patient"


def test_get_patient_data_fhir_preserves_sections(valid_fhir_json: str) -> None:
    """Test that all FHIR sections are preserved in the output."""
    result = get_patient_data(valid_fhir_json)

    assert not result.startswith("ERROR:")
    content = json.loads(result)

    # All sections should be present in FHIR output (including FHIR-specific ones)
    expected_sections = [
        "demographics",
        "problems",
        "medications",
        "allergies",
        "vitals",
        "labs",
        "immunizations",
        "encounters",  # FHIR-specific
        "procedures",  # FHIR-specific
    ]

    for section in expected_sections:
        assert section in content, f"Missing section: {section}"


def test_get_patient_data_auto_detect_ccda_no_xml_declaration() -> None:
    """Test CCDA detection without XML declaration (starts with '<')."""
    ccda_without_declaration = """<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Test</given><family>User</family></name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    result = get_patient_data(ccda_without_declaration)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")
    content = json.loads(result)
    assert "demographics" in content


# --- Security Tests: Path Traversal Prevention ---


def test_path_traversal_absolute_path_blocked() -> None:
    """Test that absolute paths outside allowed directories are blocked.

    Security test: Prevents reading sensitive system files like /etc/passwd.
    """
    result = get_patient_data(file_path="/etc/passwd")

    assert result.startswith("ERROR:")
    assert "Access denied" in result


def test_path_traversal_relative_attack_blocked() -> None:
    """Test that relative path traversal attacks are blocked.

    Security test: Prevents using ../ sequences to escape allowed directories.
    """
    result = get_patient_data(file_path="../../../etc/passwd")

    assert result.startswith("ERROR:")
    assert "Access denied" in result


def test_path_traversal_home_directory_blocked() -> None:
    """Test that access to home directory files is blocked.

    Security test: Prevents reading user credentials or config files.
    """
    result = get_patient_data(file_path="~/.aws/credentials")

    assert result.startswith("ERROR:")
    # Either "Access denied" or "File not found" depending on path resolution
    assert "ERROR:" in result


def test_path_traversal_temp_dir_allowed() -> None:
    """Test that files in temp directory are allowed.

    The temp directory is where the orchestrator stores patient data
    for optional access by the get_patient_data tool.
    """
    import tempfile
    import os

    # Create a temp file with valid CCDA content
    ccda_content = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>TempTest</given><family>User</family></name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    fd, temp_path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ccda_content)

        result = get_patient_data(file_path=temp_path)

        # Should succeed - temp directory is allowed
        assert not result.startswith("ERROR:")
        content = json.loads(result)
        assert content["demographics"]["name"]["given"] == "TempTest"
    finally:
        os.unlink(temp_path)


def test_path_traversal_test_fixtures_allowed() -> None:
    """Test that files in test fixtures directory are allowed.

    Test fixtures should be accessible for development/testing convenience.
    """
    from pathlib import Path

    fixture_path = Path(__file__).parent / "fixtures" / "sample_ccda.xml"

    if fixture_path.exists():
        result = get_patient_data(file_path=str(fixture_path))

        # Should succeed - test fixtures directory is allowed
        assert not result.startswith("ERROR:")
        content = json.loads(result)
        assert "demographics" in content


def test_path_traversal_nonexistent_file_in_allowed_dir() -> None:
    """Test that nonexistent files in allowed directories return proper error.

    Even if the directory is allowed, nonexistent files should error gracefully.
    """
    import tempfile

    nonexistent_path = f"{tempfile.gettempdir()}/definitely_does_not_exist_12345.xml"
    result = get_patient_data(file_path=nonexistent_path)

    assert result.startswith("ERROR:")
    assert "File not found" in result


# --- Pre-parsed JSON Auto-Detection Tests ---


@pytest.fixture
def preparsed_ccda_json() -> str:
    """Pre-parsed JSON matching CCDA schema."""
    return json.dumps(
        {
            "demographics": {
                "name": {"given": "Maria", "family": "Garcia"},
                "dob": "1965-03-15",
                "gender": "female",
            },
            "problems": [
                {
                    "description": "Type 2 Diabetes",
                    "code": "44054006",
                    "status": "active",
                }
            ],
            "medications": [{"name": "Metformin 500mg", "status": "active"}],
            "allergies": [],
            "vitals": [],
            "labs": [],
            "immunizations": [],
            "clinical_notes": [],
        }
    )


@pytest.fixture
def preparsed_fhir_json() -> str:
    """Pre-parsed JSON matching FHIR schema (has encounters)."""
    return json.dumps(
        {
            "demographics": {
                "name": {"given": "Robert", "family": "Johnson"},
                "dob": "1952-08-22",
                "gender": "male",
            },
            "problems": [],
            "medications": [],
            "allergies": [],
            "vitals": [],
            "labs": [],
            "immunizations": [],
            "encounters": [{"encounter_type": "Office Visit", "status": "finished"}],
            "procedures": [{"description": "Blood draw", "status": "completed"}],
        }
    )


@pytest.fixture
def generic_json() -> str:
    """Generic JSON that doesn't match any schema."""
    return json.dumps(
        {
            "patient_id": "PAT-001",
            "custom_field": "custom_value",
            "patient_info": {"name": "Sarah Williams"},
        }
    )


def test_get_patient_data_preparsed_ccda(preparsed_ccda_json: str) -> None:
    """Test pre-parsed JSON with CCDA-like structure."""
    result = get_patient_data(preparsed_ccda_json)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    content = json.loads(result)
    assert content["demographics"]["name"]["given"] == "Maria"
    assert len(content["problems"]) == 1
    assert content["problems"][0]["description"] == "Type 2 Diabetes"


def test_get_patient_data_preparsed_fhir(preparsed_fhir_json: str) -> None:
    """Test pre-parsed JSON with FHIR-like structure."""
    result = get_patient_data(preparsed_fhir_json)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    content = json.loads(result)
    assert content["demographics"]["name"]["given"] == "Robert"
    assert len(content["encounters"]) == 1
    assert len(content["procedures"]) == 1


def test_get_patient_data_generic_passthrough(generic_json: str) -> None:
    """Test generic JSON passthrough (no demographics)."""
    result = get_patient_data(generic_json)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    content = json.loads(result)
    assert content["patient_id"] == "PAT-001"
    assert content["custom_field"] == "custom_value"


def test_get_patient_data_fhir_bundle_still_works() -> None:
    """Test that FHIR Bundle detection still works correctly."""
    fhir_bundle = json.dumps(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "name": [{"given": ["Test"], "family": "Patient"}],
                        "birthDate": "1980-01-01",
                        "gender": "male",
                    }
                }
            ],
        }
    )

    result = get_patient_data(fhir_bundle)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    content = json.loads(result)
    assert "demographics" in content
    assert content["demographics"]["name"]["given"] == "Test"


def test_get_patient_data_json_with_demographics_and_resourcetype() -> None:
    """Test JSON with both demographics and resourceType: Bundle goes to FHIR."""
    # This edge case shouldn't happen in practice, but FHIR Bundle takes precedence
    mixed = json.dumps(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "demographics": {"name": {"given": "Should", "family": "Ignore"}},
            "entry": [],
        }
    )

    result = get_patient_data(mixed)

    assert isinstance(result, str)
    assert not result.startswith("ERROR:")

    content = json.loads(result)
    # FHIR parser handles it, demographics from Bundle, not raw dict
    assert "demographics" in content
