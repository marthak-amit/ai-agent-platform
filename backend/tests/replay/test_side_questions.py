"""
Replay tests: side questions and social messages in the middle of a conversation.

Reproduces the production screenshot — a customer at the colour question asks about the return policy (twice,
in Roman Gujarati). The bot used to answer with an unrelated product card, then ignore the question and re-ask the
colour. Now every such message is answered from the shop's own facts (never an invented policy) and the open step
is resumed without touching the order state — for router shops and keyword shops alike, with no LLM call.
"""

from __future__ import annotations

import pytest

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all
from tests.replay.test_router_v2 import _no_reply_delay, get_conv, messages, ph, router, say, seed_shop  # noqa: F401

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

RETURN_Q = "ketla diwas ma return kari sakai ka aebi kai policy nathi?"
RETURN_Q2 = "mare return policy joi che"


async def _at_colour_question(http, captured, session, router_on, n):
    """Seed a shop and bring a Roman-Gujarati customer to the lehenga colour question."""
    phone, pnid = ph(n)
    client, _, _ = await seed_shop(session, phone, pnid, router_on=router_on)
    await say(http, captured, phone, pnid, "LH10042")
    reply = await say(http, captured, phone, pnid, "Yes")
    assert "Colo" in reply, reply
    return phone, pnid, client


@pytest.mark.parametrize("router_on", [True, False], ids=["router", "keyword"])
async def test_return_question_at_colour_step_is_answered_honestly_and_colour_is_re_asked(
    replay_http, replay_session, router, monkeypatch, router_on,
):
    """The screenshot: no KB policy → honest hand-off offer (in Gujarati) + the colour question; no product card; state kept."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, router_on, "1" if router_on else "2")

    reply = await say(replay_http, captured, phone, pnid, RETURN_Q)

    assert "talk to someone" in reply                        # rt_handoff_offer, Roman Gujarati
    assert "pakku jawab nathi" in reply
    assert "Colo" in reply and "Pink" in reply               # the open question is resumed
    assert "order karva" not in reply.lower() and "₹" not in reply     # no product card / offer
    assert "7 days" not in reply                             # never an invented policy
    assert not router.calls                                  # ₹0: no router / LLM call
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "LH10042" and conv.current_stage == "order_collection"

    reply2 = await say(replay_http, captured, phone, pnid, RETURN_Q2)
    assert "talk to someone" in reply2 and "Colo" in reply2

    nxt = await say(replay_http, captured, phone, pnid, "Pink")      # the order continues exactly where it was
    assert "size" in nxt.lower() or "saaiz" in nxt.lower() or "M" in nxt


@pytest.mark.parametrize("router_on", [True, False], ids=["router", "keyword"])
async def test_sellers_knowledge_base_answers_the_return_question(
    replay_http, replay_session, router, monkeypatch, router_on,
):
    """When the seller wrote a return-policy FAQ, that exact answer is used (and the colour is re-asked)."""
    from app.models.knowledge_base import KnowledgeBase

    captured = capture_all(monkeypatch)
    phone, pnid, client = await _at_colour_question(replay_http, captured, replay_session, router_on, "3" if router_on else "4")
    replay_session.add(KnowledgeBase(client_id=client.id, question="What is your return policy?",
                                     answer="Exchange within 7 days of delivery, unworn with tags.", is_active=True, is_approved=True))
    await replay_session.commit()

    reply = await say(replay_http, captured, phone, pnid, RETURN_Q2)

    assert "Exchange within 7 days of delivery, unworn with tags." in reply
    assert "Colo" in reply
    assert not router.calls


async def test_thanks_and_positive_reaction_get_a_friendly_line_then_the_question(
    replay_http, replay_session, router, monkeypatch,
):
    """'thank you' and 'nice' mid-order: acknowledged, and the colour question comes back."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, True, "5")

    thanks = await say(replay_http, captured, phone, pnid, "thank you")
    assert "welcome" in thanks.lower() and "Colo" in thanks
    nice = await say(replay_http, captured, phone, pnid, "very nice")
    assert "Glad you like it" in nice and "Colo" in nice
    assert not router.calls


