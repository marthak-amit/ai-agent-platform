"""
A customer who comes back and types the name of the product already on offer must see its details first.

Live failure (conv=60): the order was left open yesterday with the only in-stock colour auto-selected
("Yeh sirf Grey color mein hai"). Today the customer typed "Festive Banarasi silk saree" and was asked
"Kitne pieces chahiye?" — the stale open question — instead of the product card + "Would you like to order?".
A colour / size alone is not a real order in progress; a quantity or payment step is.

Reproduces production: router v2 on (exact name = ₹0 fast path), a saved customer name and the stale
"Kitne pieces chahiye?" as the last assistant message. Real webhook -> real Postgres; the conversation is primed straight into order_collection.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.replay.conftest import WA_PHONE_NUMBER_ID
from tests.replay.helpers import capture_all, send_message

pytestmark = pytest.mark.asyncio


async def _seed(session, phone: str, *, router_on: bool = True):
    """COD client + a saree whose Black variant is sold out and Grey has one piece left (the conv=60 shape)."""
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Saree Store",
        email=f"emptyref_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=WA_PHONE_NUMBER_ID,
        accepts_cod=True,
        hashed_password="x",
        router_v2_enabled=router_on,
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id, name="Festive Banarasi Silk Saree", sku="SR93722", price=3849.0,
        stock=1, is_active=True, has_variants=True,
    )
    session.add(product)
    await session.flush()
    session.add_all([
        ProductVariant(product_id=product.id, client_id=client.id, color="Black", stock=0, is_active=True),
        ProductVariant(product_id=product.id, client_id=client.id, color="Grey", stock=1, is_active=True),
    ])
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


async def _prime(session, *, phone, product, **fields):
    """Create a Conversation already in order_collection with the product pinned."""
    from app.models.conversation import Conversation
    from app.models.message import Message

    conv = Conversation(
        phone_number=phone, channel="whatsapp", client_id=product.client_id,
        current_stage="order_collection", pending_product_sku=product.sku, summary_shown=False,
        last_customer_language="hindi_roman", customer_name="Amit", **fields,
    )
    session.add(conv)
    await session.flush()
    # The stale question the customer left yesterday: the last assistant line in the history.
    session.add(Message(conversation_id=conv.id, role="user", content="Hey"))
    session.add(Message(conversation_id=conv.id, role="assistant", content="Kitne pieces chahiye?"))
    await session.commit()
    return conv.id


async def _reload(session, conv_id: int):
    """Re-read the conversation row, bypassing the identity map."""
    from app.models.conversation import Conversation

    return (await session.execute(
        select(Conversation).where(Conversation.id == conv_id).execution_options(populate_existing=True)
    )).scalar_one()


async def test_product_name_with_auto_selected_colour_shows_card_first(replay_http, replay_session, monkeypatch):
    """Colour auto-filled, quantity still open: the exact product name shows the card + the order offer."""
    phone = "919900000701"
    _client, product = await _seed(replay_session, phone)
    conv_id = await _prime(replay_session, phone=phone, product=product, selected_color="Grey")
    captured = capture_all(monkeypatch)

    await send_message(replay_http, phone, "Festive Banarasi silk saree")

    text = "\n".join(t or "" for t in captured)
    assert "Festive Banarasi Silk Saree [SR93722]" in text, f"product card missing: {captured}"
    assert "₹3,849" in text
    assert "Kitne pieces" not in text, f"stale quantity question was re-asked instead of the card: {captured}"
    conv = await _reload(replay_session, conv_id)
    assert conv.current_stage != "order_collection", "still collecting an order after only the product name"
    assert conv.pending_product_sku == "SR93722"


async def test_yes_after_card_resumes_order_at_quantity(replay_http, replay_session, monkeypatch):
    """After the card, "yes" re-enters the order and the auto-selected colour is kept (quantity is asked)."""
    phone = "919900000702"
    _client, product = await _seed(replay_session, phone)
    conv_id = await _prime(replay_session, phone=phone, product=product, selected_color="Grey")
    captured = capture_all(monkeypatch)

    await send_message(replay_http, phone, "Festive Banarasi silk saree")
    captured.clear()
    await send_message(replay_http, phone, "yes")

    conv = await _reload(replay_session, conv_id)
    assert conv.current_stage == "order_collection"
    assert conv.selected_color == "Grey"
    assert any("pieces" in (t or "").lower() for t in captured), f"quantity question not asked after yes: {captured}"


async def test_product_name_with_quantity_given_keeps_the_order(replay_http, replay_session, monkeypatch):
    """A real order in progress (quantity already given) is NOT reset by typing the product name."""
    phone = "919900000703"
    _client, product = await _seed(replay_session, phone)
    conv_id = await _prime(
        replay_session, phone=phone, product=product, selected_color="Grey", pending_order_quantity=1,
    )
    capture_all(monkeypatch)

    await send_message(replay_http, phone, "Festive Banarasi silk saree")

    conv = await _reload(replay_session, conv_id)
    assert conv.current_stage == "order_collection"
    assert conv.pending_order_quantity == 1
