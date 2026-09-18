"""Evaluation API schemas."""

from typing import Optional
from pydantic import BaseModel


class AnnotationRequest(BaseModel):
    """Request to save an annotation."""

    sample_id: str
    nudge_index: Optional[int] = None
    annotation: str
    category: str  # "looks_good", "hallucination", "wrong_citation", "missing_fields", "medical_error", "other"


class AnnotationResponse(BaseModel):
    """Response after saving annotation."""

    status: str
    annotation_id: str
    timestamp: str


class AnnotationListResponse(BaseModel):
    """List of annotations."""

    annotations: list[dict]
    total: int
