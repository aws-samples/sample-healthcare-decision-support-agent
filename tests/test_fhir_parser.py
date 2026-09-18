"""Tests for FHIR R4 Bundle parser."""

import json
import pytest
from pathlib import Path

from medical_nudging.parsers.fhir_parser import (
    parse_fhir,
    ParsedFHIR,
    FHIRParseError,
    Encounter,
    Procedure,
)


# Path to sample FHIR data
SAMPLE_FHIR_PATH = Path(__file__).parent.parent / "data" / "syntheticmedicare10k"
SAMPLE_FILE = "Aaron697_Steuber698_489755ab-83c9-4d2f-a6d5-f9ee91f99e57.json"


@pytest.fixture
def sample_fhir_json() -> str:
    """Load a sample FHIR Bundle JSON."""
    sample_path = SAMPLE_FHIR_PATH / SAMPLE_FILE
    if not sample_path.exists():
        pytest.skip(f"Sample FHIR file not found: {sample_path}")
    return sample_path.read_text()


@pytest.fixture
def minimal_fhir_bundle() -> str:
    """Create a minimal valid FHIR Bundle for testing."""
    return json.dumps(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": "test-patient",
                        "name": [{"given": ["John"], "family": "Doe"}],
                        "birthDate": "1980-01-15",
                        "gender": "male",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Condition",
                        "id": "test-condition",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "code": {
                            "coding": [
                                {
                                    "system": "http://snomed.info/sct",
                                    "code": "44054006",
                                    "display": "Diabetes mellitus type 2",
                                }
                            ],
                            "text": "Diabetes mellitus type 2",
                        },
                        "onsetDateTime": "2020-05-01",
                    }
                },
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "test-med",
                        "status": "active",
                        "medicationCodeableConcept": {
                            "coding": [{"display": "Metformin 500mg"}],
                            "text": "Metformin 500mg",
                        },
                        "authoredOn": "2020-05-15",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "test-vital",
                        "status": "final",
                        "category": [{"coding": [{"code": "vital-signs"}]}],
                        "code": {
                            "coding": [{"display": "Blood Pressure"}],
                            "text": "Blood Pressure",
                        },
                        "valueQuantity": {"value": 120, "unit": "mmHg"},
                        "effectiveDateTime": "2023-01-01",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "test-lab",
                        "status": "final",
                        "category": [{"coding": [{"code": "laboratory"}]}],
                        "code": {"coding": [{"display": "HbA1c"}], "text": "HbA1c"},
                        "valueQuantity": {"value": 7.2, "unit": "%"},
                        "effectiveDateTime": "2023-01-01",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Immunization",
                        "id": "test-imm",
                        "status": "completed",
                        "vaccineCode": {
                            "coding": [{"display": "Influenza vaccine"}],
                            "text": "Influenza vaccine",
                        },
                        "occurrenceDateTime": "2023-10-01",
                    }
                },
                {
                    "resource": {
                        "resourceType": "Encounter",
                        "id": "test-enc",
                        "status": "finished",
                        "class": {"code": "AMB"},
                        "type": [{"coding": [{"display": "Office Visit"}], "text": "Office Visit"}],
                        "period": {"start": "2023-01-01T10:00:00", "end": "2023-01-01T10:30:00"},
                        "reasonCode": [{"coding": [{"display": "Annual checkup"}]}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "Procedure",
                        "id": "test-proc",
                        "status": "completed",
                        "code": {
                            "coding": [
                                {
                                    "system": "http://snomed.info/sct",
                                    "code": "123456",
                                    "display": "Blood draw",
                                }
                            ],
                            "text": "Blood draw",
                        },
                        "performedDateTime": "2023-01-01T10:15:00",
                    }
                },
            ],
        }
    )


