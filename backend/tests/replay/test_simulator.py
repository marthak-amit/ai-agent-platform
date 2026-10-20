"""
The in-dashboard chat simulator against the REAL engine on real Postgres.

The point of these tests is the fences: a simulated customer can place a full order, yet nothing reaches WhatsApp,
the owner's phone, usage/trial counters, stock, the orders list, customers, leads or the inbox.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.main  # noqa: F401 — bind every module's get_settings BEFORE the replay fixture patches app.config
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.product import Product
from app.models.sellertalk24_billing import ConversationUsageLog
from app.models.stock_reservation import StockReservation
from app.services import llm_health
from app.services.sandbox_guard import sandbox_phone
from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all
from tests.replay.test_dashboard_first_onboarding import (
    PASSWORD, checklist, client_row, done, quick_start, signup,
)
from tests.replay.test_router_v2 import router  # noqa: F401 — scripted LLM fixture shared with the router suite

# ruff: noqa: F811
pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


@pytest.fixture(autouse=True)
def _healthy_llm_breaker():
    """The LLM circuit breaker is process-global: earlier tests that fail LLM calls on purpose must not leak into these."""
    llm_health.reset_state()
    yield
    llm_health.reset_state()


@pytest.fixture
def leaks(monkeypatch):
    """Record anything that could leave the server: channel sends, owner alerts, realtime inbox events."""
    sent = capture_all(monkeypatch)

    async def _record_media(to_phone_number, *a, **kw):
        """Images / documents are recorded in the same list as text."""
        sent.append(f"<media to {to_phone_number}>")

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_image_message", _record_media)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_document_message", _record_media)
    owner = AsyncMock()
    monkeypatch.setattr("app.services.outbound.send_owner_text", owner)
    events: list[tuple] = []
    monkeypatch.setattr("app.services.realtime_service.publish", lambda cid, kind, data=None: events.append((cid, kind)))

    class Leaks:
        channel = sent
        owner_alerts = owner
        realtime = events
    return Leaks()


async def sim(http, headers, text: str | None = None, **kw):
    """POST /simulator/message and return the response."""
    body = {"text": text} if text is not None else kw
    return await http.post("/simulator/message", headers=headers, json=body)


def texts(resp) -> str:
    """All bubble text of a simulator reply, joined."""
    return "\n".join(b["text"] or "" for b in resp.json()["bubbles"])


async def setup_shop(http, session, *, accepts_cod: bool, upi: bool):
    """A fresh account past quick start (so it has the three samples) with chosen payment methods."""
    h = await signup(http)
    await quick_start(http, h)
    await http.patch("/auth/me", headers=h, json={"accepts_cod": accepts_cod, **({"upi_id": "shop@upi"} if upi else {})})
    c = await client_row(session, "owner@shop.test")
    c.accepts_upi = upi
    await session.commit()
    return h, c


async def prime_order(session, client, stage: str = "order_collection") -> Product:
    """Put the simulator customer mid-order on the (non-variant) sample tote bag."""
    tote = await session.scalar(select(Product).where(Product.client_id == client.id, Product.name == "Handmade Tote Bag"))
    session.add(Conversation(
        phone_number=sandbox_phone(client.id), channel="whatsapp", is_sandbox=True, client_id=client.id,
        current_stage=stage, pending_product_sku=tote.sku,
    ))
    await session.commit()
    return tote


async def drive_order(http, h, pay: str | None = None):
    """quantity, name, address, (payment method when the shop offers a choice), confirm — a one-line order."""
    replies = []
    steps = ["1", "Amit Shah", "12 MG Road, Surat 395002"] + ([pay] if pay else []) + ["yes"]
    for text in steps:
        r = await sim(http, h, text)
        assert r.status_code == 200, r.text
        replies.append(texts(r))
    return replies


# ── the fences ────────────────────────────────────────────────────────────────

async def test_a_full_upi_order_in_the_simulator_leaves_no_trace_outside_the_simulator(replay_http, replay_session, leaks):
    """Order placed, payment instructions shown — but no message sent, no alert, no stock, no listing, no usage."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=False, upi=True)
    tote = await prime_order(replay_session, c)

    replies = await drive_order(replay_http, h)

    assert "shop@upi" in replies[-1] or "UPI" in replies[-1], replies[-1]
    order = await replay_session.scalar(select(Order).where(Order.client_id == c.id).execution_options(populate_existing=True))
    assert order is not None and order.is_sandbox is True and order.order_number.startswith("SIM-")
    assert order.status == "pending_payment" and order.customer_phone == sandbox_phone(c.id)

    # nothing left the server
    assert leaks.channel == []
    leaks.owner_alerts.assert_not_called()
    assert [e for e in leaks.realtime if e[1] in ("new_message", "new_order", "order_created")] == []
    # no stock effect
    await replay_session.refresh(tote)
    assert tote.stock == 25
    assert await replay_session.scalar(select(func.count()).select_from(StockReservation)) == 0
    # not counted anywhere
    assert await replay_session.scalar(select(func.count()).select_from(ConversationUsageLog)) == 0
    assert await replay_session.scalar(select(func.count()).select_from(Lead)) == 0
    # not visible anywhere an owner looks
    assert (await replay_http.get("/orders", headers=h)).json() == []
    stats = (await replay_http.get("/orders/stats", headers=h)).json()
    assert stats["total_orders"] == 0 and stats["total_revenue"] == 0
    assert (await replay_http.get("/customers", headers=h)).json() == []
    assert (await replay_http.get("/customers/stats", headers=h)).json()["total_customers"] == 0
    assert (await replay_http.get("/conversations", headers=h)).json() == []
    pending = await replay_http.get("/payments/pending", headers=h)
    assert pending.status_code == 200 and pending.json() == []
    assert (await replay_http.get("/payments/pending/count", headers=h)).json() == {"count": 0}


