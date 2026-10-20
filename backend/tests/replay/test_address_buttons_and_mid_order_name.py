"""
Two things a customer sees at the saved-address question (live screenshot, conv=60):

  1. "Amit, 702 Somerest, Ahmedabad pe deliver karein? (haan/change)" must carry Yes / Change BUTTONS
     (tapping "Change" is mapped to the text "change" by id, whatever language the label is in).
  2. Typing the exact name of ANOTHER product ("Mirror Work Linen Saree") there is a product reference, not an
     answer: the customer gets that product's card + "Switch to X, add both, or keep Y?" (Switch / Add both / Keep current),
     and a "yes" switches the order, a "no" resumes the address question.

Real webhook -> real Postgres, router off and on (the exact name is a router fast path, the guard sits before it).
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

from tests.replay.conftest import WA_PHONE_NUMBER_ID, seed_client_and_product
from tests.replay.helpers import send_message as _send_message

pytestmark = pytest.mark.asyncio

_wamid_counter = itertools.count(1)


async def send_message(http, phone: str, text: str):
    """Send one text with a unique wamid (the helper's default is per-second, so rapid turns would be de-duplicated)."""
    return await _send_message(http, phone, text, wamid=f"wamid.abm.{phone}.{next(_wamid_counter)}")


def _capture_with_buttons(monkeypatch) -> list[dict]:
    """Record every outbound reply as {"text", "buttons": [(action, title)]} (plain text has no buttons)."""
    sent: list[dict] = []

    async def _text(to_phone_number, message_text, phone_number_id=None, access_token=None):
        sent.append({"text": message_text, "buttons": []})

    async def _buttons(to_phone_number, body_text, buttons, phone_number_id=None, access_token=None):
        sent.append({"text": body_text, "buttons": [(b["id"].split("~")[0], b["title"]) for b in buttons]})
        return True

    async def _list(to_phone_number, header_text, body_text, button_text, sections, phone_number_id=None, access_token=None):
        sent.append({"text": body_text, "buttons": []})
        return True

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _buttons)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _list)
    return sent


async def _seed(session, phone: str, *, router_on: bool):
    """COD client with two products and a returning customer who has a saved name + address."""
    from app.models.customer import Customer
    from app.models.product import Product

    client, first = await seed_client_and_product(
        session, phone=phone, wa_phone_number_id=WA_PHONE_NUMBER_ID,
        product_sku="SR93722", product_name="Festive Banarasi Silk Saree", price=3849.0, stock=5, payment_method="COD",
    )
    second = Product(
        client_id=client.id, name="Mirror Work Linen Saree", sku="SR55001", price=2599.0, stock=5, is_active=True,
    )
    session.add(second)
    client.router_v2_enabled = router_on
    session.add(Customer(
        client_id=client.id, phone=phone, name="Amit", address="702 Somerest, Ahmedabad", total_orders=2, total_spent=5000.0,
    ))
    await session.commit()
    return client, first, second


async def _conv(session, phone: str):
    """The sender's conversation row, re-read from the database."""
    from app.models.conversation import Conversation

    return (await session.execute(
        select(Conversation).where(Conversation.phone_number == phone).execution_options(populate_existing=True)
    )).scalar_one()


async def _to_address_question(http, phone: str, sent: list[dict]) -> None:
    """SKU -> yes -> quantity: the next question is the saved-address offer."""
    for text in ("SR93722", "yes", "1"):
        resp = await send_message(http, phone, text)
        assert resp.status_code == 200, resp.text
    assert "deliver" in sent[-1]["text"].lower() or "pe " in sent[-1]["text"].lower(), sent[-1]


@pytest.mark.parametrize("router_on", [False, True])
async def test_saved_address_offer_has_yes_and_change_buttons(replay_http, replay_session, monkeypatch, router_on):
    """The saved-address question is a button message: Yes (offer_yes) + Change (addr_change)."""
    phone = f"91990000080{int(router_on)}"
    await _seed(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)

    await _to_address_question(replay_http, phone, sent)

    ids = [b[0] for b in sent[-1]["buttons"]]
    assert ids == ["offer_yes", "addr_change"], f"saved-address offer has no Yes/Change buttons: {sent[-1]}"


async def test_change_button_asks_for_a_new_address(replay_http, replay_session, monkeypatch):
    """Tapping the Change button (id addr_change) behaves like typing "change"."""
    from tests.replay.helpers import send_button

    phone = "919900000803"
    await _seed(replay_session, phone, router_on=False)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    resp = await send_button(replay_http, phone, "addr_change", "Change", wamid=f"wamid.abm.btn.{next(_wamid_counter)}")
    assert resp.status_code == 200, resp.text

    conv = await _conv(replay_session, phone)
    assert conv.delivery_address is None
    assert "address" in sent[-1]["text"].lower(), sent[-1]


