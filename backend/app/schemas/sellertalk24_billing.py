"""Pydantic request/response schemas for the SellerTalk24 billing API (/billing/*). All money is int paise."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AmountsOut(BaseModel):
    """A price build-up in paise: base, credit, taxable value, GST split and the total charged."""

    base_paise: int
    credit_paise: int = 0
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    gst_paise: int
    total_paise: int


class PlanOut(AmountsOut):
    """A sellable plan with the amounts THIS tenant would pay for it today (no credit applied)."""

    code: str
    name: str
    conversation_limit: int
    billing_period_days: int
    features: dict[str, Any]
    sort_order: int
    is_current: bool = False
    prices_include_gst: bool
    gst_rate_bps: int


class PlanListOut(BaseModel):
    """GET /billing/plans response."""

    plans: list[PlanOut]


class SubscriptionPlanOut(BaseModel):
    """The plan a subscription period belongs to."""

    code: str
    name: str
    features: dict[str, Any]


class SubscriptionOut(BaseModel):
    """One subscription period."""

    id: int
    status: str
    plan: SubscriptionPlanOut
    current_period_start: datetime
    current_period_end: datetime
    conversations_used: int
    conversation_limit: int
    percent_used: float
    days_left: int


class QueuedPeriodOut(BaseModel):
    """A paid renewal waiting to start."""

    id: int
    plan_code: str
    plan_name: str
    current_period_start: datetime
    current_period_end: datetime


class UpgradeOptionOut(BaseModel):
    """A higher plan the tenant can switch to now, with the credit for their unused time."""

    plan_code: str
    plan_name: str
    conversation_limit: int
    amounts: AmountsOut


class EntitlementOut(BaseModel):
    """What the assistant is currently allowed to do for this tenant (drives the dashboard banners)."""

    state: str = Field(description="'active' | 'exempt' | 'grace' | 'expired'")
    grace_ends_at: Optional[datetime] = None
    enforced: bool = Field(description="True when billing enforcement is on (expired => new customers get the fallback)")
    restricted: bool = Field(description="True when the assistant is currently refusing NEW customers")
    over_limit: bool = False


class SubscriptionStateOut(BaseModel):
    """GET /billing/subscription (also returned by /verify and /mock/complete)."""

    status: str = Field(description="'active' or 'none'")
    entitlement: Optional[EntitlementOut] = None
    over_limit: bool = False
    seller_state_code: str = Field(default="", description="GST state code of the seller; same state => CGST+SGST, else IGST")
    subscription: Optional[SubscriptionOut] = None
    queued: list[QueuedPeriodOut] = []
    upgrade_options: list[UpgradeOptionOut] = []


class CheckoutRequest(BaseModel):
    """POST /billing/checkout body."""

    plan_code: str = Field(min_length=1, max_length=64)


class PrefillOut(BaseModel):
    """Checkout.js prefill block."""

    name: str
    email: str
    contact: str = ""


class CheckoutOut(BaseModel):
    """Everything the frontend needs to open Razorpay Checkout.js (plus the price summary)."""

    key_id: str
    razorpay_order_id: str
    amount: int = Field(description="Total to charge, in paise")
    currency: str
    name: str
    description: str
    prefill: PrefillOut
    mock: bool
    payment_order_id: int
    purpose: str
    plan_code: str
    amounts: AmountsOut


class VerifyRequest(BaseModel):
    """POST /billing/verify body — the three values Checkout.js hands back."""

    razorpay_order_id: str = Field(min_length=1, max_length=128)
    razorpay_payment_id: str = Field(min_length=1, max_length=128)
    razorpay_signature: str = Field(min_length=1, max_length=256)


class MockCompleteRequest(BaseModel):
    """POST /billing/mock/complete body (mock mode only)."""

    razorpay_order_id: str = Field(min_length=1, max_length=128)


class PaymentOut(BaseModel):
    """One row of the tenant's payment history (no signatures / raw webhook data)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_code: str
    plan_name: str
    purpose: str
    status: str
    razorpay_order_id: str
    razorpay_payment_id: Optional[str] = None
    currency: str
    amount_paise: int
    base_paise: int
    credit_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    gst_paise: int
    failure_reason: Optional[str] = None
    created_at: datetime
    paid_at: Optional[datetime] = None
    invoice_id: Optional[int] = None
    invoice_number: Optional[str] = None


class PaymentListOut(BaseModel):
    """GET /billing/payments response (newest first)."""

    items: list[PaymentOut]
    total: int
    page: int
    page_size: int


