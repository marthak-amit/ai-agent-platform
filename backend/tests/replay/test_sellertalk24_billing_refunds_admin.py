# ruff: noqa: F811  (fixtures imported from the E2E module are used as test arguments by name)
"""
SellerTalk24 billing — refunds + credit notes, admin actions, test/live separation, reconcile, alerting, /health/billing,
and the purchase-purpose / over_limit rules. Real Postgres, the real app, mock gateway (mode 'test').

Env/helpers are shared with the Step-8 E2E module so both exercise the exact same harness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingAdminLog,
    BillingAlert,
    BillingJobRun,
    BillingOpsEvent,
    ClientSubscription,
    CreditNote,
    Invoice,
    PaymentEvent,
    PaymentOrder,
)
from app.services import email_service
from app.services.billing import entitlement, invoices, ops, reconcile
from app.services.billing.activation import ActivationError, activate_from_payment
from app.services.billing.razorpay_client import BillingGatewayError, MockRazorpayClient
from tests.replay.test_sellertalk24_billing_e2e_mock import (  # noqa: F401  (fixtures are used by name)
    MOCK,
    _enforcement_on,
    api_state,
    buy,
    checkout,
    count_of,
    env,
    new_tenant,
    post_webhook,
    rewind,
    rows,
    subs_of,
    webhook,
)

pytestmark = pytest.mark.asyncio

ADMIN_KEY = "unit-test-admin-key"
ALERT_TO = "ops@sellertalk24.test"


class Recorder:
    """Email provider that remembers what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[email_service.EmailMessage] = []

    async def send(self, message, sender) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[email_service.EmailMessage]:
        return [m for m in self.sent if m.to == address]


@pytest.fixture(autouse=True)
def mailbox(monkeypatch) -> Recorder:
    """Capture every outbound email, pin the admin key, the seller identity and BILLING_ALERT_EMAIL."""
    rec = Recorder()
    monkeypatch.setattr(email_service, "get_email_provider", lambda settings=None: rec)
    base = get_settings()
    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: base.model_copy(update=dict(admin_secret_key=ADMIN_KEY)))
    seller = base.model_copy(update=dict(
        seller_gstin="24AAAAA0000A1Z5", seller_address="Ahmedabad, Gujarat", seller_state_code="24",
        invoice_prefix="ST24", gst_rate_bps=1800,
    ))
    monkeypatch.setattr(invoices, "get_settings", lambda: seller)
    alert = base.model_copy(update=dict(billing_alert_email=ALERT_TO))
    monkeypatch.setattr(ops, "get_settings", lambda: alert)
    return rec


def admin(actor: str | None = None) -> dict:
    """Admin auth headers (+ optional X-Admin-User)."""
    return {"X-Admin-Key": ADMIN_KEY, **({"X-Admin-User": actor} if actor else {})}


async def credit_notes_of(db, order_id: int) -> list[CreditNote]:
    """Credit notes of one payment order, oldest first."""
    return sorted(await rows(db, CreditNote, CreditNote.payment_order_id == order_id), key=lambda c: c.id)


async def bought(env, t, plan="starter_1500"):
    """Buy a plan; return (checkout json, payment order row)."""
    co = await buy(env, t, plan)
    return co, (await rows(env.db, PaymentOrder, PaymentOrder.razorpay_order_id == co["razorpay_order_id"]))[0]


async def refund(env, order, amount: int | None = None, *, refund_id="rfnd_1", event_id="evt_rf_1"):
    """POST a signed refund.processed webhook for *order* (the entity carries *refund_id*)."""
    payload = {"entity": "event", "event": "refund.processed", "payload": {"refund": {"entity": {
        "id": refund_id, "payment_id": order.razorpay_payment_id, "amount": amount if amount is not None else order.amount_paise}}}}
    import json

    body = json.dumps(payload).encode()
    return await env.http.post("/billing/webhook", content=body, headers={
        "X-Razorpay-Signature": MOCK.sign_webhook(body), "X-Razorpay-Event-Id": event_id, "Content-Type": "application/json"})


# =============================================================================================
# 4. REFUNDS
# =============================================================================================

async def test_full_refund_revokes_the_subscription_issues_a_credit_note_and_starts_grace(env, mailbox):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    mailbox.sent.clear()

    r = await refund(env, order)

    assert (r.status_code, r.json()["status"]) == (200, "ok")
    order = (await rows(env.db, PaymentOrder, PaymentOrder.id == order.id))[0]
    (sub,) = await subs_of(env.db, t.id)
    assert order.status == "refunded" and sub.status == "revoked" and sub.revoked_at is not None
    (note,) = await credit_notes_of(env.db, order.id)
    assert note.refund_kind == "full" and note.mode == "test" and note.total_paise == order.amount_paise
    assert note.credit_note_number.startswith("TEST-ST24-CN/") and note.credit_note_number.endswith("/0001")
    assert (note.taxable_paise, note.cgst_paise, note.sgst_paise, note.igst_paise) == (
        order.taxable_paise, order.cgst_paise, order.sgst_paise, order.igst_paise)
    inv = (await rows(env.db, Invoice, Invoice.payment_order_id == order.id))[0]
    assert note.invoice_id == inv.id and note.razorpay_refund_id == "rfnd_1"

    st = await api_state(env, t)                                    # tenant is in grace, counted from the revocation
    assert st["status"] == "none" and st["entitlement"]["state"] == "grace" and st["entitlement"]["restricted"] is False
    grace_ends = datetime.fromisoformat(st["entitlement"]["grace_ends_at"])
    assert abs((grace_ends - (sub.revoked_at + timedelta(days=3))).total_seconds()) < 5
    assert grace_ends < sub.current_period_end                       # NOT end-of-original-period + 3 days

    alerts = (await env.http.get("/billing/alerts", headers=t.h)).json()["alerts"]
    assert [a["kind"] for a in alerts] == ["refund_revoked"] and "refunded" in alerts[0]["message"]
    (mail,) = mailbox.to(t.email)
    assert "refunded" in mail.subject and "plan cancelled" in mail.subject