async def test_think_later_is_not_pushed_with_the_question_again(replay_http, replay_session, router, monkeypatch):
    """'I will think about it' → polite 'take your time', no re-ask nag."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, True, "6")

    reply = await say(replay_http, captured, phone, pnid, "I will think about it")

    assert "take your time" in reply.lower() and "Colo" not in reply
    assert not router.calls


async def test_cod_and_payment_questions_use_the_shops_real_settings(replay_http, replay_session, router, monkeypatch):
    """COD available? → yes (the seed shop accepts COD); after turning COD off the same question says no."""
    captured = capture_all(monkeypatch)
    phone, pnid, client = await _at_colour_question(replay_http, captured, replay_session, True, "7")

    yes = await say(replay_http, captured, phone, pnid, "Is COD available?")
    assert "Cash on Delivery (COD) is available" in yes and "Colo" in yes
    how = await say(replay_http, captured, phone, pnid, "how can I pay?")
    assert "UPI or Cash on Delivery" in how

    client.accepts_cod = False
    await replay_session.commit()
    no = await say(replay_http, captured, phone, pnid, "Is COD available?")
    assert "don't offer Cash on Delivery" in no and "Colo" in no
    assert not router.calls


async def test_delivery_time_comes_from_the_shop_not_a_hardcoded_line(replay_http, replay_session, router, monkeypatch):
    """The seller's delivery window (3–7 days in the seed) is quoted; changing it changes the answer."""
    captured = capture_all(monkeypatch)
    phone, pnid, client = await _at_colour_question(replay_http, captured, replay_session, True, "8")

    first = await say(replay_http, captured, phone, pnid, "how many days for delivery?")
    assert "3–7 business days" in first
    client.delivery_days_min, client.delivery_days_max = 2, 4
    await replay_session.commit()
    second = await say(replay_http, captured, phone, pnid, "delivery kitne din mein hogi?")
    assert "2–4" in second and "Colo" in second


async def test_a_plain_slot_answer_and_a_bare_cod_are_not_side_questions(replay_http, replay_session, router, monkeypatch):
    """'Pink' / 'COD' keep driving the order; they must never be swallowed by the side-question layer."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, False, "9")

    reply = await say(replay_http, captured, phone, pnid, "Pink")
    assert "Colo" not in reply          # accepted → moved on to the next question
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert (conv.selected_color or "").lower() == "pink"


async def test_greeting_stage_thanks_has_no_resume_and_does_not_start_anything(replay_http, replay_session, router, monkeypatch):
    """'thanks' with nothing open: just the friendly line."""
    captured = capture_all(monkeypatch)
    phone, pnid = ph("10")
    await seed_shop(replay_session, phone, pnid, router_on=True)

    reply = await say(replay_http, captured, phone, pnid, "thanks!")

    assert "welcome" in reply.lower() and "?" not in reply
    assert not router.calls


@pytest.mark.parametrize("router_on", [True, False], ids=["router", "keyword"])
async def test_a_full_order_survives_side_questions_at_every_step(replay_http, replay_session, router, monkeypatch, router_on):
    """Questions, thanks, 'ok' and 'nice' between every step never change the order: it completes with the right data."""
    import app.routers.webhook as wh
    from sqlalchemy import select
    from app.models.order import Order

    monkeypatch.setattr(wh, "_RATE_LIMIT_MESSAGES", 10_000)
    captured = capture_all(monkeypatch)
    phone, pnid = ph("11" if router_on else "12")
    await seed_shop(replay_session, phone, pnid, router_on=router_on)
    script = [
        ("LH10042", None), ("Yes", None),
        ("return policy?", "Colo"), ("Pink", None),
        ("thanks", "Size"), ("M", None),
        ("is COD available?", "Quantity"), ("1", None),
        ("why do you need my name?", "name"), ("Hiren Patel", None),
        ("ok", None),                                  # 'ok' at the address question is not an address
        ("9 Ellis Bridge, Ahmedabad 380006", None),
        ("how can I pay?", "Payment"), ("COD", None),
    ]
    for text, expect in script:
        reply = await say(replay_http, captured, phone, pnid, text)
        if expect:
            assert expect.lower() in reply.lower(), (text, reply)
    summary = captured[-1]
    assert "Order Summary" in "\n".join(captured[-2:]) or "Summary" in summary
    conv_id = (await messages(replay_session, 1))[0].conversation_id
    conv = await get_conv(replay_session, conv_id)
    assert (conv.selected_color, conv.selected_size, conv.pending_order_quantity) == ("Pink", "M", 1)
    assert conv.customer_name == "Hiren Patel" and "Ellis Bridge" in (conv.delivery_address or "")
    await say(replay_http, captured, phone, pnid, "Yes")           # confirm via text
    orders = (await replay_session.execute(select(Order).where(Order.conversation_id == conv_id))).scalars().all()
    assert len(orders) == 1 and orders[0].customer_name == "Hiren Patel"


async def test_negative_and_neutral_replies_do_not_become_invalid_options(replay_http, replay_session, router, monkeypatch):
    """'no' / 'ok' / thumbs-up at the colour question: a gentle line + the question, never "we don't have ok in colour"."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, True, "13")

    no = await say(replay_http, captured, phone, pnid, "not interested")
    assert "reply 'cancel'" in no and "Colo" in no and "don't have" not in no
    ok = await say(replay_http, captured, phone, pnid, "hmm")
    assert ok.startswith("Sure!") and "Colo" in ok
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "LH10042" and conv.slot_attempt_count in (0, None)     # not counted as failed attempts
    assert not router.calls


