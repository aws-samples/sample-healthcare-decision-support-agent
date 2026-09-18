"""Tests for medical_nudging.agents.response_parser module."""

import time

import pytest
from unittest.mock import Mock

from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

from medical_nudging.agents.response_parser import (
    STRUCTURED_OUTPUT_TOOL_NAME,
    StructuredOutputMissingError,
    build_nudge_response,
    build_streaming_result,
    create_error_response,
    create_streaming_error_result,
    extract_structured_output,
)
from medical_nudging.models import GeneratedNudgeOutput, NudgeResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_NUDGE = {
    "title": "Consider GLP-1 receptor agonist",
    "description": "Patient with T2DM and obesity may benefit from GLP-1 agonist.",
    "urgency": "warning",
    "category": "treatment_recommendations",
    "nudge_type": "medication_adjustment",
    "action_type": ["order"],
    "rationale": "ADA 2024 guidelines recommend GLP-1 RA for T2DM with obesity.",
    "grounding": "guideline",
    "guideline_citation": {
        "source": "ADA Standards of Care 2024",
        "section": "9.2 Pharmacologic Approaches",
    },
    "icd_codes": ["E11.9", "E66.9"],
}

VALID_NUDGE_2 = {
    "title": "Schedule eye exam",
    "description": "Annual comprehensive eye exam recommended.",
    "urgency": "informational",
    "category": "gaps_in_care",
    "nudge_type": "screening",
    "action_type": ["referral"],
    "rationale": "Standard of care for diabetes patients.",
    "grounding": "guideline",
    "guideline_citation": {
        "source": "CDC Adult Immunization Schedule",
        "section": "12.1 Screening",
    },
    "icd_codes": ["E11.9"],
}


def _make_generated(nudges: list[dict] | None = None, **overrides) -> GeneratedNudgeOutput:
    """Build a GeneratedNudgeOutput as the SDK would hand it back."""
    data: dict = {
        "key_findings": ["58F T2DM follow-up", "HbA1c 8.2% (2024-03-20)"],
        "patient_summary": "58F with T2DM and obesity, HbA1c worsening to 8.2%.",
        "nudges": nudges if nudges is not None else [VALID_NUDGE, VALID_NUDGE_2],
    }
    data.update(overrides)
    return GeneratedNudgeOutput(**data)


def _make_agent_result(structured_output) -> Mock:
    """Build a mock Strands AgentResult carrying structured output."""
    result = Mock()
    result.structured_output = structured_output
    return result


# ---------------------------------------------------------------------------
# Tool name contract
# ---------------------------------------------------------------------------


def test_tool_name_matches_sdk_derived_name():
    """The name we filter streaming events on must match what the SDK registers."""
    sdk_name = StructuredOutputTool(GeneratedNudgeOutput).tool_name
    assert STRUCTURED_OUTPUT_TOOL_NAME == sdk_name


# ---------------------------------------------------------------------------
# extract_structured_output tests
# ---------------------------------------------------------------------------


class TestExtractStructuredOutput:
    """Tests for extract_structured_output()."""

    def test_returns_structured_output(self):
        generated = _make_generated()
        assert extract_structured_output(_make_agent_result(generated)) is generated

    def test_raises_when_missing(self):
        with pytest.raises(StructuredOutputMissingError, match="never invoked"):
            extract_structured_output(_make_agent_result(None))

    def test_raises_when_absent_attribute(self):
        with pytest.raises(StructuredOutputMissingError):
            extract_structured_output(object())

    def test_raises_on_wrong_type(self):
        with pytest.raises(StructuredOutputMissingError, match="unexpected structured output"):
            extract_structured_output(_make_agent_result({"nudges": []}))


# ---------------------------------------------------------------------------
# build_nudge_response tests
# ---------------------------------------------------------------------------


