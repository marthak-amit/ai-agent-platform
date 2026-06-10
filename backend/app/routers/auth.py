"""
Authentication router.

Endpoints:
- POST /auth/register  : create a new client account
- POST /auth/login     : JSON login — returns 30-day JWT
- POST /auth/logout    : stateless logout (client discards token)
- GET  /auth/me        : current client profile (protected)
- PATCH /auth/me       : update business_name, phone, or system prompt (protected)

Token extraction uses HTTPBearer, which reads the Authorization: Bearer <token>
header on every protected request.
"""

import logging
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.services import auth_service


def _generate_slug(business_name: str) -> str:
    """Generate a URL-safe catalogue slug from a business name."""
    slug = business_name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug[:50]

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# auto_error=False so we can return a proper 401 instead of FastAPI's default 403
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Public dependency — imported by conversations.py, leads.py, etc.
# ---------------------------------------------------------------------------

async def get_current_client(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: AsyncSession = Depends(get_db),
) -> Client:
    """
    FastAPI dependency: extract Bearer token and return the authenticated Client.

    Raises:
        HTTPException 401: If the token is missing, invalid, or expired.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await auth_service.get_current_client(credentials.credentials, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    """Request body for client registration."""

    business_name: str
    email: str
    password: str
    phone: Optional[str] = None


class LoginRequest(BaseModel):
    """Request body for JSON login."""

    email: str
    password: str


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    token_type: str = "bearer"


class ClientOut(BaseModel):
    """Public-facing client profile returned by /auth/me and /auth/register."""

    id: int
    email: str
    business_name: str
    phone: Optional[str]
    gemini_system_prompt: str
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_account_id: Optional[str] = None
    gst_number: Optional[str] = None
    business_address: Optional[str] = None
    hsn_code: Optional[str] = None
    briefing_enabled: bool = True
    briefing_time: str = "09:00"
    dashboard_language: str = "en"
    catalogue_slug: Optional[str] = None
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None
    catalogue_tagline: Optional[str] = None
    catalogue_theme_color: str = "#6366F1"
    accepts_cod: bool = False
    upi_id: Optional[str] = None
    onboarding_step: int = 0
    onboarding_completed: bool = False
    plan_slug: str = "starter"
    business_type: Optional[str] = None
    business_description: Optional[str] = None
    whatsapp_number: Optional[str] = None

    model_config = {"from_attributes": True}


class UpdateMeRequest(BaseModel):
    """Partial update payload for PATCH /auth/me."""

    business_name: Optional[str] = None
    phone: Optional[str] = None
    gemini_system_prompt: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_account_id: Optional[str] = None
    gst_number: Optional[str] = None
    business_address: Optional[str] = None
    hsn_code: Optional[str] = None
    briefing_enabled: Optional[bool] = None
    briefing_time: Optional[str] = None
    dashboard_language: Optional[str] = None
    catalogue_slug: Optional[str] = None
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None
    catalogue_tagline: Optional[str] = None
    catalogue_theme_color: Optional[str] = None
    accepts_cod: Optional[bool] = None
    upi_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=ClientOut)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> Client:
    """
    Register a new client account.

    Args:
        body: business_name, email, password, phone (optional).

    Returns:
        Created ClientOut profile.

    Raises:
        HTTPException 409: If the email is already registered.
    """
    result = await db.execute(select(Client).where(Client.email == body.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered.",
        )

    # Generate a unique slug; append a numeric suffix on collisions
    base_slug = _generate_slug(body.business_name)
    slug = base_slug
    suffix = 1
    while True:
        existing_slug = await db.execute(select(Client).where(Client.catalogue_slug == slug))
        if existing_slug.scalar_one_or_none() is None:
            break
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    client = Client(
        email=body.email,
        hashed_password=auth_service.hash_password(body.password),
        business_name=body.business_name,
        phone=body.phone,
        plan_slug="starter",
        daily_message_limit=100,
        catalogue_slug=slug,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return client


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Authenticate and issue a 30-day JWT access token.

    Args:
        body: email and password as JSON.

    Returns:
        TokenResponse with access_token and token_type = "bearer".

    Raises:
        HTTPException 401: If credentials are invalid.
    """
    result = await db.execute(select(Client).where(Client.email == body.email))
    client = result.scalar_one_or_none()

    if client is None or not auth_service.verify_password(body.password, client.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_service.create_access_token({"sub": client.email})
    return TokenResponse(access_token=token)


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    _: Annotated[Client, Depends(get_current_client)],
) -> dict:
    """
    Invalidate the current session.

    JWTs are stateless — the server has no token store. The client must
    delete the token from storage. This endpoint exists so the frontend can
    call a logout URL and have a consistent API surface.

    Returns:
        {"message": "Logged out successfully."}
    """
    return {"message": "Logged out successfully."}


@router.get("/me", response_model=ClientOut)
async def get_me(
    current_client: Annotated[Client, Depends(get_current_client)],
) -> Client:
    """
    Return the currently authenticated client's profile.

    Returns:
        ClientOut for the decoded JWT subject.
    """
    return current_client


@router.patch("/me", response_model=ClientOut)
async def update_me(
    body: UpdateMeRequest,
    current_client: Annotated[Client, Depends(get_current_client)],
    db: AsyncSession = Depends(get_db),
) -> Client:
    """
    Partially update the current client's profile.

    Only fields present in the request body are updated.

    Args:
        body: Any combination of business_name, phone, gemini_system_prompt.

    Returns:
        Updated ClientOut.
    """
    if body.business_name is not None:
        current_client.business_name = body.business_name
    if body.phone is not None:
        current_client.phone = body.phone
    if body.gemini_system_prompt is not None:
        current_client.gemini_system_prompt = body.gemini_system_prompt
    if body.whatsapp_phone_number_id is not None:
        current_client.whatsapp_phone_number_id = body.whatsapp_phone_number_id
    if body.whatsapp_access_token is not None:
        current_client.whatsapp_access_token = body.whatsapp_access_token
    if body.instagram_access_token is not None:
        current_client.instagram_access_token = body.instagram_access_token
    if body.instagram_account_id is not None:
        current_client.instagram_account_id = body.instagram_account_id
    if body.gst_number is not None:
        current_client.gst_number = body.gst_number
    if body.business_address is not None:
        current_client.business_address = body.business_address
    if body.hsn_code is not None:
        current_client.hsn_code = body.hsn_code
    if body.dashboard_language is not None and body.dashboard_language in ("en", "hi", "gu"):
        current_client.dashboard_language = body.dashboard_language
    if body.briefing_enabled is not None:
        current_client.briefing_enabled = body.briefing_enabled
    if body.briefing_time is not None:
        current_client.briefing_time = body.briefing_time
    if body.catalogue_slug is not None:
        # Validate uniqueness before saving
        existing = await db.execute(
            select(Client).where(
                Client.catalogue_slug == body.catalogue_slug,
                Client.id != current_client.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This catalogue slug is already taken.")
        current_client.catalogue_slug = body.catalogue_slug
    if body.logo_url is not None:
        current_client.logo_url = body.logo_url
    if body.banner_url is not None:
        current_client.banner_url = body.banner_url
    if body.catalogue_tagline is not None:
        current_client.catalogue_tagline = body.catalogue_tagline
    if body.catalogue_theme_color is not None:
        current_client.catalogue_theme_color = body.catalogue_theme_color
    if body.accepts_cod is not None:
        current_client.accepts_cod = body.accepts_cod
    if body.upi_id is not None:
        current_client.upi_id = body.upi_id

    await db.commit()
    await db.refresh(current_client)
    return current_client