async def test_the_grace_after_a_refund_runs_out_three_days_after_the_revocation(env):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    await refund(env, order)
    client = (await rows(env.db, Client, Client.id == t.id))[0]
    (sub,) = await subs_of(env.db, t.id)

    ent = await entitlement.get_entitlement(env.db, client, sub.revoked_at + timedelta(days=2, hours=23))
    assert ent.state is entitlement.EntitlementState.GRACE
    ent = await entitlement.get_entitlement(env.db, client, sub.revoked_at + timedelta(days=3, hours=1))
    assert ent.state is entitlement.EntitlementState.EXPIRED and ent.restricted


async def test_partial_refund_issues_a_credit_note_flags_an_admin_and_leaves_the_plan(env, mailbox):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    mailbox.sent.clear()

    r = await refund(env, order, 100_000, refund_id="rfnd_p1", event_id="evt_p1")

    assert r.json()["status"] == "ok"
    order = (await rows(env.db, PaymentOrder, PaymentOrder.id == order.id))[0]
    (sub,) = await subs_of(env.db, t.id)
    assert order.status == "paid" and sub.status == "active" and sub.revoked_at is None
    (note,) = await credit_notes_of(env.db, order.id)
    assert note.refund_kind == "partial" and note.total_paise == 100_000
    assert note.taxable_paise + note.cgst_paise + note.sgst_paise + note.igst_paise == 100_000
    (flag,) = await rows(env.db, BillingAlert, BillingAlert.client_id == t.id, BillingAlert.audience == "admin")
    assert flag.kind == "partial_refund" and flag.params["credit_note"] == note.credit_note_number
    assert (await env.http.get("/billing/alerts", headers=t.h)).json() == {"alerts": [], "unread": 0}   # never shown to the tenant
    assert mailbox.to(t.email) == []                                 # and no customer email for a partial refund
    ev = (await rows(env.db, PaymentEvent, PaymentEvent.razorpay_event_id == "evt_p1"))[0]
    assert ev.error is None and ev.processed is True


async def test_partial_refunds_that_add_up_to_the_total_revoke_on_the_last_one(env):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    first = order.amount_paise // 2

    await refund(env, order, first, refund_id="rfnd_a", event_id="evt_a")
    assert (await subs_of(env.db, t.id))[0].status == "active"
    await refund(env, order, order.amount_paise - first, refund_id="rfnd_b", event_id="evt_b")

    notes = await credit_notes_of(env.db, order.id)
    assert [n.refund_kind for n in notes] == ["partial", "full"] and sum(n.total_paise for n in notes) == order.amount_paise
    assert [n.seq for n in notes] == [1, 2]
    assert (await subs_of(env.db, t.id))[0].status == "revoked"
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == order.id))[0].status == "refunded"


async def test_a_redelivered_refund_is_idempotent_whether_or_not_the_event_id_changes(env, mailbox):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    mailbox.sent.clear()

    first = await refund(env, order, refund_id="rfnd_dup", event_id="evt_dup_1")
    same_event = await refund(env, order, refund_id="rfnd_dup", event_id="evt_dup_1")
    new_event = await refund(env, order, refund_id="rfnd_dup", event_id="evt_dup_2")      # Razorpay re-sent under a new event id

    assert [r.json()["status"] for r in (first, same_event, new_event)] == ["ok", "duplicate", "ok"]
    assert len(await credit_notes_of(env.db, order.id)) == 1
    assert await count_of(env.db, CreditNote) == 1
    assert len(await rows(env.db, BillingAlert, BillingAlert.client_id == t.id, BillingAlert.kind == "refund_revoked")) == 1
    assert len(mailbox.to(t.email)) == 1                                                   # one customer email, ever
    assert (await subs_of(env.db, t.id))[0].status == "revoked"


