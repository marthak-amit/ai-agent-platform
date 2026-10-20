"""Round-3 HTTP tests (real Postgres replay DB): background webhook processing, the admin LLM-cost endpoint."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.replay.helpers import send_message
from tests.scenarios.harness import Customer, capture_structured, seed_shop

ADMIN_KEY = "round3-admin-key"


@pytest.fixture
def background_mode(replay_db_url, monkeypatch):
    """Switch the webhook to background processing and give its tasks a session factory on the replay database."""
    from app.routers import webhook

    engine = create_async_engine(replay_db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhook, "_background_session_factory", lambda: factory)
    webhook.get_settings().webhook_background_processing = True
    yield webhook
    webhook._inflight_wamids.clear()


async def test_webhook_returns_immediately_dedupes_in_flight_and_processes_in_the_background(replay_http, replay_session, background_mode, monkeypatch):
    """verify + dedupe, answer 200 at once, run the turn in a background task (one at a time per sender); a retry of the same
    wamid while it is still running is dropped; the reply arrives after the turn finishes."""
    webhook = background_mode
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session)
    started, release = asyncio.Event(), asyncio.Event()
    real = webhook.handle_inbound_message

    async def gated(ctx):
        """Hold the turn until the test says go."""
        started.set()
        await release.wait()
        return await real(ctx)

    monkeypatch.setattr(webhook, "handle_inbound_message", gated)

    first = await send_message(replay_http, phone, "hi", wamid="wamid.bg.1", phone_number_id=pnid)
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    await asyncio.wait_for(started.wait(), 5)
    assert sent == []                                      # the HTTP response did not wait for the turn
    retry = await send_message(replay_http, phone, "hi", wamid="wamid.bg.1", phone_number_id=pnid)
    assert retry.status_code == 200 and retry.json()["status"] == "ok"      # duplicate of an in-flight message
    release.set()
    await webhook.wait_for_background_tasks()
    assert len(sent) == 1 and "Welcome to Riya Boutique" in sent[0].text


async def test_background_turns_of_one_sender_run_in_arrival_order(replay_http, replay_session, background_mode, monkeypatch):
    """Two quick messages from one customer are answered in order (a per-sender lock inside the background tasks)."""
    webhook = background_mode
    sent = capture_structured(monkeypatch)
    client, phone, pnid = await seed_shop(replay_session)
    await send_message(replay_http, phone, "hi", wamid="wamid.o.1", phone_number_id=pnid)
    await send_message(replay_http, phone, "KU10001", wamid="wamid.o.2", phone_number_id=pnid)
    await webhook.wait_for_background_tasks()
    assert [("Welcome" in o.text, "Cotton Kurti" in o.text) for o in sent][:2] == [(True, False), (False, True)]


async def test_admin_llm_cost_endpoint_totals_per_day_purpose_model_conversation_and_order(replay_http, replay_session, monkeypatch):
    """GET /admin/llm-cost: ₹ + $ totals, per day / purpose / model / conversation, cost per order and per order created."""
    from app.config import get_settings
    from app.models.llm_usage import LLMUsage
    from app.models.order import Order

    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: get_settings().model_copy(update=dict(admin_secret_key=ADMIN_KEY)))
    client, phone, pnid = await seed_shop(replay_session)
    me = Customer(replay_http, replay_session, capture_structured(monkeypatch), client, phone, pnid)
    await me.say("hi")
    conv = await me.conv()
    order = Order(order_number="ORD-2026-0001", client_id=client.id, conversation_id=conv.id, customer_name="A", customer_phone=phone,
                  delivery_address="x", product_name="Kurti", quantity=1, unit_price=499.0, total_amount=499.0, payment_method="COD", status="paid")
    replay_session.add(order)
    await replay_session.flush()
    for purpose, model, p, c, o, usd, oid in (
        ("planner", "openai/gpt-oss-120b", 4000, 1000, 200, 0.000645, order.id),
        ("planner", "openai/gpt-oss-120b", 2000, 0, 100, 0.000360, order.id),
        ("translate", "openai/gpt-oss-20b", 300, 0, 80, 0.000050, None),
    ):
        replay_session.add(LLMUsage(client_id=client.id, conversation_id=conv.id, order_id=oid, purpose=purpose, model=model,
                                    prompt_tokens=p, cached_tokens=c, completion_tokens=o, audio_seconds=0, cost_usd=usd, cost_inr=usd * 83,
                                    latency_ms=500, success=True))
    await replay_session.commit()

    resp = await replay_http.get("/admin/llm-cost", params={"client_id": client.id}, headers={"X-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"]["calls"] == 3 and body["totals"]["cached_tokens"] == 1000
    assert body["totals"]["cost_usd"] == pytest.approx(0.001055, abs=1e-6) and body["totals"]["cost_inr"] == pytest.approx(0.001055 * 83, abs=1e-4)
    assert {r["purpose"] for r in body["per_purpose"]} == {"planner", "translate"}
    assert {r["model"] for r in body["per_model"]} == {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}
    assert len(body["per_day"]) == 1 and body["per_conversation"][0]["conversation_id"] == conv.id
    assert body["per_order"][0]["order_number"] == "ORD-2026-0001" and body["per_order"][0]["cost_usd"] == pytest.approx(0.001005, abs=1e-6)
    assert body["orders_created"] == 1 and body["cost_per_order_created_usd"] == pytest.approx(0.001055, abs=1e-6)
    assert (await replay_http.get("/admin/llm-cost")).status_code in (401, 403)         # needs an admin credential


# ── cost display cleanup: session / lifetime from the ledger, backfill, per-client 7-day average ──

async def _seed_usage(session, client, conv, rows):
    """rows = [(hours_ago, inr, purpose)] → llm_usage rows with created_at = now - hours."""
    from datetime import timedelta

    from app.models.llm_usage import LLMUsage

    now = datetime.now(timezone.utc)
    for hours_ago, inr, purpose in rows:
        session.add(LLMUsage(client_id=client.id, conversation_id=conv.id, purpose=purpose, model="openai/gpt-oss-120b", prompt_tokens=1000,
                             completion_tokens=100, cost_inr=inr, cost_usd=inr / 83.0, latency_ms=100, success=True,
                             created_at=now - timedelta(hours=hours_ago)))
    await session.commit()


async def test_cost_summary_session_resets_only_at_confirm_order_or_24_hours(replay_http, replay_session, monkeypatch):
    """session = ₹ since the later of (the last CONFIRMED CART = confirm_order, 24 h ago). Orders created any other way, cancelled carts and
    anything else do not reset it; lifetime = everything; turn = rows written after mark_turn_start."""
    from datetime import timedelta

    from app.models.conversation_cart import ConversationCart
    from app.models.order import Order
    from app.services import llm_usage_service

    client, phone, pnid = await seed_shop(replay_session)
    me = Customer(replay_http, replay_session, capture_structured(monkeypatch), client, phone, pnid)
    await me.say("hi")
    conv = await me.conv()
    now = datetime.now(timezone.utc)

    def cart(status, hours_ago):
        """A closed cart of this conversation, last touched `hours_ago` hours ago."""
        return ConversationCart(conversation_id=conv.id, client_id=client.id, lines=[], customer={}, status=status, version=2, state={},
                                updated_at=now - timedelta(hours=hours_ago))

    # rows: 30 h ago ₹1, 10 h ago ₹2, 3 h ago ₹4, 1 h ago ₹8
    await _seed_usage(replay_session, client, conv, [(30, 1.0, "router"), (10, 2.0, "router"), (3, 4.0, "planner"), (1, 8.0, "planner")])
    summary = await llm_usage_service.cost_summary(replay_session, conv.id)
    assert summary["lifetime_inr"] == pytest.approx(15.0) and summary["session_inr"] == pytest.approx(14.0)      # nothing confirmed: last 24 h
    replay_session.add(cart("cancelled", 2))                              # a cancelled cart does not end the session
    replay_session.add(Order(order_number="ORD-X", client_id=client.id, conversation_id=conv.id, customer_name="A", customer_phone=phone, delivery_address="x",
                             product_name="K", quantity=1, unit_price=1.0, total_amount=1.0, payment_method="COD", status="paid",
                             created_at=now - timedelta(hours=2)))         # nor does an order that did not come from confirm_order
    await replay_session.commit()
    assert (await llm_usage_service.cost_summary(replay_session, conv.id))["session_inr"] == pytest.approx(14.0)
    replay_session.add(cart("confirmed", 5))                              # confirm_order 5 h ago
    await replay_session.commit()
    summary = await llm_usage_service.cost_summary(replay_session, conv.id)
    assert summary["session_inr"] == pytest.approx(12.0) and summary["lifetime_inr"] == pytest.approx(15.0)       # ₹4 + ₹8
    replay_session.add(cart("confirmed", 2))
    await replay_session.commit()
    assert (await llm_usage_service.cost_summary(replay_session, conv.id))["session_inr"] == pytest.approx(8.0)

    await llm_usage_service.mark_turn_start(replay_session, conv.id)
    await _seed_usage(replay_session, client, conv, [(0, 0.5, "planner"), (0, 0.25, "translate")])
    summary = await llm_usage_service.cost_summary(replay_session, conv.id)
    assert summary["turn"]["cost_inr"] == pytest.approx(0.75) and set(summary["turn"]["by_purpose"]) == {"planner", "translate"}
    assert summary["turn"]["by_purpose"]["planner"]["prompt_tokens"] == 1000 and summary["session_inr"] == pytest.approx(8.75)


async def test_backfill_cost_usd_fills_only_null_rows_and_is_idempotent(replay_http, replay_session, monkeypatch):
    """One-off backfill: cost_usd = cost_inr / USD_INR where it is NULL; reports the count; a second run touches nothing."""
    from sqlalchemy import select

    from app.models.llm_usage import LLMUsage
    from app.services import llm_usage_service

    client, phone, pnid = await seed_shop(replay_session)
    me = Customer(replay_http, replay_session, capture_structured(monkeypatch), client, phone, pnid)
    await me.say("hi")
    conv = await me.conv()
    await _seed_usage(replay_session, client, conv, [(1, 8.3, "router")])
    replay_session.add(LLMUsage(client_id=client.id, conversation_id=conv.id, purpose="router", model="m", prompt_tokens=1, completion_tokens=1,
                                cost_inr=83.0, cost_usd=None, latency_ms=1, success=True))
    replay_session.add(LLMUsage(client_id=client.id, conversation_id=conv.id, purpose="router", model="m", prompt_tokens=1, completion_tokens=1,
                                cost_inr=41.5, cost_usd=None, latency_ms=1, success=True))
    await replay_session.commit()
    assert await llm_usage_service.backfill_cost_usd(replay_session, dry_run=True) == 2
    assert await llm_usage_service.backfill_cost_usd(replay_session) == 2
    rows = (await replay_session.execute(select(LLMUsage.cost_inr, LLMUsage.cost_usd).where(LLMUsage.model == "m").order_by(LLMUsage.cost_inr))).all()
    assert [(float(i), round(float(u), 6)) for i, u in rows] == [(41.5, 0.5), (83.0, 1.0)]       # USD_INR = 83 in the suite
    assert await llm_usage_service.backfill_cost_usd(replay_session) == 0
    kept = (await replay_session.execute(select(LLMUsage.cost_usd).where(LLMUsage.purpose == "router", LLMUsage.model != "m"))).scalar_one()
    assert float(kept) == pytest.approx(8.3 / 83.0)                                              # existing values untouched


async def test_admin_llm_cost_has_avg_inr_per_confirmed_order_per_client_for_7_days(replay_http, replay_session, monkeypatch):
    """per_client_last_7d: LLM ₹ / confirmed orders (not cancelled, not sandbox) of the last 7 days, per client; older rows / orders ignored."""
    from datetime import timedelta

    from app.config import get_settings
    from app.models.order import Order

    monkeypatch.setattr("app.routers.admin_deps.get_settings", lambda: get_settings().model_copy(update=dict(admin_secret_key=ADMIN_KEY)))
    client, phone, pnid = await seed_shop(replay_session)
    me = Customer(replay_http, replay_session, capture_structured(monkeypatch), client, phone, pnid)
    await me.say("hi")
    conv = await me.conv()
    now = datetime.now(timezone.utc)
    await _seed_usage(replay_session, client, conv, [(24, 3.0, "planner"), (48, 1.0, "router"), (24 * 8, 100.0, "router")])       # the ₹100 row is 8 days old
    for number, days_ago, status, sandbox in (("O1", 1, "paid", False), ("O2", 2, "pending_payment", False), ("O3", 3, "cancelled", False),
                                              ("O4", 1, "paid", True), ("O5", 9, "paid", False)):
        replay_session.add(Order(order_number=number, client_id=client.id, conversation_id=conv.id, customer_name="A", customer_phone=phone, delivery_address="x",
                                 product_name="K", quantity=1, unit_price=1.0, total_amount=1.0, payment_method="COD", status=status, is_sandbox=sandbox,
                                 created_at=now - timedelta(days=days_ago)))
    await replay_session.commit()

    body = (await replay_http.get("/admin/llm-cost", headers={"X-Admin-Key": ADMIN_KEY})).json()
    [row] = [r for r in body["per_client_last_7d"] if r["client_id"] == client.id]
    assert row["llm_calls"] == 2 and row["cost_inr"] == pytest.approx(4.0) and row["confirmed_orders"] == 2
    assert row["avg_inr_per_confirmed_order"] == pytest.approx(2.0) and row["business_name"] == "Riya Boutique"
    one = (await replay_http.get("/admin/llm-cost", params={"client_id": client.id}, headers={"X-Admin-Key": ADMIN_KEY})).json()
    assert [r["client_id"] for r in one["per_client_last_7d"]] == [client.id]
