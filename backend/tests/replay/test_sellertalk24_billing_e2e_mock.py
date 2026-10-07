"""
SellerTalk24 billing — END-TO-END verification in MOCK mode (Step 8).

Real FastAPI app (every /billing route, real JWT auth, the real WhatsApp /webhook pipeline), real
Postgres (the replay DB, all migrations applied), real HMAC signatures. The ONLY thing that is not
Razorpay-the-company is the gateway: the app's own factory (`get_billing_gateway`) picks the mock because
SELLERTALK24_BILLING_MOCK=true and no keys are set — nothing is injected, so the mode-selection rules are
part of what is tested.

Clock: the app reads time through now_utc(), and Razorpay-side time cannot be faked, so "fast-forward"
means rewinding the subscription period in the DB by N days (what the app sees is identical to the
wall clock moving forward by N days: period_end / grace are all relative to `now`).

    1. LIFECYCLE   new tenant -> plans -> checkout Starter -> mock pay -> active (limit 1500)
                   -> 1,201 conversations (80% alert) -> 1,501 (over_limit, bot STILL replies)
                   -> upgrade to Growth (credit applied, usage carried) -> duplicate webhooks (no double)
                   -> expiry reminders -> expired -> grace (bot served) -> after grace: fixed fallback
                   template, but an in-progress order still completes -> renew -> active again
    2. FAILED PAYMENT   payment.failed webhook -> order failed (reason stored), nothing activated, dup ignored,
                        customer retries the same order and it activates; a late failure never undoes a paid order
    3. TAMPERING   bad /verify signature, bad / missing / re-signed webhook, forged payment, someone else's
                   order, non-owner, live-mode-with-mock refusal
    4. EXTRAS      webhook-before-verify race (one activation), stacked renewal promotion, downgrade refusal
    5. REFUND      full refund via webhook -> subscription revoked, credit note, grace from the revocation, bot
                   served in grace then the fallback; an admin re-grant (₹0 offline order) restores service
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from itertools import count
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import Order
from app.models.sellertalk24_billing import (
    BillingAlert,
    ClientSubscription,
    ConversationUsageLog,
    Invoice,
    PaymentEvent,
    PaymentOrder,
)
from app.models.user import User
from app.services.auth_service import create_access_token
from app.services.billing import entitlement, maintenance, usage
from app.services.billing.razorpay_client import (
    MOCK_KEY_SECRET,
    MockRazorpayClient,
    compute_payment_signature,
)
from app.services.language_templates import get_template
from tests.replay.conftest import seed_client_and_product
from tests.replay.helpers import send_image, send_message, stub_media_and_capture_sends

pytestmark = pytest.mark.asyncio

_n = count(1)
_wamid = count(1)
FALLBACK = get_template("english", "billing_assistant_unavailable")
MOCK = MockRazorpayClient()          # used only to SIGN webhooks the way Razorpay would


def step(msg: str) -> None:
    """Print one verified step (visible with `pytest -s`)."""
    print(f"   [ok] {msg}")


# =============================================================================================
# fixtures + helpers
# =============================================================================================

@pytest.fixture(autouse=True)
def _enforcement_on(monkeypatch):
    """Production-intent settings for the entitlement gate: enforcement ON, 3-day grace."""
    from app.config import get_settings

    pinned = get_settings().model_copy(update=dict(sellertalk24_billing_enforce=True, grace_days=3))
    monkeypatch.setattr(entitlement, "get_settings", lambda: pinned)
    monkeypatch.setattr(maintenance, "get_settings", lambda: pinned)


@pytest_asyncio.fixture
async def env(replay_http, replay_session):
    """
    HTTP client + DB session, with settings pinned to MOCK billing (keys empty, mode=test).
    The gateway dependency is NOT overridden: the real factory must choose the mock itself.
    """
    from app.main import app
    from app.routers import billing as billing_router

    # Key the override on the function the router's Depends() really holds (replay_http monkeypatches
    # app.config.get_settings, so importing it from app.config here could give us the wrong object).
    get_settings = billing_router.get_settings
    pinned = get_settings().model_copy(update=dict(
        sellertalk24_billing_mock=True, razorpay_mode="test", razorpay_key_id="", razorpay_key_secret="",
        razorpay_webhook_secret="", prices_include_gst=False, gst_rate_bps=1800, seller_state_code="24",
    ))
    app.dependency_overrides[get_settings] = lambda: pinned
    yield SimpleNamespace(http=replay_http, db=replay_session, settings=pinned, settings_dep=get_settings)
    app.dependency_overrides.pop(get_settings, None)


async def new_tenant(env, *, staff: bool = False) -> SimpleNamespace:
    """A fresh tenant: client + product + owner user (+ optional staff user). Returns ids and auth headers."""
    n = next(_n)
    client, product = await seed_client_and_product(
        env.db, phone=f"91660{n:05d}", wa_phone_number_id=f"E2E{n}", product_sku=f"E2E{n}",
        product_name="E2E Kurta", price=650.0, payment_method="UPI",
    )
    env.db.add(User(client_id=client.id, email=client.email, role="owner", permissions=[], is_active=True))
    t = SimpleNamespace(
        id=client.id, email=client.email, pnid=f"E2E{n}", product_id=product.id, sku=product.sku,
        product_name=product.name, price=product.price,
        h={"Authorization": f"Bearer {create_access_token({'sub': client.email})}"}, staff_h=None,
    )
    if staff:
        email = f"staff{client.id}@e2e.test"
        env.db.add(User(client_id=client.id, email=email, role="staff", permissions=[], is_active=True))
        t.staff_h = {"Authorization": f"Bearer {create_access_token({'sub': email})}"}
    await env.db.commit()
    return t


async def rows(db, model, *where):
    """Fresh rows of *model* from the DB (bypasses the identity map)."""
    stmt = select(model).execution_options(populate_existing=True)
    for w in where:
        stmt = stmt.where(w)
    return list((await db.execute(stmt)).scalars().all())


async def subs_of(db, client_id) -> list[ClientSubscription]:
    """All of a client's subscription rows, oldest first."""
    return sorted(await rows(db, ClientSubscription, ClientSubscription.client_id == client_id), key=lambda s: s.id)


