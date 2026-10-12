"""Pydantic request/response schemas for the admin panel (/admin/auth, /admin/users, /admin/audit-log, /admin/panel/*)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── auth ─────────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    """POST /admin/auth/login. `otp` is required once the operator has enrolled 2FA."""

    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    otp: Optional[str] = Field(default=None, max_length=12)


class AdminMeOut(BaseModel):
    """The signed-in operator (or the legacy-key pseudo-operator)."""

    id: Optional[int] = None
    email: str
    name: str = ""
    role: str
    permissions: list[str]
    totp_enabled: bool = False
    must_change_password: bool = False
    last_login_at: Optional[datetime] = None
    via: str = "jwt"


class AdminTokenOut(BaseModel):
    """Successful login: a bearer token plus the operator profile."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    admin: AdminMeOut


class ChangePasswordRequest(BaseModel):
    """POST /admin/auth/change-password."""

    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class TotpSetupOut(BaseModel):
    """2FA enrolment material: show the QR for `otpauth_uri` (or type `secret`) then confirm with a code."""

    secret: str
    otpauth_uri: str


class TotpConfirmRequest(BaseModel):
    """POST /admin/auth/totp/enable — a current code proves the authenticator is set up."""

    code: str = Field(max_length=12)


class TotpDisableRequest(BaseModel):
    """POST /admin/auth/totp/disable — requires the password AND a current code."""

    password: str = Field(max_length=256)
    code: str = Field(max_length=12)


class OkOut(BaseModel):
    """Generic acknowledgement."""

    ok: bool = True
    message: str = ""


# ── operators ────────────────────────────────────────────────────────────────

class AdminUserOut(BaseModel):
    """One operator row in the admin-management list (never includes credentials)."""

    id: int
    email: str
    name: str
    role: str
    is_active: bool
    totp_enabled: bool
    must_change_password: bool
    locked_until: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminUserCreate(BaseModel):
    """POST /admin/users. If `password` is omitted a one-time temporary password is generated and returned."""

    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=100)
    role: str
    password: Optional[str] = Field(default=None, max_length=256)


class AdminUserCreated(BaseModel):
    """Create/reset response. `temporary_password` is shown exactly once and must be changed at first login."""

    admin: AdminUserOut
    temporary_password: str