class TestBuildNudgeResponse:
    """Tests for build_nudge_response()."""

    def test_success(self):
        result = build_nudge_response(_make_generated(), time.perf_counter(), {})

        assert isinstance(result, NudgeResponse)
        assert result.status == "success"
        assert result.error is None
        assert result.warnings == []
        assert len(result.nudges) == 2
        assert result.nudges[0].title == "Consider GLP-1 receptor agonist"
        assert result.nudges[0].urgency == "warning"
        assert result.nudges[0].guideline_citation is not None

    def test_key_findings_and_summary(self):
        result = build_nudge_response(_make_generated(), time.perf_counter(), {})

        assert result.key_findings == ["58F T2DM follow-up", "HbA1c 8.2% (2024-03-20)"]
        assert result.patient_summary is not None
        assert "HbA1c" in result.patient_summary

    def test_max_nudges_truncation(self):
        generated = _make_generated([VALID_NUDGE, VALID_NUDGE_2, VALID_NUDGE])
        result = build_nudge_response(generated, time.perf_counter(), {"max_nudges": 2})

        assert len(result.nudges) == 2

    def test_default_max_nudges_is_five(self):
        generated = _make_generated([VALID_NUDGE] * 7)
        result = build_nudge_response(generated, time.perf_counter(), {})

        assert len(result.nudges) == 5

    def test_guidelines_used_deduplicated_and_sorted(self):
        result = build_nudge_response(_make_generated(), time.perf_counter(), {})

        assert result.metadata.guidelines_used == [
            "ADA Standards of Care 2024",
            "CDC Adult Immunization Schedule",
        ]

    def test_guidelines_used_excludes_uncited_nudges(self):
        nudge = dict(VALID_NUDGE, grounding="clinical_reasoning", guideline_citation=None)
        result = build_nudge_response(_make_generated([nudge]), time.perf_counter(), {})

        assert result.metadata.guidelines_used == []

    def test_codes_validated_forced_false(self):
        """The model must not be able to assert its ICD codes were validated."""
        nudge = dict(VALID_NUDGE, codes_validated=True)
        result = build_nudge_response(_make_generated([nudge]), time.perf_counter(), {})

        assert result.nudges[0].codes_validated is False

    def test_processing_time_recorded(self):
        start = time.perf_counter() - 0.05
        result = build_nudge_response(_make_generated(), start, {})

        assert result.metadata.processing_time_ms >= 50

    def test_model_version_from_config(self):
        result = build_nudge_response(
            _make_generated(), time.perf_counter(), {"model_version": "custom-v2"}
        )

        assert result.metadata.model_version == "custom-v2"

    def test_model_version_is_unknown_without_runtime_identity(self):
        result = build_nudge_response(_make_generated(), time.perf_counter(), {})

        assert result.metadata.model_version == "unknown"

    def test_empty_nudges_still_succeeds(self):
        result = build_nudge_response(_make_generated([]), time.perf_counter(), {})

        assert result.status == "success"
        assert result.nudges == []


# ---------------------------------------------------------------------------
# build_streaming_result tests
# ---------------------------------------------------------------------------


class TestBuildStreamingResult:
    """Tests for build_streaming_result()."""

    def test_success(self):
        result = build_streaming_result(_make_generated(), {})

        assert result["status"] == "success"
        assert len(result["nudges"]) == 2
        assert result["patient_summary"] is not None
        assert result["key_findings"] == ["58F T2DM follow-up", "HbA1c 8.2% (2024-03-20)"]

    def test_nudges_are_plain_dicts(self):
        result = build_streaming_result(_make_generated(), {})

        assert all(isinstance(nudge, dict) for nudge in result["nudges"])
        assert result["nudges"][0]["title"] == "Consider GLP-1 receptor agonist"

    def test_max_nudges_truncation(self):
        generated = _make_generated([VALID_NUDGE, VALID_NUDGE_2])
        result = build_streaming_result(generated, {"max_nudges": 1})

        assert len(result["nudges"]) == 1

    def test_codes_validated_forced_false(self):
        nudge = dict(VALID_NUDGE, codes_validated=True)
        result = build_streaming_result(_make_generated([nudge]), {})

        assert result["nudges"][0]["codes_validated"] is False


# ---------------------------------------------------------------------------
# create_streaming_error_result tests
# ---------------------------------------------------------------------------


class TestCreateStreamingErrorResult:
    """Tests for create_streaming_error_result()."""

    def test_shape(self):
        result = create_streaming_error_result("model refused")

        assert result["status"] == "error"
        assert result["error"] == "model refused"
        assert result["nudges"] == []
        assert result["key_findings"] == []
        assert result["patient_summary"] is None


# ---------------------------------------------------------------------------
# create_error_response tests
# ---------------------------------------------------------------------------


class TestCreateErrorResponse:
    """Tests for create_error_response()."""

    def test_error_response(self):
        error = ValueError("bad xml")
        result = create_error_response("CCDA parsing", error, time.perf_counter(), {})

        assert result.status == "error"
        assert result.error is not None
        assert "CCDA parsing failed" in result.error
        assert "bad xml" in result.error
        assert result.nudges == []

    def test_processing_time_recorded(self):
        start = time.perf_counter() - 0.05
        result = create_error_response("test", ValueError("x"), start, {})

        assert result.metadata.processing_time_ms >= 50

    def test_model_version_from_config(self):
        result = create_error_response(
            "test", ValueError("x"), time.perf_counter(), {"model_version": "v9"}
        )

        assert result.metadata.model_version == "v9"

    def test_model_version_is_unknown_without_runtime_identity(self):
        result = create_error_response("test", ValueError("x"), time.perf_counter(), {})

        assert result.metadata.model_version == "unknown"