class TestParseFHIR:
    """Tests for parse_fhir function."""

    def test_parse_minimal_bundle(self, minimal_fhir_bundle: str):
        """Test parsing a minimal FHIR Bundle."""
        result = parse_fhir(minimal_fhir_bundle)

        assert isinstance(result, ParsedFHIR)

        # Check demographics
        assert result.demographics.name.get("given") == "John"
        assert result.demographics.name.get("family") == "Doe"
        assert result.demographics.dob == "1980-01-15"
        assert result.demographics.gender == "male"

        # Check problems
        assert len(result.problems) == 1
        assert result.problems[0].description == "Diabetes mellitus type 2"
        assert result.problems[0].code == "44054006"
        assert result.problems[0].status == "active"

        # Check medications
        assert len(result.medications) == 1
        assert result.medications[0].name == "Metformin 500mg"
        assert result.medications[0].status == "active"

        # Check vitals
        assert len(result.vitals) == 1
        assert result.vitals[0].name == "Blood Pressure"
        assert result.vitals[0].value == "120"
        assert result.vitals[0].unit == "mmHg"

        # Check labs
        assert len(result.labs) == 1
        assert result.labs[0].test == "HbA1c"
        assert result.labs[0].value == "7.2"

        # Check immunizations
        assert len(result.immunizations) == 1
        assert result.immunizations[0].vaccine == "Influenza vaccine"

        # Check encounters
        assert len(result.encounters) == 1
        assert result.encounters[0].encounter_type == "Office Visit"
        assert result.encounters[0].class_code == "AMB"

        # Check procedures
        assert len(result.procedures) == 1
        assert result.procedures[0].description == "Blood draw"
        assert result.procedures[0].code == "123456"

    def test_parse_real_synthea_bundle(self, sample_fhir_json: str):
        """Test parsing a real Synthea FHIR Bundle."""
        result = parse_fhir(sample_fhir_json)

        assert isinstance(result, ParsedFHIR)

        # Verify demographics are extracted
        assert result.demographics.name.get("given") is not None
        assert result.demographics.name.get("family") is not None
        assert result.demographics.dob is not None
        assert result.demographics.gender is not None

        # Verify we got multiple entries (Synthea generates rich data)
        assert len(result.problems) > 0
        assert len(result.medications) > 0
        assert len(result.vitals) > 0
        assert len(result.immunizations) > 0
        assert len(result.encounters) > 0
        assert len(result.procedures) > 0

    def test_parse_invalid_json(self):
        """Test that invalid JSON raises FHIRParseError."""
        with pytest.raises(FHIRParseError, match="Invalid JSON"):
            parse_fhir("not valid json")

    def test_parse_non_bundle(self):
        """Test that non-Bundle resources raise FHIRParseError."""
        patient_only = json.dumps({"resourceType": "Patient", "id": "test"})
        with pytest.raises(FHIRParseError, match="Expected FHIR Bundle"):
            parse_fhir(patient_only)

    def test_parse_empty_bundle(self):
        """Test parsing an empty Bundle."""
        empty_bundle = json.dumps({"resourceType": "Bundle", "type": "transaction", "entry": []})
        result = parse_fhir(empty_bundle)

        assert isinstance(result, ParsedFHIR)
        assert result.demographics.name == {}
        assert len(result.problems) == 0
        assert len(result.medications) == 0

    def test_parse_bundle_missing_entries(self):
        """Test parsing a Bundle with no entries field."""
        no_entries = json.dumps({"resourceType": "Bundle", "type": "transaction"})
        result = parse_fhir(no_entries)

        assert isinstance(result, ParsedFHIR)
        assert len(result.problems) == 0


class TestFHIRModels:
    """Tests for FHIR-specific models."""

    def test_encounter_model(self):
        """Test Encounter model creation."""
        encounter = Encounter(
            encounter_type="Office Visit",
            status="finished",
            start_date="2023-01-01",
            end_date="2023-01-01",
            reason="Annual checkup",
            class_code="AMB",
        )
        assert encounter.encounter_type == "Office Visit"
        assert encounter.class_code == "AMB"

    def test_procedure_model(self):
        """Test Procedure model creation."""
        procedure = Procedure(
            description="Blood draw",
            code="123456",
            code_system="http://snomed.info/sct",
            status="completed",
            performed_date="2023-01-01",
        )
        assert procedure.description == "Blood draw"
        assert procedure.code == "123456"


class TestParseObservations:
    """Tests for observation parsing (vitals vs labs)."""

    def test_vital_signs_category(self, minimal_fhir_bundle: str):
        """Test that vital-signs category observations go to vitals."""
        result = parse_fhir(minimal_fhir_bundle)
        vital_names = [v.name for v in result.vitals]
        assert "Blood Pressure" in vital_names

    def test_laboratory_category(self, minimal_fhir_bundle: str):
        """Test that laboratory category observations go to labs."""
        result = parse_fhir(minimal_fhir_bundle)
        lab_names = [lab.test for lab in result.labs]
        assert "HbA1c" in lab_names


class TestModelDump:
    """Tests for model serialization."""

    def test_parsed_fhir_model_dump(self, minimal_fhir_bundle: str):
        """Test that ParsedFHIR can be serialized to dict."""
        result = parse_fhir(minimal_fhir_bundle)
        dumped = result.model_dump()

        assert isinstance(dumped, dict)
        assert "demographics" in dumped
        assert "problems" in dumped
        assert "encounters" in dumped
        assert "procedures" in dumped

    def test_parsed_fhir_json_serializable(self, minimal_fhir_bundle: str):
        """Test that ParsedFHIR can be serialized to JSON."""
        result = parse_fhir(minimal_fhir_bundle)
        json_str = json.dumps(result.model_dump())
        assert isinstance(json_str, str)
        assert "John" in json_str
