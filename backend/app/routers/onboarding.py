"""
Onboarding router.

Endpoints:
- POST /onboarding/setup-agent : configure the AI agent for the authenticated client
- GET  /onboarding/status      : return setup completion percentage and step checklist
"""

import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status  # noqa: F401
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import BUSINESS_TYPES, Client
from app.routers.auth import get_current_client
from app.services import onboarding_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ── schemas ──────────────────────────────────────────────────────────────────

class ProductItem(BaseModel):
    """A single product in the client's catalogue."""

    name: str
    price: Optional[float] = None
    stock: Optional[int] = None


class SetupAgentRequest(BaseModel):
    """Request body for POST /onboarding/setup-agent."""

    business_name: str
    business_type: str
    business_description: str
    products: Optional[list[ProductItem]] = None
    whatsapp_number: Optional[str] = None

    @field_validator("business_type")
    @classmethod
    def validate_business_type(cls, v: str) -> str:
        """Ensure business_type is one of the supported values."""
        if v not in BUSINESS_TYPES:
            raise ValueError(
                f"business_type must be one of: {', '.join(sorted(BUSINESS_TYPES))}"
            )
        return v


class SetupStatusOut(BaseModel):
    """Onboarding completion state."""

    completion_percentage: int
    steps_done: list[str]
    steps_pending: list[str]


class SetupAgentResponse(BaseModel):
    """Response body for POST /onboarding/setup-agent."""

    client_id: int
    api_key: str
    setup_status: SetupStatusOut


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post(
    "/setup-agent",
    response_model=SetupAgentResponse,
    status_code=status.HTTP_200_OK,
)
async def setup_agent(
    body: SetupAgentRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> SetupAgentResponse:
    """
    Configure the AI agent for the authenticated client.

    Generates a tailored Gemini system prompt from the supplied business
    details, persists all onboarding fields, and issues an API key if one
    does not already exist.

    Args:
        body:           Setup payload (business_name, type, description, products, whatsapp_number).
        current_client: JWT-authenticated Client from the dependency.
        db:             Injected async DB session.

    Returns:
        SetupAgentResponse containing client_id, api_key, and setup_status.

    Raises:
        HTTPException 422: If business_type is not a supported value.
    """
    products_dicts: Optional[list[dict[str, Any]]] = (
        [p.model_dump(exclude_none=True) for p in body.products]
        if body.products
        else None
    )

    current_client.business_name = body.business_name
    current_client.business_type = body.business_type
    current_client.business_description = body.business_description
    current_client.products = products_dicts
    if body.whatsapp_number:
        current_client.whatsapp_number = body.whatsapp_number

    current_client.gemini_system_prompt = onboarding_service.generate_system_prompt(
        business_type=body.business_type,
        business_name=body.business_name,
        business_description=body.business_description,
        products=products_dicts,
    )

    if not current_client.api_key:
        current_client.api_key = onboarding_service.generate_api_key()

    await db.commit()
    await db.refresh(current_client)

    return SetupAgentResponse(
        client_id=current_client.id,
        api_key=current_client.api_key,
        setup_status=SetupStatusOut(
            **onboarding_service.get_setup_status(current_client)
        ),
    )


@router.get(
    "/status",
    response_model=SetupStatusOut,
    status_code=status.HTTP_200_OK,
)
async def onboarding_status(
    current_client: Annotated[Client, Depends(get_current_client)],
) -> SetupStatusOut:
    """
    Return the current onboarding completion state for the authenticated client.

    Steps tracked: registered, agent_configured, products_added, whatsapp_connected.

    Returns:
        SetupStatusOut with completion_percentage, steps_done, steps_pending.
    """
    return SetupStatusOut(**onboarding_service.get_setup_status(current_client))


class ProgressRequest(BaseModel):
    """Request body for PATCH /onboarding/progress."""

    step: int


class ProgressResponse(BaseModel):
    """Response for PATCH /onboarding/progress."""

    onboarding_step: int
    onboarding_completed: bool


@router.patch(
    "/progress",
    response_model=ProgressResponse,
    status_code=status.HTTP_200_OK,
)
async def update_progress(
    body: ProgressRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> ProgressResponse:
    """
    Advance the onboarding wizard step for the authenticated client.

    Only advances forward — a lower step value is ignored to prevent regression.
    When step reaches 6, onboarding_completed is set to True.

    Args:
        body:           {step: int} — target step number (1-6).
        current_client: JWT-authenticated Client from the dependency.
        db:             Injected async DB session.

    Returns:
        ProgressResponse with updated onboarding_step and onboarding_completed.
    """
    if body.step < 0 or body.step > 6:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="step must be between 0 and 6",
        )

    # Only advance, never regress
    if body.step > current_client.onboarding_step:
        current_client.onboarding_step = body.step

    if current_client.onboarding_step >= 6:
        current_client.onboarding_completed = True

    await db.commit()
    await db.refresh(current_client)

    return ProgressResponse(
        onboarding_step=current_client.onboarding_step,
        onboarding_completed=current_client.onboarding_completed,
    )
