"""Nudge-related Pydantic schemas."""

from typing import Literal
from pydantic import BaseModel, Field


class GuidelineSearchLimits(BaseModel):
    """Request-scoped guideline retrieval limits."""

    max_calls: int | None = Field(default=None, ge=0)
    max_results_per_call: int | None = Field(default=None, ge=1)


class FHIRQueryLimits(BaseModel):
    """Request-scoped FHIR exploration limits."""

    max_calls: int | None = Field(default=None, ge=0)
    max_resource_types_per_call: int | None = Field(default=None, ge=1)
    max_results_per_resource: int | None = Field(default=None, ge=1)
    recent_history_days: int | None = Field(default=None, ge=1)
    history_anchor: Literal["wall_clock", "latest_resource"] = "wall_clock"


class ToolLimits(BaseModel):
    """External tool limits shared by a request and its subagents."""

    guideline_search: GuidelineSearchLimits = Field(default_factory=GuidelineSearchLimits)
    fhir_query: FHIRQueryLimits = Field(default_factory=FHIRQueryLimits)


class VisitContext(BaseModel):
    """Visit context for nudge generation."""

    visit_type: str | None = None
    specialty: str = "general"
    chief_complaint: str | None = None
    search_mode: Literal["auto", "knowledge_base", "summaries"] = "auto"
    """Search mode for clinical guidelines:
    - auto: Try knowledge_base first, fall back to summaries if unavailable (default)
    - knowledge_base: Full indexed search via OpenSearch
    - summaries: Local search on pre-generated guideline summaries only
    """
    data_source: Literal["file", "fhir_api"] | None = None
    """Data source: 'file' (or None) = document string, 'fhir_api' = HealthLake queries."""
    patient_id: str | None = None
    """FHIR Patient resource ID (required when data_source='fhir_api')."""


class NudgeConfig(BaseModel):
    """Configuration for nudge generation."""

    max_nudges: int = 5
    capture_trace: bool = False
    tool_limits: ToolLimits | None = None


class NudgeGenerationRequest(BaseModel):
    """Request for nudge generation."""

    patient_id: str
    visit_context: VisitContext
    config: NudgeConfig | None = None


class ToolCallTraceResponse(BaseModel):
    """A tool call in the execution trace."""

    tool_name: str
    tool_use_id: str
    input_params: dict
    output: str | None = None
    duration_ms: int | None
    completed: bool
    success: bool
    error: str | None = None


class TimingResponse(BaseModel):
    """Timing breakdown for execution."""

    total_ms: int
    tool_ms: int
    model_ms: int
    incomplete_tool_calls: int


class ExecutionTraceResponse(BaseModel):
    """Full execution trace for debugging."""

    system_prompt: str
    user_prompt: str
    tool_calls: list[ToolCallTraceResponse]
    messages: list[dict]
    raw_response: str | None = None
    timing: TimingResponse


class GuidelineCitationResponse(BaseModel):
    """A guideline citation."""

    source: str
    section: str | None = None


class NudgeItemResponse(BaseModel):
    """A single nudge item."""

    title: str
    description: str
    urgency: Literal["informational", "warning", "urgent"]
    category: str
    nudge_type: str
    action_type: str
    rationale: str
    grounding: str
    guideline_citation: GuidelineCitationResponse | None = None
    icd_codes: list[str] = []
    cpt_codes: list[str] = []


class NudgeMetadataResponse(BaseModel):
    """Metadata about nudge generation."""

    model_version: str
    processing_time_ms: int
    guidelines_used: list[str]


class NudgeGenerationResponse(BaseModel):
    """Response for nudge generation."""

    status: Literal["success", "partial", "error"]
    warnings: list[str] = []
    error: str | None = None
    patient_summary: str | None = None
    nudges: list[NudgeItemResponse] = []
    metadata: NudgeMetadataResponse
    trace: ExecutionTraceResponse | None = None