class AlertOut(BaseModel):
    """A dashboard billing alert; `params` has the numbers for client-side translation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    severity: str
    title: str
    message: str
    params: dict[str, Any]
    created_at: datetime
    read_at: Optional[datetime] = None


class AlertListOut(BaseModel):
    """GET /billing/alerts response (newest first)."""

    alerts: list[AlertOut]
    unread: int


class WebhookAck(BaseModel):
    """POST /billing/webhook response; always HTTP 200 once the event is persisted."""

    status: str = Field(description="'ok' | 'duplicate' | 'ignored' | 'error'")


# ── admin ────────────────────────────────────────────────────────────────────────

class AdminSubscriptionOut(BaseModel):
    """One subscription period in the admin list (manual grants have source_payment_id = null)."""

    id: int
    client_id: int
    client_email: str
    business_name: str
    billing_exempt: bool
    plan_code: str
    plan_name: str
    status: str
    current_period_start: datetime
    current_period_end: datetime
    conversations_used: int
    conversation_limit: int
    over_limit: bool
    source_payment_id: Optional[int] = None
    created_at: datetime


class AdminSubscriptionListOut(BaseModel):
    """GET /admin/billing/subscriptions response."""

    items: list[AdminSubscriptionOut]
    total: int
    page: int
    page_size: int


_REASON = AliasChoices("reason", "note")   # "note" was the pre-0065 field name; still accepted


class AdminGrantRequest(BaseModel):
    """POST /admin/billing/subscriptions/grant — record an offline payment as a plan period (client in the body)."""

    model_config = ConfigDict(populate_by_name=True)

    client_id: int
    plan_code: str = Field(min_length=1, max_length=64)
    days: Optional[int] = Field(default=None, ge=1, le=3660, description="Defaults to the plan's period")
    amount_paise: Optional[int] = Field(default=None, ge=100, description="GST-inclusive amount received offline; omit for a ₹0 grant (no invoice)")
    reason: str = Field(min_length=3, max_length=500, validation_alias=_REASON, description="Why / payment reference — kept in the audit log")


class AdminClientGrantRequest(BaseModel):
    """POST /admin/billing/clients/{client_id}/grant — the same grant, with the client taken from the path."""

    model_config = ConfigDict(populate_by_name=True)

    plan_code: str = Field(min_length=1, max_length=64)
    days: Optional[int] = Field(default=None, ge=1, le=3660, description="Defaults to the plan's period")
    amount_paise: Optional[int] = Field(default=None, ge=100, description="GST-inclusive amount received offline; omit for a ₹0 grant (no invoice)")
    reason: str = Field(min_length=3, max_length=500, validation_alias=_REASON)


class AdminExtendRequest(BaseModel):
    """POST /admin/billing/subscriptions/{id}/extend."""

    model_config = ConfigDict(populate_by_name=True)

    days: int = Field(ge=1, le=3660)
    reason: str = Field(min_length=3, max_length=500, validation_alias=_REASON)


class AdminRevokeRequest(BaseModel):
    """POST /admin/billing/subscriptions/{id}/revoke."""

    reason: str = Field(min_length=3, max_length=500)


class AdminExemptRequest(BaseModel):
    """PUT /admin/billing/clients/{id}/billing-exempt."""

    model_config = ConfigDict(populate_by_name=True)

    exempt: bool
    reason: str = Field(min_length=3, max_length=500, validation_alias=_REASON)


class AdminPaymentEventOut(BaseModel):
    """A stored Razorpay webhook event (payload only on the single-event endpoint)."""

    id: int
    razorpay_event_id: str
    event_type: str
    processed: bool
    error: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None
    payload: Optional[dict[str, Any]] = None


class AdminPaymentEventListOut(BaseModel):
    """GET /admin/billing/payment-events response, newest first."""

    items: list[AdminPaymentEventOut]
    total: int
    page: int
    page_size: int


class AdminReprocessOut(BaseModel):
    """Result of reprocessing one payment event."""

    outcome: str = Field(description="'ok' | 'ignored' | 'error'")
    processed: bool
    error: Optional[str] = None


class AdminBackfillOut(BaseModel):
    """Invoices created for paid orders that had none."""

    created: int
    invoice_numbers: list[str]


class JobRunOut(BaseModel):
    """Last run of a billing background job (from billing_job_runs)."""

    last_run_at: Optional[datetime] = None
    status: str = "never"
    detail: dict[str, Any] = {}
    overdue: bool = Field(default=False, description="True when it has not run for over 45 minutes (it should run every 15)")


class BillingHealthOut(BaseModel):
    """GET /health/billing (admin key): is billing doing its job? `status` is 'ok' or 'degraded' (see `problems`)."""

    status: str
    checked_at: datetime
    mode: str = Field(description="'test' or 'live' — what this process would charge in")
    mock: bool = Field(description="True when the mock gateway is active (no real payments possible)")
    last_webhook_received_at: Optional[datetime] = None
    webhook_errors_24h: int = Field(description="payment_events with an error recorded in the last 24 h (incl. benign notes)")
    webhook_errors_actionable_24h: int = Field(description="the same, minus known-benign notes (stale failure, not our order…)")
    amount_mismatches_24h: int
    bad_signatures_10m: int
    stuck_orders: int = Field(description="orders still created/attempted after 30 minutes (last 3 days)")
    stuck_paid_at_razorpay: Optional[int] = Field(default=None, description="of those, how many Razorpay says are PAID — only with ?verify=true")
    reconcile: JobRunOut
    maintenance: JobRunOut
    problems: list[str] = []