async def test_a_refund_for_a_payment_that_never_activated_blocks_it_and_issues_no_credit_note(env):
    t = await new_tenant(env)
    co = (await checkout(env, t, "starter_1500")).json()
    creds = MOCK.simulate_payment(co["razorpay_order_id"])
    await env.db.execute(update(PaymentOrder).where(PaymentOrder.razorpay_order_id == co["razorpay_order_id"])
                         .values(razorpay_payment_id=creds["razorpay_payment_id"]))
    await env.db.commit()
    order = (await rows(env.db, PaymentOrder, PaymentOrder.razorpay_order_id == co["razorpay_order_id"]))[0]

    r = await refund(env, order, refund_id="rfnd_early", event_id="evt_early")

    assert r.json()["status"] == "ok"
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == order.id))[0].status == "refunded"
    assert await count_of(env.db, CreditNote) == 0 and await subs_of(env.db, t.id) == []
    late = await env.http.post("/billing/verify", json=creds, headers=t.h)
    assert late.status_code == 400 and late.json()["detail"]["code"] == "order_refunded"


async def test_a_full_refund_of_an_upgrade_revokes_only_the_upgraded_period(env):
    t = await new_tenant(env)
    await bought(env, t, "starter_1500")
    await rewind(env, t.id, days=10)
    _, up = await bought(env, t, "growth_5000")

    await refund(env, up)

    statuses = [s.status for s in await subs_of(env.db, t.id)]
    assert statuses == ["superseded", "revoked"]


async def test_a_refund_never_touches_another_tenant(env):
    a, b = await new_tenant(env), await new_tenant(env)
    _, order_a = await bought(env, a)
    await bought(env, b)

    await refund(env, order_a)

    assert [s.status for s in await subs_of(env.db, a.id)] == ["revoked"]
    assert [s.status for s in await subs_of(env.db, b.id)] == ["active"]
    assert await count_of(env.db, CreditNote, CreditNote.client_id == b.id) == 0


# =============================================================================================
# 4. ADMIN ENDPOINTS
# =============================================================================================

async def audit(env, action: str) -> list[BillingAdminLog]:
    """Audit rows of one action, oldest first."""
    return sorted(await rows(env.db, BillingAdminLog, BillingAdminLog.action == action), key=lambda r: r.id)


async def test_admin_revoke_cuts_the_period_short_and_is_audited_with_actor_and_reason(env):
    a, b = await new_tenant(env), await new_tenant(env)
    await bought(env, a)
    await bought(env, b)
    (sub_a,), (sub_b,) = await subs_of(env.db, a.id), await subs_of(env.db, b.id)

    r = await env.http.post(f"/admin/billing/subscriptions/{sub_a.id}/revoke", headers=admin("amit@sellertalk24.com"),
                            json={"reason": "chargeback received"})

    assert r.status_code == 200 and r.json()["status"] == "revoked"
    (sub_a,), (sub_b,) = await subs_of(env.db, a.id), await subs_of(env.db, b.id)
    assert sub_a.status == "revoked" and sub_a.revoked_at is not None and sub_b.status == "active"   # tenant-scoped
    st = await api_state(env, a)
    assert st["status"] == "none" and st["entitlement"]["state"] == "grace"
    (row,) = await audit(env, "revoke_subscription")
    assert (row.actor, row.reason, row.client_id, row.subscription_id) == ("amit@sellertalk24.com", "chargeback received", a.id, sub_a.id)
    assert row.detail["previous_status"] == "active"


async def test_admin_revoke_rejects_bad_input_and_double_revocation(env):
    t = await new_tenant(env)
    await bought(env, t)
    (sub,) = await subs_of(env.db, t.id)
    url = f"/admin/billing/subscriptions/{sub.id}/revoke"

    assert (await env.http.post(url, json={"reason": "x" * 3})).status_code == 401                       # no admin key
    assert (await env.http.post(url, headers=admin(), json={})).status_code == 422                        # reason is mandatory
    assert (await env.http.post(url, headers=admin(), json={"reason": "no"})).status_code == 422          # ...and meaningful
    assert (await env.http.post("/admin/billing/subscriptions/99999/revoke", headers=admin(), json={"reason": "why not"})).status_code == 404
    ok = await env.http.post(url, headers=admin(), json={"reason": "fraud"})
    again = await env.http.post(url, headers=admin(), json={"reason": "fraud again"})
    assert ok.status_code == 200 and again.status_code == 409 and again.json()["detail"]["code"] == "not_revocable"
    assert len(await audit(env, "revoke_subscription")) == 1                 # failed calls leave no audit row
    assert (await audit(env, "revoke_subscription"))[0].actor == "admin-key"     # no X-Admin-User -> the key itself


async def test_admin_extend_needs_a_reason_is_audited_and_is_tenant_scoped(env):
    a, b = await new_tenant(env), await new_tenant(env)
    await bought(env, a)
    await bought(env, b)
    (sub_a,), (sub_b,) = await subs_of(env.db, a.id), await subs_of(env.db, b.id)
    end_b = sub_b.current_period_end

    r = await env.http.post(f"/admin/billing/subscriptions/{sub_a.id}/extend", headers=admin("priya"),
                            json={"days": 7, "reason": "outage compensation"})
    legacy = await env.http.post(f"/admin/billing/subscriptions/{sub_a.id}/extend", headers=admin("priya"),
                                 json={"days": 1, "note": "legacy field name"})
    missing = await env.http.post(f"/admin/billing/subscriptions/{sub_a.id}/extend", headers=admin(), json={"days": 1})

    assert r.status_code == 200 and legacy.status_code == 200 and missing.status_code == 422
    (sub_a,), (sub_b,) = await subs_of(env.db, a.id), await subs_of(env.db, b.id)
    assert sub_a.current_period_end - sub_a.current_period_start == timedelta(days=38)
    assert sub_b.current_period_end == end_b                                  # the other tenant is untouched
    rows_ = await audit(env, "extend_subscription")
    assert [(x.actor, x.reason, x.client_id) for x in rows_] == [
        ("priya", "outage compensation", a.id), ("priya", "legacy field name", a.id)]


