"""
Replay regression tests for side-questions asked at the
awaiting_final_confirmation (AFC) stage.

Bug: a customer asking about a specific, named product ("is PR17761
available?") at AFC fell through KB search + the deterministic fact table
(neither of which does a SKU/name catalogue lookup) straight into a raw,
context-free LLM call that was explicitly instructed to "say you'll check
and get back" if unsure. With zero product context, the model always
produced that filler, and the code's own max_tokens=60 cap cut it off
mid-sentence (worse for non-English scripts, which cost more tokens per
word) — the customer received a broken, incomplete, meaningless reply.

Fix: run_summary_confirmation() now tries the same catalog SKU/name-lookup
+ availability-answer tier FIX2/FIX4 already use at order_collection,
before falling to KB/fact-table, and the raw LLM fallback has been removed
entirely in favor of a localized "I don't have that information" template.
"""

from __future__ import annotations

import time
import unittest.mock as mock

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


async def _add_second_product(session: AsyncSession, *, client_id: int, sku: str, name: str, price: float, stock: int = 7):
    from app.models.product import Product
    product = Product(
        client_id=client_id, name=name, sku=sku, price=price,
        stock=stock, is_active=True, has_variants=False,
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product


async def _prime_conv(session: AsyncSession, *, phone: str, product, **fields):
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage="awaiting_final_confirmation",
        pending_product_sku=product.sku,
        summary_shown=True,
        **fields,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def _reload_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


@pytest.mark.asyncio
async def test_availability_question_about_named_product_answers_from_catalog(
    replay_http, replay_session
):
    """
    "Is <other product's SKU> available?" at AFC must be answered from the
    catalog (real stock/variant facts about THAT product), never from an
    LLM guess, and must NOT touch conversation state (still AFC, still
    pinned to the original product, summary_shown unchanged).
    """
    suffix = "Q001"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product_a = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku="PR17761", product_name="Kurti New One", price=239.0, stock=10,
    )
    product_b = await _add_second_product(
        replay_session, client_id=client.id, sku="SR27754", name="Traditional Choli",
        price=899.0, stock=5,
    )

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product_a,
        customer_name="Meera Shah", delivery_address="21 Ring Road, Ahmedabad",
        pending_order_quantity=1, payment_method="COD",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "openai.resources.chat.completions.AsyncCompletions.create",
        side_effect=AssertionError("raw LLM fallback must never be called — catalog lookup should answer this"),
    ):
        resp = await send_message(
            replay_http, phone, f"Is {product_b.sku} available?",
            wamid=f"wamid.q001.avail.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    conv = await _reload_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_final_confirmation", "Aside question must not advance/change stage"
    assert conv.pending_product_sku == product_a.sku, "Aside question must not switch the pinned product"
    assert conv.summary_shown is True

    assert sent_texts, "Expected a reply to be sent"
    reply = sent_texts[-1]
    assert "Traditional Choli" in reply or "available" in reply.lower(), (
        f"Expected a real catalog-grounded answer about {product_b.sku}, got: {reply!r}"
    )
    # The historical bug's exact filler text must never appear.
    assert "લાગશે" not in reply and "check and get back" not in reply.lower()


@pytest.mark.asyncio
async def test_gujarati_script_named_sku_question_gets_grounded_answer(
    replay_http, replay_session
):
    """
    Exact reported scenario: a Gujarati-script question naming a specific
    SKU ("PR17761 ઉપલબ્ધ છે?") must get a real, complete, catalog-grounded
    reply — never the truncated LLM filler ("હવે મને કેટલીક સેકન્ડ
    લાગશે, પછી...") this test guards against, and no raw LLM call at all.
    """
    suffix = "Q002"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product_a = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku="KU76326", product_name="Kurti New One", price=239.0, stock=10,
    )
    product_b = await _add_second_product(
        replay_session, client_id=client.id, sku="PR17761", name="Traditional Choli",
        price=899.0, stock=5,
    )

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product_a,
        customer_name="Riya Patel", delivery_address="9 Satellite Road, Ahmedabad",
        pending_order_quantity=1, payment_method="COD",
        last_customer_language="gujarati_script",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "openai.resources.chat.completions.AsyncCompletions.create",
        side_effect=AssertionError("raw LLM fallback must never be called for a named-SKU question"),
    ):
        resp = await send_message(
            replay_http, phone, f"શું {product_b.sku} ઉપલબ્ધ છે?",
            wamid=f"wamid.q002.avail.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    conv = await _reload_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_final_confirmation"
    assert conv.pending_product_sku == product_a.sku

    assert sent_texts, "Expected a reply to be sent"
    reply = sent_texts[-1]
    assert product_b.name in reply, f"Expected a grounded answer naming {product_b.name!r}, got: {reply!r}"
    assert "લાગશે" not in reply, "Historical filler text must never leak to the customer again"


@pytest.mark.asyncio
async def test_unanswerable_aside_question_uses_localized_template_not_llm(
    replay_http, replay_session
):
    """
    A side-question with no catalog/KB/fact-table answer must fall back to
    the localized "I don't have that information" template — never to a
    raw LLM guess (which is how the filler-text bug was introduced).
    """
    suffix = "Q003"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product_a = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku="GF99881", product_name="Festive Lehenga", price=1499.0, stock=4,
    )

    conv_id = await _prime_conv(
        replay_session, phone=phone, product=product_a,
        customer_name="Anjali Mehta", delivery_address="5 Park Street, Surat",
        pending_order_quantity=1, payment_method="COD",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "openai.resources.chat.completions.AsyncCompletions.create",
        side_effect=AssertionError("no deterministic answer exists — must use the localized fallback, not an LLM guess"),
    ):
        resp = await send_message(
            replay_http, phone, "Do you offer gift wrapping for this?",
            wamid=f"wamid.q003.nowrap.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    conv = await _reload_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_final_confirmation"

    assert sent_texts, "Expected a reply to be sent"
    reply = sent_texts[-1]
    from app.services.language_templates import get_template
    assert get_template("english", "aside_question_no_info") in reply
