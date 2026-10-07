"""
SellerTalk24 billing API integration tests — real Postgres, the real FastAPI app, real JWT auth.

Only the Razorpay gateway is swapped (stateless mock whose signatures are real HMACs), so the
verify path, row locking, idempotency, stacking and credit maths are all exercised for real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.client import Client
from app.models.sellertalk24_billing import (
    BillingPlan,
    ClientSubscription,
    PaymentEvent,
    PaymentOrder,
)
from app.models.user import User
from app.services.auth_service import create_access_token
from app.services.billing import subscriptions as subs_service
from app.services.billing.razorpay_client import (
    MOCK_KEY_SECRET,
    BillingGatewayError,
    MockRazorpayClient,
    RazorpayClient,
    compute_payment_signature,
)
from tests.replay.conftest import seed_client_and_product

pytestmark = pytest.mark.asyncio

_phone = count(1)


class ScriptedGateway(MockRazorpayClient):
    """Mock gateway whose fetch_payment can be bent: wrong amount, wrong status, outage."""

    def __init__(self) -> None:
        self.amount_delta = 0
        self.status = None
        self.fail = False
        self.fetches = 0

    async def fetch_payment(self, payment_id: str) -> dict:
        self.fetches += 1
        if self.fail:
            raise BillingGatewayError("simulated outage")
        payment = await super().fetch_payment(payment_id)
        payment["amount"] += self.amount_delta
        if self.status:
            payment["status"] = self.status
        return payment


@dataclass
class Env:
    """Handles used by the tests: HTTP client, DB session, and the swappable gateway."""

    http: object
    session: object
    gateway: MockRazorpayClient = field(default_factory=MockRazorpayClient)

    async def owner(self, **client_kw) -> tuple[SimpleNamespace, dict]:
        """Seed a client + owner user; return (plain client snapshot, auth headers)."""
        n = next(_phone)
        client, _ = await seed_client_and_product(
            self.session, phone=f"91990000{n:04d}", wa_phone_number_id=f"PN{n}"
        )
        for k, v in client_kw.items():
            setattr(client, k, v)
        self.session.add(User(client_id=client.id, email=client.email, role="owner", permissions=[], is_active=True))
        await self.session.commit()
        # A plain snapshot: ORM objects held by the test session are expired by rows()/expire_all().
        snap = SimpleNamespace(
            id=client.id, email=client.email, phone=client.phone, business_name=client.business_name
        )
        return snap, {"Authorization": f"Bearer {create_access_token({'sub': client.email})}"}

    async def staff(self, client) -> dict:
        """Seed a staff user of *client*; return auth headers."""
        email = f"staff{client.id}@test.com"
        self.session.add(User(client_id=client.id, email=email, role="staff", permissions=[], is_active=True))
        await self.session.commit()
        return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}

    async def plan(self, code: str) -> BillingPlan:
        """A seeded billing plan row."""
        return (await self.session.execute(select(BillingPlan).where(BillingPlan.code == code))).scalar_one()

    async def checkout(self, headers: dict, code: str):
        """POST /billing/checkout."""
        return await self.http.post("/billing/checkout", json={"plan_code": code}, headers=headers)

    async def pay(self, headers: dict, razorpay_order_id: str):
        """Mock-complete an order (valid signature, real verify + activation)."""
        return await self.http.post("/billing/mock/complete", json={"razorpay_order_id": razorpay_order_id}, headers=headers)

    async def buy(self, headers: dict, code: str) -> dict:
        """Checkout then pay; returns the checkout JSON (asserting both succeeded)."""
        co = await self.checkout(headers, code)
        assert co.status_code == 200, co.text
        paid = await self.pay(headers, co.json()["razorpay_order_id"])
        assert paid.status_code == 200, paid.text
        return co.json()

    def paid_credentials(self, razorpay_order_id: str) -> dict:
        """What Checkout.js would hand the browser for a successful payment of this order."""
        return self.gateway.simulate_payment(razorpay_order_id)

    def webhook(self, event: str, *, order_id=None, payment_id=None, amount=None, event_id="evt_1", extra=None):
        """Build (body, headers) for a signed Razorpay webhook delivery."""
        payment = {"id": payment_id, "order_id": order_id, "amount": amount, "status": "captured"}
        payment.update(extra or {})
        payload = {"entity": "event", "event": event, "payload": {"payment": {"entity": payment}}}
        if event == "refund.processed":
            payload["payload"] = {"refund": {"entity": {"id": "rfnd_1", "payment_id": payment_id, "amount": amount}}}
        body = json.dumps(payload).encode()
        headers = {
            "X-Razorpay-Signature": self.gateway.sign_webhook(body),
            "X-Razorpay-Event-Id": event_id,
            "Content-Type": "application/json",
        }
        return body, headers

    async def post_webhook(self, event: str, **kw):
        """POST a signed webhook and return the response."""
        body, headers = self.webhook(event, **kw)
        return await self.http.post("/billing/webhook", content=body, headers=headers)

    async def seed_period(self, client_id: int, code: str, *, start: datetime, status="active") -> ClientSubscription:
        """Insert an already-paid 30-day period (as if bought earlier) with its source order."""
        plan = await self.plan(code)
        n = next(_phone)
        order = PaymentOrder(
            client_id=client_id, plan_id=plan.id, razorpay_order_id=f"order_seed_{client_id}_{n}",
            receipt=f"seed_{client_id}_{n}", amount_paise=plan.price_paise, base_paise=plan.price_paise,
            taxable_paise=plan.price_paise, status="paid", purpose="new", paid_at=start,
            razorpay_payment_id=f"pay_seed_{client_id}_{n}",
        )
        self.session.add(order)
        await self.session.flush()
        sub = ClientSubscription(
            client_id=client_id, plan_id=plan.id, status=status, current_period_start=start,
            current_period_end=start + timedelta(days=30), conversation_limit=plan.conversation_limit,
            source_payment_id=order.id,
        )
        self.session.add(sub)
        await self.session.commit()
        return sub

    async def rows(self, model, *where):
        """Fresh rows of *model* (populate_existing refreshes anything already in the identity map)."""
        stmt = select(model).execution_options(populate_existing=True)
        for w in where:
            stmt = stmt.where(w)
        return list((await self.session.execute(stmt)).scalars().all())

    async def order(self, razorpay_order_id: str) -> PaymentOrder:
        """The payment order row for a Razorpay order id."""
        return (await self.rows(PaymentOrder, PaymentOrder.razorpay_order_id == razorpay_order_id))[0]

    async def subs(self, client_id: int) -> list[ClientSubscription]:
        """All subscription rows of a client, oldest first."""
        rows = await self.rows(ClientSubscription, ClientSubscription.client_id == client_id)
        return sorted(rows, key=lambda s: s.id)


@pytest_asyncio.fixture
async def env(replay_http, replay_session):
    """Billing test environment: pinned settings + mock gateway injected via dependency overrides."""
    from app.config import get_settings
    from app.main import app
    from app.routers.billing import get_gateway

    pinned = get_settings().model_copy(update=dict(
        prices_include_gst=False, gst_rate_bps=1800, seller_state_code="24",
        razorpay_key_id="rzp_test_unit", razorpay_key_secret="unit-secret", razorpay_mode="test",
    ))
    e = Env(http=replay_http, session=replay_session)
    app.dependency_overrides[get_settings] = lambda: pinned
    app.dependency_overrides[get_gateway] = lambda: e.gateway
    yield e
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_gateway, None)
    await _restore_seeded_plans(replay_session)


async def _restore_seeded_plans(session) -> None:
    """billing_plans survives the per-test TRUNCATE (it is seeded reference data), so undo test edits."""
    import importlib.util
    from pathlib import Path

    from tests.replay.conftest import BACKEND_DIR

    path = Path(BACKEND_DIR) / "alembic/versions/0062_sellertalk24_billing_tables.py"
    spec = importlib.util.spec_from_file_location("mig0062_restore", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    await session.rollback()
    await session.execute(BillingPlan.__table__.update().values(is_active=True))
    conn = await session.connection()
    await conn.run_sync(lambda sync_conn: mig._seed_plans(sync_conn))
    await session.commit()


# --- auth ---------------------------------------------------------------------------------

async def test_endpoints_require_authentication_except_the_webhook(env):
    for method, path in [("get", "/billing/plans"), ("get", "/billing/subscription"), ("get", "/billing/payments"),
                         ("post", "/billing/checkout"), ("post", "/billing/verify"), ("post", "/billing/mock/complete")]:
        resp = await getattr(env.http, method)(path)
        assert resp.status_code == 401, (method, path)


async def test_money_moving_routes_are_owner_only_but_reads_allow_staff(env):
    client, _ = await env.owner()
    staff = await env.staff(client)

    assert (await env.checkout(staff, "starter_1500")).status_code == 403
    assert (await env.http.get("/billing/payments", headers=staff)).status_code == 403
    assert (await env.http.get("/billing/plans", headers=staff)).status_code == 200
    assert (await env.http.get("/billing/subscription", headers=staff)).status_code == 200


# --- plans ---------------------------------------------------------------------------------

async def test_plans_lists_active_plans_in_order_with_gst_amounts(env):
    _, h = await env.owner()

    resp = await env.http.get("/billing/plans", headers=h)

    assert resp.status_code == 200
    plans = resp.json()["plans"]
    assert [p["code"] for p in plans] == ["starter_1500", "growth_5000", "pro_9000"]
    starter = plans[0]
    assert (starter["base_paise"], starter["gst_paise"], starter["total_paise"]) == (459900, 82782, 542682)
    assert (starter["cgst_paise"], starter["sgst_paise"], starter["igst_paise"]) == (41391, 41391, 0)
    assert starter["features"] == {"whatsapp": True, "instagram": False}
    assert starter["prices_include_gst"] is False and starter["gst_rate_bps"] == 1800
    assert [p["total_paise"] for p in plans] == [542682, 1415882, 2123882]


async def test_plans_use_igst_for_a_client_in_another_state(env):
    _, h = await env.owner(gst_number="27AAPFU0939F1ZV")  # Maharashtra

    starter = (await env.http.get("/billing/plans", headers=h)).json()["plans"][0]

    assert (starter["cgst_paise"], starter["sgst_paise"], starter["igst_paise"]) == (0, 0, 82782)
    assert starter["total_paise"] == 542682


async def test_plans_use_cgst_sgst_for_a_gujarat_gstin_and_when_no_gstin(env):
    _, h = await env.owner(gst_number="24AAPFU0939F1ZV")
    assert (await env.http.get("/billing/plans", headers=h)).json()["plans"][0]["igst_paise"] == 0


async def test_inactive_plans_are_hidden(env):
    _, h = await env.owner()
    await env.session.execute(BillingPlan.__table__.update().where(BillingPlan.code == "pro_9000").values(is_active=False))
    await env.session.commit()

    codes = [p["code"] for p in (await env.http.get("/billing/plans", headers=h)).json()["plans"]]

    assert codes == ["starter_1500", "growth_5000"]
    assert (await env.checkout(h, "pro_9000")).status_code == 404


# --- checkout + happy path ---------------------------------------------------------------

async def test_checkout_creates_order_row_and_returns_checkout_options(env):
    client, h = await env.owner()

    resp = await env.checkout(h, "starter_1500")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mock"] is True and body["key_id"] == "rzp_test_mock"
    assert (body["name"], body["currency"], body["amount"]) == ("SellerTalk24", "INR", 542682)
    assert body["razorpay_order_id"].startswith("order_mock_542682_")
    assert body["description"] == "SellerTalk24 Starter — 30 days"
    assert body["prefill"] == {"name": client.business_name, "email": client.email, "contact": client.phone}
    assert body["purpose"] == "new" and body["plan_code"] == "starter_1500"
    order = await env.order(body["razorpay_order_id"])
    assert (order.status, order.client_id, order.amount_paise, order.base_paise, order.gst_paise) == ("created", client.id, 542682, 459900, 82782)
    assert order.receipt.startswith(f"st24_{client.id}_") and len(order.receipt) <= 40
    assert order.id == body["payment_order_id"]


async def test_checkout_notes_carry_client_plan_and_payment_order_id(env):
    client, h = await env.owner()
    captured = {}
    real_create = env.gateway.create_order

    async def spy(amount, receipt, notes=None):
        captured.update(notes=notes, amount=amount, receipt=receipt)
        return await real_create(amount, receipt, notes)

    env.gateway.create_order = spy
    body = (await env.checkout(h, "growth_5000")).json()

    assert captured["notes"] == {"client_id": str(client.id), "plan_code": "growth_5000", "payment_order_id": str(body["payment_order_id"])}


async def test_checkout_unknown_plan_is_404_and_creates_nothing(env):
    _, h = await env.owner()
    resp = await env.checkout(h, "nope")
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "unknown_plan"
    assert await env.rows(PaymentOrder) == []


async def test_checkout_gateway_failure_is_502_and_persists_no_order(env):
    _, h = await env.owner()

    async def boom(*a, **k):
        raise BillingGatewayError("down")

    env.gateway.create_order = boom
    resp = await env.checkout(h, "starter_1500")

    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "gateway_error"
    assert await env.rows(PaymentOrder) == []


async def test_happy_path_new_subscription_end_to_end(env):
    client, h = await env.owner(plan_slug="starter")
    co = (await env.checkout(h, "growth_5000")).json()

    paid = await env.pay(h, co["razorpay_order_id"])

    assert paid.status_code == 200
    state = paid.json()
    sub = state["subscription"]
    assert state["status"] == "active" and sub["status"] == "active"
    assert (sub["plan"]["code"], sub["conversation_limit"], sub["conversations_used"]) == ("growth_5000", 5000, 0)
    assert sub["days_left"] == 30 and sub["percent_used"] == 0.0
    start = datetime.fromisoformat(sub["current_period_start"])
    assert datetime.fromisoformat(sub["current_period_end"]) - start == timedelta(days=30)

    order = await env.order(co["razorpay_order_id"])
    assert order.status == "paid" and order.razorpay_payment_id.startswith("pay_mock_1415882_") and order.paid_at
    assert order.razorpay_signature
    (db_sub,) = await env.subs(client.id)
    assert (db_sub.status, db_sub.source_payment_id, db_sub.credited_paise) == ("active", order.id, 0)

    legacy = (await env.rows(Client, Client.id == client.id))[0]
    assert (legacy.plan_slug, legacy.plan_conv_limit_snapshot, legacy.plan_price_snapshot) == ("growth", 5000, 11999)
    assert legacy.plan_grandfathered is True and legacy.billing_cycle_start is not None


async def test_subscription_endpoint_with_nothing_bought(env):
    _, h = await env.owner()
    body = (await env.http.get("/billing/subscription", headers=h)).json()
    assert (body["status"], body["subscription"], body["queued"], body["upgrade_options"], body["over_limit"]) == ("none", None, [], [], False)
    assert body["entitlement"]["state"] == "grace"            # a brand-new account is in its grace window


async def test_subscription_endpoint_reports_usage_percent_and_days_left(env):
    client, h = await env.owner()
    sub = await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=10))
    sub.conversations_used = 1200
    await env.session.commit()

    body = (await env.http.get("/billing/subscription", headers=h)).json()["subscription"]

    assert (body["conversations_used"], body["conversation_limit"], body["percent_used"]) == (1200, 1500, 80.0)
    assert body["days_left"] == 20


async def test_payments_history_is_paginated_newest_first_and_tenant_scoped(env):
    client, h = await env.owner()
    _, other_h = await env.owner()
    ids = [(await env.checkout(h, "starter_1500")).json()["razorpay_order_id"] for _ in range(3)]
    await env.checkout(other_h, "starter_1500")

    page1 = (await env.http.get("/billing/payments?page=1&page_size=2", headers=h)).json()
    page2 = (await env.http.get("/billing/payments?page=2&page_size=2", headers=h)).json()

    assert (page1["total"], page1["page"], page1["page_size"]) == (3, 1, 2)
    assert [i["razorpay_order_id"] for i in page1["items"] + page2["items"]] == ids[::-1]
    assert len(page2["items"]) == 1
    item = page1["items"][0]
    assert item["plan_code"] == "starter_1500" and item["amount_paise"] == 542682 and item["status"] == "created"
    assert "razorpay_signature" not in item and "raw_webhook" not in item


async def test_checkout_is_rate_limited_per_tenant(env):
    _, h = await env.owner()
    _, other_h = await env.owner()

    codes = [(await env.checkout(h, "starter_1500")).status_code for _ in range(11)]

    assert codes == [200] * 10 + [429]
    assert (await env.checkout(other_h, "starter_1500")).status_code == 200   # other tenants unaffected


async def test_mock_complete_is_404_when_the_gateway_is_not_mock(env):
    _, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    env.gateway = RazorpayClient("rzp_test_x", "s", "w")   # a real client (never contacted here)

    resp = await env.pay(h, co["razorpay_order_id"])

    assert resp.status_code == 404


# --- verify: signature, tenant, tampering ------------------------------------------------

async def test_verify_with_valid_credentials_activates(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])

    resp = await env.http.post("/billing/verify", json=creds, headers=h)

    assert resp.status_code == 200 and resp.json()["subscription"]["plan"]["code"] == "starter_1500"
    assert (await env.order(co["razorpay_order_id"])).status == "paid"


async def test_verify_rejects_a_tampered_signature_and_activates_nothing(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    creds["razorpay_signature"] = creds["razorpay_signature"][:-1] + ("0" if creds["razorpay_signature"][-1] != "0" else "1")

    resp = await env.http.post("/billing/verify", json=creds, headers=h)

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_signature"
    assert (await env.order(co["razorpay_order_id"])).status == "created"
    assert await env.subs(client.id) == []


async def test_verify_rejects_a_signature_for_a_different_payment_id(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    creds["razorpay_payment_id"] = creds["razorpay_payment_id"][:-6] + "ffffff"   # signature no longer matches

    resp = await env.http.post("/billing/verify", json=creds, headers=h)

    assert resp.status_code == 400 and await env.subs(client.id) == []


async def test_verify_cannot_be_used_on_another_tenants_order(env):
    victim, victim_h = await env.owner()
    attacker, attacker_h = await env.owner()
    co = (await env.checkout(victim_h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])

    resp = await env.http.post("/billing/verify", json=creds, headers=attacker_h)

    assert resp.status_code == 404
    assert await env.subs(victim.id) == [] and await env.subs(attacker.id) == []
    assert (await env.order(co["razorpay_order_id"])).status == "created"


async def test_verify_unknown_order_is_404(env):
    _, h = await env.owner()
    creds = env.paid_credentials("order_mock_100_deadbeef")
    assert (await env.http.post("/billing/verify", json=creds, headers=h)).status_code == 404


async def test_verify_is_idempotent_calling_twice_activates_once(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])

    first = await env.http.post("/billing/verify", json=creds, headers=h)
    second = await env.http.post("/billing/verify", json=creds, headers=h)

    assert first.status_code == second.status_code == 200
    assert first.json()["subscription"]["id"] == second.json()["subscription"]["id"]
    assert len(await env.subs(client.id)) == 1


# --- amount / status checks --------------------------------------------------------------

async def test_amount_mismatch_is_rejected_marks_the_order_failed_and_activates_nothing(env):
    client, h = await env.owner()
    env.gateway = ScriptedGateway()
    co = (await env.checkout(h, "starter_1500")).json()
    env.gateway.amount_delta = -100     # Razorpay says the customer paid less than the order amount

    resp = await env.pay(h, co["razorpay_order_id"])

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "amount_mismatch"
    order = await env.order(co["razorpay_order_id"])
    assert order.status == "failed" and "amount_mismatch" in order.failure_reason and order.paid_at is None
    assert await env.subs(client.id) == []


async def test_a_payment_that_belongs_to_a_different_order_is_rejected(env):
    client, h = await env.owner()
    a = (await env.checkout(h, "starter_1500")).json()
    b = (await env.checkout(h, "growth_5000")).json()
    pay_for_a = env.gateway.simulate_payment(a["razorpay_order_id"])
    # Browser claims A's (cheap) payment pays for B, with a signature valid for the pair it presents.
    forged_sig = compute_payment_signature(MOCK_KEY_SECRET, b["razorpay_order_id"], pay_for_a["razorpay_payment_id"])

    resp = await env.http.post("/billing/verify", headers=h, json={
        "razorpay_order_id": b["razorpay_order_id"],
        "razorpay_payment_id": pay_for_a["razorpay_payment_id"],
        "razorpay_signature": forged_sig,
    })

    assert resp.status_code == 400 and await env.subs(client.id) == []
    assert (await env.order(b["razorpay_order_id"])).status != "paid"


async def test_authorized_but_not_yet_captured_payment_returns_409_and_leaves_order_open(env):
    client, h = await env.owner()
    env.gateway = ScriptedGateway()
    co = (await env.checkout(h, "starter_1500")).json()
    env.gateway.status = "authorized"

    resp = await env.pay(h, co["razorpay_order_id"])

    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "payment_not_captured"
    assert (await env.order(co["razorpay_order_id"])).status == "created"
    env.gateway.status = None                       # capture completes; the webhook (or a retry) now succeeds
    assert (await env.pay(h, co["razorpay_order_id"])).status_code == 200


async def test_gateway_outage_during_verify_is_a_retryable_502(env):
    client, h = await env.owner()
    env.gateway = ScriptedGateway()
    co = (await env.checkout(h, "starter_1500")).json()
    env.gateway.fail = True

    resp = await env.pay(h, co["razorpay_order_id"])

    assert resp.status_code == 502 and await env.subs(client.id) == []
    env.gateway.fail = False
    assert (await env.pay(h, co["razorpay_order_id"])).status_code == 200


# --- webhook ------------------------------------------------------------------------------

async def test_webhook_rejects_a_bad_signature_and_persists_nothing(env):
    body, headers = env.webhook("payment.captured", order_id="order_mock_100_aaaaaaaa", payment_id="pay_mock_100_aaaaaaaa_bbbbbb")
    headers["X-Razorpay-Signature"] = "0" * 64

    resp = await env.http.post("/billing/webhook", content=body, headers=headers)

    assert resp.status_code == 400
    assert await env.rows(PaymentEvent) == []


async def test_webhook_rejects_a_missing_signature_header(env):
    body, headers = env.webhook("payment.captured")
    del headers["X-Razorpay-Signature"]
    assert (await env.http.post("/billing/webhook", content=body, headers=headers)).status_code == 400


async def test_webhook_needs_no_login(env):
    # signature-only auth: a valid signed body with no Authorization header is accepted
    body, headers = env.webhook("some.other.event", event_id="evt_open")
    assert (await env.http.post("/billing/webhook", content=body, headers=headers)).status_code == 200


async def test_webhook_unparseable_but_signed_body_is_400(env):
    body = b"not json"
    headers = {"X-Razorpay-Signature": env.gateway.sign_webhook(body), "X-Razorpay-Event-Id": "evt_bad"}
    assert (await env.http.post("/billing/webhook", content=body, headers=headers)).status_code == 400


async def test_webhook_payment_captured_activates_the_subscription(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])

    resp = await env.post_webhook(
        "payment.captured", order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"], amount=542682
    )

    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    assert (await env.order(co["razorpay_order_id"])).status == "paid"
    (sub,) = await env.subs(client.id)
    assert sub.status == "active"
    (event,) = await env.rows(PaymentEvent)
    assert (event.event_type, event.processed, event.error) == ("payment.captured", True, None)
    assert (await env.order(co["razorpay_order_id"])).raw_webhook["event"] == "payment.captured"


async def test_webhook_order_paid_also_activates(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    payload = {"event": "order.paid", "payload": {
        "payment": {"entity": {"id": creds["razorpay_payment_id"], "order_id": co["razorpay_order_id"]}},
        "order": {"entity": {"id": co["razorpay_order_id"]}}}}
    body = json.dumps(payload).encode()

    resp = await env.http.post("/billing/webhook", content=body, headers={
        "X-Razorpay-Signature": env.gateway.sign_webhook(body), "X-Razorpay-Event-Id": "evt_op"})

    assert resp.json() == {"status": "ok"} and len(await env.subs(client.id)) == 1


async def test_duplicate_webhook_event_is_ignored_and_activates_once(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    kw = dict(order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"], amount=542682, event_id="evt_dup")

    first = await env.post_webhook("payment.captured", **kw)
    second = await env.post_webhook("payment.captured", **kw)

    assert (first.json(), second.json()) == ({"status": "ok"}, {"status": "duplicate"})
    assert len(await env.rows(PaymentEvent)) == 1 and len(await env.subs(client.id)) == 1


async def test_webhook_without_event_id_header_dedupes_on_the_body_hash(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    body, headers = env.webhook("payment.captured", order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"])
    del headers["X-Razorpay-Event-Id"]

    first = await env.http.post("/billing/webhook", content=body, headers=headers)
    second = await env.http.post("/billing/webhook", content=body, headers=headers)

    assert (first.json()["status"], second.json()["status"]) == ("ok", "duplicate")
    (event,) = await env.rows(PaymentEvent)
    assert event.razorpay_event_id.startswith("body-sha256:")


async def test_webhook_for_an_unknown_order_is_acknowledged_and_ignored(env):
    resp = await env.post_webhook("payment.captured", order_id="order_other_app", payment_id="pay_x", amount=100)

    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    (event,) = await env.rows(PaymentEvent)
    assert event.processed is True and "order_not_found" in event.error


async def test_webhook_amount_mismatch_is_stored_on_the_event_and_still_200(env):
    client, h = await env.owner()
    env.gateway = ScriptedGateway()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    env.gateway.amount_delta = 5000

    resp = await env.post_webhook("payment.captured", order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"], amount=1)

    assert resp.status_code == 200 and resp.json() == {"status": "error"}
    (event,) = await env.rows(PaymentEvent)
    assert "amount_mismatch" in event.error and event.processed is True
    assert await env.subs(client.id) == []


async def test_webhook_retryable_failure_stays_unprocessed_and_a_redelivery_completes_it(env):
    client, h = await env.owner()
    env.gateway = ScriptedGateway()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    kw = dict(order_id=co["razorpay_order_id"], payment_id=creds["razorpay_payment_id"], event_id="evt_retry")
    env.gateway.fail = True

    first = await env.post_webhook("payment.captured", **kw)

    assert first.status_code == 200 and first.json() == {"status": "error"}
    (event,) = await env.rows(PaymentEvent)
    assert event.processed is False and "gateway_error" in event.error and await env.subs(client.id) == []

    env.gateway.fail = False
    second = await env.post_webhook("payment.captured", **kw)

    assert second.json() == {"status": "ok"}
    (event,) = await env.rows(PaymentEvent)
    assert event.processed is True and event.error is None
    assert len(await env.subs(client.id)) == 1


async def test_webhook_payment_failed_marks_the_order_failed_with_the_reason_but_a_retry_can_still_pay(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()

    resp = await env.post_webhook("payment.failed", order_id=co["razorpay_order_id"], payment_id="pay_f1",
                                  extra={"status": "failed", "error_description": "Card declined by bank"})

    assert resp.json() == {"status": "ok"}
    order = await env.order(co["razorpay_order_id"])
    assert order.status == "failed" and order.failure_reason == "Card declined by bank"
    # the customer retries on the same order and succeeds
    assert (await env.pay(h, co["razorpay_order_id"])).status_code == 200
    order = await env.order(co["razorpay_order_id"])
    assert order.status == "paid" and order.failure_reason is None


async def test_webhook_payment_failed_never_downgrades_a_paid_order(env):
    client, h = await env.owner()
    co = await env.buy(h, "starter_1500")

    resp = await env.post_webhook("payment.failed", order_id=co["razorpay_order_id"], payment_id="pay_late_fail",
                                  extra={"status": "failed"}, event_id="evt_late")

    assert resp.json() == {"status": "ignored"}
    assert (await env.order(co["razorpay_order_id"])).status == "paid"


async def test_webhook_full_refund_marks_the_order_refunded_and_revokes_the_subscription(env, caplog):
    client, h = await env.owner()
    co = await env.buy(h, "starter_1500")
    order = await env.order(co["razorpay_order_id"])

    with caplog.at_level("ERROR"):
        resp = await env.post_webhook("refund.processed", payment_id=order.razorpay_payment_id, amount=order.amount_paise, event_id="evt_rf")

    assert resp.json() == {"status": "ok"}
    assert (await env.order(co["razorpay_order_id"])).status == "refunded"
    (sub,) = await env.subs(client.id)
    assert sub.status == "revoked" and sub.revoked_at is not None     # a full refund ends the plan (see test_sellertalk24_billing_refunds_admin.py)
    assert "FULL REFUND" in caplog.text and f"client {client.id}" in caplog.text


async def test_webhook_partial_refund_leaves_the_order_paid(env):
    client, h = await env.owner()
    co = await env.buy(h, "starter_1500")
    order = await env.order(co["razorpay_order_id"])

    resp = await env.post_webhook("refund.processed", payment_id=order.razorpay_payment_id, amount=1000, event_id="evt_pr")

    assert resp.json() == {"status": "ok"}
    assert (await env.order(co["razorpay_order_id"])).status == "paid"
    (event,) = await env.rows(PaymentEvent)
    assert event.error is None and event.processed          # handled cleanly: a credit note + an admin flag, subscription untouched
    (sub,) = await env.subs(client.id)
    assert sub.status == "active"


async def test_a_refunded_order_cannot_be_activated_again(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    await env.session.execute(PaymentOrder.__table__.update().where(PaymentOrder.razorpay_order_id == co["razorpay_order_id"]).values(status="refunded"))
    await env.session.commit()

    resp = await env.http.post("/billing/verify", json=creds, headers=h)

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "order_refunded"
    assert await env.subs(client.id) == []


async def test_refund_for_an_unknown_payment_is_ignored(env):
    resp = await env.post_webhook("refund.processed", payment_id="pay_nobody", amount=100)
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}


# --- the verify + webhook race -------------------------------------------------------------

async def test_verify_and_webhook_race_activates_exactly_once_every_time(env):
    """The headline race: browser callback and webhook for the same payment, repeated on fresh orders."""
    for round_no in range(8):
        client, h = await env.owner()
        co = (await env.checkout(h, "starter_1500")).json()
        creds = env.paid_credentials(co["razorpay_order_id"])
        body, hdrs = env.webhook("payment.captured", order_id=co["razorpay_order_id"],
                                 payment_id=creds["razorpay_payment_id"], amount=542682, event_id=f"evt_race_{round_no}")

        verify, webhook = await asyncio.gather(
            env.http.post("/billing/verify", json=creds, headers=h),
            env.http.post("/billing/webhook", content=body, headers=hdrs),
        )

        assert verify.status_code == 200 and webhook.status_code == 200, (round_no, verify.text, webhook.text)
        subs = await env.subs(client.id)
        assert len(subs) == 1 and subs[0].status == "active"
        assert (await env.order(co["razorpay_order_id"])).status == "paid"
        assert verify.json()["subscription"]["id"] == subs[0].id
        (event,) = await env.rows(PaymentEvent, PaymentEvent.razorpay_event_id == f"evt_race_{round_no}")
        assert event.processed is True and event.error is None


async def test_many_concurrent_verifies_and_webhooks_still_activate_once(env):
    client, h = await env.owner()
    co = (await env.checkout(h, "starter_1500")).json()
    creds = env.paid_credentials(co["razorpay_order_id"])
    calls = [env.http.post("/billing/verify", json=creds, headers=h) for _ in range(5)]
    for i in range(5):
        body, hdrs = env.webhook("payment.captured", order_id=co["razorpay_order_id"],
                                 payment_id=creds["razorpay_payment_id"], event_id=f"evt_many_{i}")
        calls.append(env.http.post("/billing/webhook", content=body, headers=hdrs))

    responses = await asyncio.gather(*calls)

    assert all(r.status_code == 200 for r in responses)
    assert len(await env.subs(client.id)) == 1
    n = (await env.session.execute(select(func.count()).select_from(ClientSubscription))).scalar_one()
    assert n == 1


# --- downgrade / upgrade / renewal -----------------------------------------------------------

async def test_downgrade_mid_cycle_is_blocked_with_a_clear_message(env):
    client, h = await env.owner()
    await env.seed_period(client.id, "growth_5000", start=datetime.now(timezone.utc) - timedelta(days=5))

    resp = await env.checkout(h, "starter_1500")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "downgrade_not_allowed"
    assert "Growth" in detail["message"] and "Starter" in detail["message"] and "after your current period ends" in detail["message"]
    assert len(await env.rows(PaymentOrder, PaymentOrder.client_id == client.id)) == 1   # only the seeded one


async def test_downgrade_is_allowed_after_the_period_has_expired(env):
    client, h = await env.owner()
    await env.seed_period(client.id, "growth_5000", start=datetime.now(timezone.utc) - timedelta(days=40))

    co = await env.checkout(h, "starter_1500")

    assert co.status_code == 200 and co.json()["purpose"] == "new"
    assert (await env.pay(h, co.json()["razorpay_order_id"])).json()["subscription"]["plan"]["code"] == "starter_1500"
    statuses = sorted(s.status for s in await env.subs(client.id))
    assert statuses == ["active", "expired"]


async def test_upgrade_applies_pro_rata_credit_before_gst_and_carries_usage(env):
    client, h = await env.owner()
    old = await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=10))
    old.conversations_used = 321
    await env.session.commit()

    co = (await env.checkout(h, "growth_5000")).json()

    assert co["purpose"] == "upgrade"
    credit = co["amounts"]["credit_paise"]
    assert 306600 - 300 <= credit <= 306600          # 20/30 of Rs 4,599, minus the seconds the test took
    a = co["amounts"]
    assert a["base_paise"] == 1199900 and a["taxable_paise"] == 1199900 - credit
    assert a["cgst_paise"] == a["sgst_paise"] == round((a["taxable_paise"] * 9 + 50) // 100)
    assert co["amount"] == a["total_paise"] == a["taxable_paise"] + a["gst_paise"]

    state = (await env.pay(h, co["razorpay_order_id"])).json()

    new = state["subscription"]
    assert (new["plan"]["code"], new["status"], new["conversation_limit"], new["conversations_used"]) == ("growth_5000", "active", 5000, 321)
    assert new["days_left"] == 30
    subs = await env.subs(client.id)
    assert [s.status for s in subs] == ["superseded", "active"]
    assert subs[1].credited_paise == credit
    legacy = (await env.rows(Client, Client.id == client.id))[0]
    assert (legacy.plan_slug, legacy.plan_conv_limit_snapshot) == ("growth", 5000)


async def test_upgrade_preview_in_subscription_endpoint_matches_the_checkout_quote(env):
    client, h = await env.owner()
    await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=15))

    state = (await env.http.get("/billing/subscription", headers=h)).json()
    options = {o["plan_code"]: o for o in state["upgrade_options"]}
    co = (await env.checkout(h, "growth_5000")).json()

    assert set(options) == {"growth_5000", "pro_9000"}
    assert abs(options["growth_5000"]["amounts"]["credit_paise"] - co["amounts"]["credit_paise"]) <= 20
    assert options["growth_5000"]["amounts"]["total_paise"] > co["amounts"]["total_paise"] - 100
    assert all(o["amounts"]["credit_paise"] > 0 for o in options.values())


async def test_upgrade_also_credits_and_supersedes_queued_paid_renewals(env):
    client, h = await env.owner()
    now = datetime.now(timezone.utc)
    active = await env.seed_period(client.id, "starter_1500", start=now - timedelta(days=15))
    await env.seed_period(client.id, "starter_1500", start=active.current_period_end, status="pending")

    co = (await env.checkout(h, "growth_5000")).json()

    credit = co["amounts"]["credit_paise"]
    assert 229950 + 459900 - 300 <= credit <= 229950 + 459900      # half of active + all of the queued month
    await env.pay(h, co["razorpay_order_id"])
    statuses = [s.status for s in await env.subs(client.id)]
    assert statuses == ["superseded", "superseded", "active"]


async def test_renewal_stacks_after_the_current_period_end(env):
    client, h = await env.owner()
    active = await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=20))

    co = (await env.checkout(h, "starter_1500")).json()
    assert co["purpose"] == "renewal" and co["amounts"]["credit_paise"] == 0 and co["amount"] == 542682
    state = (await env.pay(h, co["razorpay_order_id"])).json()

    assert state["subscription"]["id"] == active.id                      # the running period is untouched
    (queued,) = state["queued"]
    assert datetime.fromisoformat(queued["current_period_start"]) == active.current_period_end
    assert datetime.fromisoformat(queued["current_period_end"]) == active.current_period_end + timedelta(days=30)
    subs = await env.subs(client.id)
    assert [s.status for s in subs] == ["active", "pending"]
    assert subs[1].conversations_used == 0 and subs[1].conversation_limit == 1500


async def test_second_renewal_stacks_after_the_first_queued_period(env):
    client, h = await env.owner()
    active = await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=20))

    await env.buy(h, "starter_1500")
    await env.buy(h, "starter_1500")

    subs = await env.subs(client.id)
    assert [s.status for s in subs] == ["active", "pending", "pending"]
    assert subs[1].current_period_start == active.current_period_end
    assert subs[2].current_period_start == subs[1].current_period_end
    assert subs[2].current_period_end == subs[1].current_period_end + timedelta(days=30)
    state = (await env.http.get("/billing/subscription", headers=h)).json()
    assert len(state["queued"]) == 2


async def test_buying_while_an_expired_period_is_still_marked_active_starts_now(env):
    client, h = await env.owner()
    await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=45))  # lapsed, row still 'active'

    co = (await env.checkout(h, "starter_1500")).json()
    assert co["purpose"] == "new"
    state = (await env.pay(h, co["razorpay_order_id"])).json()

    assert state["subscription"]["days_left"] == 30 and state["queued"] == []
    assert sorted(s.status for s in await env.subs(client.id)) == ["active", "expired"]


async def test_conversation_limit_is_snapshotted_so_a_later_plan_edit_does_not_change_a_paid_period(env):
    client, h = await env.owner()
    await env.buy(h, "starter_1500")
    await env.session.execute(BillingPlan.__table__.update().where(BillingPlan.code == "starter_1500").values(conversation_limit=99999))
    await env.session.commit()

    (sub,) = await env.subs(client.id)

    assert sub.conversation_limit == 1500


async def test_roll_forward_expires_the_lapsed_period_and_promotes_the_queued_one(env):
    client, h = await env.owner()
    now = datetime.now(timezone.utc)
    active = await env.seed_period(client.id, "starter_1500", start=now - timedelta(days=29))
    await env.buy(h, "starter_1500")                                  # queued renewal (Starter, 1500)
    later = active.current_period_end + timedelta(seconds=5)

    promoted = await subs_service.roll_forward(env.session, client.id, later)
    await env.session.commit()

    subs = await env.subs(client.id)
    assert [s.status for s in subs] == ["expired", "active"]
    assert promoted.id == subs[1].id
    legacy = (await env.rows(Client, Client.id == client.id))[0]
    assert legacy.plan_slug == "starter" and legacy.plan_conv_limit_snapshot == 1500


async def test_roll_forward_is_idempotent_and_a_no_op_when_nothing_changed(env):
    client, _ = await env.owner()
    await env.seed_period(client.id, "starter_1500", start=datetime.now(timezone.utc) - timedelta(days=2))

    a = await subs_service.roll_forward(env.session, client.id)
    b = await subs_service.roll_forward(env.session, client.id)

    assert a.id == b.id and a.status == "active"