async def test_admin_grant_creates_a_zero_rupee_paid_order_and_no_invoice(env):
    a, b = await new_tenant(env), await new_tenant(env)

    r = await env.http.post(f"/admin/billing/clients/{a.id}/grant", headers=admin("amit"),
                            json={"plan_code": "growth_5000", "days": 45, "reason": "cash received 7 Oct"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["client_id"], body["status"], body["plan_code"]) == (a.id, "active", "growth_5000")
    order = (await rows(env.db, PaymentOrder, PaymentOrder.id == body["source_payment_id"]))[0]
    assert order.status == "paid" and order.amount_paise == 0 and order.razorpay_order_id.startswith("offline_")
    assert order.mode == "test" and order.client_id == a.id
    assert await count_of(env.db, Invoice) == 0                                # ₹0 grant: no invoice
    assert (await subs_of(env.db, b.id)) == []                                 # tenant-scoped
    st = await api_state(env, a)
    assert st["status"] == "active" and st["subscription"]["plan"]["code"] == "growth_5000"
    (row,) = await audit(env, "grant_subscription")
    assert (row.actor, row.reason, row.client_id, row.subscription_id) == ("amit", "cash received 7 Oct", a.id, body["id"])
    assert row.detail["amount_paise"] == 0 and row.detail["invoice"] is None


async def test_admin_grant_with_an_amount_records_the_payment_and_issues_a_tax_invoice(env):
    t = await new_tenant(env)

    r = await env.http.post(f"/admin/billing/clients/{t.id}/grant", headers=admin("amit"),
                            json={"plan_code": "starter_1500", "reason": "bank transfer ref 4471", "amount_paise": 542682})

    assert r.status_code == 201, r.text
    order = (await rows(env.db, PaymentOrder, PaymentOrder.id == r.json()["source_payment_id"]))[0]
    assert (order.amount_paise, order.taxable_paise, order.gst_paise) == (542682, 459900, 82782)
    assert order.cgst_paise + order.sgst_paise + order.igst_paise == 82782
    (inv,) = await rows(env.db, Invoice, Invoice.payment_order_id == order.id)
    assert inv.total_paise == 542682 and inv.invoice_number.startswith("TEST-ST24/") and inv.mode == "test"
    assert (await audit(env, "grant_subscription"))[0].detail["invoice"] == inv.invoice_number
    low = await env.http.post(f"/admin/billing/clients/{t.id}/grant", headers=admin(),
                              json={"plan_code": "starter_1500", "reason": "too small", "amount_paise": 50})
    assert low.status_code == 422


async def test_admin_grant_validates_and_writes_nothing_on_failure(env):
    t = await new_tenant(env)
    url = f"/admin/billing/clients/{t.id}/grant"

    assert (await env.http.post(url, json={"plan_code": "starter_1500", "reason": "cash"})).status_code == 401
    assert (await env.http.post("/admin/billing/clients/99999/grant", headers=admin(), json={"plan_code": "starter_1500", "reason": "cash"})).status_code == 404
    assert (await env.http.post(url, headers=admin(), json={"plan_code": "nope", "reason": "cash"})).status_code == 404
    assert (await env.http.post(url, headers=admin(), json={"plan_code": "starter_1500"})).status_code == 422

    assert await audit(env, "grant_subscription") == [] and await subs_of(env.db, t.id) == []
    assert await count_of(env.db, PaymentOrder) == 0


async def test_a_grant_stacks_after_a_revoked_period_starts_now_and_restores_service(env):
    t = await new_tenant(env)
    await bought(env, t)
    (sub,) = await subs_of(env.db, t.id)
    await env.http.post(f"/admin/billing/subscriptions/{sub.id}/revoke", headers=admin(), json={"reason": "test revoke"})

    r = await env.http.post(f"/admin/billing/clients/{t.id}/grant", headers=admin(), json={"plan_code": "starter_1500", "reason": "goodwill"})

    assert r.json()["status"] == "active"                                      # a revoked period does not push the start out
    assert (await api_state(env, t))["entitlement"]["state"] == "active"


# =============================================================================================
# 2. TEST vs LIVE
# =============================================================================================

class LiveGateway(MockRazorpayClient):
    """A gateway that claims to be live (to prove a live process refuses test orders)."""

    mode = "live"


async def test_checkout_stamps_the_mode_of_the_gateway_on_the_order(env):
    t = await new_tenant(env)
    co = (await checkout(env, t, "starter_1500")).json()
    order = (await rows(env.db, PaymentOrder, PaymentOrder.razorpay_order_id == co["razorpay_order_id"]))[0]
    assert order.mode == "test"                                                # mock gateway => test


async def test_a_live_process_refuses_to_activate_a_test_order_and_changes_nothing(env):
    t = await new_tenant(env)
    co = (await checkout(env, t, "starter_1500")).json()
    paid = MOCK.simulate_payment(co["razorpay_order_id"])

    with pytest.raises(ActivationError) as exc:
        await activate_from_payment(env.db, LiveGateway(), razorpay_order_id=paid["razorpay_order_id"],
                                    razorpay_payment_id=paid["razorpay_payment_id"], source="test")

    assert exc.value.code == "mode_mismatch" and exc.value.http_status == 409 and not exc.value.retryable
    order = (await rows(env.db, PaymentOrder, PaymentOrder.razorpay_order_id == co["razorpay_order_id"]))[0]
    assert order.status == "created" and await subs_of(env.db, t.id) == [] and await count_of(env.db, Invoice) == 0


async def test_a_test_process_refuses_to_activate_a_live_order(env):
    t = await new_tenant(env)
    co = (await checkout(env, t, "starter_1500")).json()
    await env.db.execute(update(PaymentOrder).where(PaymentOrder.razorpay_order_id == co["razorpay_order_id"]).values(mode="live"))
    await env.db.commit()

    r = await env.http.post("/billing/mock/complete", json={"razorpay_order_id": co["razorpay_order_id"]}, headers=t.h)

    assert r.status_code == 409 and r.json()["detail"]["code"] == "mode_mismatch"
    assert await subs_of(env.db, t.id) == []


async def test_a_signed_webhook_for_a_foreign_mode_order_is_recorded_as_an_error_not_activated(env):
    t = await new_tenant(env)
    co = (await checkout(env, t, "starter_1500")).json()
    await env.db.execute(update(PaymentOrder).where(PaymentOrder.razorpay_order_id == co["razorpay_order_id"]).values(mode="live"))
    await env.db.commit()
    paid = MOCK.simulate_payment(co["razorpay_order_id"])

    r = await post_webhook(env, "payment.captured", order_id=co["razorpay_order_id"], payment_id=paid["razorpay_payment_id"],
                           amount=co["amount"], event_id="evt_mode")

    assert (r.status_code, r.json()["status"]) == (200, "error")
    ev = (await rows(env.db, PaymentEvent, PaymentEvent.razorpay_event_id == "evt_mode"))[0]
    assert ev.error.startswith("mode_mismatch") and await subs_of(env.db, t.id) == []


async def test_live_and_test_invoice_series_are_independent_and_live_starts_at_0001(env):
    async def alloc(mode: str) -> str:
        number, _, _ = await invoices.allocate_invoice_number(env.db, datetime(2026, 10, 7, tzinfo=timezone.utc), mode=mode, prefix="ST24")
        await env.db.commit()
        return number

    assert [await alloc("test") for _ in range(3)] == ["TEST-ST24/2026-27/0001", "TEST-ST24/2026-27/0002", "TEST-ST24/2026-27/0003"]
    assert await alloc("live") == "ST24/2026-27/0001"                          # untouched by 3 test invoices
    assert await alloc("live") == "ST24/2026-27/0002"
    assert await alloc("test") == "TEST-ST24/2026-27/0004"


async def test_credit_note_series_follow_the_same_mode_rules(env):
    from app.services.billing import credit_notes

    when = datetime(2026, 10, 7, tzinfo=timezone.utc)
    test_nums = [(await credit_notes.allocate_credit_note_number(env.db, when, mode="test", prefix="ST24"))[0] for _ in range(2)]
    live = (await credit_notes.allocate_credit_note_number(env.db, when, mode="live", prefix="ST24"))[0]
    await env.db.commit()
    assert test_nums == ["TEST-ST24-CN/2026-27/0001", "TEST-ST24-CN/2026-27/0002"] and live == "ST24-CN/2026-27/0001"


# =============================================================================================
# 5. ALERTING, RECONCILE, /health/billing
# =============================================================================================

class ReconGateway(MockRazorpayClient):
    """Mock gateway whose Razorpay-side view of orders can be scripted."""

    def __init__(self) -> None:
        self.paid_orders: dict[str, str] = {}      # razorpay_order_id -> captured payment id
        self.broken: set[str] = set()
        self.amount_delta = 0

    async def fetch_order(self, order_id: str) -> dict:
        if order_id in self.broken:
            raise BillingGatewayError("simulated outage")
        order = await super().fetch_order(order_id)
        if order_id in self.paid_orders:
            order.update(status="paid", amount_paid=order["amount"], amount_due=0)
        return order

    async def fetch_order_payments(self, order_id: str) -> list[dict]:
        if order_id in self.paid_orders:
            return [await self.fetch_payment(self.paid_orders[order_id])]
        return []

    async def fetch_payment(self, payment_id: str) -> dict:
        payment = await super().fetch_payment(payment_id)
        payment["amount"] += self.amount_delta
        return payment


async def stuck_order(env, t, *, minutes_old: int, plan="starter_1500") -> PaymentOrder:
    """A created order whose checkout happened *minutes_old* minutes ago."""
    co = (await checkout(env, t, plan)).json()
    await env.db.execute(update(PaymentOrder).where(PaymentOrder.razorpay_order_id == co["razorpay_order_id"])
                         .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_old)))
    await env.db.commit()
    return (await rows(env.db, PaymentOrder, PaymentOrder.razorpay_order_id == co["razorpay_order_id"]))[0]


