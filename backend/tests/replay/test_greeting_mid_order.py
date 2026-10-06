"""
Replay tests: a bare greeting ("Hi") while an order is in progress.

A mid-order greeting must be answered deterministically (never by the LLM):
  - fresh state  -> greeting + a resume line for the pending step
                    ("Welcome back! You were looking at {product} — {question}
                    Or type a new product name.");
  - state older than the flow-state expiry -> reset and the normal welcome.
State is never advanced and no slot attempt is consumed by the greeting.

Runs against real Postgres (skipped automatically when it is not reachable).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product, seed_variant_and_simple
from tests.replay.helpers import send_message

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

AI_SENTINEL = "__AI_REPLY__"


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay (asyncio.sleep(random.uniform(min, max)))."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


def _phone(suffix: str) -> str:
    return f"91940000{suffix}"


def _pnid(suffix: str) -> str:
    return f"555{suffix}"


async def _prime_conv(session: AsyncSession, *, phone: str, client_id: int, stage: str, **fields) -> int:
    """Insert a conversation with a pre-set stage / pinned product / timestamps."""
    from app.models.conversation import Conversation

    conv = Conversation(phone_number=phone, channel="whatsapp", client_id=client_id, current_stage=stage, **fields)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def _get_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation

    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id).execution_options(populate_existing=True)
    )
    return r.scalar_one()


async def _say_hi(http, phone: str, pnid: str, text: str = "Hi") -> tuple[str, int]:
    """Send a greeting; return (all text the bot sent, number of LLM reply calls it triggered)."""
    from app.services import gemini_service, whatsapp_service as ws

    ws._raw_send_text_message.reset_mock()
    calls_before = gemini_service.generate_reply.call_count
    resp = await send_message(http, phone, text, phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    parts = []
    for args, kwargs in ws._raw_send_text_message.call_args_list:
        parts.append(kwargs.get("message_text") or (args[1] if len(args) > 1 else ""))
    return "\n".join(parts), gemini_service.generate_reply.call_count - calls_before


async def test_hi_in_product_inquiry_resumes_pending_slot_without_llm(replay_http, replay_session):
    """product_inquiry, variant product pinned, colour still unfilled -> welcome-back + product + colour question."""
    phone, pnid = _phone("0001"), _pnid("0001")
    client, variant, _ = await seed_variant_and_simple(replay_session, phone=phone, wa_phone_number_id=pnid)
    conv_id = await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
        pending_product_sku=variant.sku, flow_state_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and AI_SENTINEL not in reply
    assert "Welcome back" in reply
    assert "Cotton Lehenga" in reply
    assert "Colour?" in reply and "Pink" in reply and "Blue" in reply  # the pending slot question
    assert "new product name" in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "product_inquiry"          # state untouched
    assert conv.pending_product_sku == variant.sku           # product still pinned
    assert not getattr(conv, "slot_attempt_count", 0)        # greeting burned no attempt


async def test_hi_in_order_collection_resumes_pending_slot_without_llm(replay_http, replay_session):
    """order_collection with next_slot=colour -> deterministic re-ask of the colour question, state untouched."""
    phone, pnid = _phone("0002"), _pnid("0002")
    client, variant, _ = await seed_variant_and_simple(replay_session, phone=phone, wa_phone_number_id=pnid)
    conv_id = await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection",
        pending_product_sku=variant.sku, flow_state_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and AI_SENTINEL not in reply
    assert "Colour?" in reply and "Pink" in reply
    assert "Welcome back" in reply or "welcome back" in reply.lower()
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "order_collection" and conv.pending_product_sku == variant.sku


async def test_hi_in_product_inquiry_without_pending_slot_resumes_on_product(replay_http, replay_session):
    """Simple product (no slot to ask yet) -> welcome-back naming the product + invitation to type another."""
    phone, pnid = _phone("0003"), _pnid("0003")
    client, product = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid, product_sku="GM003", product_name="Silk Stole",
    )
    conv_id = await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
        pending_product_sku=product.sku, flow_state_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and AI_SENTINEL not in reply
    assert "Welcome back" in reply and "Silk Stole" in reply and "new product name" in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "product_inquiry" and conv.pending_product_sku == product.sku


async def test_hi_after_flow_state_expiry_resets_and_sends_normal_welcome(replay_http, replay_session):
    """In-progress state older than the flow-state TTL -> reset + normal welcome, no resume line, no LLM."""
    phone, pnid = _phone("0004"), _pnid("0004")
    client, variant, _ = await seed_variant_and_simple(replay_session, phone=phone, wa_phone_number_id=pnid)
    conv_id = await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
        pending_product_sku=variant.sku, flow_state_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and AI_SENTINEL not in reply
    assert "Welcome to" in reply and "catalogue" in reply.lower()
    assert "You were looking at" not in reply and "Colour?" not in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting" and conv.pending_product_sku is None


async def test_hi_in_order_collection_after_expiry_resets_and_sends_normal_welcome(replay_http, replay_session):
    """Same expiry rule for order_collection (the other mid-order stage)."""
    phone, pnid = _phone("0005"), _pnid("0005")
    client, variant, _ = await seed_variant_and_simple(replay_session, phone=phone, wa_phone_number_id=pnid)
    conv_id = await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection",
        pending_product_sku=variant.sku, flow_state_at=datetime.now(timezone.utc) - timedelta(hours=30),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and "Welcome to" in reply and "Colour?" not in reply
    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting" and conv.pending_product_sku is None


async def test_hi_resume_is_localised(replay_http, replay_session):
    """A Hindi-roman conversation gets the Hindi resume template (not English)."""
    phone, pnid = _phone("0006"), _pnid("0006")
    client, variant, _ = await seed_variant_and_simple(replay_session, phone=phone, wa_phone_number_id=pnid)
    await _prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
        pending_product_sku=variant.sku, last_customer_language="hindi_roman",
        flow_state_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    reply, llm_calls = await _say_hi(replay_http, phone, pnid)

    assert llm_calls == 0 and "Cotton Lehenga" in reply
    assert "Welcome back" not in reply and "new product name" not in reply
