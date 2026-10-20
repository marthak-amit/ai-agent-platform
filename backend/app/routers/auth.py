"""
Authentication router.

Endpoints:
- POST /auth/register  : create a new client account (starts in account_status "setup")
- POST /auth/login     : JSON login — returns 30-day JWT
- GET  /auth/google/config : whether "Continue with Google" is configured, and the OAuth client id
- POST /auth/google    : sign in / sign up with a Google ID token (links to an existing login with the same email)
- POST /auth/logout    : stateless logout (client discards token)
- GET  /auth/me        : current client profile (protected)
- PATCH /auth/me       : update business_name, phone, or system prompt (protected)

Token extraction uses HTTPBearer, which reads the Authorization: Bearer <token>
header on every protected request.
"""

import logging
import secrets
from datetime import date, datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.models.user import User
from app.services import account_lifecycle, auth_service, google_auth_service, plan_cache


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# auto_error=False so we can return a proper 401 instead of FastAPI's default 403
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Public dependency — imported by conversations.py, leads.py, etc.
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency: extract Bearer token and return the authenticated User.

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
        return await auth_service.get_current_user(credentials.credentials, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_client(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)],
    db: AsyncSession = Depends(get_db),
) -> Client:
    """
    FastAPI dependency: extract Bearer token and return the authenticated
    user's Client business record.

    Any active user of the business (owner, manager, or staff) passes this
    dependency — it only checks authentication, not per-permission
    authorization. Use `require_permission` or `get_owner_client` on top of
    this for routes that need to gate by checklist permission or Owner role.

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


async def get_owner_client(
    current_user: Annotated[User, Depends(get_current_user)],
) -> Client:
    """
    FastAPI dependency: like get_current_client, but 403s unless the caller
    is the business Owner.

    This checks `current_user.role` directly rather than the permissions
    checklist, so a corrupted or tampered permissions array can never grant
    access to a hard-locked, Owner-only action (billing, channel
    connect/disconnect, staff management, credential display).

    Raises:
        HTTPException 403: If the authenticated user is not the Owner.
    """
    if not current_user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the account Owner can perform this action.",
        )
    return current_user.client


def require_permission(permission_key: str):
    """
    FastAPI dependency factory: 403s unless the caller is the Owner or holds
    `permission_key` in their permissions checklist.

    Args:
        permission_key: One of app.models.user.PERMISSION_KEYS.

    Returns:
        A dependency callable suitable for `Depends(...)`.
    """

    async def _check(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if not current_user.has_permission(permission_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission_key}.",
            )
        return current_user

    return _check


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    """Request body for client registration. business_name is optional: the quick-start screen asks for it."""

    business_name: str = ""
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


class GoogleConfigOut(BaseModel):
    """GET /auth/google/config: lets the login page render the Google button without a build-time env var."""

    enabled: bool
    client_id: str = ""


class GoogleLoginRequest(BaseModel):
    """Body of POST /auth/google: the `credential` (ID token) returned by the Google Identity Services button."""

    credential: str


class GoogleLoginResponse(TokenResponse):
    """Session token plus what happened: a brand-new account, a link to an existing login, or a plain sign-in."""

    is_new_account: bool = False
    linked_existing: bool = False
    password_login_disabled: bool = False


class CurrentUserOut(BaseModel):
    """The requesting login identity — distinct from the shared business profile."""

    id: int
    email: str
    role: str
    permissions: list[str]
    is_owner: bool

    model_config = {"from_attributes": True}


class ClientOut(BaseModel):
    """Public-facing client profile returned by /auth/me and /auth/register."""

    id: int
    email: str
    business_name: str
    phone: Optional[str]
    gemini_system_prompt: str
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_connected: bool = False
    instagram_connected: bool = False
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
    upi_display_name: Optional[str] = None
    cod_limit: Optional[int] = None
    accepts_upi: bool = True
    accepts_bank_transfer: bool = False
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None  # masked: last 4 digits only
    bank_ifsc: Optional[str] = None
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None  # always "****" in GET responses
    payment_instructions: Optional[str] = None
    delivery_days_min: Optional[int] = 3
    delivery_days_max: Optional[int] = 7
    onboarding_step: int = 0
    onboarding_completed: bool = False
    # Dashboard-first onboarding (migration 0070). Defaults describe a live legacy account.
    account_status: str = "active"
    bot_active: bool = True
    quick_start_completed: bool = True
    go_live_at: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None
    plan_slug: str = "starter"
    business_type: Optional[str] = None
    business_description: Optional[str] = None
    whatsapp_number: Optional[str] = None
    ig_comment_autoreply_enabled: bool = False
    ig_comment_reply_all: bool = False
    ig_comment_triggers: list[str] = []
    ig_comment_reply_text: dict[str, str] = {}
    # Always populated by from_client() below — Optional only because the
    # Client ORM object itself has no such attribute for model_validate to read.
    current_user: Optional[CurrentUserOut] = None

    model_config = {"from_attributes": True}

    @field_validator("account_status", "bot_active", mode="before")
    @classmethod
    def _lifecycle_defaults(cls, v, info):
        """A Client built in memory (not yet flushed) has None for column-defaulted fields: treat it as a live legacy account."""
        if v is None:
            return "active" if info.field_name == "account_status" else True
        return v

    @classmethod
    def from_client(cls, client: "Client", user: "User") -> "ClientOut":
        """Build ClientOut for `user`, masking sensitive payment fields before exposure."""
        obj = cls.model_validate(client, from_attributes=True)
        obj.current_user = CurrentUserOut(
            id=user.id,
            email=user.email,
            role=user.role,
            permissions=list(user.permissions or []),
            is_owner=user.is_owner,
        )
        # Mask bank account number — show only last 4 digits
        if obj.bank_account_number:
            obj.bank_account_number = "****" + obj.bank_account_number[-4:]
        # Never expose Razorpay secret key
        if obj.razorpay_key_secret:
            obj.razorpay_key_secret = "****"
        obj.whatsapp_connected = bool(client.whatsapp_access_token)
        obj.instagram_connected = bool(client.instagram_access_token)
        obj.quick_start_completed = client.quick_start_completed_at is not None
        return obj


class UpdateMeRequest(BaseModel):
    """Partial update payload for PATCH /auth/me."""

    business_name: Optional[str] = None
    phone: Optional[str] = None
    gemini_system_prompt: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
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
    upi_display_name: Optional[str] = None
    cod_limit: Optional[int] = None
    accepts_upi: Optional[bool] = None
    accepts_bank_transfer: Optional[bool] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    payment_instructions: Optional[str] = None
    delivery_days_min: Optional[int] = None
    delivery_days_max: Optional[int] = None
    ig_comment_autoreply_enabled: Optional[bool] = None
    ig_comment_reply_all: Optional[bool] = None
    ig_comment_triggers: Optional[list[str]] = None
    ig_comment_reply_text: Optional[dict[str, str]] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=ClientOut)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> ClientOut:
    """
    Register a new client business, plus its first (Owner) User login.

    Args:
        body: business_name, email, password, phone (optional).

    Returns:
        Created ClientOut profile, with current_user describing the new Owner.

    Raises:
        HTTPException 409: If the email is already registered, as a Client or
            as a User (invited team member) of any other business.
    """
    if await _email_taken(db, body.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered.",
        )

    client, owner = await _create_account(
        db, email=body.email, password_hash=auth_service.hash_password(body.password),
        business_name=body.business_name.strip(), phone=body.phone,
    )
    await db.commit()
    await db.refresh(client)
    await db.refresh(owner)
    return ClientOut.from_client(client, owner)


async def _email_taken(db: AsyncSession, email: str) -> bool:
    """Whether *email* (case-insensitively) already belongs to a Client or a User of any business."""
    lowered = email.strip().lower()
    clash_client = await db.execute(select(Client.id).where(func.lower(Client.email) == lowered).limit(1))
    clash_user = await db.execute(select(User.id).where(func.lower(User.email) == lowered).limit(1))
    return clash_client.scalar_one_or_none() is not None or clash_user.scalar_one_or_none() is not None


async def _create_account(
    db: AsyncSession,
    *,
    email: str,
    password_hash: Optional[str],
    business_name: str = "",
    phone: Optional[str] = None,
    google_sub: Optional[str] = None,
) -> tuple[Client, User]:
    """
    Create a Client (status "setup": bot silent, no trial) and its Owner User; flushes, does not commit.

    A Google sign-up passes password_hash=None and google_sub: the Client row's (legacy, unused for login) password
    column gets an unguessable random hash so nobody can ever sign in with a password they were not given.
    """
    starter_plan = await plan_cache.get_plan(db, "starter")
    client = Client(
        email=email,
        hashed_password=password_hash or auth_service.hash_password(secrets.token_urlsafe(32)),
        business_name=business_name,
        phone=phone,
        plan_slug="starter",
        daily_message_limit=starter_plan["daily_msg_limit"],
        plan_conv_limit_snapshot=starter_plan["conv_limit"],
        plan_price_snapshot=starter_plan["price_inr"],
        plan_image_quota_snapshot=starter_plan["image_quota"],
        plan_image_overage_price_snapshot=starter_plan["image_overage_price"],
        billing_cycle_start=date.today(),
        account_status="setup",
        bot_active=False,
    )
    db.add(client)
    await db.flush()  # populate client.id for the owner User row below
    if business_name:
        await account_lifecycle.ensure_catalogue_slug(db, client)

    owner = User(
        client_id=client.id,
        email=email,
        hashed_password=password_hash,
        role="owner",
        permissions=[],
        is_active=True,
        google_sub=google_sub,
        email_verified_at=datetime.now(timezone.utc) if google_sub else None,
    )
    db.add(owner)
    await db.flush()
    return client, owner


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
        HTTPException 401: If credentials are invalid, or the account is an
            accepted invite whose password has not yet been set.
    """
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None or user.hashed_password is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not auth_service.verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_service.create_access_token({"sub": user.email})
    return TokenResponse(access_token=token)