@pytest.mark.parametrize("router_on", [False, True])
async def test_product_name_at_address_question_shows_card_and_switch_offer(
    replay_http, replay_session, monkeypatch, router_on,
):
    """"Mirror Work Linen Saree" at the saved-address question -> its card + switch offer with Yes/No buttons."""
    phone = f"91990000081{int(router_on)}"
    await _seed(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    await send_message(replay_http, phone, "Mirror Work Linen Saree")

    reply = sent[-1]
    assert "Mirror Work Linen Saree [SR55001]" in reply["text"], f"product card missing: {reply}"
    assert "₹2,599" in reply["text"]
    assert "keep Festive Banarasi Silk Saree?" in reply["text"], f"switch offer missing: {reply}"
    assert [b[0] for b in reply["buttons"]] == ["switch_product", "add_both", "keep_current"]
    conv = await _conv(replay_session, phone)
    assert conv.interrupted_sku == "SR55001"
    assert conv.pending_product_sku == "SR93722", "the order must not switch before the customer says yes"


async def test_yes_after_switch_offer_switches_the_order(replay_http, replay_session, monkeypatch):
    """"yes" to the offer pins the named product and the order restarts for it."""
    phone = "919900000820"
    await _seed(replay_session, phone, router_on=False)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)
    await send_message(replay_http, phone, "Mirror Work Linen Saree")

    await send_message(replay_http, phone, "yes")

    conv = await _conv(replay_session, phone)
    assert conv.pending_product_sku == "SR55001"
    assert conv.current_stage == "order_collection"
    assert "pieces" in sent[-1]["text"].lower() or "quantity" in sent[-1]["text"].lower(), sent[-1]


async def test_no_after_switch_offer_resumes_the_address_question(replay_http, replay_session, monkeypatch):
    """"no" keeps the original order and re-asks the saved-address question."""
    phone = "919900000821"
    await _seed(replay_session, phone, router_on=False)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)
    await send_message(replay_http, phone, "Mirror Work Linen Saree")

    await send_message(replay_http, phone, "no")

    conv = await _conv(replay_session, phone)
    assert conv.pending_product_sku == "SR93722"
    assert conv.current_stage == "order_collection"
    assert conv.interrupted_sku is None
    assert "Somerest" in sent[-1]["text"], f"address question not resumed: {sent[-1]}"
    assert [b[0] for b in sent[-1]["buttons"]] == ["offer_yes", "addr_change"], sent[-1]


@pytest.mark.parametrize("router_on", [False, True])
@pytest.mark.parametrize("text", ["SR55001", "sr55001", "SR 55001"])
async def test_other_product_sku_at_address_question_shows_card_and_switch_offer(
    replay_http, replay_session, monkeypatch, router_on, text,
):
    """A bare SKU of ANOTHER product at the saved-address question is a product reference, not an address."""
    phone = f"91990000083{int(router_on)}{['SR55001', 'sr55001', 'SR 55001'].index(text)}"
    await _seed(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    await send_message(replay_http, phone, text)

    reply = sent[-1]
    assert "Mirror Work Linen Saree [SR55001]" in reply["text"], f"product card missing: {reply}"
    assert "keep Festive Banarasi Silk Saree?" in reply["text"], f"switch offer missing: {reply}"
    assert "address" not in reply["text"].lower().replace("saved address", ""), f"treated as address: {reply}"
    assert [b[0] for b in reply["buttons"]] == ["switch_product", "add_both", "keep_current"]
    conv = await _conv(replay_session, phone)
    assert conv.interrupted_sku == "SR55001"
    assert conv.pending_product_sku == "SR93722"
    assert (conv.slot_attempt_count or 0) == 0


async def test_no_after_sku_switch_offer_resumes_the_address_question(replay_http, replay_session, monkeypatch):
    """SKU -> "no" keeps the original order and re-asks the saved-address question."""
    phone = "919900000840"
    await _seed(replay_session, phone, router_on=True)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)
    await send_message(replay_http, phone, "SR55001")

    await send_message(replay_http, phone, "no")

    conv = await _conv(replay_session, phone)
    assert conv.pending_product_sku == "SR93722"
    assert conv.interrupted_sku is None
    assert "Somerest" in sent[-1]["text"], sent[-1]


async def _seed_sold_out(session, phone: str, *, router_on: bool):
    """The usual shop plus a sold-out second product (SR55001 "Mirror Work Linen Saree", stock 0)."""
    _client, _first, second = await _seed(session, phone, router_on=router_on)
    second.stock = 0
    await session.commit()