async def test_reconcile_activates_an_order_paid_at_razorpay_through_the_normal_activation(env, mailbox):
    t = await new_tenant(env)
    order = await stuck_order(env, t, minutes_old=45)
    gw = ReconGateway()
    gw.paid_orders[order.razorpay_order_id] = MOCK.simulate_payment(order.razorpay_order_id)["razorpay_payment_id"]

    report = await reconcile.reconcile_orders(env.db, gw)

    assert (report.checked, report.activated, report.expired, report.errors) == (1, 1, 0, 0) and report.order_ids == [order.id]
    order = (await rows(env.db, PaymentOrder, PaymentOrder.id == order.id))[0]
    assert order.status == "paid" and order.razorpay_payment_id == gw.paid_orders[order.razorpay_order_id]
    st = await api_state(env, t)
    assert st["status"] == "active" and st["subscription"]["plan"]["code"] == "starter_1500"
    assert await count_of(env.db, Invoice, Invoice.payment_order_id == order.id) == 1
    assert len(mailbox.to(t.email)) == 1                                       # the customer's payment email
    (alert,) = mailbox.to(ALERT_TO)                                             # and the operator hears that a callback path failed
    assert "Reconcile activated" in alert.subject and str(order.id) in alert.subject
    again = await reconcile.reconcile_orders(env.db, gw)
    assert (again.checked, again.activated) == (0, 0)                           # idempotent: it is paid now


