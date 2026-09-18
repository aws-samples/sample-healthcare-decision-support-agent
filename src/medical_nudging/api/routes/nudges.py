"""Nudge generation API routes."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from medical_nudging.api.schemas.nudge import (
    NudgeGenerationRequest,
    NudgeGenerationResponse,
    NudgeItemResponse,
    NudgeMetadataResponse,
    GuidelineCitationResponse,
)
from medical_nudging.api.services.patient_service import get_patient_by_id
from medical_nudging.api.services.nudge_service import NudgeService, trace_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nudges", tags=["nudges"])


@router.post("/generate", response_model=NudgeGenerationResponse)
async def generate_nudges(request: NudgeGenerationRequest) -> NudgeGenerationResponse:
    """Generate clinical nudges for a patient.

    Takes a patient ID and visit context, generates nudges using the
    medical nudging orchestrator, and optionally returns execution trace
    for debugging.
    """
    # Build visit context
    visit_context: dict[str, str | None] = {
        "visit_type": request.visit_context.visit_type,
        "specialty": request.visit_context.specialty,
        "chief_complaint": request.visit_context.chief_complaint,
    }

    patient_data_str: str | None
    if request.visit_context.data_source == "fhir_api":
        # FHIR API mode: no file loading, pass patient_id through visit_context
        fhir_pid = request.visit_context.patient_id or request.patient_id
        if not fhir_pid:
            raise HTTPException(
                status_code=400, detail="patient_id required for fhir_api data source"
            )
        visit_context["data_source"] = "fhir_api"
        visit_context["patient_id"] = fhir_pid
        patient_data_str = None
    else:
        # File-based mode: load patient document from local file index
        patient = get_patient_by_id(request.patient_id)
        if not patient:
            raise HTTPException(status_code=404, detail=f"Patient not found: {request.patient_id}")
        try:
            patient_data_str = Path(patient.file_path).read_text()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load patient file: {str(e)}")

    config = {
        "max_nudges": request.config.max_nudges if request.config else 5,
    }
    if request.config and request.config.tool_limits:
        config["tool_limits"] = request.config.tool_limits.model_dump(exclude_none=True)

    # Generate nudges
    service = NudgeService(specialty=request.visit_context.specialty)

    try:
        nudge_response, trace = service.generate_nudges_with_trace(
            patient_data=patient_data_str,
            visit_context=visit_context,
            config=config,
        )
    except Exception as e:
        logger.exception("Nudge generation failed")
        raise HTTPException(status_code=500, detail=f"Nudge generation failed: {str(e)}")

    # Convert to response
    nudges = [
        NudgeItemResponse(
            title=n.title,
            description=n.description,
            urgency=n.urgency,
            category=n.category,
            nudge_type=n.nudge_type.value if hasattr(n.nudge_type, "value") else str(n.nudge_type),
            action_type=n.action_type,
            rationale=n.rationale,
            grounding=n.grounding,
            guideline_citation=(
                GuidelineCitationResponse(
                    source=n.guideline_citation.source,
                    section=n.guideline_citation.section,
                )
                if n.guideline_citation
                else None
            ),
            icd_codes=n.icd_codes,
            cpt_codes=n.cpt_codes,
        )
        for n in nudge_response.nudges
    ]

    return NudgeGenerationResponse(
        status=nudge_response.status,
        warnings=nudge_response.warnings,
        error=nudge_response.error,
        patient_summary=nudge_response.patient_summary,
        nudges=nudges,
        metadata=NudgeMetadataResponse(
            model_version=nudge_response.metadata.model_version,
            processing_time_ms=nudge_response.metadata.processing_time_ms,
            guidelines_used=nudge_response.metadata.guidelines_used,
        ),
        trace=trace_to_response(trace) if request.config and request.config.capture_trace else None,
    )
