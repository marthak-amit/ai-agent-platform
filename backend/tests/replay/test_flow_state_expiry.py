"""
Replay tests for flow-state / context expiry (migration 0052).

Conversations previously had no time-based state expiry: a customer who went
silent mid-flow (e.g. `awaiting_final_confirmation` with a product pinned)
and returned weeks later would have that stale flow resumed against a bare
"hi". These tests characterize the fix:

  1. Greeting after a >24h flow_state gap resets to a fresh welcome +
     catalogue, instead of resuming the stale stage/pinned product.
  2. A pronoun reference ("is that available") within the 5-day CONTEXT_TTL
     is answered directly from last_context, without a flow restart.
  3. The same pronoun reference falls through to normal flow dispatch once
     last_context itself has expired (>5 days).
  4. An ordinary in-flow turn (<24h, no greeting/pronoun) is unaffected —
     regression guard for the new early guard block.

All tests run against a real Postgres instance (replay_ci_test) and are
skipped automatically when local Postgres is not reachable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


def _phone(suffix: str) -> str:
    return f"91930000{suffix}"


def _pnid(suffix: str) -> str:
    return f"444{suffix}"


async def _seed(session: AsyncSession, *, phone: str, pnid: str, **kwargs):
    return await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=pnid, **kwargs
    )


async def _prime_conv(session: AsyncSession, *, phone: str, product, stage: str, **fields):
    """Insert a conversation with pre-set stage/slots/expiry timestamps."""
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage=stage,
        **fields,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def _get_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


async def _msg(http_client, phone: str, text: str, *, pnid: str):
    return await send_message(http_client, phone, text, phone_number_id=pnid)


# ===========================================================================
# 1. Greeting after >24h flow_state gap -> fresh catalogue, no stale resume
# ===========================================================================

async def test_greeting_after_expired_flow_state_resets_not_resumes(replay_http, replay_session):
    """
    Conversation was left mid-order (awaiting_final_confirmation, product
    pinned) with flow_state_at >24h in the past. A bare "hi" must reset to a
    fresh welcome + catalogue, NOT resume the stale confirmation step.
    """
    phone = _phone("0001")
    pnid = _pnid("0001")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="EXP001", product_name="Expiry Test Kurta",
        price=1999.0, stock=8, payment_method="COD",
    )
    stale_at = datetime.now(timezone.utc) - timedelta(hours=25)
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="awaiting_final_confirmation",
        pending_product_sku=product.sku,
        pending_order_quantity=1,
        customer_name="Stale Customer",
        delivery_address="Some old address",
        summary_shown=True,
        flow_state_at=stale_at,
    )

    from app.services import whatsapp_service, gemini_service
    whatsapp_service._raw_send_text_message.reset_mock()
    ai_calls_before = gemini_service.generate_reply.call_count

    resp = await _msg(replay_http, phone, "hi", pnid=pnid)
    assert resp.status_code == 200, resp.text

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting", (
        f"Expired flow_state must reset to 'greeting' on a bare greeting, got {conv.current_stage!r}"
    )
    assert conv.pending_product_sku is None, "Stale pending_product_sku must be cleared, not resumed"
    assert conv.pending_order_quantity is None
    assert conv.summary_shown is False

    call_args, call_kwargs = whatsapp_service._raw_send_text_message.call_args_list[-1]
    reply_text = call_kwargs.get("message_text") or (call_args[1] if len(call_args) > 1 else "")
    assert "catalogue" in reply_text.lower(), f"Expected fresh catalogue welcome, got: {reply_text!r}"
    assert "confirm" not in reply_text.lower(), (
        f"Must not resume the stale awaiting_final_confirmation step, got: {reply_text!r}"
    )

    # Deterministic template — no AI call for this path.
    assert gemini_service.generate_reply.call_count == ai_calls_before, (
        "generate_reply was called — flow-state-expiry greeting reset did not skip the LLM"
    )


# ===========================================================================
# 2. Pronoun reference within CONTEXT_TTL -> direct answer, no flow restart
# ===========================================================================

async def test_pronoun_reference_within_context_ttl_answers_directly(replay_http, replay_session):
    """
    "Is that available?" 12h after the last product reference (well within
    both the 24h FLOW_STATE_TTL and 5-day CONTEXT_TTL) must be answered
    directly from last_context — no stage change, no flow restart.
    """
    phone = _phone("0002")
    pnid = _pnid("0002")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="EXP002", product_name="Context Test Saree",
        price=2499.0, stock=6, payment_method="COD",
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=12)
    last_context = {
        "product_id": product.id, "sku": product.sku,
        "name": product.name, "price": product.price,
    }
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="greeting",
        flow_state_at=recent,
        last_context=last_context,
        last_context_at=recent,
    )

    from app.services import gemini_service
    ai_calls_before = gemini_service.generate_reply.call_count

    resp = await _msg(replay_http, phone, "is that available", pnid=pnid)
    assert resp.status_code == 200, resp.text

    conv = await _get_conv(replay_session, conv_id)
    assert conv.current_stage == "greeting", "Direct pronoun answer must not change stage / restart flow"
    assert conv.pending_product_sku is None, "Direct pronoun answer must not pin/restart an order"

    from app.services import whatsapp_service
    call_args, call_kwargs = whatsapp_service._raw_send_text_message.call_args_list[-1]
    reply_text = call_kwargs.get("message_text") or (call_args[1] if len(call_args) > 1 else "")
    assert "context test saree" in reply_text.lower(), f"Expected direct product answer, got: {reply_text!r}"
    assert "available" in reply_text.lower() or "stock" in reply_text.lower()

    assert gemini_service.generate_reply.call_count == ai_calls_before, (
        "generate_reply was called — pronoun-reference direct answer did not skip the LLM"
    )


# ===========================================================================
# 3. Pronoun reference after CONTEXT_TTL expired -> falls through to normal flow
# ===========================================================================

async def test_pronoun_reference_after_context_expired_falls_through(replay_http, replay_session):
    """
    Same "is that available?" message, but last_context_at is >5 days old —
    the direct-answer short-circuit must NOT fire; the message proceeds
    through normal flow dispatch (mocked LLM reply) instead.
    """
    phone = _phone("0003")
    pnid = _pnid("0003")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="EXP003", product_name="Stale Context Saree",
        price=1899.0, stock=4, payment_method="COD",
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=6)
    expired_context = datetime.now(timezone.utc) - timedelta(days=6)
    last_context = {
        "product_id": product.id, "sku": product.sku,
        "name": product.name, "price": product.price,
    }
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="greeting",
        flow_state_at=recent,
        last_context=last_context,
        last_context_at=expired_context,
    )

    resp = await _msg(replay_http, phone, "is that available", pnid=pnid)
    assert resp.status_code == 200, resp.text

    from app.services import whatsapp_service
    call_args, call_kwargs = whatsapp_service._raw_send_text_message.call_args_list[-1]
    reply_text = call_kwargs.get("message_text") or (call_args[1] if len(call_args) > 1 else "")
    assert "stale context saree is available at" not in reply_text.lower(), (
        f"Expired last_context must NOT produce the direct-answer template, got: {reply_text!r}"
    )

    conv = await _get_conv(replay_session, conv_id)
    # last_context snapshot itself is untouched by the (skipped) short-circuit.
    assert conv.last_context is not None


# ===========================================================================
# 4. Regression guard: ordinary in-flow turn (<24h, no greeting/pronoun)
# ===========================================================================

async def test_normal_inflow_turn_unaffected_by_expiry_guard(replay_http, replay_session):
    """
    An ordinary slot-filling turn, well within FLOW_STATE_TTL and containing
    neither a bare greeting nor a reference pronoun, must behave exactly as
    before — the new early guard block must not intercept it.
    """
    phone = _phone("0004")
    pnid = _pnid("0004")
    _, product = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="EXP004", product_name="Normal Flow Kurta",
        price=899.0, stock=12, payment_method="COD",
    )
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product,
        stage="order_collection",
        pending_product_sku=product.sku,
        pending_order_quantity=2,
        customer_name="Regular Customer",
        flow_state_at=recent,
    )

    resp = await _msg(replay_http, phone, "42 MG Road, Pune, 411001", pnid=pnid)
    assert resp.status_code == 200, resp.text

    conv = await _get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == product.sku, "Normal in-flow turn must not be reset by the expiry guard"
    assert conv.delivery_address is not None, "Address slot must still be filled normally"
    assert "mg road" in conv.delivery_address.lower()