async def order_row(db, razorpay_order_id: str) -> PaymentOrder:
    """The payment order row for a Razorpay order id."""
    return (await rows(db, PaymentOrder, PaymentOrder.razorpay_order_id == razorpay_order_id))[0]


async def count_of(db, model, *where) -> int:
    """SELECT count(*) FROM model WHERE ..."""
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return int((await db.execute(stmt)).scalar_one())


async def alert_kinds(db, client_id) -> list[str]:
    """Kinds of every billing alert raised for a client."""
    return sorted(a.kind for a in await rows(db, BillingAlert, BillingAlert.client_id == client_id))


async def api_state(env, t) -> dict:
    """GET /billing/subscription as JSON."""
    r = await env.http.get("/billing/subscription", headers=t.h)
    assert r.status_code == 200, r.text
    return r.json()


async def checkout(env, t, plan_code: str):
    """POST /billing/checkout."""
    return await env.http.post("/billing/checkout", json={"plan_code": plan_code}, headers=t.h)


async def buy(env, t, plan_code: str) -> dict:
    """Checkout then mock-pay; returns the checkout JSON (asserting both calls succeeded)."""
    co = await checkout(env, t, plan_code)
    assert co.status_code == 200, co.text
    paid = await env.http.post("/billing/mock/complete", json={"razorpay_order_id": co.json()["razorpay_order_id"]}, headers=t.h)
    assert paid.status_code == 200, paid.text
    return co.json()


async def rewind(env, client_id: int, *, days: float = 0, hours: float = 0) -> None:
    """
    Fast-forward the world for one tenant by rewinding EVERY subscription row (start AND end) by that much,
    superseded/expired ones included — a real clock move would age them all, and entitlement looks at the latest end.
    """
    delta = timedelta(days=days, hours=hours)
    await env.db.execute(
        update(ClientSubscription).where(ClientSubscription.client_id == client_id).values(
            current_period_start=ClientSubscription.current_period_start - delta,
            current_period_end=ClientSubscription.current_period_end - delta,
        )
    )
    await env.db.commit()


def webhook(event: str, *, order_id=None, payment_id=None, amount=None, event_id: str, status="captured",
            extra=None, secret_signer=MOCK) -> tuple[bytes, dict]:
    """(body, headers) for a signed Razorpay webhook delivery."""
    payment = {"id": payment_id, "order_id": order_id, "amount": amount, "status": status, **(extra or {})}
    payload = {"entity": "event", "event": event, "payload": {"payment": {"entity": payment}}}
    if event == "refund.processed":
        payload["payload"] = {"refund": {"entity": {"id": "rfnd_e2e", "payment_id": payment_id, "amount": amount}}}
    body = json.dumps(payload).encode()
    return body, {
        "X-Razorpay-Signature": secret_signer.sign_webhook(body),
        "X-Razorpay-Event-Id": event_id,
        "Content-Type": "application/json",
    }


async def post_webhook(env, event: str, **kw):
    """POST a correctly signed webhook."""
    body, headers = webhook(event, **kw)
    return await env.http.post("/billing/webhook", content=body, headers=headers)


async def bulk_conversations(db, client_id: int, start: int, stop: int) -> None:
    """Open conversation windows #start+1 .. #stop through the SAME function the message pipeline calls."""
    for i in range(start, stop):
        rec = await usage.track_inbound_conversation(db, client_id, "whatsapp", f"9180{i:07d}")
        assert rec is not None and rec.inserted, f"conversation {i + 1} was not counted"


async def customer_says(env, t, phone: str, text: str, sent: dict) -> list[str]:
    """Send a WhatsApp text through the real webhook; return the texts the bot sent in reply."""
    before = len(sent["texts"])
    r = await send_message(env.http, phone, text, phone_number_id=t.pnid, wamid=f"wamid.e2e.{next(_wamid)}")
    assert r.status_code == 200, r.text
    return sent["texts"][before:]


async def seed_open_order(env, t, phone: str) -> int:
    """A real UPI order waiting for payment (pending_payment + stock reservation), as a mid-purchase customer has."""
    from app.services import order_service

    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=t.id, current_stage="payment",
        customer_name="Asha Shah", delivery_address="12 MG Road, Surat", ai_enabled=True,
        payment_method="UPI", pending_product_sku=t.sku, pending_order_quantity=1,
    )
    env.db.add(conv)
    await env.db.commit()
    order = await order_service.create_order(
        db=env.db, client_id=t.id, customer_name="Asha Shah", customer_phone=phone,
        delivery_address="12 MG Road, Surat", product_name=t.product_name, quantity=1,
        unit_price=t.price, payment_method="UPI", product_id=t.product_id, product_sku=t.sku,
        conversation_id=conv.id,
    )
    return order.id


# =============================================================================================
# 1. THE FULL LIFECYCLE
# =============================================================================================