async def test_a_cod_order_in_the_simulator_is_marked_paid_without_touching_stock_or_the_owner(replay_http, replay_session, leaks):
    """COD is auto-paid by the engine: the status flips, but no stock deduction, invoice or owner notification."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    tote = await prime_order(replay_session, c)

    await drive_order(replay_http, h, pay="COD")

    order = await replay_session.scalar(select(Order).where(Order.client_id == c.id).execution_options(populate_existing=True))
    assert order.is_sandbox and order.status == "paid" and order.payment_method == "COD"
    await replay_session.refresh(tote)
    assert tote.stock == 25                                           # the real catalogue is untouched
    assert order.invoice_url is None and order.invoice_number is None
    leaks.owner_alerts.assert_not_called()
    assert leaks.channel == []


async def test_simulator_orders_never_consume_real_order_numbers(replay_http, replay_session, leaks):
    """Real orders keep an unbroken ORD sequence however many test orders were placed."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    await prime_order(replay_session, c)
    await drive_order(replay_http, h, pay="COD")
    from app.services import order_service

    real = await order_service.create_cart_order(
        replay_session, c.id, "Real Customer", "919812300000", "Somewhere 1", [
            {"product_name": "Real", "quantity": 1, "unit_price": 100.0}], payment_method="COD")
    assert real.order_number.endswith("-0001") and real.order_number.startswith("ORD-") and real.is_sandbox is False
    assert [o["order_number"] for o in (await replay_http.get("/orders", headers=h)).json()] == [real.order_number]
    stats = (await replay_http.get("/orders/stats", headers=h)).json()
    assert stats["total_orders"] == 1 and stats["total_revenue"] == 100


async def test_the_simulator_works_before_go_live_and_nothing_is_counted_toward_the_trial(replay_http, replay_session, leaks):
    """Account in 'setup': unlimited simulator, no trial started, no conversation-window usage, status unchanged."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    assert c.account_status == "setup" and c.bot_active is False

    for text in ("hi", "do you have kurti?", "price of tote bag", "thanks", "show me something else"):
        r = await sim(replay_http, h, text)
        assert r.status_code == 200 and r.json()["bubbles"], text

    c = await client_row(replay_session, "owner@shop.test")
    assert c.account_status == "setup" and c.trial_started_at is None and c.bot_active is False
    assert await replay_session.scalar(select(func.count()).select_from(ConversationUsageLog)) == 0
    conv = await replay_session.scalar(select(Conversation).where(Conversation.client_id == c.id))
    assert conv.is_sandbox is True and conv.phone_number == sandbox_phone(c.id)
    assert leaks.channel == []


async def test_the_simulator_runs_the_real_llm_router_when_the_shop_uses_it(replay_http, replay_session, leaks, router):
    """A router-enabled shop is simulated through the router (tenant-attributed call), not a lookalike."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    c.router_v2_enabled = True
    await replay_session.commit()
    router.when("do you stock kurtis?", "search_catalog", {"query": "kurti"})

    r = await sim(replay_http, h, "do you stock kurtis?")

    assert r.status_code == 200 and "Cotton Kurti" in texts(r)
    assert [call["message"] for call in router.calls] == ["do you stock kurtis?"]
    assert router.calls[0]["client_id"] == c.id and router.calls[0]["purpose"] == "router"
    assert (await replay_http.get("/simulator/session", headers=h)).json()["router_enabled"] is True


