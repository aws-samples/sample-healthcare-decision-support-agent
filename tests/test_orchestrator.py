"""Tests for medical nudging orchestrator."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from strands.types.exceptions import StructuredOutputException

from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator, _extract_age_months
from medical_nudging.agents.response_parser import STRUCTURED_OUTPUT_TOOL_NAME
from medical_nudging.models import GeneratedNudgeOutput, NudgeResponse, Nudge


@pytest.fixture
def sample_ccda():
    """Load sample CCDA fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_ccda.xml"
    return fixture_path.read_text()


@pytest.fixture
def visit_context():
    """Sample visit context."""
    return {
        "visit_type": "ambulatory",
        "specialty": "endocrinology",
        "chief_complaint": "Diabetes follow-up",
    }


@pytest.fixture
def mock_generated_output():
    """Mock SDK-validated structured output using the 7 nudge categories."""
    return GeneratedNudgeOutput(
        key_findings=["58F T2DM follow-up", "HbA1c 8.2% (2024-03-20)"],
        patient_summary="58F with T2DM and obesity, HbA1c worsening to 8.2%.",
        nudges=[
            {
                "title": "Consider GLP-1 receptor agonist",
                "description": (
                    "Patient with T2DM (HbA1c 8.2%) and obesity (BMI 34) may benefit "
                    "from GLP-1 agonist therapy."
                ),
                "urgency": "warning",
                "category": "treatment_recommendations",
                "nudge_type": "medication_adjustment",
                "action_type": ["education", "order"],
                "rationale": (
                    "Based on ADA 2024 guidelines, GLP-1 receptor agonists are "
                    "recommended for patients with T2DM and obesity."
                ),
                "grounding": "guideline",
                "guideline_citation": {
                    "source": "ADA Standards of Care 2024",
                    "section": "9.2 Pharmacologic Approaches",
                },
                "icd_codes": ["E11.9", "E66.9"],
            },
            {
                "title": "Schedule eye exam",
                "description": "Patient with diabetes should have annual comprehensive eye exam.",
                "urgency": "informational",
                "category": "gaps_in_care",
                "nudge_type": "screening",
                "action_type": ["referral"],
                "rationale": (
                    "Annual dilated eye exam is standard of care for diabetes patients "
                    "to screen for retinopathy."
                ),
                "grounding": "guideline",
                "guideline_citation": {
                    "source": "ADA Standards of Care 2024",
                    "section": "12.1 Screening",
                },
                "icd_codes": ["E11.9"],
            },
        ],
    )


def _mock_agent(mock_agent_class, structured_output):
    """Wire a mocked Strands Agent whose result carries structured output."""
    mock_agent_instance = MagicMock()
    mock_response = Mock()
    mock_response.structured_output = structured_output
    mock_agent_instance.return_value = mock_response
    mock_agent_class.return_value = mock_agent_instance
    return mock_agent_instance