async def test_reconcile_leaves_young_and_unpaid_orders_alone_and_expires_day_old_ones(env):
    t = await new_tenant(env)
    young = await stuck_order(env, t, minutes_old=10)
    unpaid = await stuck_order(env, t, minutes_old=90)
    abandoned = await stuck_order(env, t, minutes_old=60 * 25)

    report = await reconcile.reconcile_orders(env.db, ReconGateway())

    assert (report.checked, report.activated, report.expired) == (2, 0, 1)
    status = {o.id: o for o in await rows(env.db, PaymentOrder, PaymentOrder.client_id == t.id)}
    assert status[young.id].status == "created" and status[unpaid.id].status == "created"
    assert status[abandoned.id].status == "failed" and status[abandoned.id].failure_reason.startswith("expired")
    assert await subs_of(env.db, t.id) == []


async def test_reconcile_skips_the_other_mode_isolates_failures_and_dry_run_changes_nothing(env):
    t = await new_tenant(env)
    live_order = await stuck_order(env, t, minutes_old=60)
    await env.db.execute(update(PaymentOrder).where(PaymentOrder.id == live_order.id).values(mode="live"))
    await env.db.commit()
    broken = await stuck_order(env, t, minutes_old=60)
    paid = await stuck_order(env, t, minutes_old=60)
    gw = ReconGateway()
    gw.broken.add(broken.razorpay_order_id)
    gw.paid_orders[paid.razorpay_order_id] = MOCK.simulate_payment(paid.razorpay_order_id)["razorpay_payment_id"]

    dry = await reconcile.reconcile_orders(env.db, gw, dry_run=True)
    assert (dry.activated, dry.skipped_mode, dry.errors) == (1, 1, 1) and dry.order_ids == [paid.id]
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == paid.id))[0].status == "created"      # dry run: untouched

    real = await reconcile.reconcile_orders(env.db, gw)
    assert (real.activated, real.skipped_mode, real.errors) == (1, 1, 1)
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == paid.id))[0].status == "paid"
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == live_order.id))[0].status == "created"


async def test_reconcile_job_runs_under_the_advisory_lock_and_stamps_the_run(env, replay_db_url):
    t = await new_tenant(env)
    order = await stuck_order(env, t, minutes_old=45)
    gw = ReconGateway()
    gw.paid_orders[order.razorpay_order_id] = MOCK.simulate_payment(order.razorpay_order_id)["razorpay_payment_id"]
    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.connect() as other_instance:
        assert await other_instance.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": reconcile.ADVISORY_LOCK_KEY})
        skipped = await reconcile.run_reconcile_job(factory, gw)
        await other_instance.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": reconcile.ADVISORY_LOCK_KEY})
    ran = await reconcile.run_reconcile_job(factory, gw)
    async with engine.connect() as probe:                                      # released afterwards
        assert await probe.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": reconcile.ADVISORY_LOCK_KEY})
    await engine.dispose()

    assert skipped is None and ran.activated == 1
    (stamp,) = await rows(env.db, BillingJobRun, BillingJobRun.job == "reconcile")
    assert stamp.last_status == "ok" and stamp.detail["activated"] == 1 and stamp.last_finished_at is not None


async def test_the_maintenance_job_also_stamps_its_run(env, replay_db_url):
    from app.services.billing import maintenance

    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await maintenance.run_maintenance_job(factory)
    await engine.dispose()
    (stamp,) = await rows(env.db, BillingJobRun, BillingJobRun.job == "maintenance")
    assert stamp.last_status == "ok" and stamp.detail["errors"] == 0


