"""
Replay regression test for free-text product switches at the
awaiting_final_confirmation (AFC) stage.

Bug: previously, only the 1/2/3 button replies were handled at AFC. Free
text (e.g. a customer typing a different product's code to swap the item)
fell through with no match, and the bot silently re-sent the exact same
order summary verbatim ("dumb loop") — the catalog lookup that ran for the
message was never wired into a real product switch.

Fix: order_pipeline.run_summary_confirmation() now tries a catalog match
(SKU first, then name-score) on any free text reaching AFC. A confident
match overwrites the single active draft (product-specific slots reset,
name/address/payment kept) and drops back into order_collection for the
new item; no match returns an explicit escape hatch instead of re-echoing
the summary.
"""

from __future__ import annotations

import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import send_message


pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


def _phone(suffix: str) -> str:
    return f"91920000{suffix}"


def _pnid(suffix: str) -> str:
    return f"333{suffix}"


async def _seed_two_products(session: AsyncSession, *, suffix: str):
    """
    Seed one client with two simple (no-variant) products so a customer can
    switch from one to the other by SKU at the confirmation step.

    SKUs use a 5-digit numeric tail (not the alphanumeric test suffix) to
    match catalogue_service.SKU_PATTERN (2-4 letters + 4-6 digits) — the
    same shape as the bug report's "SR27754" example.

    Returns (client, product_a, product_b).
    """
    from app.models.client import Client
    from app.models.product import Product

    phone = _phone(suffix)
    sku_tail = f"{abs(hash(suffix)) % 100000:05d}"
    client = Client(
        business_name="AFC Switch Test Store",
        email=f"afc_switch_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=_pnid(suffix),
        accepts_cod=True,
        hashed_password="x",
    )
    session.add(client)
    await session.flush()

    product_a = Product(
        client_id=client.id,
        name="Original Saree",
        sku=f"OR{sku_tail}",
        price=1200.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    product_b = Product(
        client_id=client.id,
        name="Switch Target Saree",
        sku=f"SR{sku_tail}",
        price=999.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    session.add_all([product_a, product_b])
    await session.commit()
    await session.refresh(client)
    await session.refresh(product_a)
    await session.refresh(product_b)
    return client, product_a, product_b


async def _prime_conv(session: AsyncSession, *, phone: str, product, **slots):
    from app.models.conversation import Conversation

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage="awaiting_final_confirmation",
        pending_product_sku=product.sku,
        summary_shown=True,
        **slots,
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
async def test_valid_product_code_at_confirm_switches_draft_and_resets_slots(
    replay_http, replay_session
):
    """
    Customer at the AFC summary sends a valid product code for a DIFFERENT
    product instead of tapping 1/2/3. Expected:
      - pending_product_sku switches to the new product
      - current_stage drops back to order_collection (re-enter slot-fill)
      - quantity slot is reset (None) — product-specific, must be re-collected
      - summary_shown resets to False — old summary must not be reused
      - customer_name / delivery_address are KEPT (single draft, not re-asked)
      - reply does NOT re-echo the old product's summary
    """
    suffix = "P001"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product_a, product_b = await _seed_two_products(replay_session, suffix=suffix)

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product_a,
        customer_name="Kavita Rao",
        delivery_address="12 MG Road, Bengaluru",
        pending_order_quantity=2,
        payment_method="COD",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ):
        resp = await send_message(
            replay_http, phone, product_b.sku,
            wamid=f"wamid.p001.switch.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    conv = await _reload_conv(replay_session, conv_id)

    assert conv.pending_product_sku == product_b.sku, (
        f"Expected draft to switch to {product_b.sku!r}, got {conv.pending_product_sku!r}"
    )
    assert conv.current_stage == "order_collection", (
        "Switch must drop back into order_collection to re-collect product-specific slots"
    )
    assert conv.pending_order_quantity is None, "Quantity must reset for the new product"
    assert conv.summary_shown is False, "Old summary must be discarded, not reused"

    # Single active draft: previously-collected name/address are not re-asked.
    assert conv.customer_name == "Kavita Rao"
    assert conv.delivery_address == "12 MG Road, Bengaluru"

    assert sent_texts, "Expected a reply to be sent"
    reply = sent_texts[-1]
    assert product_a.name not in reply, "Reply must not re-echo the old product's summary"
    assert product_b.name in reply, "Reply should acknowledge the newly switched product"


@pytest.mark.asyncio
async def test_unrecognized_free_text_at_confirm_gives_escape_hatch_not_dumb_loop(
    replay_http, replay_session
):
    """
    Free text at AFC that matches no product and no button must NOT silently
    re-send the same summary verbatim — it must return an explicit escape
    hatch pointing back at 1/2/3 or a product name/code.
    """
    suffix = "P002"
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, product_a, _product_b = await _seed_two_products(replay_session, suffix=suffix)

    conv_id = await _prime_conv(
        replay_session,
        phone=phone,
        product=product_a,
        customer_name="Rohit Verma",
        delivery_address="9 Park Street, Kolkata",
        pending_order_quantity=1,
        payment_method="COD",
    )

    sent_texts: list[str] = []
    with mock.patch(
        "app.services.whatsapp_service._raw_send_text_message",
        side_effect=lambda to_phone_number, message_text, **kw: sent_texts.append(message_text),
    ):
        resp = await send_message(
            replay_http, phone, "asdkjqwe999",
            wamid=f"wamid.p002.gibberish.{time.time_ns()}",
            phone_number_id=pnid,
        )
    assert resp.status_code == 200

    conv = await _reload_conv(replay_session, conv_id)
    # Nothing should have moved — no match means no switch.
    assert conv.pending_product_sku == product_a.sku
    assert conv.current_stage == "awaiting_final_confirmation"

    assert sent_texts, "Expected a reply to be sent"
    reply_lower = sent_texts[-1].lower()
    assert "asdkjqwe999" in sent_texts[-1], "Escape hatch should echo back what wasn't understood"
    assert "1" in reply_lower and "2" in reply_lower and "3" in reply_lower, (
        "Escape hatch should point back at the 1/2/3 buttons"
    )