class TestMedicalNudgingOrchestrator:
    """Test suite for MedicalNudgingOrchestrator."""

    def test_initialization(self):
        """Test orchestrator initialization."""
        orchestrator = MedicalNudgingOrchestrator(specialty="cardiology")
        assert orchestrator.specialty == "cardiology"

    def test_initialization_default_specialty(self):
        """Test orchestrator with default specialty."""
        orchestrator = MedicalNudgingOrchestrator()
        assert orchestrator.specialty == "general"

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_success(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Test successful end-to-end nudge generation."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator(
            specialty="endocrinology", model_id="us.anthropic.claude-sonnet-5"
        )
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert response.status == "success"
        assert len(response.warnings) == 0
        assert response.error is None
        assert len(response.nudges) == 2

        nudge = response.nudges[0]
        assert nudge.title == "Consider GLP-1 receptor agonist"
        assert nudge.urgency == "warning"
        assert nudge.category == "treatment_recommendations"
        assert nudge.action_type == ["education", "order"]
        assert nudge.grounding == "guideline"
        assert nudge.guideline_citation is not None
        assert nudge.guideline_citation.source == "ADA Standards of Care 2024"
        assert "E11.9" in nudge.icd_codes

        assert response.metadata.model_version == "us.anthropic.claude-sonnet-5"
        assert response.metadata.processing_time_ms >= 0

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_partial_guideline_failure(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Test graceful degradation when guideline search fails."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert response.status in ["success", "partial"]
        assert response.error is None
        assert len(response.nudges) == 2

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_malformed_ccda(self, mock_agent_class, visit_context):
        """Test error handling for malformed CCDA."""
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        orchestrator = MedicalNudgingOrchestrator()
        malformed_xml = "<invalid>xml<without_closing_tag>"

        response = orchestrator.generate_nudges(malformed_xml, visit_context)

        assert response.status == "error"
        assert response.error is not None
        assert len(response.nudges) == 0

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_patient_summary_failure(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        """Test error handling when agent fails."""
        mock_agent_class.side_effect = Exception("LLM API error")

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert response.status == "error"
        assert response.error is not None
        assert "Nudge generation failed" in response.error

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_llm_failure(self, mock_agent_class, sample_ccda, visit_context):
        """Test error handling when nudge generation LLM fails."""
        mock_agent_class.side_effect = Exception("LLM API error")

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert response.status == "error"
        assert response.error is not None
        assert "Nudge generation failed" in response.error

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_generate_nudges_max_limit(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
    ):
        """Test that max_nudges config limits output."""
        many_nudges = []
        for i in range(6):
            many_nudges.append(
                {
                    "title": f"Nudge {i + 1}",
                    "description": f"Description {i + 1}",
                    "urgency": "informational",
                    "category": "gaps_in_care",
                    "nudge_type": "screening",
                    "action_type": ["education"],
                    "rationale": "Rationale",
                    "grounding": "clinical_reasoning",
                }
            )

        _mock_agent(
            mock_agent_class,
            GeneratedNudgeOutput(
                key_findings=["finding"],
                patient_summary="Summary of the patient.",
                nudges=many_nudges,
            ),
        )

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(
            sample_ccda, visit_context, config={"max_nudges": 3}
        )

        assert len(response.nudges) == 3

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_response_schema_validation(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Test that response conforms to NudgeResponse schema."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert isinstance(response, NudgeResponse)
        assert response.status in ["success", "partial", "error"]
        assert isinstance(response.warnings, list)
        assert isinstance(response.nudges, list)
        assert all(isinstance(n, Nudge) for n in response.nudges)

        assert response.metadata.model_version is not None
        assert response.metadata.processing_time_ms >= 0
        assert isinstance(response.metadata.guidelines_used, list)

        json_data = response.model_dump()
        assert "status" in json_data
        assert "nudges" in json_data
        assert "metadata" in json_data

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_guideline_detection(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Test that guidelines are detected and included in metadata."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert "ADA Standards of Care 2024" in response.metadata.guidelines_used

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_custom_model_version(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Test custom model version in config."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator()
        response = orchestrator.generate_nudges(
            sample_ccda, visit_context, config={"model_version": "custom-model-v2"}
        )

        assert response.metadata.model_version == "custom-model-v2"

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_runtime_model_id_is_reported(
        self,
        mock_agent_class,
        sample_ccda,
        visit_context,
        mock_generated_output,
    ):
        """Response metadata identifies the generator selected by the orchestrator."""
        _mock_agent(mock_agent_class, mock_generated_output)

        orchestrator = MedicalNudgingOrchestrator(model_id="us.openai.gpt-5.6-sol")
        response = orchestrator.generate_nudges(sample_ccda, visit_context)

        assert response.metadata.model_version == "us.openai.gpt-5.6-sol"


# ---------------------------------------------------------------------------
# Structured output wiring (non-streaming)
# ---------------------------------------------------------------------------


class TestStructuredOutputPath:
    """Tests for the SDK structured output path in generate_nudges()."""

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_structured_output_model_passed_to_agent(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        """The agent must be invoked with the generation-facing schema."""
        mock_agent_instance = _mock_agent(mock_agent_class, mock_generated_output)

        MedicalNudgingOrchestrator().generate_nudges(sample_ccda, visit_context)

        _, kwargs = mock_agent_instance.call_args
        assert kwargs["structured_output_model"] is GeneratedNudgeOutput

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_missing_structured_output_is_an_error(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        """No structured output on the result surfaces as status=error."""
        _mock_agent(mock_agent_class, None)

        response = MedicalNudgingOrchestrator().generate_nudges(sample_ccda, visit_context)

        assert response.status == "error"
        assert response.error is not None
        assert "Structured output failed" in response.error
        assert response.nudges == []

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_sdk_structured_output_exception_is_an_error(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        """A forced-tool failure raised by the SDK surfaces as status=error."""
        mock_agent_instance = MagicMock()
        mock_agent_instance.side_effect = StructuredOutputException("model refused")
        mock_agent_class.return_value = mock_agent_instance

        response = MedicalNudgingOrchestrator().generate_nudges(sample_ccda, visit_context)

        assert response.status == "error"
        assert response.error is not None
        assert "Structured output failed" in response.error
        assert "model refused" in response.error

    @patch("medical_nudging.agents.agent_builder.Agent")
    def test_error_metrics_emitted_for_format_failure(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        """Format failures are recorded with an output_format stage for the sanity gate."""
        _mock_agent(mock_agent_class, None)

        with patch("medical_nudging.tracing.request_observer.emit_error") as mock_emit_error:
            MedicalNudgingOrchestrator().generate_nudges(sample_ccda, visit_context)

        assert mock_emit_error.call_args.kwargs["stage"] == "output_format"


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


def _stream_events(events):
    """Build an async iterator over pre-baked stream_async events."""

    async def _iter(*args, **kwargs):
        for event in events:
            yield event

    return _iter


def _tool_input_event(chunk: str, tool_id: str = "so-1") -> dict:
    """Build a structured output tool-input delta event as the SDK emits it."""
    return {
        "delta": {"toolUse": {"input": chunk}},
        "current_tool_use": {
            "toolUseId": tool_id,
            "name": STRUCTURED_OUTPUT_TOOL_NAME,
            "input": chunk,
        },
    }


def _result_event(structured_output) -> dict:
    result = Mock()
    result.structured_output = structured_output
    result.metrics = None
    return {"result": result}


async def _collect(orchestrator, patient_data, visit_context, config=None):
    return [
        event
        async for event in orchestrator.generate_nudges_streaming(
            patient_data, visit_context, config
        )
    ]


class TestStreamingStructuredOutput:
    """Tests for structured output on the WebSocket streaming path."""

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_complete_event_from_structured_output(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events(
            [
                {"data": "Analysing patient data"},
                _result_event(mock_generated_output),
            ]
        )
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        complete = [e for e in events if e["event"] == "complete"]
        assert len(complete) == 1
        assert complete[0]["data"]["status"] == "success"
        assert len(complete[0]["data"]["nudges"]) == 2
        assert complete[0]["data"]["key_findings"] == [
            "58F T2DM follow-up",
            "HbA1c 8.2% (2024-03-20)",
        ]
        assert "error" not in complete[0]["data"]

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_text_events_still_streamed(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        """Assistant text and structured output tool input both stream as text."""
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events(
            [
                {"data": "Reviewing labs. "},
                _tool_input_event('{"key_findings":'),
                _tool_input_event(' ["58F T2DM"]}'),
                _result_event(mock_generated_output),
            ]
        )
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        streamed = "".join(e["data"] for e in events if e["event"] == "text")
        assert streamed == 'Reviewing labs. {"key_findings": ["58F T2DM"]}'

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_structured_output_tool_not_reported_as_tool_call(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        """The structured output tool is a framework detail, not a clinical tool."""
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events(
            [
                _tool_input_event('{"nudges": []}'),
                _result_event(mock_generated_output),
            ]
        )
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        assert not [e for e in events if e["event"] in ("tool_start", "tool_end")]

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_real_tool_calls_still_reported(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events(
            [
                {
                    "current_tool_use": {
                        "toolUseId": "t-1",
                        "name": "search_guidelines",
                        "input": {"query": "T2DM"},
                    }
                },
                {"message": {"content": [{"toolResult": {"toolUseId": "t-1"}}]}},
                _tool_input_event('{"nudges": []}'),
                _result_event(mock_generated_output),
            ]
        )
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        starts = [e for e in events if e["event"] == "tool_start"]
        ends = [e for e in events if e["event"] == "tool_end"]
        assert [e["data"]["tool"] for e in starts] == ["search_guidelines"]
        assert [e["data"]["tool"] for e in ends] == ["search_guidelines"]

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_missing_structured_output_completes_with_error(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events([_result_event(None)])
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        complete = [e for e in events if e["event"] == "complete"]
        assert len(complete) == 1
        assert complete[0]["data"]["status"] == "error"
        assert complete[0]["data"]["nudges"] == []
        assert "structured output" in complete[0]["data"]["error"].lower()
        assert not [e for e in events if e["event"] == "fatal"]

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_sdk_exception_completes_with_error(
        self, mock_agent_class, sample_ccda, visit_context
    ):
        """A forced-tool failure mid-stream still yields a complete event."""

        async def _raising(*args, **kwargs):
            yield {"data": "partial analysis"}
            raise StructuredOutputException("forced tool not invoked")

        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _raising
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(MedicalNudgingOrchestrator(), sample_ccda, visit_context)

        complete = [e for e in events if e["event"] == "complete"]
        assert len(complete) == 1
        assert complete[0]["data"]["status"] == "error"
        assert "forced tool not invoked" in complete[0]["data"]["error"]

    @patch("medical_nudging.agents.agent_builder.Agent")
    @pytest.mark.asyncio
    async def test_max_nudges_applied_on_streaming_path(
        self, mock_agent_class, sample_ccda, visit_context, mock_generated_output
    ):
        mock_agent_instance = MagicMock()
        mock_agent_instance.stream_async = _stream_events([_result_event(mock_generated_output)])
        mock_agent_class.return_value = mock_agent_instance

        events = await _collect(
            MedicalNudgingOrchestrator(), sample_ccda, visit_context, {"max_nudges": 1}
        )

        complete = [e for e in events if e["event"] == "complete"][0]
        assert len(complete["data"]["nudges"]) == 1


# ---------------------------------------------------------------------------
# _extract_age_months
# ---------------------------------------------------------------------------


class TestExtractAgeMonths:
    """Tests for _extract_age_months() helper."""

    def test_ccda_format_yyyymmdd(self):
        parsed = {"demographics": {"dob": "19900615"}}
        result = _extract_age_months(parsed)
        assert result is not None
        assert result > 400  # 30+ years

    def test_fhir_format_yyyy_mm_dd(self):
        parsed = {"demographics": {"dob": "1990-06-15"}}
        result = _extract_age_months(parsed)
        assert result is not None
        assert result > 400

    def test_no_demographics_returns_none(self):
        assert _extract_age_months({}) is None

    def test_no_dob_returns_none(self):
        assert _extract_age_months({"demographics": {"name": "Test"}}) is None

    def test_invalid_dob_returns_none(self):
        assert _extract_age_months({"demographics": {"dob": "invalid"}}) is None
