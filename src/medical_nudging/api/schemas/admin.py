"""Admin-related Pydantic schemas."""

from pydantic import BaseModel


class PromptInfo(BaseModel):
    """Information about a prompt file."""

    name: str
    path: str
    category: str = "general"


class PromptListResponse(BaseModel):
    """Response for prompt list endpoint."""

    prompts: list[PromptInfo]


class PromptContentResponse(BaseModel):
    """Response for prompt content endpoint."""

    name: str
    path: str
    content: str


class ConfigResponse(BaseModel):
    """Response for configuration endpoint."""

    specialties: list[str]
    visit_types: list[str]
    model_version: str
    guidelines_path: str