async def test_answers_come_from_the_tenants_own_data(replay_http, replay_session, leaks):
    """Payment settings, catalogue and name are the tenant's: the sample shop's UPI id shows up in its payment answer."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=False, upi=True)
    r = await sim(replay_http, h, "do you accept COD?")
    assert r.status_code == 200
    assert "UPI" in texts(r) or "upi" in texts(r).lower()


# ── "Test your agent" ─────────────────────────────────────────────────────────

async def test_three_answered_turns_pass_the_agent_test_and_tick_the_checklist(replay_http, replay_session, leaks):
    """test_agent flips when the simulator has had 3 customer turns with a healthy LLM — derived, not ticked by hand."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    assert (await replay_http.get("/simulator/session", headers=h)).json()["test_passed"] is False

    first = await sim(replay_http, h, "hi")
    second = await sim(replay_http, h, "tote bag price?")
    assert first.json()["test_passed"] is False and second.json()["test_passed"] is False
    assert done(await checklist(replay_http, h))["test_agent"] is False

    third = await sim(replay_http, h, "do you have kurti?")
    assert third.json()["test_passed"] is True
    assert done(await checklist(replay_http, h))["test_agent"] is True
    assert (await client_row(replay_session, "owner@shop.test")).agent_tested_at is not None
    session_view = (await replay_http.get("/simulator/session", headers=h)).json()
    assert session_view["test_passed"] is True and session_view["turns_required"] == 3


async def test_turns_answered_while_the_llm_is_down_do_not_pass_the_test(replay_http, replay_session, leaks):
    """An outage template proves nothing about the agent, so the test stays open until the LLM is healthy."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    llm_health.trip("test outage")
    try:
        for text in ("hi", "tote bag price?", "do you have kurti?"):
            r = await sim(replay_http, h, text)
            assert r.status_code == 200 and r.json()["test_passed"] is False and r.json()["llm_available"] is False
        assert (await replay_http.get("/simulator/session", headers=h)).json()["llm_available"] is False
        assert (await client_row(replay_session, "owner@shop.test")).agent_tested_at is None
    finally:
        llm_health.reset_state()
    r = await sim(replay_http, h, "any saree?")
    assert r.json()["test_passed"] is True and r.json()["llm_available"] is True


# ── session, reset, limits, access ────────────────────────────────────────────

async def test_session_resumes_after_reload_and_reset_wipes_everything_it_created(replay_http, replay_session, leaks):
    """GET /simulator/session replays the transcript; POST /simulator/reset removes chat, orders and the fake customer."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    await prime_order(replay_session, c)
    await drive_order(replay_http, h, pay="COD")
    view = (await replay_http.get("/simulator/session", headers=h)).json()
    assert [m["role"] for m in view["messages"]][:2] == ["customer", "bot"] and view["sample_products"] == 3
    assert any(m["text"] == "Amit Shah" for m in view["messages"])
    assert await replay_session.scalar(select(func.count()).select_from(Customer).where(Customer.phone == sandbox_phone(c.id))) == 1

    r = await replay_http.post("/simulator/reset", headers=h)
    assert r.status_code == 200 and r.json() == {"status": "reset", "orders_removed": 1}

    replay_session.expire_all()
    for model in (Order, Conversation, Message, Customer):
        assert await replay_session.scalar(select(func.count()).select_from(model)) == 0, model.__name__
    assert (await replay_http.get("/simulator/session", headers=h)).json()["messages"] == []
    assert (await replay_http.post("/simulator/reset", headers=h)).status_code == 200      # idempotent
    first = await sim(replay_http, h, "hi")                                              # and a fresh chat starts cleanly
    assert first.status_code == 200 and first.json()["stage"] in ("greeting", "product_inquiry")