async def test_an_amount_mismatch_alerts_the_operator_once_and_is_counted(env, mailbox, caplog):
    from app.main import app
    from app.routers.billing import get_gateway

    t = await new_tenant(env)
    gw = ReconGateway()
    gw.amount_delta = -100                                                       # Razorpay says ₹1 less than we charged
    app.dependency_overrides[get_gateway] = lambda: gw
    try:
        co = (await checkout(env, t, "starter_1500")).json()
        creds = MOCK.simulate_payment(co["razorpay_order_id"])
        with caplog.at_level("ERROR"):
            first = await env.http.post("/billing/verify", json=creds, headers=t.h)
            second = await env.http.post("/billing/verify", json=creds, headers=t.h)
    finally:
        app.dependency_overrides.pop(get_gateway, None)

    assert first.status_code == 400 and first.json()["detail"]["code"] == "amount_mismatch"
    assert second.status_code == 400
    assert "AMOUNT MISMATCH" in caplog.text and "BILLING ADMIN ALERT" in caplog.text
    (mail,) = mailbox.to(ALERT_TO)                                               # exactly one email for this order
    assert "Amount mismatch" in mail.subject and "NOT activated" in mail.text
    assert await count_of(env.db, BillingOpsEvent, BillingOpsEvent.kind == "amount_mismatch") >= 1
    assert await subs_of(env.db, t.id) == []


async def test_a_burst_of_bad_webhook_signatures_alerts_once_per_window(env, mailbox):
    body, headers = webhook("payment.captured", order_id="order_x", payment_id="pay_x", amount=1, event_id="evt_burst")
    bad = {**headers, "X-Razorpay-Signature": "0" * 64}

    for _ in range(5):                                                           # at the threshold: no alert yet
        assert (await env.http.post("/billing/webhook", content=body, headers=bad)).status_code == 400
    assert mailbox.to(ALERT_TO) == []
    for _ in range(4):                                                           # past it: ONE alert, not one per request
        assert (await env.http.post("/billing/webhook", content=body, headers=bad)).status_code == 400

    (mail,) = mailbox.to(ALERT_TO)
    assert "signature failures" in mail.subject and "RAZORPAY_WEBHOOK_SECRET" in mail.text
    assert await count_of(env.db, BillingOpsEvent, BillingOpsEvent.kind == "webhook_bad_signature") == 9
    assert await count_of(env.db, PaymentEvent) == 0                             # rejected deliveries are never stored as payments


async def test_admin_alert_is_only_logged_when_no_address_is_configured(env, mailbox, monkeypatch, caplog):
    monkeypatch.setattr(ops, "get_settings", lambda: get_settings().model_copy(update=dict(billing_alert_email="")))
    with caplog.at_level("WARNING"):
        sent = await ops.send_admin_alert(env.db, "amount_mismatch", "1", "subject", "body")
    assert sent is False and mailbox.sent == []
    assert "BILLING ADMIN ALERT" in caplog.text and "BILLING_ALERT_EMAIL is not set" in caplog.text


async def test_health_billing_requires_the_admin_key(env):
    assert (await env.http.get("/health/billing")).status_code == 401
    assert (await env.http.get("/health/billing", headers={"X-Admin-Key": "wrong"})).status_code == 401
    t = await new_tenant(env)
    assert (await env.http.get("/health/billing", headers=t.h)).status_code == 401       # a tenant JWT is not an admin key


async def test_health_billing_reports_a_clean_system_as_ok_once_the_jobs_have_run(env, replay_db_url):
    from app.services.billing import maintenance

    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await maintenance.run_maintenance_job(factory)
    await reconcile.run_reconcile_job(factory, ReconGateway())
    await engine.dispose()
    t = await new_tenant(env)
    await buy(env, t, "starter_1500")
    await post_webhook(env, "payment.captured", order_id="order_nobody", payment_id="pay_nobody", amount=1, event_id="evt_h1")

    r = await env.http.get("/health/billing", headers=admin())

    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "ok" and h["problems"] == [] and h["mock"] is True and h["mode"] == "test"
    assert h["last_webhook_received_at"] is not None and h["stuck_orders"] == 0 and h["stuck_paid_at_razorpay"] is None
    assert h["webhook_errors_24h"] == 1 and h["webhook_errors_actionable_24h"] == 0     # "not our order" is benign
    assert (h["amount_mismatches_24h"], h["bad_signatures_10m"]) == (0, 0)
    assert h["reconcile"]["status"] == "ok" and h["reconcile"]["overdue"] is False
    assert h["maintenance"]["status"] == "ok" and h["maintenance"]["detail"]["errors"] == 0


async def test_health_billing_is_degraded_when_jobs_never_ran(env):
    h = (await env.http.get("/health/billing", headers=admin())).json()
    assert h["status"] == "degraded" and h["reconcile"]["status"] == "never" and h["reconcile"]["overdue"] is True
    assert any("reconcile job has not run" in p for p in h["problems"]) and any("maintenance job has not run" in p for p in h["problems"])