@router.get("/google/config", response_model=GoogleConfigOut)
async def google_config() -> GoogleConfigOut:
    """Report whether "Continue with Google" is configured and the public OAuth client id to render the button with."""
    return GoogleConfigOut(enabled=google_auth_service.is_enabled(), client_id=get_settings().google_client_id)


@router.post("/google", response_model=GoogleLoginResponse)
async def google_login(
    body: GoogleLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> GoogleLoginResponse:
    """
    Sign in — or sign up — with a Google ID token.

    * Known Google account (`sub`) -> sign in.
    * Same (verified) email as an existing email/password login -> the two are LINKED and the user signs in. If that
      login's email was never verified (plain sign-up form), its password is revoked at the moment of linking, so a
      squatter who registered someone else's address first cannot keep access once the real owner signs in with Google.
    * Otherwise a new account (status "setup") is created; the quick-start screen then asks for the business name.

    Raises:
        HTTPException 503: Google sign-in is not configured on this server.
        HTTPException 401: The credential is invalid, expired, for another app, or its email is unverified.
        HTTPException 403: The login is deactivated.
        HTTPException 409: This email is already linked to a different Google account.
    """
    try:
        identity = await google_auth_service.verify_id_token(body.credential)
    except google_auth_service.GoogleNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except google_auth_service.GoogleAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    is_new = linked = revoked = False
    user = await db.scalar(select(User).where(User.google_sub == identity.sub))
    if user is None:
        user = await db.scalar(select(User).where(func.lower(User.email) == identity.email.lower()))
        if user is not None:
            if user.google_sub and user.google_sub != identity.sub:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This email is already linked to a different Google account.",
                )
            if user.email_verified_at is None and user.hashed_password is not None:
                user.hashed_password = None      # unproven password: the verified Google owner takes over
                revoked = True
            user.google_sub = identity.sub
            user.email_verified_at = user.email_verified_at or datetime.now(timezone.utc)
            linked = True
        else:
            _client, user = await _create_account(db, email=identity.email, password_hash=None, google_sub=identity.sub)
            is_new = True
        await db.commit()
        await db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated.")
    token = auth_service.create_access_token({"sub": user.email})
    return GoogleLoginResponse(
        access_token=token, is_new_account=is_new, linked_existing=linked, password_login_disabled=revoked,
    )


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
    current_user: Annotated[User, Depends(get_current_user)],
) -> ClientOut:
    """
    Return the currently authenticated user's business profile + own identity.

    Returns:
        ClientOut for the decoded JWT subject (sensitive fields masked).
    """
    return ClientOut.from_client(current_user.client, current_user)