@pytest.mark.parametrize("router_on", [False, True])
@pytest.mark.parametrize("text", ["SR55001", "Mirror Work Linen Saree"])
async def test_sold_out_product_at_quantity_step_is_told_and_quantity_is_re_asked(
    replay_http, replay_session, monkeypatch, router_on, text,
):
    """Live screenshot: "SR25027" (sold out) at "Quantity?" -> "Sorry, … out of stock." + "Quantity?" again, order intact."""
    phone = f"91990000085{int(router_on)}{int(text == 'SR55001')}"
    await _seed_sold_out(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    for t in ("SR93722", "yes", "yes"):         # product -> order -> quantity question
        await send_message(replay_http, phone, t)
    assert sent[-1]["text"] == "Quantity?", sent[-1]

    await send_message(replay_http, phone, text)

    reply = sent[-1]["text"]
    assert "Mirror Work Linen Saree is currently out of stock" in reply, reply
    assert reply.rstrip().endswith("Quantity?"), f"open question not re-asked: {reply}"
    assert "other products" not in reply
    conv = await _conv(replay_session, phone)
    assert conv.pending_product_sku == "SR93722" and conv.current_stage == "order_collection"
    assert conv.pending_order_quantity is None

    await send_message(replay_http, phone, "2")        # the order carries on from exactly where it was
    conv = await _conv(replay_session, phone)
    assert conv.pending_order_quantity == 2
    assert "Somerest" in sent[-1]["text"], sent[-1]


@pytest.mark.parametrize("router_on", [False, True])
@pytest.mark.parametrize("text", ["SR55001", "Mirror Work Linen Saree"])
async def test_sold_out_product_at_address_question_is_told_and_address_offer_is_re_asked(
    replay_http, replay_session, monkeypatch, router_on, text,
):
    """At the saved-address question a sold-out product gets the sold-out line + the Yes/Change offer again."""
    phone = f"91990000086{int(router_on)}{int(text == 'SR55001')}"
    await _seed_sold_out(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    await send_message(replay_http, phone, text)

    reply = sent[-1]
    assert "Mirror Work Linen Saree is currently out of stock" in reply["text"], reply
    assert "Somerest" in reply["text"] and [b[0] for b in reply["buttons"]] == ["offer_yes", "addr_change"], reply
    conv = await _conv(replay_session, phone)
    assert conv.pending_product_sku == "SR93722" and conv.interrupted_sku is None
    assert (conv.slot_attempt_count or 0) == 0


@pytest.mark.parametrize("router_on", [False, True])
async def test_every_turn_logs_a_trace_with_user_reply_and_the_decision_path(
    replay_http, replay_session, monkeypatch, caplog, router_on,
):
    """A real webhook turn emits ONE "TURN conv=… USER/REPLY/WHY" block naming the guard that produced the reply."""
    import logging

    phone = f"91990000087{int(router_on)}"
    await _seed(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    with caplog.at_level(logging.INFO, logger="app"):
        caplog.clear()
        await send_message(replay_http, phone, "SR55001")

    blocks = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TURN conv=")]
    assert len(blocks) == 1, blocks
    block = blocks[0]
    assert "stage=order_collection→awaiting_order_switch_confirm" in block, block
    assert "USER : 'SR55001'" in block
    assert "Mirror Work Linen Saree [SR55001]" in block and "[buttons: Switch | Add both | Keep current]" in block
    assert "order_pipeline: Mid-order product name: conv=" in block and "named=SR55001" in block, f"decision path missing: {block}"


@pytest.mark.parametrize("router_on", [False, True])
@pytest.mark.parametrize("text", ["SR93722", "sr93722", "Festive Banarasi Silk Saree"])
async def test_own_product_pasted_at_address_question_shows_card_and_re_asks_the_address_offer(
    replay_http, replay_session, monkeypatch, router_on, text,
):
    """Real order (quantity given): the product already being ordered, pasted again, is not an address and not a canned note."""
    phone = f"91990000088{int(router_on)}{['SR93722', 'sr93722', 'Festive Banarasi Silk Saree'].index(text)}"
    await _seed(replay_session, phone, router_on=router_on)
    sent = _capture_with_buttons(monkeypatch)
    await _to_address_question(replay_http, phone, sent)

    await send_message(replay_http, phone, text)

    reply = sent[-1]
    assert "Festive Banarasi Silk Saree [SR93722] — ₹3,849" in reply["text"], reply
    assert "still open" not in reply["text"] and "type 'cancel'" not in reply["text"]
    assert "Somerest" in reply["text"] and [b[0] for b in reply["buttons"]] == ["offer_yes", "addr_change"], reply
    conv = await _conv(replay_session, phone)
    assert conv.pending_order_quantity == 1 and conv.pending_product_sku == "SR93722"
    assert (conv.slot_attempt_count or 0) == 0
