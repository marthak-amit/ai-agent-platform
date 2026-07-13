"""
Team management router — Owner-only invite + permission-checklist admin.

Endpoints:
- GET   /team                : list all Users (team members) for the business
- POST  /team/invite         : create a pending invite, return a copyable invite link
- PATCH /team/{user_id}      : update a team member's role/permissions/is_active
- POST  /team/accept-invite  : public — invitee sets their password and signs in

There is no email-sending integration in this project (see CLAUDE.md), so the
invite flow returns a one-time signed link for the Owner to share manually
rather than dispatching an email.
"""

from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.client import Client
from app.models.user import (
    MANAGER_PRESET_PERMISSIONS,
    PERMISSION_KEYS,
    STAFF_PRESET_PERMISSIONS,
    User,
)
from app.routers.auth import TokenResponse, get_current_user, get_owner_client
from app.services import auth_service

router = APIRouter(prefix="/team", tags=["team"])

INVITE_EXPIRE_DAYS = 7
_INVITE_PURPOSE = "invite"
_ROLE_PRESETS = {"manager": MANAGER_PRESET_PERMISSIONS, "staff": STAFF_PRESET_PERMISSIONS}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TeamMemberOut(BaseModel):
    """One row in the Owner's Team settings list."""

    id: int
    email: str
    role: str
    permissions: list[str]
    is_active: bool
    is_pending: bool
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user: User) -> "TeamMemberOut":
        """Build TeamMemberOut, deriving is_pending from a not-yet-set password."""
        return cls(
            id=user.id,
            email=user.email,
            role=user.role,
            permissions=list(user.permissions or []),
            is_active=user.is_active,
            is_pending=user.hashed_password is None,
            created_at=user.created_at.isoformat(),
        )


class InviteRequest(BaseModel):
    """Request body for POST /team/invite."""

    email: str
    role: str  # "manager" or "staff" — picks the initial checklist preset


class InviteResponse(BaseModel):
    """Response for POST /team/invite — a one-time link, not an emailed invite."""

    invite_link: str


class UpdateTeamMemberRequest(BaseModel):
    """Partial update payload for PATCH /team/{user_id}."""

    role: Optional[str] = None
    permissions: Optional[list[str]] = None
    is_active: Optional[bool] = None


class AcceptInviteRequest(BaseModel):
    """Request body for POST /team/accept-invite."""

    token: str
    password: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[TeamMemberOut])
async def list_team(
    current_client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> list[TeamMemberOut]:
    """
    List every User (owner + invited team members) for the business.

    Returns:
        TeamMemberOut rows ordered oldest-first (Owner is always first).
    """
    result = await db.execute(
        select(User).where(User.client_id == current_client.id).order_by(User.created_at)
    )
    return [TeamMemberOut.from_user(u) for u in result.scalars().all()]


@router.post("/invite", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def invite_team_member(
    body: InviteRequest,
    current_client: Annotated[Client, Depends(get_owner_client)],
    db: AsyncSession = Depends(get_db),
) -> InviteResponse:
    """
    Create a pending team-member invite and return a one-time signup link.

    The new User row has no password set until the invitee visits the link
    and calls POST /team/accept-invite. permissions starts at the role's
    preset checklist (Manager/Staff) but remains fully editable afterward.

    Args:
        body: email of the invitee, and role ("manager" or "staff") to pick
            the initial permissions preset.

    Returns:
        InviteResponse with a signed, 7-day link for the Owner to share.

    Raises:
        HTTPException 422: If role is not "manager" or "staff".
        HTTPException 409: If the email is already a Client or User anywhere
            on the platform.
    """
    if body.role not in _ROLE_PRESETS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role must be 'manager' or 'staff'.",
        )

    existing_client = await db.execute(select(Client).where(Client.email == body.email))
    existing_user = await db.execute(select(User).where(User.email == body.email))
    if existing_client.scalar_one_or_none() is not None or existing_user.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered.",
        )

    member = User(
        client_id=current_client.id,
        email=body.email,
        hashed_password=None,
        role=body.role,
        permissions=list(_ROLE_PRESETS[body.role]),
        is_active=True,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)

    invite_token = auth_service.create_access_token(
        {"sub": member.email, "purpose": _INVITE_PURPOSE, "user_id": member.id},
        expires_delta=timedelta(days=INVITE_EXPIRE_DAYS),
    )
    settings = get_settings()
    return InviteResponse(invite_link=f"{settings.frontend_url}/accept-invite?token={invite_token}")


@router.patch("/{user_id}", response_model=TeamMemberOut)
async def update_team_member(
    user_id: int,
    body: UpdateTeamMemberRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> TeamMemberOut:
    """
    Update a team member's role, permissions checklist, or active status.

    Only the Owner may call this. An Owner may not target their own row here
    (closes both self-privilege-escalation and accidental self-lockout) — use
    PATCH /auth/me for the Owner's own profile instead.

    Args:
        user_id: The User row to update (must belong to the caller's business).
        body: Any combination of role ("manager"/"staff"), permissions
            (subset of app.models.user.PERMISSION_KEYS), is_active.

    Returns:
        Updated TeamMemberOut.

    Raises:
        HTTPException 403: If the caller is not the Owner.
        HTTPException 400: If the caller targets their own row.
        HTTPException 404: If no such team member exists for this business.
        HTTPException 422: If role or a permission key is invalid.
    """
    if not current_user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the account Owner can perform this action.",
        )
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use your own account settings to change your own profile.",
        )

    result = await db.execute(
        select(User).where(User.id == user_id, User.client_id == current_user.client_id)
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found.")

    if body.role is not None:
        if body.role not in _ROLE_PRESETS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="role must be 'manager' or 'staff'.",
            )
        member.role = body.role
    if body.permissions is not None:
        invalid_keys = set(body.permissions) - PERMISSION_KEYS
        if invalid_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown permission key(s): {', '.join(sorted(invalid_keys))}.",
            )
        member.permissions = body.permissions
    if body.is_active is not None:
        member.is_active = body.is_active

    await db.commit()
    await db.refresh(member)
    return TeamMemberOut.from_user(member)


@router.post("/accept-invite", response_model=TokenResponse)
async def accept_invite(
    body: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Set a password for a pending invite and sign the invitee straight in.

    Public endpoint — authorization comes entirely from possessing a valid,
    unexpired invite token (issued by POST /team/invite), not from a Bearer
    token.

    Args:
        body: the invite token from the link, and the invitee's chosen password.

    Returns:
        TokenResponse — a normal 30-day access token, same as POST /auth/login.

    Raises:
        HTTPException 400: If the token is invalid, expired, not an invite
            token, already accepted, or the user is inactive.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(body.token, settings.secret_key, algorithms=[auth_service.ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite link.")

    if payload.get("purpose") != _INVITE_PURPOSE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite link.")

    result = await db.execute(
        select(User).where(User.id == payload.get("user_id"), User.email == payload.get("sub"))
    )
    member = result.scalar_one_or_none()
    if member is None or not member.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite link.")
    if member.hashed_password is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This invite has already been accepted.")

    member.hashed_password = auth_service.hash_password(body.password)
    await db.commit()

    token = auth_service.create_access_token({"sub": member.email})
    return TokenResponse(access_token=token)