async def test_unlimited_by_default_and_the_optional_daily_limit_returns_429(replay_http, replay_session, leaks, monkeypatch):
    """No cap unless SIMULATOR_DAILY_MESSAGE_LIMIT is set; then the (limit+1)th message of the day is refused."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    for i in range(8):
        assert (await sim(replay_http, h, f"hello {i}")).status_code == 200
    assert (await replay_http.get("/simulator/session", headers=h)).json()["daily_limit"] == 0

    from app.config import get_settings
    capped = get_settings().model_copy(update={"simulator_daily_message_limit": 9})
    monkeypatch.setattr("app.services.simulator_service.get_settings", lambda: capped)
    assert (await sim(replay_http, h, "ninth")).status_code == 200
    r = await sim(replay_http, h, "tenth")
    assert r.status_code == 429 and "9 messages per day" in r.json()["detail"]


async def test_simulator_is_owner_only_and_validates_input(replay_http, replay_session, leaks):
    """Staff cannot use it; a request must carry exactly one of text / button / list row, non-blank."""
    from app.models.user import User
    from app.services import auth_service

    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    await replay_http.post("/team/invite", headers=h, json={"email": "staff@shop.test", "role": "staff"})
    staff = await replay_session.scalar(select(User).where(User.email == "staff@shop.test"))
    staff.hashed_password = "x"
    await replay_session.commit()
    sh = {"Authorization": f"Bearer {auth_service.create_access_token({'sub': 'staff@shop.test'})}"}
    assert (await replay_http.post("/simulator/message", headers=sh, json={"text": "hi"})).status_code == 403
    assert (await replay_http.post("/simulator/message", json={"text": "hi"})).status_code == 401

    for bad in ({}, {"text": "   "}, {"text": "hi", "button_id": "x"}, {"button_id": "a", "list_row_id": "b"}):
        assert (await replay_http.post("/simulator/message", headers=h, json=bad)).status_code == 422, bad


async def test_tapped_buttons_work_and_stale_taps_are_rejected_like_production(replay_http, replay_session, leaks):
    """Buttons carry nonce-encoded ids; tapping the current one works, replaying an old one is 'expired'."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    kurti = await replay_session.scalar(select(Product).where(Product.client_id == c.id, Product.name == "Cotton Kurti"))
    first = await sim(replay_http, h, kurti.sku)
    buttons = [b for bub in first.json()["bubbles"] for b in bub["buttons"]]
    rows = [r for bub in first.json()["bubbles"] for r in bub["list_rows"]]
    tappable = buttons or rows
    assert tappable, f"the product card should offer a tappable choice: {first.json()}"
    assert "~" in tappable[0]["id"]                                              # action~conversation~nonce, like WhatsApp

    again = await sim(replay_http, h, kurti.sku)                                   # a new card rotates the nonce
    assert again.status_code == 200
    kw = {"button_id": tappable[0]["id"]} if buttons else {"list_row_id": tappable[0]["id"]}
    stale = await sim(replay_http, h, title=tappable[0]["title"], **kw)
    assert stale.status_code == 200 and "expired" in texts(stale).lower()


async def test_a_real_customer_on_the_same_shop_is_unaffected(replay_http, replay_session, leaks):
    """Simulator and real customers share a shop but never share conversations, customers or lists."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=True, upi=False)
    c.bot_active = True
    c.account_status = "active"
    c.whatsapp_phone_number_id = "PN-REAL"
    await replay_session.commit()
    from tests.replay.helpers import send_message

    await sim(replay_http, h, "hi")
    await send_message(replay_http, "919800011111", "hello", phone_number_id="PN-REAL")

    convs = (await replay_http.get("/conversations", headers=h)).json()
    assert [cv["phone_number"] for cv in convs] == ["919800011111"]
    assert [cu["phone"] for cu in (await replay_http.get("/customers", headers=h)).json()] == ["919800011111"]
    assert await replay_session.scalar(select(func.count()).select_from(ConversationUsageLog)) == 1     # only the real customer
    assert PASSWORD


async def test_removing_samples_after_a_simulated_order_still_works(replay_http, replay_session, leaks):
    """A simulator order references a sample product; 'Remove samples' must not fail on it (the order keeps its snapshot)."""
    h, c = await setup_shop(replay_http, replay_session, accepts_cod=False, upi=True)
    await prime_order(replay_session, c)
    await drive_order(replay_http, h)

    r = await replay_http.delete("/onboarding/demo-products", headers=h)

    assert r.status_code == 200 and r.json() == {"removed": 3}
    order = await replay_session.scalar(select(Order).where(Order.client_id == c.id).execution_options(populate_existing=True))
    assert order is not None and order.product_id is None and order.product_name == "Handmade Tote Bag"