class AdminUserUpdate(BaseModel):
    """PATCH /admin/users/{id}: only the provided fields change."""

    name: Optional[str] = Field(default=None, max_length=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None


# ── audit log ────────────────────────────────────────────────────────────────

class AuditLogOut(BaseModel):
    """One admin_audit_log row."""

    id: int
    actor: str
    admin_user_id: Optional[int] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    client_id: Optional[int] = None
    reason: str
    detail: dict[str, Any]
    ip: Optional[str] = None
    success: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogPage(BaseModel):
    """Paginated audit-log listing."""

    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int


# ── overview ─────────────────────────────────────────────────────────────────

class OverviewOut(BaseModel):
    """GET /admin/panel/overview — the landing-page numbers and a signups series."""

    clients: dict[str, int]
    revenue: dict[str, Any]
    messages: dict[str, int]
    llm_30d: dict[str, Any]
    attention: dict[str, int]
    signups_14d: list[dict[str, Any]]
    llm_daily_14d: list[dict[str, Any]]


# ── clients ──────────────────────────────────────────────────────────────────

class ClientRowOut(BaseModel):
    """One row of the client directory."""

    id: int
    email: str
    business_name: str
    phone: Optional[str] = None
    is_active: bool
    billing_exempt: bool
    plan_slug: str
    sub_plan: Optional[str] = None
    sub_status: Optional[str] = None
    sub_period_end: Optional[datetime] = None
    conversations_used: Optional[int] = None
    conversation_limit: Optional[int] = None
    whatsapp_connected: bool
    instagram_connected: bool
    created_at: Optional[datetime] = None


class ClientSearchOut(BaseModel):
    """Paginated client directory."""

    items: list[ClientRowOut]
    total: int
    page: int
    page_size: int


class ClientDetailOut(BaseModel):
    """Everything an operator needs to support one tenant. Contains NO credentials or tokens."""

    profile: dict[str, Any]
    flags: dict[str, Any]
    channels: dict[str, Any]
    billing: dict[str, Any]
    users: list[dict[str, Any]]
    counts: dict[str, int]
    usage: dict[str, Any]
    llm_30d: dict[str, Any]
    recent_audit: list[AuditLogOut]


class ClientFlagsUpdate(BaseModel):
    """
    PATCH /admin/panel/clients/{id}/flags. Only fields present in the request change.

    `router_v2_enabled`: true/false force the LLM router on/off for this client, null = follow the env default.
    """

    router_v2_enabled: Optional[bool] = None
    daily_message_limit: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    bot_auto_resume_minutes: Optional[int] = Field(default=None, ge=1, le=10_080)
    plan_grandfathered: Optional[bool] = None
    reason: str = Field(default="", max_length=1000)


class FlagsChangedOut(BaseModel):
    """PATCH flags response: the fields that actually changed (new values)."""

    changed: dict[str, Any]


class ImpersonateRequest(BaseModel):
    """POST /admin/panel/clients/{id}/impersonate — a reason is mandatory (it is audited)."""

    reason: str = Field(min_length=5, max_length=1000)


class ImpersonateOut(BaseModel):
    """Short-lived tenant token for the client's owner login."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    client_id: int
    email: str


class ConversationRowOut(BaseModel):
    """A conversation in a client's inbox (admin view)."""

    id: int
    phone_number: str
    channel: str
    current_stage: str
    ai_enabled: bool
    bot_pause_source: Optional[str] = None
    customer_name: Optional[str] = None
    is_sandbox: bool
    message_count: int
    last_message_at: Optional[datetime] = None
    created_at: datetime


class ConversationPage(BaseModel):
    """Paginated conversations."""

    items: list[ConversationRowOut]
    total: int
    page: int
    page_size: int


class MessageRowOut(BaseModel):
    """One message in a conversation transcript."""

    id: int
    role: str
    direction: Optional[str] = None
    sender_type: Optional[str] = None
    content: str
    media_type: Optional[str] = None
    created_at: datetime


class OrderRowOut(BaseModel):
    """An order in a client's order list (admin view)."""

    id: int
    order_number: str
    customer_name: str
    customer_phone: str
    product_name: str
    quantity: int
    total_amount: float
    payment_method: str
    payment_status: str
    status: str
    created_at: datetime
    paid_at: Optional[datetime] = None


class OrderPage(BaseModel):
    """Paginated orders."""

    items: list[OrderRowOut]
    total: int
    page: int
    page_size: int


# ── billing plans (sellable plans, billing_plans table) ──────────────────────

class BillingPlanOut(BaseModel):
    """A sellable SellerTalk24 plan with its current subscriber count."""

    id: int
    code: str
    name: str
    conversation_limit: int
    price_paise: int
    currency: str
    billing_period_days: int
    features: dict[str, Any]
    is_active: bool
    sort_order: int
    active_subscribers: int = 0


class BillingPlanUpdate(BaseModel):
    """PUT /admin/panel/billing-plans/{id}. Price/limit edits never change already-paid periods."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    conversation_limit: Optional[int] = Field(default=None, gt=0, le=10_000_000)
    price_paise: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    features: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10_000)
    reason: str = Field(min_length=5, max_length=1000)


# ── system ───────────────────────────────────────────────────────────────────

class SystemHealthOut(BaseModel):
    """GET /admin/panel/system — live platform health for the operator."""

    status: str
    checks: dict[str, str]
    llm: dict[str, Any]
    instagram: dict[str, Any]
    scheduler: list[dict[str, Any]]
    config: dict[str, Any]
    billing: dict[str, Any]
    timestamp: datetime
