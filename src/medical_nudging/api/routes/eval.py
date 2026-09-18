"""Evaluation API routes for annotations."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from medical_nudging.evidence_contract.artifact_paths import ensure_output_dir_outside_repo

from medical_nudging.api.schemas.eval import (
    AnnotationRequest,
    AnnotationResponse,
    AnnotationListResponse,
)

router = APIRouter(prefix="/eval", tags=["evaluation"])

TAXONOMY_FILE = Path(
    os.environ.get(
        "NUDGE_ANNOTATIONS_FILE",
        str(Path.home() / ".local/share/medical-nudging/annotations.json"),
    )
)


def _load_taxonomy() -> dict:
    """Load error taxonomy from file."""
    path = ensure_output_dir_outside_repo(TAXONOMY_FILE, argument="NUDGE_ANNOTATIONS_FILE")
    if path.exists():
        return json.loads(path.read_text())
    return {"observations": [], "failure_categories": {}}


def _save_taxonomy(taxonomy: dict) -> None:
    """Save error taxonomy to file."""
    path = ensure_output_dir_outside_repo(TAXONOMY_FILE, argument="NUDGE_ANNOTATIONS_FILE")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(taxonomy, indent=2))


@router.post("/annotations", response_model=AnnotationResponse)
async def save_annotation(request: AnnotationRequest) -> AnnotationResponse:
    """Save a human annotation for a nudge."""
    taxonomy = _load_taxonomy()

    annotation_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).isoformat()

    observation = {
        "id": annotation_id,
        "sample_id": request.sample_id,
        "nudge_index": request.nudge_index,
        "annotation": request.annotation,
        "category": request.category,
        "timestamp": timestamp,
    }

    taxonomy["observations"].append(observation)

    # Update category counts
    if request.category not in taxonomy["failure_categories"]:
        taxonomy["failure_categories"][request.category] = 0
    taxonomy["failure_categories"][request.category] += 1

    _save_taxonomy(taxonomy)

    return AnnotationResponse(
        status="saved",
        annotation_id=annotation_id,
        timestamp=timestamp,
    )


@router.get("/annotations", response_model=AnnotationListResponse)
async def list_annotations(sample_id: str | None = None) -> AnnotationListResponse:
    """List annotations, optionally filtered by sample_id."""
    taxonomy = _load_taxonomy()
    observations = taxonomy.get("observations", [])

    if sample_id:
        observations = [o for o in observations if o.get("sample_id") == sample_id]

    return AnnotationListResponse(
        annotations=observations,
        total=len(observations),
    )


@router.get("/taxonomy")
async def get_taxonomy() -> dict:
    """Get the full error taxonomy."""
    return _load_taxonomy()
