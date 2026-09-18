"""Pydantic models for medical nudging response schema."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# Known nudge types - used for documentation and validation warnings
KNOWN_NUDGE_TYPES = {
    # Gaps in Care
    "sdoh_screening",
    "health_maintenance",
    "screening",
    # Treatment Recommendations
    "incomplete_order",
    "medication_adjustment",
    "intervention_recommendation",
    # Risk Alerts
    "sepsis_risk",
    "readmission_risk",
    "critical_labs",
    "stemi_alert",
    # Follow-up Actions
    "abnormal_results",
    "additional_testing",
    # Community Data Integration
    "community_data_alert",
    "external_ccda",
    # Clinical Monitoring
    "fluid_imbalance",
    "condition_change",
    # Operational Efficiency
    "discharge_readiness",
    "los_variation",
    # General
    "standard_of_care",
    "counseling",
    "vaccination",
    "lifestyle_counseling",
}


class GuidelineCitation(BaseModel):
    """Citation for a clinical guideline."""

    model_config = {"populate_by_name": True}

    source: str = Field(description="Guideline source (e.g., 'ADA Standards of Care 2024')")
    section: str = Field(description="Specific section reference")
    page_number: int | None = Field(
        default=None, alias="page", description="Page number in source PDF"
    )
    relevance_score: float | None = Field(default=None, description="Search relevance score 0-1")


class Nudge(BaseModel):
    """Individual clinical nudge/recommendation."""

    title: str = Field(description="Short title for the nudge")
    description: str = Field(description="Detailed description of the recommendation")
    urgency: Literal["informational", "warning", "urgent"] = Field(
        description="Urgency level of the recommendation"
    )
    category: Literal[
        "gaps_in_care",
        "treatment_recommendations",
        "risk_alerts",
        "follow_up_actions",
        "community_data_integration",
        "clinical_monitoring",
        "operational_efficiency",
    ] = Field(description="Category of the nudge aligned with customer requirements")
    nudge_type: str = Field(
        default="standard_of_care",
        description="Specific nudge type (flexible - accepts known and novel types)",
    )
    action_type: list[
        Literal["order", "referral", "education", "follow_up", "documentation", "assessment"]
    ] = Field(
        description="List of action types required (e.g., ['education', 'order'] for vaccination)"
    )
    rationale: str = Field(description="Clinical rationale for the recommendation")
    grounding: Literal["guideline", "clinical_reasoning"] = Field(
        description="Source of the recommendation"
    )
    guideline_citation: GuidelineCitation | None = Field(
        default=None, description="Citation if grounded in guidelines"
    )
    icd_codes: list[str] = Field(
        default_factory=list, description="Relevant ICD-10 codes (LLM-generated, unvalidated)"
    )
    codes_validated: bool = Field(
        default=False,
        description="Whether ICD codes have been validated against official code sets",
    )


class GeneratedNudgeOutput(BaseModel):
    """Clinical analysis output: rapid-scan findings, patient summary, and nudges.

    Generation-facing subset of :class:`NudgeResponse` — only the fields the model
    produces.  The Strands SDK turns this schema into the structured output tool
    the agent must invoke, so the docstring and field descriptions are part of the
    prompt the model sees.
    """

    key_findings: list[str] = Field(
        description="Exactly 3 compact, complementary clinical bullets, most actionable first"
    )
    patient_summary: str = Field(description="Encounter-focused clinical snapshot, 40-70 words")
    nudges: list[Nudge] = Field(
        description="Distinct clinical nudges, highest urgency first, within the configured maximum"
    )


class NudgeMetadata(BaseModel):
    """Metadata about the nudge generation process."""

    model_version: str = Field(description="Model version used")
    processing_time_ms: int = Field(description="Total processing time in milliseconds")
    guidelines_used: list[str] = Field(
        default_factory=list, description="List of guideline sources used"
    )


class NudgeResponse(BaseModel):
    """Complete response from the medical nudging system."""

    status: Literal["success", "partial", "error"] = Field(
        description="Overall status of the operation"
    )
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings")
    error: str | None = Field(default=None, description="Error message if status is error")
    key_findings: list[str] = Field(
        default_factory=list, description="Three rapid-scan clinical bullets"
    )
    patient_summary: str | None = Field(
        default=None, description="40-70 word encounter-focused clinical snapshot"
    )
    nudges: list[Nudge] = Field(default_factory=list, description="List of clinical nudges")
    metadata: NudgeMetadata = Field(description="Processing metadata")


# -----------------------------------------------------------------------------
# Inference tracking dataclasses (used by scripts/run_inference.py)
# -----------------------------------------------------------------------------


@dataclass
class InferenceResult:
    """Result of a single inference run."""

    sample_id: str
    file_path: str
    format: str  # "ccda" or "fhir"
    source: str  # "local" or "agentcore"
    status: str  # "success", "partial", "error"
    latency_ms: int
    nudge_count: int
    categories_used: list[str] = field(default_factory=list)
    nudge_types_used: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    nudges: list[dict] = field(default_factory=list)  # Full nudge data for analysis
    key_findings: list[str] = field(default_factory=list)  # Rapid-scan clinical bullets
    patient_summary: str | None = None  # Patient history summary from LLM
    # AgentCore trace correlation fields
    invocation_start: datetime | None = None
    invocation_end: datetime | None = None
    session_id: str | None = None  # AgentCore runtime session ID for OTEL trace lookup


@dataclass
class InferenceReport:
    """Aggregated inference report across multiple samples."""

    timestamp: str
    mode: str
    total_samples: int
    success_count: int
    partial_count: int
    error_count: int
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    category_distribution: dict[str, int] = field(default_factory=dict)
    nudge_type_distribution: dict[str, int] = field(default_factory=dict)
    error_types: dict[str, int] = field(default_factory=dict)
    results: list[InferenceResult] = field(default_factory=list)