async def test_full_lifecycle(env, monkeypatch):
    """new tenant -> ... -> expired -> grace -> after grace -> renewed (see the module docstring)."""
    sent = stub_media_and_capture_sends(monkeypatch)
    db, http = env.db, env.http
    t = await new_tenant(env)
    print(f"\n── tenant {t.id} ({t.email})")

    # ── A. new tenant: no plan yet ──────────────────────────────────────────────────────────
    st = await api_state(env, t)
    assert st["status"] == "none" and st["subscription"] is None
    assert st["entitlement"]["state"] == "grace" and st["entitlement"]["enforced"] is True
    assert st["entitlement"]["restricted"] is False
    step("new tenant: no subscription, entitlement=grace (bot works), enforcement flag visible to the dashboard")

    # ── B. GET plans ────────────────────────────────────────────────────────────────────────
    r = await http.get("/billing/plans", headers=t.h)
    assert r.status_code == 200, r.text
    plans = {p["code"]: p for p in r.json()["plans"]}
    assert list(plans) == ["starter_1500", "growth_5000", "pro_9000"]
    assert [plans[c]["base_paise"] for c in plans] == [459900, 1199900, 1799900]
    assert [plans[c]["conversation_limit"] for c in plans] == [1500, 5000, 9000]
    for p in plans.values():
        assert p["gst_paise"] == round(p["base_paise"] * 0.18) and p["total_paise"] == p["base_paise"] + p["gst_paise"]
        assert p["is_current"] is False and p["billing_period_days"] == 30
    assert plans["starter_1500"]["total_paise"] == 542682
    step("GET /billing/plans: Starter/Growth/Pro, GST-exclusive base + 18% GST = total (Starter ₹5,426.82)")

    # ── C. checkout Starter ─────────────────────────────────────────────────────────────────
    co = await checkout(env, t, "starter_1500")
    assert co.status_code == 200, co.text
    co = co.json()
    assert co["mock"] is True and co["purpose"] == "new" and co["currency"] == "INR"
    assert co["amount"] == plans["starter_1500"]["total_paise"] and co["amounts"]["credit_paise"] == 0
    assert co["razorpay_order_id"].startswith("order_mock_") and co["key_id"] == "rzp_test_mock"
    order = await order_row(db, co["razorpay_order_id"])
    assert order.status == "created" and order.amount_paise == co["amount"] and order.client_id == t.id
    assert await subs_of(db, t.id) == [], "checkout alone must not create a subscription"
    step(f"checkout Starter: mock order {co['razorpay_order_id']}, ₹{co['amount'] / 100:,.2f}, DB row 'created', nothing activated yet")

    # ── D. mock pay -> active ───────────────────────────────────────────────────────────────
    r = await http.post("/billing/mock/complete", json={"razorpay_order_id": co["razorpay_order_id"]}, headers=t.h)
    assert r.status_code == 200, r.text
    st = r.json()
    sub = st["subscription"]
    assert st["status"] == "active" and sub["status"] == "active" and sub["plan"]["code"] == "starter_1500"
    assert (sub["conversation_limit"], sub["conversations_used"], sub["days_left"]) == (1500, 0, 30)
    assert st["entitlement"]["state"] == "active"
    order = await order_row(db, co["razorpay_order_id"])
    assert order.status == "paid" and order.razorpay_payment_id and order.razorpay_signature and order.paid_at
    assert await count_of(db, Invoice, Invoice.client_id == t.id) == 1
    inv1 = (await rows(db, Invoice, Invoice.client_id == t.id))[0]
    assert inv1.mode == "test" and order.mode == "test" and inv1.invoice_number.startswith("TEST-ST24/")
    client = (await rows(db, Client, Client.id == t.id))[0]
    assert client.plan_conv_limit_snapshot == 1500 and client.plan_grandfathered is True
    starter_sub_id = sub["id"]
    step(f"mock pay: subscription ACTIVE, limit 1500, used 0, 30 days left; order paid (mode=test) + signature stored; "
         f"1 invoice {inv1.invoice_number} (TEST series); legacy client plan synced")

    # ── E. 1,201 conversations -> 80% alert ─────────────────────────────────────────────────
    t0 = time.perf_counter()
    await bulk_conversations(db, t.id, 0, 1201)
    st = await api_state(env, t)
    assert st["subscription"]["conversations_used"] == 1201 and st["over_limit"] is False
    assert st["subscription"]["percent_used"] == 80.1
    kinds = await alert_kinds(db, t.id)
    assert kinds == ["usage_80"], kinds
    assert await count_of(db, ConversationUsageLog, ConversationUsageLog.client_id == t.id) == 1201
    r = await http.get("/billing/alerts", headers=t.h)
    assert r.status_code == 200 and [a["kind"] for a in r.json()["alerts"]] == ["usage_80"] and r.json()["unread"] == 1
    step(f"1,201 conversations counted ({time.perf_counter() - t0:.1f}s): used=1201 (80.1%), exactly one usage_80 alert, not over limit")

    # ── F. 1,501 -> over_limit, bot still replies ───────────────────────────────────────────
    await bulk_conversations(db, t.id, 1201, 1500)
    st = await api_state(env, t)
    assert st["subscription"]["conversations_used"] == 1500
    assert st["over_limit"] is False, "AT the limit is not over it: over_limit = used > limit"
    assert st["subscription"]["percent_used"] == 100.0
    assert await alert_kinds(db, t.id) == ["usage_100", "usage_80"]          # ...but the 100% alert fires on reaching it
    # conversation #1,501 arrives from a real customer through the real WhatsApp webhook
    texts = await customer_says(env, t, "919800150100", "hi, do you have kurtas?", sent)
    assert texts and not any(FALLBACK in x for x in texts), texts
    st = await api_state(env, t)
    assert st["subscription"]["conversations_used"] == 1501 and st["over_limit"] is True
    assert st["subscription"]["percent_used"] == 100.1 and st["entitlement"]["restricted"] is False
    assert await alert_kinds(db, t.id) == ["usage_100", "usage_80"]          # 120% is 1,800 — not reached
    step("1,500th: 100% alert, over_limit still false; 1,501st via real WhatsApp webhook: counted, over_limit=TRUE, bot REPLIED "
         "(not the fallback), never blocked")

    # ── G. upgrade to Growth: credit applied, usage carried ─────────────────────────────────
    r = await checkout(env, t, "starter_1500")                                 # same plan while active = a renewal (left unpaid on purpose)
    assert r.status_code == 200 and r.json()["purpose"] == "renewal" and r.json()["amounts"]["credit_paise"] == 0
    await rewind(env, t.id, days=10)                                  # 10 of 30 days used -> 20/30 of the period left
    st = await api_state(env, t)
    assert st["subscription"]["days_left"] == 20
    opts = {o["plan_code"]: o for o in st["upgrade_options"]}
    assert set(opts) == {"growth_5000", "pro_9000"}
    expected_credit = round(459900 * 20 / 30)                                   # 306,600 (minus a few paise of real elapsed time)
    credit = opts["growth_5000"]["amounts"]["credit_paise"]
    assert expected_credit - 300 <= credit <= expected_credit, credit
    step(f"upgrade preview after 10 days: credit ₹{credit / 100:,.2f} (≈ 20/30 of ₹4,599) shown for Growth and Pro")

    r = await checkout(env, t, "growth_5000")
    assert r.status_code == 200, r.text
    up = r.json()
    a = up["amounts"]
    assert up["purpose"] == "upgrade" and a["base_paise"] == 1199900
    assert expected_credit - 300 <= a["credit_paise"] <= expected_credit
    assert a["taxable_paise"] == 1199900 - a["credit_paise"]
    assert abs(a["gst_paise"] - a["taxable_paise"] * 18 / 100) <= 1               # GST on the NET amount (credit applied before GST)
    assert a["gst_paise"] == a["cgst_paise"] + a["sgst_paise"] + a["igst_paise"]
    assert up["amount"] == a["taxable_paise"] + a["gst_paise"] < plans["growth_5000"]["total_paise"]
    r = await http.post("/billing/mock/complete", json={"razorpay_order_id": up["razorpay_order_id"]}, headers=t.h)
    assert r.status_code == 200, r.text
    st = r.json()
    sub = st["subscription"]
    assert sub["plan"]["code"] == "growth_5000" and sub["status"] == "active"
    assert (sub["conversation_limit"], sub["conversations_used"], sub["days_left"]) == (5000, 1501, 30)
    assert st["over_limit"] is False and st["queued"] == []                    # carried 1,501 of 5,000 -> back under the limit
    old = (await rows(db, ClientSubscription, ClientSubscription.id == starter_sub_id))[0]
    assert old.status == "superseded"
    new_db = (await rows(db, ClientSubscription, ClientSubscription.id == sub["id"]))[0]
    assert new_db.credited_paise == a["credit_paise"]
    client = (await rows(db, Client, Client.id == t.id))[0]
    assert client.plan_conv_limit_snapshot == 5000
    growth_sub_id = sub["id"]
    growth_order = await order_row(db, up["razorpay_order_id"])
    step(f"upgrade to Growth: charged ₹{up['amount'] / 100:,.2f} (list ₹{plans['growth_5000']['total_paise'] / 100:,.2f} − credit, GST on net); "
         f"Starter superseded, used 1501 carried, limit 5000, over_limit cleared, full 30 days")

    r = await checkout(env, t, "starter_1500")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "downgrade_not_allowed"
    step("mid-cycle downgrade to Starter refused (400 downgrade_not_allowed)")

    # ── H. duplicate webhooks: no double activation ─────────────────────────────────────────
    ev = dict(order_id=growth_order.razorpay_order_id, payment_id=growth_order.razorpay_payment_id, amount=growth_order.amount_paise)
    r1 = await post_webhook(env, "payment.captured", event_id="evt_e2e_dup_1", **ev)
    r2 = await post_webhook(env, "payment.captured", event_id="evt_e2e_dup_1", **ev)          # Razorpay retry: same event id
    r3 = await post_webhook(env, "order.paid", event_id="evt_e2e_dup_2", **ev)                # different event, same payment
    assert (r1.status_code, r1.json()["status"]) == (200, "ok")
    assert (r2.status_code, r2.json()["status"]) == (200, "duplicate")
    assert (r3.status_code, r3.json()["status"]) == (200, "ok")
    subs = await subs_of(db, t.id)
    assert [s.status for s in subs] == ["superseded", "active"], [(s.id, s.status) for s in subs]
    assert await count_of(db, PaymentOrder, PaymentOrder.client_id == t.id, PaymentOrder.status == "paid") == 2
    assert await count_of(db, Invoice, Invoice.client_id == t.id) == 2
    assert (await api_state(env, t))["subscription"]["conversations_used"] == 1501
    events = await rows(db, PaymentEvent, PaymentEvent.razorpay_event_id.in_(["evt_e2e_dup_1", "evt_e2e_dup_2"]))
    assert len(events) == 2 and all(e.processed and e.error is None for e in events)
    step("webhooks: payment.captured x2 (same id) -> ok + duplicate; order.paid (new id) -> ok; still 2 subscriptions, 2 paid orders, "
         "2 invoices, usage unchanged — no double activation")

    # a customer is mid-purchase when the plan lapses (real order, waiting for the payment screenshot)
    open_order_id = await seed_open_order(env, t, "919800777001")
    step(f"customer 919800777001 has an open UPI order #{open_order_id} (pending_payment, stock reserved) while the plan is still active")

    # ── I. clock moves toward expiry: reminders ─────────────────────────────────────────────
    await rewind(env, t.id, days=28)                                  # period now ends in ~2 days
    rep = await maintenance.run_maintenance(db)
    assert rep.reminders == 1 and rep.errors == 0
    assert "expiring_3d" in await alert_kinds(db, t.id)
    assert (await maintenance.run_maintenance(db)).reminders == 0               # same tick again: deduped
    await rewind(env, t.id, hours=36)                                  # ~12 hours left
    assert (await maintenance.run_maintenance(db)).reminders == 1
    kinds = await alert_kinds(db, t.id)
    assert "expiring_1d" in kinds and kinds.count("expiring_3d") == 1
    step("scheduler ticks: 'expires in 3 days' alert at T-2d, 'expires in 1 day' at T-12h, each exactly once")

    # ── J. past period end -> expired -> GRACE ──────────────────────────────────────────────
    await rewind(env, t.id, hours=13)                                 # ended ~1 hour ago
    rep = await maintenance.run_maintenance(db)
    assert rep.expired == 1 and rep.errors == 0
    st = await api_state(env, t)
    assert st["status"] == "none" and st["entitlement"]["state"] == "grace" and st["entitlement"]["restricted"] is False
    assert (await rows(db, ClientSubscription, ClientSubscription.id == growth_sub_id))[0].status == "expired"
    assert "grace_started" in await alert_kinds(db, t.id)
    in_grace = (await checkout(env, t, "growth_5000")).json()
    assert in_grace["purpose"] == "renewal" and in_grace["amounts"]["credit_paise"] == 0
    texts = await customer_says(env, t, "919800888001", "hello, kurtas available?", sent)
    assert texts and not any(FALLBACK in x for x in texts)
    assert await count_of(db, ConversationUsageLog, ConversationUsageLog.client_id == t.id, ConversationUsageLog.subscription_id.is_(None)) == 1
    step("period ended 1h ago: subscription 'expired', entitlement=GRACE, grace alert raised; a checkout now is purpose=RENEWAL "
         "(left unpaid); bot still serves new customers (that conversation is logged but not counted against any plan)")

    # ── K. grace over -> fallback template; in-progress order still completes ───────────────
    await rewind(env, t.id, days=3)                                   # ended 3d 1h ago > 3-day grace
    rep = await maintenance.run_maintenance(db)
    assert rep.errors == 0 and "expired" in await alert_kinds(db, t.id)
    st = await api_state(env, t)
    assert st["status"] == "none" and st["entitlement"]["state"] == "expired" and st["entitlement"]["restricted"] is True
    step("grace over: entitlement=EXPIRED, restricted=true, 'expired' alert raised (dashboard + checkout still reachable)")

    texts = await customer_says(env, t, "919800888002", "hi, any new kurtas?", sent)
    assert any(FALLBACK in x for x in texts), texts
    conv = (await rows(db, Conversation, Conversation.client_id == t.id, Conversation.phone_number == "919800888002"))[0]
    msgs = [(m.role, m.content) for m in await rows(db, Message, Message.conversation_id == conv.id)]
    assert msgs == [("user", "hi, any new kurtas?"), ("assistant", FALLBACK)]
    step("NEW customer after grace -> the fixed 'assistant unavailable' template (no AI reply, 1 inbound + 1 template stored)")

    texts = await customer_says(env, t, "919800777001", "paid", sent)           # the mid-purchase customer
    assert texts and not any(FALLBACK in x for x in texts), texts
    r = await send_image(http, "919800777001", phone_number_id=t.pnid, wamid=f"wamid.e2e.{next(_wamid)}")
    assert r.status_code == 200
    order = (await rows(db, Order, Order.id == open_order_id))[0]
    assert order.status == "payment_submitted", order.status
    assert "Thanks! Our team will verify your payment shortly and update you here." in sent["texts"]
    r = await http.post(f"/orders/{open_order_id}/payment/approve", headers=t.h)
    assert r.status_code == 200, r.text
    order = (await rows(db, Order, Order.id == open_order_id))[0]
    assert order.status == "paid" and order.stock_deducted is True
    assert any("Payment confirmed" in x for x in sent["texts"])
    step("IN-PROGRESS order after grace: customer's messages served normally, screenshot accepted, seller approves, order PAID + "
         "customer notified — the lapse never strands a purchase")

    # ── L. renew -> active ──────────────────────────────────────────────────────────────────
    r = await http.get("/billing/plans", headers=t.h)
    assert r.json()["plans"][1]["is_current"] is False                         # nothing is current while expired
    renewal = await buy(env, t, "growth_5000")
    assert renewal["purpose"] == "new" and renewal["amounts"]["credit_paise"] == 0      # grace is over: a fresh period, not a renewal
    assert renewal["amount"] == plans["growth_5000"]["total_paise"]
    st = await api_state(env, t)
    sub = st["subscription"]
    assert st["status"] == "active" and sub["plan"]["code"] == "growth_5000" and st["entitlement"]["state"] == "active"
    assert (sub["conversation_limit"], sub["conversations_used"], sub["days_left"]) == (5000, 0, 30)   # fresh counter
    texts = await customer_says(env, t, "919800888003", "hello again", sent)
    assert texts and not any(FALLBACK in x for x in texts)
    assert (await api_state(env, t))["subscription"]["conversations_used"] == 1
    step("buy Growth after grace: purpose=NEW, ACTIVE again, fresh period (used 0, 30 days), new customer served and counted as 1")

    # ── M. books balance ────────────────────────────────────────────────────────────────────
    assert [s.status for s in await subs_of(db, t.id)] == ["superseded", "expired", "active"]
    paid = await rows(db, PaymentOrder, PaymentOrder.client_id == t.id, PaymentOrder.status == "paid")
    assert len(paid) == 3 and await count_of(db, Invoice, Invoice.client_id == t.id) == 3
    assert {o.mode for o in await rows(db, PaymentOrder, PaymentOrder.client_id == t.id)} == {"test"}
    assert all(i.invoice_number.startswith("TEST-ST24/") for i in await rows(db, Invoice, Invoice.client_id == t.id))
    r = await http.get("/billing/payments", headers=t.h)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 5                          # 3 paid + 2 abandoned renewals (one while active, one in grace)
    assert sorted(i["status"] for i in body["items"]) == ["created", "created", "paid", "paid", "paid"]
    inv = next(i for i in body["items"] if i["invoice_id"])
    r = await http.get(f"/billing/invoices/{inv['invoice_id']}/pdf", headers=t.h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    errors = await rows(db, PaymentEvent, PaymentEvent.error.is_not(None))
    assert errors == [], [(e.event_type, e.error) for e in errors]
    assert await count_of(db, PaymentEvent, PaymentEvent.processed.is_(False)) == 0
    step("ledger: 3 subscriptions (superseded/expired/active), 3 paid orders = 3 invoices, PDF downloads, history lists 5 orders, "
         "payment_events has 0 errors / 0 unprocessed")


# =============================================================================================
# 2. FAILED PAYMENT PATH
# =============================================================================================

async def test_failed_payment_path(env):
    """payment.failed -> order failed (reason kept), nothing activated; retry on the same order succeeds."""
    db = env.db
    t = await new_tenant(env)
    print(f"\n── tenant {t.id}")
    co = (await checkout(env, t, "starter_1500")).json()
    oid = co["razorpay_order_id"]

    fail_kw = dict(order_id=oid, payment_id="pay_mock_failed_1", amount=co["amount"], status="failed",
                   extra={"error_code": "BAD_REQUEST_ERROR", "error_description": "Your card was declined by the bank"})
    r = await post_webhook(env, "payment.failed", event_id="evt_e2e_fail_1", **fail_kw)
    assert (r.status_code, r.json()["status"]) == (200, "ok")
    o = await order_row(db, oid)
    assert o.status == "failed" and o.failure_reason == "Your card was declined by the bank"
    assert await subs_of(db, t.id) == [] and (await api_state(env, t))["status"] == "none"
    step("payment.failed webhook: order 'failed' with Razorpay's reason, no subscription, tenant still has no plan")

    r = await post_webhook(env, "payment.failed", event_id="evt_e2e_fail_1", **fail_kw)
    assert r.json()["status"] == "duplicate" and (await order_row(db, oid)).status == "failed"
    step("redelivered payment.failed -> duplicate, ignored")

    r = await env.http.get("/billing/payments", headers=t.h)
    item = r.json()["items"][0]
    assert item["status"] == "failed" and item["failure_reason"] == "Your card was declined by the bank" and item["invoice_id"] is None
    step("payment history shows the failure and its reason, no invoice issued")

    paid = MOCK.simulate_payment(oid)                                          # the customer retries inside the same Checkout and succeeds
    r = await post_webhook(env, "payment.captured", event_id="evt_e2e_fail_2", order_id=oid,
                           payment_id=paid["razorpay_payment_id"], amount=co["amount"])
    assert (r.status_code, r.json()["status"]) == (200, "ok")
    o = await order_row(db, oid)
    assert o.status == "paid" and o.failure_reason is None and o.razorpay_payment_id == paid["razorpay_payment_id"]
    st = await api_state(env, t)
    assert st["status"] == "active" and st["subscription"]["plan"]["code"] == "starter_1500"
    step("retry on the same order succeeds via webhook: order 'paid', failure_reason cleared, subscription ACTIVE")

    r = await post_webhook(env, "payment.failed", event_id="evt_e2e_fail_3", **fail_kw)     # a stale failure arrives late
    assert (r.status_code, r.json()["status"]) == (200, "ignored")
    assert (await order_row(db, oid)).status == "paid" and (await api_state(env, t))["status"] == "active"
    step("a late payment.failed for an already-paid order is ignored (paid is never undone)")

    errs = await rows(db, PaymentEvent, PaymentEvent.error.is_not(None))
    assert [e.event_type for e in errs] == ["payment.failed"] and "already paid" in errs[0].error
    step(f"payment_events recorded the stale failure as a note, not a fault: {errs[0].error!r}")


async def test_failed_signature_check_on_browser_callback_leaves_order_retryable(env):
    """A browser callback with a bad signature is refused; the order stays 'created' and can still be paid properly."""
    t = await new_tenant(env)
    co = (await checkout(env, t, "growth_5000")).json()
    paid = MOCK.simulate_payment(co["razorpay_order_id"])
    bad = {**paid, "razorpay_signature": "0" * 64}
    r = await env.http.post("/billing/verify", json=bad, headers=t.h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_signature"
    assert (await order_row(env.db, co["razorpay_order_id"])).status == "created"
    r = await env.http.post("/billing/verify", json=paid, headers=t.h)           # the genuine callback
    assert r.status_code == 200 and r.json()["status"] == "active"
    step("bad signature -> 400, order untouched; the genuine callback for the same order then activates it")


# =============================================================================================
# 3. TAMPERING
# =============================================================================================

async def test_tampered_signature_paths(env):
    """Every way of faking a payment is refused and leaves no subscription behind."""
    db, http = env.db, env.http
    t = await new_tenant(env, staff=True)
    other = await new_tenant(env)
    print(f"\n── tenants {t.id} (victim) / {other.id} (attacker)")
    co = (await checkout(env, t, "starter_1500")).json()
    oid, amount = co["razorpay_order_id"], co["amount"]
    good = MOCK.simulate_payment(oid)

    async def assert_untouched(why: str) -> None:
        o = await order_row(db, oid)
        assert o.status == "created" and o.razorpay_payment_id is None, why
        assert await subs_of(db, t.id) == [], why

    # -- /billing/verify ------------------------------------------------------------------
    flipped = good["razorpay_signature"][:-1] + ("0" if good["razorpay_signature"][-1] != "0" else "1")
    for label, sig in (
        ("one hex digit flipped", flipped),
        ("signed with the wrong secret", compute_payment_signature("not-the-secret", oid, good["razorpay_payment_id"])),
        ("signature of a different payment id", compute_payment_signature(MOCK_KEY_SECRET, oid, "pay_mock_other")),
        ("garbage", "deadbeef"),
    ):
        r = await http.post("/billing/verify", json={**good, "razorpay_signature": sig}, headers=t.h)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_signature", (label, r.text)
        await assert_untouched(label)
    r = await http.post("/billing/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": good["razorpay_payment_id"]}, headers=t.h)
    assert r.status_code == 422
    await assert_untouched("missing signature")
    step("/billing/verify: flipped digit / wrong secret / wrong payment id / garbage -> 400 invalid_signature; missing field -> 422; order untouched")

    # a *validly signed* payment id that claims a different order/amount (signature proves nothing about the money)
    forged_pay = f"pay_mock_100_{oid.split('_')[-1]}_abcdef"                    # ₹1 payment, mock-encoded
    forged = {"razorpay_order_id": oid, "razorpay_payment_id": forged_pay,
              "razorpay_signature": compute_payment_signature(MOCK_KEY_SECRET, oid, forged_pay)}
    r = await http.post("/billing/verify", json=forged, headers=t.h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "payment_order_mismatch", r.text
    await assert_untouched("forged payment")
    step("validly-signed but forged ₹1 payment for a ₹5,426 order -> 400 payment_order_mismatch (gateway is asked, amounts are never trusted)")

    # -- other tenant / role / auth --------------------------------------------------------
    r = await http.post("/billing/verify", json=good, headers=other.h)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "order_not_found"
    r = await http.post("/billing/mock/complete", json={"razorpay_order_id": oid}, headers=other.h)
    assert r.status_code == 404
    await assert_untouched("cross-tenant")
    step("another tenant cannot verify or mock-complete this order (404 — existence not revealed)")
    r = await http.post("/billing/checkout", json={"plan_code": "starter_1500"}, headers=t.staff_h)
    assert r.status_code == 403
    r = await http.post("/billing/checkout", json={"plan_code": "starter_1500"})
    assert r.status_code in (401, 403)
    r = await http.post("/billing/checkout", json={"plan_code": "nope"}, headers=t.h)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_plan"
    step("staff user -> 403, no token -> 401/403, unknown plan -> 404 on checkout")

    # -- /billing/webhook ------------------------------------------------------------------
    ev = dict(order_id=oid, payment_id=good["razorpay_payment_id"], amount=amount)
    body, headers = webhook("payment.captured", event_id="evt_e2e_t1", **ev)
    n_events = await count_of(db, PaymentEvent)

    r = await http.post("/billing/webhook", content=body, headers={**headers, "X-Razorpay-Signature": "0" * 64})
    assert r.status_code == 400
    r = await http.post("/billing/webhook", content=body, headers={k: v for k, v in headers.items() if k != "X-Razorpay-Signature"})
    assert r.status_code == 400
    r = await http.post("/billing/webhook", content=body.replace(b"captured", b"CAPTURED"), headers=headers)   # body altered after signing
    assert r.status_code == 400
    attacker = SimpleNamespace(sign_webhook=lambda b: __import__("hmac").new(b"attacker", b, "sha256").hexdigest())
    ab, ah = webhook("payment.captured", event_id="evt_e2e_t2", secret_signer=attacker, **ev)
    r = await http.post("/billing/webhook", content=ab, headers=ah)             # re-signed with the attacker's own secret
    assert r.status_code == 400
    assert await count_of(db, PaymentEvent) == n_events, "rejected webhooks must not be persisted"
    await assert_untouched("webhook tamper")
    step("/billing/webhook: wrong signature / missing header / body altered after signing / attacker-signed -> 400 each, nothing stored, order untouched")

    r = await http.post("/billing/webhook", content=b"[1,2]", headers={"X-Razorpay-Signature": MOCK.sign_webhook(b"[1,2]")})
    assert r.status_code == 400
    r = await post_webhook(env, "payment.captured", event_id="evt_e2e_t3", order_id=oid, payment_id=forged_pay, amount=amount)
    assert (r.status_code, r.json()["status"]) == (200, "error")                 # signed OK, but the gateway says it is not this order's payment
    ev3 = (await rows(db, PaymentEvent, PaymentEvent.razorpay_event_id == "evt_e2e_t3"))[0]
    assert ev3.error and ev3.error.startswith("payment_order_mismatch") and ev3.processed is True
    await assert_untouched("signed-but-forged webhook")
    step(f"correctly-signed webhook carrying a forged payment: 200 'error' (never a 5xx), stored on payment_events.error = {ev3.error[:48]!r}…, nothing activated")

    # unknown order in a signed webhook (a different Razorpay account use) is acknowledged and noted
    r = await post_webhook(env, "payment.captured", event_id="evt_e2e_t4", order_id="order_someone_else", payment_id="pay_x", amount=1)
    assert (r.status_code, r.json()["status"]) == (200, "ignored")
    step("signed webhook for an order that is not ours -> 200 'ignored'")

    # finally, the genuine payment still works
    r = await http.post("/billing/verify", json=good, headers=t.h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    step("after all that, the genuine signed callback activates the plan normally")


async def test_live_mode_refuses_the_mock_gateway(env):
    """RAZORPAY_MODE=live with mock still on (or a test key in live mode) must fail closed with a 503, never fake-accept."""
    from app.main import app

    get_settings = env.settings_dep
    t = await new_tenant(env)
    live_mock = env.settings.model_copy(update=dict(razorpay_mode="live", sellertalk24_billing_mock=True))
    app.dependency_overrides[get_settings] = lambda: live_mock
    r = await env.http.post("/billing/checkout", json={"plan_code": "starter_1500"}, headers=t.h)
    assert r.status_code == 503
    r = await env.http.post("/billing/webhook", content=b"{}", headers={"X-Razorpay-Signature": "x"})
    assert r.status_code == 503
    step("mode=live + mock flag -> checkout and webhook both 503 (fake payments impossible in live mode)")

    wrong_key = env.settings.model_copy(update=dict(
        razorpay_mode="live", sellertalk24_billing_mock=False, razorpay_key_id="rzp_test_abc", razorpay_key_secret="s"))
    app.dependency_overrides[get_settings] = lambda: wrong_key
    r = await env.http.post("/billing/checkout", json={"plan_code": "starter_1500"}, headers=t.h)
    assert r.status_code == 503
    step("mode=live + rzp_test_ key -> 503 (key/mode prefix mismatch refused)")
    assert await subs_of(env.db, t.id) == [] and await count_of(env.db, PaymentOrder, PaymentOrder.client_id == t.id) == 0


# =============================================================================================
# 4. EXTRAS: races and stacking
# =============================================================================================

async def test_webhook_and_verify_racing_activate_exactly_once(env):
    """Razorpay's webhook and the browser callback land at the same instant: one activation, one invoice."""
    db = env.db
    t = await new_tenant(env)
    co = (await checkout(env, t, "growth_5000")).json()
    oid = co["razorpay_order_id"]
    paid = MOCK.simulate_payment(oid)
    body, headers = webhook("payment.captured", event_id="evt_e2e_race", order_id=oid,
                            payment_id=paid["razorpay_payment_id"], amount=co["amount"])

    results = await asyncio.gather(
        env.http.post("/billing/verify", json=paid, headers=t.h),
        env.http.post("/billing/webhook", content=body, headers=headers),
        env.http.post("/billing/verify", json=paid, headers=t.h),
    )
    assert [r.status_code for r in results] == [200, 200, 200], [r.text for r in results]
    subs = await subs_of(db, t.id)
    assert len(subs) == 1 and subs[0].status == "active"
    assert await count_of(db, Invoice, Invoice.client_id == t.id) == 1
    assert await count_of(db, PaymentOrder, PaymentOrder.client_id == t.id, PaymentOrder.status == "paid") == 1
    step("verify + webhook + verify fired concurrently: 1 subscription, 1 invoice, 1 paid order")


async def test_stacked_renewal_starts_when_the_current_period_ends(env):
    """Renewing while active queues a period behind the current one; the scheduler promotes it at the boundary."""
    db = env.db
    t = await new_tenant(env)
    first = await buy(env, t, "starter_1500")
    assert first["purpose"] == "new"
    await bulk_conversations(db, t.id, 0, 5)
    second = await buy(env, t, "starter_1500")
    assert second["purpose"] == "renewal" and second["amounts"]["credit_paise"] == 0
    st = await api_state(env, t)
    assert st["subscription"]["conversations_used"] == 5 and len(st["queued"]) == 1
    subs = await subs_of(db, t.id)
    assert [s.status for s in subs] == ["active", "pending"] and subs[1].current_period_start == subs[0].current_period_end
    step("renewal while active: queued 'pending' period starts exactly when the current one ends, current usage untouched")

    await rewind(env, t.id, days=31)
    rep = await maintenance.run_maintenance(db)
    assert (rep.expired, rep.promoted, rep.errors) == (1, 1, 0)
    st = await api_state(env, t)
    assert st["status"] == "active" and st["queued"] == [] and st["subscription"]["conversations_used"] == 0
    assert st["entitlement"]["state"] == "active"
    step("after the first period lapses the scheduler expires it and promotes the queued one with a fresh counter — no gap, no grace")


# =============================================================================================
# 5. REFUND -> REVOKE -> GRACE -> ADMIN RE-GRANT
# =============================================================================================

async def test_refund_revokes_then_grace_then_fallback_then_admin_regrant_restores(env, monkeypatch):
    """A refunded customer keeps 3 days of grace from the refund, then gets the fallback; an admin can grant service back."""
    from app.config import get_settings
    from app.models.sellertalk24_billing import BillingAdminLog, CreditNote

    sent = stub_media_and_capture_sends(monkeypatch)
    admin_key = "e2e-admin-key"
    base = get_settings()
    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: base.model_copy(update=dict(admin_secret_key=admin_key)))
    admin = {"X-Admin-Key": admin_key, "X-Admin-User": "amit@sellertalk24.com"}
    db, http = env.db, env.http
    t = await new_tenant(env)
    print(f"\n── tenant {t.id}")

    co = await buy(env, t, "starter_1500")
    order = await order_row(db, co["razorpay_order_id"])
    assert (await api_state(env, t))["status"] == "active"
    step("tenant buys Starter (mock), plan active")

    body, headers = webhook("refund.processed", payment_id=order.razorpay_payment_id, amount=order.amount_paise,
                            event_id="evt_e2e_refund")
    payload = json.loads(body)
    payload["payload"]["refund"]["entity"]["id"] = "rfnd_e2e_1"
    body = json.dumps(payload).encode()
    headers["X-Razorpay-Signature"] = MOCK.sign_webhook(body)
    r = await http.post("/billing/webhook", content=body, headers=headers)
    assert (r.status_code, r.json()["status"]) == (200, "ok")
    (sub,) = await subs_of(db, t.id)
    (note,) = await rows(db, CreditNote, CreditNote.client_id == t.id)
    assert sub.status == "revoked" and (await order_row(db, co["razorpay_order_id"])).status == "refunded"
    assert note.refund_kind == "full" and note.total_paise == order.amount_paise and note.credit_note_number.startswith("TEST-ST24-CN/")
    st = await api_state(env, t)
    assert st["status"] == "none" and st["entitlement"]["state"] == "grace"
    assert [a["kind"] for a in (await http.get("/billing/alerts", headers=t.h)).json()["alerts"]] == ["refund_revoked"]
    step(f"full refund webhook: order refunded, subscription REVOKED, credit note {note.credit_note_number}, tenant alerted, entitlement=GRACE")

    r2 = await http.post("/billing/webhook", content=body, headers={**headers, "X-Razorpay-Event-Id": "evt_e2e_refund_again"})
    assert r2.json()["status"] == "ok" and await count_of(db, CreditNote) == 1
    step("the same refund re-sent under a new event id: still exactly one credit note")

    texts = await customer_says(env, t, "919800999001", "hello, kurtas?", sent)
    assert texts and not any(FALLBACK in x for x in texts)
    step("bot still serves new customers during the refund grace")

    await db.execute(update(ClientSubscription).where(ClientSubscription.client_id == t.id).values(
        revoked_at=ClientSubscription.revoked_at - timedelta(days=3, hours=1)))
    await db.commit()
    assert (await api_state(env, t))["entitlement"]["state"] == "expired"
    texts = await customer_says(env, t, "919800999002", "anyone there?", sent)
    assert any(FALLBACK in x for x in texts)
    step("3 days after the revocation: EXPIRED, new customers get the fixed fallback template")

    r = await http.post(f"/admin/billing/clients/{t.id}/grant", headers=admin,
                        json={"plan_code": "starter_1500", "days": 30, "reason": "goodwill after refund dispute"})
    assert r.status_code == 201 and r.json()["status"] == "active"
    assert (await api_state(env, t))["entitlement"]["state"] == "active"
    texts = await customer_says(env, t, "919800999003", "hello again", sent)
    assert texts and not any(FALLBACK in x for x in texts)
    (log,) = await rows(db, BillingAdminLog, BillingAdminLog.action == "grant_subscription")
    assert (log.actor, log.reason, log.client_id) == ("amit@sellertalk24.com", "goodwill after refund dispute", t.id)
    step("admin grants 30 days (₹0 offline order, audited with actor + reason): ACTIVE again, bot serving")

    r = await http.post(f"/admin/billing/subscriptions/{r.json()['id']}/revoke", headers=admin, json={"reason": "granted by mistake"})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert (await api_state(env, t))["entitlement"]["state"] == "grace"
    step("admin revoke: cut short immediately, tenant back in grace, audit row written")

    h = (await http.get("/health/billing", headers=admin)).json()
    assert h["mock"] is True and h["mode"] == "test" and h["last_webhook_received_at"] is not None
    assert h["webhook_errors_actionable_24h"] == 0 and h["amount_mismatches_24h"] == 0 and h["stuck_orders"] == 0
    step(f"/health/billing: mode={h['mode']} mock={h['mock']} last_webhook set, 0 actionable errors, 0 mismatches, 0 stuck orders "
         f"(status={h['status']}: {h['problems'][0] if h['problems'] else 'no problems'})")
