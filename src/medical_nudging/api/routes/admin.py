"""Admin API routes for prompts and configuration."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from medical_nudging.api.schemas.admin import (
    PromptInfo,
    PromptListResponse,
    PromptContentResponse,
    ConfigResponse,
)
from medical_nudging.config import get_guidelines_path

router = APIRouter(prefix="/admin", tags=["admin"])

# Prompts directory
PROMPTS_DIR = Path(__file__).parent.parent.parent.parent.parent / "prompts"


@router.get("/prompts", response_model=PromptListResponse)
async def list_prompts() -> PromptListResponse:
    """List all available prompts."""
    prompts: list[PromptInfo] = []

    # Core prompts
    for prompt_file in PROMPTS_DIR.glob("*.txt"):
        prompts.append(
            PromptInfo(
                name=prompt_file.stem,
                path=str(prompt_file.relative_to(PROMPTS_DIR)),
                category="core",
            )
        )

    # Specialty prompts
    specialties_dir = PROMPTS_DIR / "specialties"
    if specialties_dir.exists():
        for prompt_file in specialties_dir.glob("*.md"):
            prompts.append(
                PromptInfo(
                    name=prompt_file.stem,
                    path=str(prompt_file.relative_to(PROMPTS_DIR)),
                    category="specialty",
                )
            )

    return PromptListResponse(prompts=prompts)


@router.get("/prompts/{prompt_name}", response_model=PromptContentResponse)
async def get_prompt(prompt_name: str) -> PromptContentResponse:
    """Get the content of a specific prompt."""
    # Check core prompts
    core_path = PROMPTS_DIR / f"{prompt_name}.txt"
    if core_path.exists():
        return PromptContentResponse(
            name=prompt_name,
            path=str(core_path.relative_to(PROMPTS_DIR)),
            content=core_path.read_text(),
        )

    # Check specialty prompts
    specialty_path = PROMPTS_DIR / "specialties" / f"{prompt_name}.md"
    if specialty_path.exists():
        return PromptContentResponse(
            name=prompt_name,
            path=str(specialty_path.relative_to(PROMPTS_DIR)),
            content=specialty_path.read_text(),
        )

    raise HTTPException(status_code=404, detail=f"Prompt not found: {prompt_name}")


@router.get("/specialties", response_model=list[str])
async def list_specialties() -> list[str]:
    """List available specialties."""
    specialties = ["general"]

    specialties_dir = PROMPTS_DIR / "specialties"
    if specialties_dir.exists():
        for prompt_file in specialties_dir.glob("*.md"):
            specialty = prompt_file.stem
            if specialty not in specialties:
                specialties.append(specialty)

    return sorted(specialties)


@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    """Get current configuration."""
    specialties = await list_specialties()

    return ConfigResponse(
        specialties=specialties,
        visit_types=["ambulatory", "inpatient"],
        model_version="claude-sonnet-4.5",
        guidelines_path=str(get_guidelines_path()),
    )
