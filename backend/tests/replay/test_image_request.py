"""
Replay regression test for the "send me a photo" image-request handler.

Phase 2: a customer asking for a product photo mid-order ("give me images")
must get the photo as its own message, followed by the existing product-card
text as a fully separate message — and none of it may touch order state
(pinned SKU, stage, slot attempt count, or the still-pending slot value).
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


async def _prime_conv(session: AsyncSession, *, phone: str, product):
    """Insert a conversation mid order_collection with only quantity pending."""
    from app.models.conversation import Conversation

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage="order_collection",
        pending_product_sku=product.sku,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv.id


async def _reload_conv(session: AsyncSession, conv_id: int):
    from app.models.conversation import Conversation

    r = await session.execute(
        select(Conversation)
        .where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


@pytest.mark.asyncio
async def test_image_request_sends_image_then_card_and_leaves_state_untouched(
    replay_http, replay_session
):
    """
    "give me images" for the pinned product sends its photo, then a separate
    text message with the product card + re-asked (quantity) slot — pinned
    SKU, stage, and slot attempt count are all unchanged afterward. Order
    intent for the pinned product is already confirmed (pending_product_sku
    is only ever set via a confirmed pin), so the card must NOT re-ask
    "Would you like to order?" — only the still-pending slot question.
    """
    suffix = "I001"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku=f"IM{suffix}", product_name="Photo Test Saree",
        image_url="https://media.example.com/1/1/photo.jpg",
    )
    conv_id = await _prime_conv(replay_session, phone=phone, product=product)

    sent_texts: list[str] = []
    sent_images: list[tuple] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "app.services.whatsapp_service._raw_send_image_message",
        side_effect=lambda to_phone_number, image_url, caption=None, **kw: sent_images.append(
            (image_url, caption)
        ),
    ):
        resp = await send_message(
            replay_http, phone, "give me images",
            wamid=f"wamid.i001.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    assert sent_images == [("https://media.example.com/1/1/photo.jpg", None)], (
        "Expected exactly one image send, no caption (a separate text message follows)"
    )
    assert sent_texts, "Expected a separate text reply"
    reply = sent_texts[-1]
    assert product.name in reply
    assert "Would you like to order" not in reply, (
        "order intent for the already-pinned product was already confirmed — "
        "the card must not re-ask it"
    )
    assert "Quantity?" in reply, "must still re-ask the actual pending slot"

    conv = await _reload_conv(replay_session, conv_id)
    assert conv.pending_product_sku == product.sku, "Pinned SKU must not change"
    assert conv.current_stage == "order_collection", "Stage must not change"
    assert (conv.slot_attempt_count or 0) == 0, "Answering an aside must not burn a slot attempt"
    assert conv.pending_order_quantity is None, "Quantity slot must still be unanswered"


@pytest.mark.asyncio
async def test_image_request_with_no_image_url_falls_back_to_text_only(
    replay_http, replay_session
):
    """A product with no image_url set must not error — just skip the image, text-only."""
    suffix = "I002"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku=f"IM{suffix}", product_name="No Photo Saree",
        image_url=None,
    )
    conv_id = await _prime_conv(replay_session, phone=phone, product=product)

    sent_texts: list[str] = []
    sent_images: list[tuple] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "app.services.whatsapp_service._raw_send_image_message",
        side_effect=lambda to_phone_number, image_url, caption=None, **kw: sent_images.append(
            (image_url, caption)
        ),
    ):
        resp = await send_message(
            replay_http, phone, "show photo please",
            wamid=f"wamid.i002.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    assert sent_images == [], "No image_url — must not attempt an image send"
    assert sent_texts, "Must still send the text-only product card"
    assert product.name in sent_texts[-1]

    conv = await _reload_conv(replay_session, conv_id)
    assert conv.pending_product_sku == product.sku
    assert conv.current_stage == "order_collection"


@pytest.mark.asyncio
async def test_image_request_with_invalid_image_url_falls_back_to_text_only(
    replay_http, replay_session
):
    """
    A product whose image_url is a non-empty but malformed/placeholder value
    (not a real http(s) URL) must not be treated as a real image send —
    "sent=True" in the log would be misleading otherwise. Regression test:
    the handler previously used bare truthiness instead of _is_valid_image_url().
    """
    suffix = "I003"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product = await seed_client_and_product(
        replay_session, phone=phone, wa_phone_number_id=pnid,
        product_sku=f"IM{suffix}", product_name="Bad URL Saree",
        image_url="pending-upload",
    )
    conv_id = await _prime_conv(replay_session, phone=phone, product=product)

    sent_texts: list[str] = []
    sent_images: list[tuple] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ), mock.patch(
        "app.services.whatsapp_service._raw_send_image_message",
        side_effect=lambda to_phone_number, image_url, caption=None, **kw: sent_images.append(
            (image_url, caption)
        ),
    ):
        resp = await send_message(
            replay_http, phone, "send pic",
            wamid=f"wamid.i003.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    assert sent_images == [], "Malformed image_url must not be treated as a real image send"
    assert sent_texts, "Must still send the text-only product card"
    assert product.name in sent_texts[-1]


@pytest.mark.asyncio
async def test_browsing_stage_image_request_no_llm_call(replay_session):
    """
    Browsing-stage counterpart of the order_collection image-request handler:
    when the customer's message is an image request AND we already know
    which product they mean (pinned_product from an earlier turn), it must
    be answered deterministically — no tier-3 LLM call, regardless of how
    short/typo'd the phrasing is ("pic", "Image", etc.). Regression test for
    the reported cost bug: these messages were previously falling through to
    generate_reply(), which embeds the full catalogue in the system prompt.
    """
    from app.services.order_pipeline import run_llm_routing

    client, product = await seed_client_and_product(
        replay_session, phone=_phone("I004"), wa_phone_number_id=_pnid("I004"),
        product_sku="IMI004", product_name="Browsing Photo Saree", price=999.0,
        image_url="https://media.example.com/browsing/photo.jpg",
    )
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=_phone("I004"), channel="whatsapp", client_id=client.id,
        current_stage="product_inquiry",
    )
    replay_session.add(conv)
    await replay_session.commit()
    await replay_session.refresh(conv)

    with mock.patch(
        "app.services.gemini_service.generate_reply",
        side_effect=AssertionError("must not call the LLM for a resolvable image request"),
    ):
        for text in ("pic", "Image", "Cna you please share images"):
            outcome = await run_llm_routing(
                db=replay_session, conv=conv, client=client, user_text=text,
                stage="product_inquiry", language="english", history_dicts=[],
                system_prompt="", catalogue_context="", _canonical_browse_products=[],
                pinned_product=product, variant_info={}, _pick_just_resolved=False,
                _llm_budget="normal", _llm_calls_today=0, _name_match_count=0,
            )
            assert not outcome.llm_called, f"{text!r} must be answered without an LLM call"
            assert product.name in outcome.text, f"{text!r} reply should show the product card"
            assert outcome.pre_images == [(product.image_url, None)], (
                f"{text!r} should queue the product's photo"
            )