async def test_health_billing_flags_real_webhook_errors_mismatches_and_stuck_orders(env, replay_db_url, monkeypatch):
    from app.services.billing import maintenance

    engine = create_async_engine(replay_db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await maintenance.run_maintenance_job(factory)
    await reconcile.run_reconcile_job(factory, ReconGateway())
    await engine.dispose()
    t = await new_tenant(env)
    stuck = await stuck_order(env, t, minutes_old=60)
    await env.db.execute(PaymentEvent.__table__.insert().values(
        razorpay_event_id="evt_bad", event_type="payment.captured", payload={}, processed=True, error="gateway_error: could not confirm"))
    await env.db.execute(BillingOpsEvent.__table__.insert().values(kind="amount_mismatch", detail={}))
    await env.db.commit()

    h = (await env.http.get("/health/billing", headers=admin())).json()

    assert h["status"] == "degraded"
    assert (h["webhook_errors_actionable_24h"], h["amount_mismatches_24h"], h["stuck_orders"]) == (1, 1, 1)
    assert any("webhook event(s) with errors" in p for p in h["problems"]) and any("amount mismatch" in p for p in h["problems"])

    gw = ReconGateway()
    gw.paid_orders[stuck.razorpay_order_id] = MOCK.simulate_payment(stuck.razorpay_order_id)["razorpay_payment_id"]
    monkeypatch.setattr("app.routers.billing_health.get_billing_gateway", lambda settings: gw)
    v = (await env.http.get("/health/billing?verify=true", headers=admin())).json()

    assert v["stuck_paid_at_razorpay"] == 1 and v["stuck_orders"] == 1          # Razorpay says this one is PAID: reconcile will fix it
    assert any("PAID at Razorpay" in p for p in v["problems"])
    assert (await rows(env.db, PaymentOrder, PaymentOrder.id == stuck.id))[0].status == "created"      # health never mutates


# =============================================================================================
# 6. SMALL BEHAVIOUR
# =============================================================================================

async def test_a_purchase_inside_the_grace_window_is_a_renewal_after_it_is_new(env):
    t = await new_tenant(env)
    await buy(env, t, "starter_1500")
    await rewind(env, t.id, days=31)                                             # ended ~1 day ago: inside the 3-day grace
    assert (await api_state(env, t))["entitlement"]["state"] == "grace"

    inside = (await checkout(env, t, "growth_5000")).json()
    await rewind(env, t.id, days=4)                                              # now ended ~5 days ago: grace is over
    after = (await checkout(env, t, "growth_5000")).json()

    assert (inside["purpose"], inside["amounts"]["credit_paise"]) == ("renewal", 0)
    assert (after["purpose"], after["amounts"]["credit_paise"]) == ("new", 0)


async def test_a_never_subscribed_tenant_buys_as_new_even_though_it_is_in_grace(env):
    t = await new_tenant(env)
    assert (await api_state(env, t))["entitlement"]["state"] == "grace"
    assert (await checkout(env, t, "starter_1500")).json()["purpose"] == "new"


async def test_a_purchase_after_a_refund_inside_grace_is_a_renewal_and_activates_normally(env):
    t = await new_tenant(env)
    _, order = await bought(env, t)
    await refund(env, order)

    co = await buy(env, t, "starter_1500")

    assert co["purpose"] == "renewal"
    st = await api_state(env, t)
    assert st["status"] == "active" and st["subscription"]["conversations_used"] == 0
    assert [s.status for s in await subs_of(env.db, t.id)] == ["revoked", "active"]


async def test_over_limit_means_strictly_over_and_the_100_percent_alert_fires_at_the_limit(env):
    from app.services.billing import usage

    t = await new_tenant(env)
    await buy(env, t, "starter_1500")
    (sub,) = await subs_of(env.db, t.id)
    await env.db.execute(update(ClientSubscription).where(ClientSubscription.id == sub.id).values(conversation_limit=3))
    await env.db.commit()

    seen = []
    for i in range(1, 6):
        await usage.track_inbound_conversation(env.db, t.id, "whatsapp", f"c{i}")
        row = (await rows(env.db, ClientSubscription, ClientSubscription.id == sub.id))[0]
        kinds = sorted(a.kind for a in await rows(env.db, BillingAlert, BillingAlert.client_id == t.id))
        seen.append((row.conversations_used, row.over_limit, "usage_100" in kinds))

    assert seen == [(1, False, False), (2, False, False), (3, False, True), (4, True, True), (5, True, True)]


async def test_an_upgrade_that_carries_more_usage_than_the_new_limit_starts_over_limit(env):
    # defensive: activation computes over_limit from the carried usage instead of assuming False
    t = await new_tenant(env)
    await buy(env, t, "starter_1500")
    (sub,) = await subs_of(env.db, t.id)
    await env.db.execute(update(ClientSubscription).where(ClientSubscription.id == sub.id).values(conversations_used=6000))
    await env.db.commit()
    await buy(env, t, "growth_5000")

    new = [s for s in await subs_of(env.db, t.id) if s.status == "active"][0]
    assert (new.conversations_used, new.conversation_limit, new.over_limit) == (6000, 5000, True)