# Fields a non-owner may update via PATCH /auth/me, gated by comment_settings.
# Every other field is Owner-only — this endpoint predates per-user accounts
# and still carries business-critical/credential fields (WhatsApp/Razorpay
# tokens, bank details, etc.) that must never be reachable by an invited user.
_COMMENT_SETTINGS_FIELDS = {
    "ig_comment_autoreply_enabled",
    "ig_comment_reply_all",
    "ig_comment_triggers",
    "ig_comment_reply_text",
}


@router.patch("/me", response_model=ClientOut)
async def update_me(
    body: UpdateMeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> ClientOut:
    """
    Partially update the current business profile.

    Only fields present in the request body are updated. The Owner may update
    any field. A non-owner may only submit the IG comment-settings fields,
    and only if they hold the comment_settings permission — every other field
    is hard-locked to the Owner regardless of the permissions checklist.

    Args:
        body: Any combination of business_name, phone, gemini_system_prompt,
            or (non-owner, with comment_settings) the ig_comment_* fields.

    Returns:
        Updated ClientOut.

    Raises:
        HTTPException 403: If a non-owner submits any field outside
            comment_settings, or lacks the comment_settings permission
            entirely while submitting ig_comment_* fields.
    """
    current_client = current_user.client
    submitted_fields = {k for k, v in body.model_dump(exclude_unset=True).items() if v is not None}

    if not current_user.is_owner:
        disallowed_fields = submitted_fields - _COMMENT_SETTINGS_FIELDS
        if disallowed_fields:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Only the Owner can update: {', '.join(sorted(disallowed_fields))}.",
            )
        if submitted_fields and not current_user.has_permission("comment_settings"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing required permission: comment_settings.",
            )

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
    if body.gst_number is not None:
        from app.services.billing.invoices import is_valid_gstin, normalise_gstin

        gstin = normalise_gstin(body.gst_number)
        # Only validate a CHANGED value: the dashboard re-sends the stored one on every profile save, and
        # older free-text entries must not start failing those saves. The GSTIN decides CGST/SGST vs IGST on
        # invoices, so a new value has to be a real, well-formed one.
        if gstin != normalise_gstin(current_client.gst_number) and gstin is not None and not is_valid_gstin(gstin):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid GSTIN. Expected 15 characters, e.g. 24ABCDE1234F1Z5.",
            )
        current_client.gst_number = gstin
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
        from app.services.payment_verification_service import is_valid_upi_id

        upi = body.upi_id.strip()
        if upi and not is_valid_upi_id(upi):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "INVALID_UPI_ID", "message": "UPI ID must look like name@handle."},
            )
        current_client.upi_id = upi or None
        if upi:
            current_client.payment_setup_alert_at = None
    if body.upi_display_name is not None:
        current_client.upi_display_name = body.upi_display_name
    if body.cod_limit is not None:
        current_client.cod_limit = body.cod_limit
    if body.accepts_upi is not None:
        current_client.accepts_upi = body.accepts_upi
    if body.accepts_bank_transfer is not None:
        current_client.accepts_bank_transfer = body.accepts_bank_transfer
    if body.bank_account_name is not None:
        current_client.bank_account_name = body.bank_account_name
    # Only save if not the masked placeholder
    if body.bank_account_number is not None and not body.bank_account_number.startswith("****"):
        current_client.bank_account_number = body.bank_account_number
    if body.bank_ifsc is not None:
        current_client.bank_ifsc = body.bank_ifsc
    if body.razorpay_key_id is not None:
        current_client.razorpay_key_id = body.razorpay_key_id
    # Only save if not the masked placeholder
    if body.razorpay_key_secret is not None and body.razorpay_key_secret != "****":
        current_client.razorpay_key_secret = body.razorpay_key_secret
    if body.payment_instructions is not None:
        current_client.payment_instructions = body.payment_instructions
    if body.delivery_days_min is not None:
        current_client.delivery_days_min = body.delivery_days_min
    if body.delivery_days_max is not None:
        current_client.delivery_days_max = body.delivery_days_max
    if body.ig_comment_autoreply_enabled is not None:
        current_client.ig_comment_autoreply_enabled = body.ig_comment_autoreply_enabled
    if body.ig_comment_reply_all is not None:
        current_client.ig_comment_reply_all = body.ig_comment_reply_all
    if body.ig_comment_triggers is not None:
        current_client.ig_comment_triggers = body.ig_comment_triggers
    if body.ig_comment_reply_text is not None:
        current_client.ig_comment_reply_text = body.ig_comment_reply_text

    await db.commit()
    await db.refresh(current_client)
    return ClientOut.from_client(current_client, current_user)