async def test_cancel_at_an_order_step_cancels_without_any_llm(replay_http, replay_session, router, monkeypatch):
    """'cancel' while picking the colour ends the draft deterministically (works when the LLM is down)."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, False, "14")

    reply = await say(replay_http, captured, phone, pnid, "I want to cancel this order")

    assert "cancel" in reply.lower()
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku is None and conv.current_stage == "greeting"
    assert not router.calls


@pytest.mark.parametrize("router_on", [True, False], ids=["router", "keyword"])
async def test_human_request_pauses_the_bot_and_tells_the_customer(replay_http, replay_session, router, monkeypatch, router_on):
    """'talk to someone' (the phrase our own hand-off offer teaches) at any step → bot paused + 'team will reply'."""
    captured = capture_all(monkeypatch)
    phone, pnid, _ = await _at_colour_question(replay_http, captured, replay_session, router_on, "15" if router_on else "16")

    reply = await say(replay_http, captured, phone, pnid, "talk to someone")

    assert "team will reply" in reply.lower()
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.ai_enabled is False and conv.bot_pause_source == "escalation"
    assert not router.calls


async def test_change_product_request_offers_the_catalogue_and_keeps_the_open_question(replay_http, replay_session, router, monkeypatch):
    """'show me something else' mid-order: the shop's catalogue link, then the colour question again (order untouched)."""
    captured = capture_all(monkeypatch)
    phone, pnid, client = await _at_colour_question(replay_http, captured, replay_session, True, "17")

    reply = await say(replay_http, captured, phone, pnid, "show me something else")

    assert f"/{client.catalogue_slug}" in reply and "Colo" in reply
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.pending_product_sku == "LH10042"


async def test_side_question_at_the_summary_re_shows_the_summary_with_confirm_buttons(replay_http, replay_session, router, monkeypatch):
    """At 'Confirm your order?' a question is answered and the summary comes back; 'wait' pauses without losing it."""
    import app.routers.webhook as wh

    monkeypatch.setattr(wh, "_RATE_LIMIT_MESSAGES", 10_000)
    captured = capture_all(monkeypatch)
    phone, pnid = ph("18")
    await seed_shop(replay_session, phone, pnid, router_on=True)
    for m in ["LH10042", "Yes", "Pink", "M", "1", "Hiren Patel", "9 Ellis Bridge, Ahmedabad 380006", "COD"]:
        await say(replay_http, captured, phone, pnid, m)

    ask = await say(replay_http, captured, phone, pnid, "what is the return policy?")
    assert "talk to someone" in ask and "Order Summary" in ask
    wait = await say(replay_http, captured, phone, pnid, "wait")
    assert "take your time" in wait.lower()
    conv = await get_conv(replay_session, (await messages(replay_session, 1))[0].conversation_id)
    assert conv.current_stage == "awaiting_final_confirmation"
    done = await say(replay_http, captured, phone, pnid, "Yes")
    assert "placed" in done.lower() or "order" in done.lower()
