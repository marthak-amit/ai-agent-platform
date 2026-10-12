"""
Regression suite for conversation 60 (bugs A–I from the production transcript).

Two kinds of tests live here:

* Pure unit tests (no database): the per-unit breakdown parser, negation-aware payment intent, the payment
  help matcher, the Groq ``json_validate_failed`` retry, lenient JSON extraction, ``ensure_reply`` (the
  ``NO_REPLY`` guarantee) and the stage-aware fallback wording.
* Replay tests: real webhook -> pipeline -> Postgres, replaying the transcript turn by turn with a scripted
  LLM (router timeout / invalid-JSON mocks included). They assert reply text, stage, slots and order status.
  Skipped automatically when local Postgres is not reachable.

Bug map (see each test's docstring):
  A fake "payment received" reply        B per-unit variant split lost       C order detached on denial
  D silent / wrong reply (NO_REPLY)       E invalid option drops the flow      F "no" repeats the product card
  G LLM JSON failures / router timeout    H stale open-order nudge             I name-match pin wipes name/address
"""

from __future__ import annotations

import itertools
import logging
import time
import types
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

import httpx
import openai
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

# Fixtures reused from the replay harness (module-scoped throwaway Postgres + the ASGI client).
from tests.replay.conftest import (  # noqa: F401  (fixtures are used by name)
    _PG_AVAILABLE,
    replay_db_url,
    replay_http,
    replay_session,
)
from tests.replay.helpers import (
    TINY_PNG,
    send_button,
    send_image,
    send_message,
)
from tests.replay.test_router_v2 import ScriptedRouter, get_conv, messages, prime_conv  # noqa: F401

needs_pg = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")


@pytest_asyncio.fixture(autouse=True)
async def _clean_replay_db(request):
    """
    Truncate the replay database before every test that uses it (same job as tests/replay/conftest.py's
    autouse fixture) — but stay out of the way of the pure unit tests in this module, which need no Postgres.
    """
    if not ({"replay_http", "replay_session"} & set(request.fixturenames)):
        yield
        return
    url = request.getfixturevalue("replay_db_url")
    engine = create_async_engine(url, echo=False)
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
        tables = [t for t in names if t not in ("alembic_version", "plans", "billing_plans")]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(sa.text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
            await conn.commit()
    await engine.dispose()
    yield


# ═════════════════════════════════════════════════════════════════════════════
# Pure unit tests
# ═════════════════════════════════════════════════════════════════════════════

VI = {
    "needs_color": True, "needs_size": True, "needs_material": False,
    "available_colors": ["Black", "Blue", "Red"], "available_sizes": ["L", "M", "S"],
}


def _bd(text, **kw):
    """parse_variant_breakdown with Blue/M already collected (the transcript's state)."""
    from app.services import conversation_flow as cf

    return cf.parse_variant_breakdown(text, VI, base_color=kw.get("color", "Blue"), base_size=kw.get("size", "M"))


def test_B_transcript_message_parses_to_two_line_items_keeping_colour():
    """'I want 2 L and one M size and colour same' -> [{Blue,L,2},{Blue,M,1}] (words, sizes, 'same colour')."""
    res = _bd("I want 2 L and one M size and colour same")
    assert res.invalid == []
    assert res.items == [
        {"color": "Blue", "size": "L", "qty": 2},
        {"color": "Blue", "size": "M", "qty": 1},
    ]


@pytest.mark.parametrize("text,expected", [
    ("L x 2, M x 1", [("Blue", "L", 2), ("Blue", "M", 1)]),
    ("L 2 M 1", [("Blue", "L", 2), ("Blue", "M", 1)]),
    ("do L aur ek M", [("Blue", "L", 2), ("Blue", "M", 1)]),
    ("2 pcs L, 1 pc S", [("Blue", "L", 2), ("Blue", "S", 1)]),
    ("1 blue L and 2 red M", [("Blue", "L", 1), ("Red", "M", 2)]),
    ("1 black S 1 red M 1 red L", [("Black", "S", 1), ("Red", "M", 1), ("Red", "L", 1)]),
    ("2 L, 1 L and 1 M", [("Blue", "L", 3), ("Blue", "M", 1)]),     # duplicate lines are merged
])
def test_B_breakdown_phrasings(text, expected):
    """Digits/number words, either order, per-line colours, Hinglish 'do'/'ek' and merged duplicates."""
    res = _bd(text)
    assert [(i["color"], i["size"], i["qty"]) for i in res.items] == expected


@pytest.mark.parametrize("text", ["same", "different", "3 M", "2 L", "it's 2 L", "hello there", "I do want it"])
def test_B_non_breakdowns_are_left_alone(text):
    """Plain answers and single-group messages are not splits (they keep their normal handling)."""
    assert _bd(text) is None


def test_B_size_the_product_does_not_carry_is_reported_not_dropped():
    """'2 XL and 1 M' on an L/M/S product names a size we lack -> invalid=['XL'], never silently '1 M'."""
    res = _bd("2 XL and 1 M")
    assert res.items == [] and res.invalid == ["XL"]


def test_B_incomplete_split_is_not_guessed():
    """When colour is required and neither the text nor the order so far gives one, nothing is invented."""
    from app.services import conversation_flow as cf

    assert cf.parse_variant_breakdown("2 L and 1 M", VI) is None


def test_B_numeric_sizes_are_never_treated_as_quantities():
    """Catalogues with sizes '38'/'40' make digits ambiguous: the deterministic parser steps aside."""
    from app.services import conversation_flow as cf

    vi = {**VI, "available_sizes": ["38", "40"]}
    assert cf.parse_variant_breakdown("2 38 and 1 40", vi, base_color="Blue") is None
    assert cf.looks_like_variant_breakdown("2 38 and 1 40", vi) is False


def test_B_looks_like_and_describe():
    """looks_like gates the LLM fallback; describe_breakdown renders the echo ('2 L + 1 M')."""
    from app.services import conversation_flow as cf

    assert cf.looks_like_variant_breakdown("I want 2 L and one M", VI)
    assert not cf.looks_like_variant_breakdown("same for all", VI)
    assert cf.describe_breakdown([{"color": "Blue", "size": "L", "qty": 2}, {"color": "Blue", "size": "M", "qty": 1}]) == "2 L + 1 M"
    assert cf.describe_breakdown(
        [{"color": "Blue", "size": "L", "qty": 1}, {"color": "Red", "size": "M", "qty": 2}]
    ) == "1 Blue L + 2 Red M"


@pytest.mark.parametrize("text,paid,help_", [
    ("I did not send payment screenshot yet", False, True),
    ("I already given you order give me payment details", False, True),
    ("not paid", False, True),
    ("I have not paid", False, True),
    ("payment nahi kiya", False, True),
    ("kaise pay karu", False, True),
    ("upi?", False, True),
    ("QR", False, True),
    ("paid", True, False),
    ("payment kar diya", True, False),
    ("done", True, False),
    ("what is the price", False, False),
])
def test_C_paid_intent_is_negation_aware_and_payment_help_is_recognised(text, paid, help_):
    """'not paid' is not a payment claim; payment-detail/denial wording is routed to the instruction resend."""
    from app.services import payment_verification_service as pvs

    assert pvs.is_paid_intent(text) is paid
    assert pvs.is_payment_help_request(text) is help_


def test_G_first_json_block_ignores_prose_braces_and_strings():
    """The FIRST balanced {...} is taken (string-aware), not first-'{'-to-last-'}'."""
    from app.services import llm_client

    raw = 'Sure! {"action":"greeting","args":{"q":"a } b"},"confidence":0.9} hope that helps {not json}'
    assert llm_client.first_json_block(raw) == '{"action":"greeting","args":{"q":"a } b"},"confidence":0.9}'
    assert llm_client.parse_json_object(raw)["action"] == "greeting"
    assert llm_client.parse_json_object("no json here") is None


def _json_400(code="json_validate_failed"):
    """The BadRequestError Groq raises when its own JSON validation rejects a generation."""
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return openai.BadRequestError(
        "Error code: 400 - Failed to validate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
        response=httpx.Response(400, request=req),
        body={"error": {"message": "Failed to validate JSON", "type": "invalid_request_error", "code": code}},
    )


def _ok_response(content='{"a": 1}'):
    """A minimal chat-completions response object."""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))], usage=None,
    )


async def test_G_json_validate_failed_is_retried_once_without_strict_response_format(monkeypatch):
    """400 json_validate_failed -> ONE retry with no response_format; the lenient parser reads the text."""
    from app.services import llm_client

    create = mock.AsyncMock(side_effect=[_json_400(), _ok_response('Here you go: {"a": 1} thanks')])
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_client, "get_client", lambda: fake)

    data = await llm_client.chat_json("openai/gpt-oss-20b", [{"role": "user", "content": "give json"}], max_tokens=50)

    assert data == {"a": 1}
    assert create.await_count == 2
    assert create.await_args_list[0].kwargs["response_format"] == {"type": "json_object"}
    assert "response_format" not in create.await_args_list[1].kwargs


async def test_G_retry_happens_only_once_and_only_for_json_failures(monkeypatch):
    """Two json failures in a row raise (no loop); a timeout or other 400 is never retried."""
    from app.services import llm_client

    msgs = [{"role": "user", "content": "give json"}]
    twice = mock.AsyncMock(side_effect=[_json_400(), _json_400()])
    monkeypatch.setattr(llm_client, "get_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=twice))))
    with pytest.raises(openai.BadRequestError):
        await llm_client.llm_call("t", "m", msgs, max_tokens=10, response_format={"type": "json_object"})
    assert twice.await_count == 2

    timeout = mock.AsyncMock(side_effect=openai.APITimeoutError(request=httpx.Request("POST", "http://x")))
    monkeypatch.setattr(llm_client, "get_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=timeout))))
    with pytest.raises(openai.APITimeoutError):
        await llm_client.llm_call("t", "m", msgs, max_tokens=10)
    assert timeout.await_count == 1

    other = mock.AsyncMock(side_effect=_json_400(code="invalid_request"))
    other.side_effect = openai.BadRequestError(
        "Error code: 400 - model not found", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
        body={"error": {"code": "model_not_found"}},
    )
    monkeypatch.setattr(llm_client, "get_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=other))))
    with pytest.raises(openai.BadRequestError):
        await llm_client.llm_call("t", "m", msgs, max_tokens=10)
    assert other.await_count == 1


async def test_D_ensure_reply_substitutes_a_fallback_and_logs_no_reply(caplog):
    """A pipeline that ends with nothing to send is logged 'NO_REPLY conv=…' and answered deterministically."""
    from app.services import order_pipeline as op

    conv = types.SimpleNamespace(id=60, current_stage="greeting", pending_product_sku=None,
                                 last_customer_language="english", channel="whatsapp")
    client = types.SimpleNamespace(id=1, upi_id=None, catalogue_slug="riyasarees", accepts_cod=False)
    ctx = types.SimpleNamespace(db=mock.MagicMock(), client=client, conv=conv, message=types.SimpleNamespace(type="text"))
    saved = []

    async def _save(db, conv_id, role, content, **kw):
        """Capture what ensure_reply stores to the transcript."""
        saved.append((conv_id, role, content))

    with mock.patch.object(op.conversation_service, "save_message", _save), \
            caplog.at_level(logging.ERROR, logger=op.logger.name):
        res = await op.ensure_reply(None, ctx)
        empty = await op.ensure_reply(op.PipelineResult(text=None), ctx)

    assert "catalogue" in res.text.lower() and "riyasarees" in res.text
    assert empty.text == res.text
    assert sum("NO_REPLY conv=60" in r.getMessage() for r in caplog.records) == 2
    assert saved and saved[0][1] == "assistant"


async def test_D_ensure_reply_leaves_intentional_silence_and_real_replies_alone(caplog):
    """skip_send (bot paused / blocked) and results that already have text are returned untouched, unlogged."""
    from app.services import order_pipeline as op

    ctx = types.SimpleNamespace(db=None, client=None, conv=types.SimpleNamespace(id=1), message=None)
    silent = op.PipelineResult(text=None, skip_send=True)
    real = op.PipelineResult(text="hello")
    with caplog.at_level(logging.ERROR, logger=op.logger.name):
        assert await op.ensure_reply(silent, ctx) is silent
        assert await op.ensure_reply(real, ctx) is real
    assert not [r for r in caplog.records if "NO_REPLY" in r.getMessage()]


# ═════════════════════════════════════════════════════════════════════════════
# Replay harness for the transcript
# ═════════════════════════════════════════════════════════════════════════════

PHONE, PNID = "919500006001", "7776001"
PROOF_REVIEW_MARK = "payment screenshot for order"      # pay_awaiting_verification: "…is received ✅"


@pytest.fixture(autouse=True)
def _reset_llm_health():
    """The LLM failure counter / circuit breaker are process globals: these tests fail calls on purpose."""
    from app.services import llm_health

    llm_health.reset_state()
    yield
    llm_health.reset_state()


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay and lift the per-phone rate limiter (turns are replayed back to back)."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)
    monkeypatch.setattr("app.routers.webhook._is_rate_limited", mock.AsyncMock(return_value=False))


class Outbox:
    """Everything the bot sent, in order: (kind, to, text, extra). kind: text | buttons | list | image."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[tuple] = []

    def since(self, n: int, to: str = PHONE) -> list[tuple]:
        """Events after index n addressed to the customer."""
        return [e for e in self.events[n:] if e[1] == to]


@pytest.fixture
def outbox(monkeypatch):
    """Capture every outbound send (text/buttons/list/image) and stub media download + re-hosting."""
    box = Outbox()
    ids = itertools.count(1)

    async def _text(to_phone_number, message_text, phone_number_id=None, access_token=None):
        box.events.append(("text", to_phone_number, message_text, None))
        return {"messages": [{"id": f"wamid.out.{next(ids)}"}]}

    async def _buttons(to_phone_number, body_text, buttons, phone_number_id=None, access_token=None):
        box.events.append(("buttons", to_phone_number, body_text, [b["title"] for b in buttons]))
        return True

    async def _list(to_phone_number, header_text, body_text, button_text, sections, phone_number_id=None, access_token=None):
        box.events.append(("list", to_phone_number, body_text, [r["title"] for s in sections for r in s["rows"]]))
        return True

    async def _image(to_phone_number, image_url, caption=None, phone_number_id=None, access_token=None):
        box.events.append(("image", to_phone_number, caption or "", image_url))
        return {"messages": [{"id": f"wamid.img.{next(ids)}"}]}

    async def _store(client_id, data, content_type, folder="chat"):
        return f"https://cdn.test/{folder}/{next(ids)}.png"

    async def _download(media_id, *a, **kw):
        return TINY_PNG

    monkeypatch.setattr("app.services.whatsapp_service._raw_send_text_message", _text)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_button_message", _buttons)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_list_message", _list)
    monkeypatch.setattr("app.services.whatsapp_service._raw_send_image_message", _image)
    monkeypatch.setattr("app.services.media_service.store_media", _store)
    monkeypatch.setattr("app.services.vision_service.download_whatsapp_media", _download)
    monkeypatch.setattr("app.services.ocr_service.detect_sku_from_image", lambda *_: None)
    return box


@pytest.fixture
def router(monkeypatch):
    """The scripted router LLM (llm_client.chat_json); an unscripted message fails the call -> test sees it."""
    fake = ScriptedRouter()
    monkeypatch.setattr("app.services.llm_client.chat_json", fake)
    return fake


async def seed_store(session, *, phone=PHONE, pnid=PNID, router_on=True):
    """
    'Riya Sarees Surat' (UPI-only, riyasarees@paytm):
      SR31045 Georgette Party Wear ₹1,100 — Black S/M/L/XL/XXL, Blue S/M/L, Red S/M
      SR27754 Banarasi Silk Saree ₹2,450 — Blue/Pink/Red (colour only)
      SR33210 Cotton Printed Saree ₹850, SR29821 Kanjivaram Silk Saree ₹4,200 (simple products)
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Riya Sarees Surat", email=f"conv60_{phone}@test.com", phone=phone[:-1] + "0",
        whatsapp_phone_number_id=pnid, accepts_cod=False, accepts_upi=True, upi_id="riyasarees@paytm",
        hashed_password="x", delivery_days_min=3, delivery_days_max=7, router_v2_enabled=router_on,
        catalogue_slug="riyasarees",
    )
    session.add(client)
    await session.flush()
    prods = {}
    for sku, name, price, stock, variants in (
        ("SR31045", "Georgette Party Wear", 1100.0, 60, True),
        ("SR27754", "Banarasi Silk Saree", 2450.0, 30, True),
        ("SR33210", "Cotton Printed Saree", 850.0, 10, False),
        ("SR29821", "Kanjivaram Silk Saree", 4200.0, 10, False),
    ):
        p = Product(client_id=client.id, name=name, sku=sku, price=price, stock=stock, is_active=True,
                    has_variants=variants, category="Dress" if sku == "SR31045" else "Saree")
        session.add(p)
        prods[sku] = p
    await session.flush()
    grid = {
        "SR31045": [("Black", s) for s in ("S", "M", "L", "XL", "XXL")] + [("Blue", s) for s in ("S", "M", "L")]
        + [("Red", s) for s in ("S", "M")],
        "SR27754": [("Blue", None), ("Pink", None), ("Red", None)],
    }
    for sku, combos in grid.items():
        for color, size in combos:
            session.add(ProductVariant(product_id=prods[sku].id, client_id=client.id, color=color, size=size,
                                       stock=10, is_active=True, price=prods[sku].price))
    await session.commit()
    await session.refresh(client)
    return client


async def seed_stale_orders(session, client, conv_id, *, phone=PHONE):
    """The two abandoned orders conversation 60 carried: 0046 pending_payment and 0047 payment_submitted."""
    from app.models.order import Order

    now = datetime.now(timezone.utc)
    rows = []
    for number, status, product, days in (
        ("ORD-2026-0046", "pending_payment", "Kurti New One", 5),
        ("ORD-2026-0047", "payment_submitted", "Designer Lehenga", 4),
    ):
        o = Order(
            order_number=number, client_id=client.id, conversation_id=conv_id, customer_name="Amit",
            customer_phone=phone, delivery_address="702 Somerest, Ahmedabad", product_name=product,
            product_sku="OLD0001", quantity=2, unit_price=6500.0, total_amount=13000.0, payment_method="UPI",
            status=status, created_at=now - timedelta(days=days),
        )
        session.add(o)
        rows.append(o)
    await session.commit()
    return rows


async def turn(http, box: Outbox, text: str | None = None, *, button: str | None = None, image: bool = False,
               phone=PHONE, pnid=PNID) -> list[tuple]:
    """Send one customer event; return the events the bot sent back to the customer for it."""
    n = len(box.events)
    stamp = time.time_ns()
    if button is not None:
        resp = await send_button(http, phone, button, button, wamid=f"wamid.c60.{stamp}", phone_number_id=pnid)
    elif image:
        resp = await send_image(http, phone, wamid=f"wamid.c60.{stamp}", phone_number_id=pnid)
    else:
        resp = await send_message(http, phone, text, wamid=f"wamid.c60.{stamp}", phone_number_id=pnid)
    assert resp.status_code == 200, resp.text
    return box.since(n, phone)


def body(events: list[tuple]) -> str:
    """All text bodies of a turn, joined (QR captions included)."""
    return "\n".join(e[2] for e in events)


def one_reply(events: list[tuple]) -> None:
    """Exactly one text-like outbound message for the turn (a QR image may ride along)."""
    assert len([e for e in events if e[0] != "image"]) == 1, events


async def orders_of(session, conv_id):
    """All orders of the conversation, oldest first, with fresh state."""
    from sqlalchemy.orm import selectinload

    from app.models.order import Order

    r = await session.execute(
        select(Order).where(Order.conversation_id == conv_id).order_by(Order.id)
        .options(selectinload(Order.line_items)).execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


# ─── the full transcript, turn by turn ───────────────────────────────────────

@needs_pg
async def test_conv60_transcript_replay_turn_by_turn(replay_http, replay_session, router, outbox, monkeypatch):
    """
    Replays conversation 60 from 'Georgette Wear is available?' to the declined product card, with the router
    timing out on turn 1. Asserts reply text, stage, slots and order status on every turn.
    Covers A (real order + instructions), B (2 L + 1 M kept), C (denial/payment-details keep the order),
    D (exactly one reply per turn), F ("no" is deterministic), G (router timeout), I (name/address kept).
    """
    import openai as _openai

    client = await seed_store(replay_session)
    conv_id = await prime_conv(replay_session, phone=PHONE, client_id=client.id, stage="greeting",
                               customer_name="Amit", delivery_address="702 Somerest, Ahmedabad")
    stale = await seed_stale_orders(replay_session, client, conv_id)
    generate_reply = mock.AsyncMock(return_value="__AI_REPLY__")
    monkeypatch.setattr("app.services.gemini_service.generate_reply", generate_reply)

    router.fails("Georgette Wear is available?", _openai.APITimeoutError(request=httpx.Request("POST", "http://x")))
    router.when("oh sorry 3 quantity I want", "change_slot", {"slot": "quantity", "value": "3"}, confidence=0.98)

    # T1 — router times out -> deterministic catalogue match, card + order offer (no open LLM reply)
    ev = await turn(replay_http, outbox, "Georgette Wear is available?")
    one_reply(ev)
    assert "Georgette Party Wear [SR31045] — ₹1,100" in body(ev) and "Would you like to order?" in body(ev)
    assert "__AI_REPLY__" not in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR31045" and conv.current_stage in ("greeting", "product_inquiry")
    assert conv.customer_name == "Amit" and conv.delivery_address == "702 Somerest, Ahmedabad"     # bug I

    # T2-T5 — slots
    ev = await turn(replay_http, outbox, "yes")
    one_reply(ev)
    assert "Colour? Black / Blue / Red" in body(ev)
    ev = await turn(replay_http, outbox, button="Blue")
    assert "Size? L / M / S" in body(ev)
    ev = await turn(replay_http, outbox, button="M")
    assert "Quantity?" in body(ev)
    ev = await turn(replay_http, outbox, "2")
    assert "Same for all 2, or different for each?" in body(ev)

    # T6 — quantity change via the router
    ev = await turn(replay_http, outbox, "oh sorry 3 quantity I want")
    assert "Updated ✅ Quantity: 3" in body(ev) and "Same for all 3" in body(ev)

    # T7 — bug B: the per-unit split survives, no router call, summary shows both lines with prices
    ev = await turn(replay_http, outbox, "I want 2 L and one M size and colour same")
    one_reply(ev)
    text = body(ev)
    assert "Blue L × 2 = ₹2,200" in text and "Blue M × 1 = ₹1,100" in text and "₹3,300" in text
    assert [c["message"] for c in router.calls] == ["Georgette Wear is available?", "oh sorry 3 quantity I want"]
    conv = await get_conv(replay_session, conv_id)
    assert conv.cart_variant_mode == "different" and conv.pending_order_quantity == 3
    assert [(i["color"], i["size"], i["qty"]) for i in conv.cart_items] == [("Blue", "L", 2), ("Blue", "M", 1)]
    assert conv.current_stage == "awaiting_final_confirmation"

    # T8 — bug A: confirm creates a NEW pending_payment order (the stale ones don't block it) + instructions
    ev = await turn(replay_http, outbox, button="confirm")
    one_reply([e for e in ev if e[0] != "image"])
    text = body(ev)
    orders = await orders_of(replay_session, conv_id)
    new = [o for o in orders if o.id not in {s.id for s in stale}]
    assert len(new) == 1 and new[0].status == "pending_payment"
    assert new[0].total_amount == 3300
    assert sorted((li.variant_color, li.variant_size, li.quantity, li.unit_price) for li in new[0].line_items) == [
        ("Blue", "L", 2, 1100.0), ("Blue", "M", 1, 1100.0)]
    assert new[0].order_number in text and "riyasarees@paytm" in text and "₹3,300" in text
    assert "upi://pay?pa=riyasarees@paytm" in text and "send the payment screenshot" in text
    assert "What to do next:" in text and "Pay ₹3,300 to the UPI ID above" in text
    assert "Send the payment screenshot here in this chat" in text and "Our team will verify your payment and confirm your order" in text
    assert PROOF_REVIEW_MARK not in text and "is received" not in text.lower()
    assert any(e[0] == "image" for e in ev)                                           # QR delivered
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "payment"
    # the two stale orders were not touched
    assert [(o.order_number, o.status) for o in stale] == [("ORD-2026-0046", "pending_payment"), ("ORD-2026-0047", "payment_submitted")]

    # T9 / T10 — bug C + D: denial and "give me payment details" resend THIS order's instruction, nothing resets
    for message in ("I did not send payment screenshot yet", "I already given you order give me payment details"):
        ev = await turn(replay_http, outbox, message)
        one_reply([e for e in ev if e[0] != "image"])
        assert new[0].order_number in body(ev) and "riyasarees@paytm" in body(ev) and "₹3,300" in body(ev)
        assert PROOF_REVIEW_MARK not in body(ev)
        conv = await get_conv(replay_session, conv_id)
        assert conv.current_stage == "payment" and conv.pending_product_sku == "SR31045"       # not detached/reset
        assert (await orders_of(replay_session, conv_id))[-1].status == "pending_payment"

    # T11 — the review message appears only for a real screenshot
    ev = await turn(replay_http, outbox, image=True)
    assert "verify your payment" in body(ev)
    order_now = (await orders_of(replay_session, conv_id))[-1]
    assert order_now.status == "payment_submitted"

    # T12 — a new product after submission: fresh turn, name/address kept (bug I)
    ev = await turn(replay_http, outbox, "Georgette Party Wear")
    one_reply(ev)
    assert "Georgette Party Wear [SR31045]" in body(ev) and "Would you like to order?" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR31045" and conv.customer_name == "Amit"
    assert conv.delivery_address == "702 Somerest, Ahmedabad"

    # T13 — bug F: "no" is deterministic (no LLM), product unpinned, back to browsing
    calls_before = (len(router.calls), generate_reply.await_count)
    ev = await turn(replay_http, outbox, "no")
    one_reply(ev)
    assert "No problem" in body(ev) and "https://" in body(ev) and "riyasarees" in body(ev)
    assert "Georgette Party Wear [SR31045]" not in body(ev)
    assert (len(router.calls), generate_reply.await_count) == calls_before
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku is None and conv.last_shown_sku is None and conv.current_stage == "greeting"


@needs_pg
async def test_A_review_message_needs_a_real_screenshot_and_stale_orders_do_not_leak(replay_http, replay_session, router, outbox):
    """
    With ONLY stale orders open (0046 pending, 0047 under review) a fresh confirm still creates its own order.
    The 'payment screenshot … is received' text is produced only for an order whose proof was uploaded.
    """
    client = await seed_store(replay_session, phone="919500006002", pnid="7776002")
    conv_id = await prime_conv(
        replay_session, phone="919500006002", client_id=client.id, stage="awaiting_final_confirmation",
        pending_product_sku="SR31045", selected_color="Blue", selected_size="M", pending_order_quantity=1,
        customer_name="Amit", delivery_address="702 Somerest, Ahmedabad", payment_method="UPI", summary_shown=True,
        mobile_number="919500006002",
    )
    await seed_stale_orders(replay_session, client, conv_id, phone="919500006002")

    ev = await turn(replay_http, outbox, button="confirm", phone="919500006002", pnid="7776002")

    text = body(ev)
    mine = (await orders_of(replay_session, conv_id))[-1]
    assert mine.status == "pending_payment" and mine.product_sku == "SR31045" and mine.total_amount == 1100
    assert mine.order_number in text and "riyasarees@paytm" in text and "upi://pay" in text
    assert "is received" not in text.lower()


@needs_pg
async def test_A_duplicate_confirm_inside_the_window_does_not_create_a_second_order(replay_http, replay_session, router, outbox):
    """A double-tap on Confirm (same product, moments apart) is one order: the instruction is simply re-sent."""
    phone, pnid = "919500006003", "7776003"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="awaiting_final_confirmation",
        pending_product_sku="SR31045", selected_color="Blue", selected_size="M", pending_order_quantity=1,
        customer_name="Amit", delivery_address="702 Somerest, Ahmedabad", payment_method="UPI", summary_shown=True,
        mobile_number=phone,
    )
    await turn(replay_http, outbox, button="confirm", phone=phone, pnid=pnid)
    conv = await get_conv(replay_session, conv_id)
    # simulate the replay of the same confirm: stage rolled back to awaiting_final_confirmation
    from app.services import conversation_service
    await conversation_service.update_stage(replay_session, conv_id, "awaiting_final_confirmation")
    await turn(replay_http, outbox, button="confirm", phone=phone, pnid=pnid)
    assert len(await orders_of(replay_session, conv_id)) == 1


@needs_pg
async def test_B_mismatching_split_is_explained_and_nothing_is_committed(replay_http, replay_session, router, outbox):
    """qty 3 but '2 L and 2 M' (4 pcs): the customer is told the totals differ; cart/qty untouched; slot unchanged."""
    phone, pnid = "919500006004", "7776004"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="SR31045",
        selected_color="Blue", selected_size="M", pending_order_quantity=3, customer_name="Amit",
        delivery_address="702 Somerest, Ahmedabad",
    )

    ev = await turn(replay_http, outbox, "2 L and 2 M", phone=phone, pnid=pnid)

    one_reply(ev)
    assert "adds up to 4 pcs (2 L + 2 M)" in body(ev) and "order is for 3 pcs" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert not conv.cart_items and conv.cart_variant_mode is None and conv.pending_order_quantity == 3
    assert conv.current_stage == "order_collection"


@needs_pg
async def test_B_split_with_a_size_the_product_lacks_names_it_and_lists_valid_options(replay_http, replay_session, router, outbox):
    """'2 XL and 1 M' on Blue (L/M/S only): "'XL' isn't a variant we carry" + the valid sizes; nothing committed."""
    phone, pnid = "919500006005", "7776005"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="SR31045",
        selected_color="Blue", selected_size="M", pending_order_quantity=3, customer_name="Amit",
        delivery_address="702 Somerest, Ahmedabad",
    )
    ev = await turn(replay_http, outbox, "2 XL and 1 M", phone=phone, pnid=pnid)
    assert "'XL' isn't a variant we carry" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert not conv.cart_items and conv.cart_variant_mode is None


@needs_pg
async def test_B_split_against_other_colour_stock_is_checked_per_line(replay_http, replay_session, router, outbox):
    """'1 red L and 2 blue M': Red has no L -> that exact combination is reported unavailable, nothing is committed."""
    phone, pnid = "919500006006", "7776006"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="SR31045",
        selected_color="Blue", selected_size="M", pending_order_quantity=3, customer_name="Amit",
        delivery_address="702 Somerest, Ahmedabad",
    )
    ev = await turn(replay_http, outbox, "1 red L and 2 blue M", phone=phone, pnid=pnid)
    assert "Red L isn't available right now" in body(ev) and "Available sizes: M, S" in body(ev)
    assert not (await get_conv(replay_session, conv_id)).cart_items


@needs_pg
async def test_B_llm_fallback_for_an_unparseable_split_is_always_confirmed(replay_http, replay_session, router, outbox, monkeypatch):
    """'half L half M' (no digits): strict-schema LLM split is echoed back for confirmation, then 'yes' commits it."""
    phone, pnid = "919500006007", "7776007"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="order_collection", pending_product_sku="SR31045",
        selected_color="Blue", selected_size="M", pending_order_quantity=2, customer_name="Amit",
        delivery_address="702 Somerest, Ahmedabad", mobile_number=phone,
    )

    async def fake_extract(user_text, variant_info, product_name, target_qty, conversation_id=None):
        """The strict-schema LLM result: an inferred 1 L + 1 M split."""
        return {"items": [{"qty": 1, "color": "Blue", "size": "L"}, {"qty": 1, "color": "Blue", "size": "M"}],
                "inferred_split": True}

    monkeypatch.setattr("app.services.conversation_flow.extract_cart_breakdown", fake_extract)
    monkeypatch.setattr("app.services.conversation_flow.looks_like_variant_breakdown", lambda *a, **k: True)
    router.when("half in L half in M", "answer_slot", {"slot": "variant_mode", "value": "same"})

    ev = await turn(replay_http, outbox, "half in L half in M", phone=phone, pnid=pnid)
    assert "1 Blue L + 1 Blue M" in body(ev) and "correct?" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.cart_pending_confirmation and not conv.cart_items and conv.cart_variant_mode == "different"

    ev = await turn(replay_http, outbox, "yes", phone=phone, pnid=pnid)
    conv = await get_conv(replay_session, conv_id)
    assert [(i["size"], i["qty"]) for i in conv.cart_items] == [("L", 1), ("M", 1)]
    assert "Blue L × 1 = ₹1,100" in body(ev)


@needs_pg
async def test_C_denial_while_under_review_is_answered_without_resetting(replay_http, replay_session, router, outbox):
    """Order genuinely under review: 'not paid' gets the review status and the conversation is NOT detached."""
    phone, pnid = "919500006008", "7776008"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="payment",
                               pending_product_sku="SR31045", selected_color="Blue", selected_size="M",
                               pending_order_quantity=1, customer_name="Amit", payment_method="UPI")
    from app.models.order import Order
    replay_session.add(Order(
        order_number="ORD-2026-0100", client_id=client.id, conversation_id=conv_id, customer_name="Amit",
        customer_phone=phone, delivery_address="x", product_name="Georgette Party Wear", product_sku="SR31045",
        quantity=1, unit_price=1100.0, total_amount=1100.0, payment_method="UPI", status="payment_submitted"))
    await replay_session.commit()

    ev = await turn(replay_http, outbox, "not paid", phone=phone, pnid=pnid)

    one_reply(ev)
    assert "ORD-2026-0100" in body(ev) and "verifying" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "payment" and conv.pending_product_sku == "SR31045"


@needs_pg
async def test_C_plain_text_still_frees_a_conversation_whose_proof_is_under_review(replay_http, replay_session, router, outbox):
    """Unchanged behaviour: a non-payment message ('Hi') after submission is a fresh turn, not a payment template."""
    phone, pnid = "919500006009", "7776009"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="payment",
                               pending_product_sku="SR31045", pending_order_quantity=1, payment_method="UPI")
    from app.models.order import Order
    replay_session.add(Order(
        order_number="ORD-2026-0101", client_id=client.id, conversation_id=conv_id, customer_name="Amit",
        customer_phone=phone, delivery_address="x", product_name="Georgette Party Wear", product_sku="SR31045",
        quantity=1, unit_price=1100.0, total_amount=1100.0, payment_method="UPI", status="payment_submitted"))
    await replay_session.commit()
    router.when("hi", "greeting", {})

    ev = await turn(replay_http, outbox, "Hi", phone=phone, pnid=pnid)

    assert "Welcome" in body(ev) and "ORD-2026-0101" not in body(ev)
    assert (await get_conv(replay_session, conv_id)).current_stage == "greeting"


@needs_pg
async def test_D_pipeline_returning_nothing_still_answers_once(replay_http, replay_session, router, outbox, monkeypatch, caplog):
    """The webhook never leaves a customer unanswered: empty result and a crash both produce one fallback reply."""
    phone, pnid = "919500006010", "7776010"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="greeting")
    from app.services.order_pipeline import PipelineResult

    monkeypatch.setattr("app.routers.webhook.handle_inbound_message", mock.AsyncMock(return_value=PipelineResult(text=None)))
    with caplog.at_level(logging.ERROR):
        ev = await turn(replay_http, outbox, "hello?", phone=phone, pnid=pnid)
    one_reply(ev)
    assert "catalogue" in body(ev).lower()
    assert any(f"NO_REPLY conv={conv_id}" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr("app.routers.webhook.handle_inbound_message", mock.AsyncMock(side_effect=RuntimeError("boom")))
    ev = await turn(replay_http, outbox, "anyone there?", phone=phone, pnid=pnid)
    one_reply(ev)
    assert "catalogue" in body(ev).lower()


@needs_pg
async def test_D_intentional_silence_is_respected(replay_http, replay_session, router, outbox, monkeypatch):
    """A paused bot (skip_send) stays silent: the guarantee is for messages that should be answered."""
    phone, pnid = "919500006011", "7776011"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="greeting")
    from app.services.order_pipeline import PipelineResult

    monkeypatch.setattr("app.routers.webhook.handle_inbound_message",
                        mock.AsyncMock(return_value=PipelineResult(text=None, skip_send=True)))
    assert await turn(replay_http, outbox, "hello?", phone=phone, pnid=pnid) == []


@needs_pg
async def test_E_unavailable_colour_keeps_the_slot_offers_buttons_and_escapes_on_the_second_miss(replay_http, replay_session, router, outbox):
    """'Gray' at the colour question: 'isn't available' + the real options as buttons, same slot; 2nd miss adds an exit."""
    phone, pnid = "919500006012", "7776012"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="SR27754", customer_name="Amit")

    first = await turn(replay_http, outbox, "Gray", phone=phone, pnid=pnid)
    one_reply(first)
    assert "Gray isn't available for Banarasi Silk Saree" in body(first) and "Blue / Pink / Red" in body(first)
    assert first[0][0] == "buttons" and first[0][3] == ["Blue", "Pink", "Red"]
    assert "Happy to help" not in body(first) and "talk to someone" not in body(first)
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "order_collection" and conv.pending_product_sku == "SR27754" and conv.slot_attempt_count == 1

    second = await turn(replay_http, outbox, "Gray", phone=phone, pnid=pnid)
    one_reply(second)
    assert "isn't available" in body(second) and "talk to someone" in body(second) and "riyasarees" in body(second)
    assert (await get_conv(replay_session, conv_id)).current_stage == "order_collection"
    assert router.calls == []                        # settled for ₹0, the LLM was never asked

    third = await turn(replay_http, outbox, "Pink", phone=phone, pnid=pnid)       # a valid answer moves on and resets the count
    conv = await get_conv(replay_session, conv_id)
    assert conv.selected_color == "Pink" and conv.slot_attempt_count == 0
    assert "Gray" not in body(third)


@needs_pg
async def test_F_no_at_the_product_card_is_deterministic_and_a_later_yes_does_not_revive_it(replay_http, replay_session, router, outbox, monkeypatch):
    """'no' to 'Would you like to order?' -> catalogue offer, SKU unpinned, stage browsing; bare 'yes' can't re-pin it."""
    phone, pnid = "919500006013", "7776013"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    generate_reply = mock.AsyncMock(return_value="__AI_REPLY__")
    monkeypatch.setattr("app.services.gemini_service.generate_reply", generate_reply)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                               pending_product_sku="SR31045", last_shown_sku="SR31045")
    from app.services import conversation_service
    await conversation_service.save_message(replay_session, conv_id, "assistant",
                                            "Georgette Party Wear [SR31045] — ₹1,100\n\nWould you like to order? (Yes / No)")

    ev = await turn(replay_http, outbox, "no", phone=phone, pnid=pnid)

    one_reply(ev)
    assert body(ev).startswith("No problem 👍 Want to see other products?") and "riyasarees" in body(ev)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku is None and conv.last_shown_sku is None and conv.current_stage == "greeting"
    assert router.calls == [] and generate_reply.await_count == 0

    ev = await turn(replay_http, outbox, "yes", phone=phone, pnid=pnid)
    assert (await get_conv(replay_session, conv_id)).pending_product_sku is None      # not revived


@needs_pg
async def test_F_a_no_that_answers_something_else_is_not_hijacked(replay_http, replay_session, router, outbox, monkeypatch):
    """The decline guard needs the order offer to be the last thing the bot said."""
    phone, pnid = "919500006014", "7776014"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    monkeypatch.setattr("app.services.gemini_service.generate_reply", mock.AsyncMock(return_value="__AI_REPLY__"))
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                               pending_product_sku="SR31045")
    from app.services import conversation_service
    await conversation_service.save_message(replay_session, conv_id, "assistant", "Delivery takes 3–7 business days.")

    from app.services import order_pipeline as op
    conv = await get_conv(replay_session, conv_id)
    res = await op.run_product_decline_guard(replay_session, conv, client, types.SimpleNamespace(type="text"),
                                             "no", None, "product_inquiry", mock.AsyncMock())
    assert res is None and conv.pending_product_sku == "SR31045"


@needs_pg
@pytest.mark.parametrize("exc_kind", ["timeout", "bad_json"])
async def test_G_router_failure_gets_a_deterministic_stage_aware_reply_not_an_open_llm_one(
    replay_http, replay_session, router, outbox, monkeypatch, exc_kind,
):
    """Router timeout / 400 json_validate_failed on an open-ended message with nothing in progress: no open LLM reply."""
    phone, pnid = "919500006015", "7776015"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="greeting")
    generate_reply = mock.AsyncMock(return_value="__AI_REPLY__")
    monkeypatch.setattr("app.services.gemini_service.generate_reply", generate_reply)
    exc = (openai.APITimeoutError(request=httpx.Request("POST", "http://x")) if exc_kind == "timeout" else _json_400())
    msg = "tell me something about weddings next month"
    router.fails(msg, exc)

    ev = await turn(replay_http, outbox, msg, phone=phone, pnid=pnid)

    one_reply(ev)
    assert body(ev) == "Sorry, could you rephrase? Or type a product code, or 'order status'."   # nothing in progress
    assert "__AI_REPLY__" not in body(ev) and generate_reply.await_count == 0


@needs_pg
async def test_G_router_failure_with_an_open_order_reminds_the_payment_not_rephrase(replay_http, replay_session, router, outbox, monkeypatch):
    """Router down while an order awaits payment: the reply is that order's payment reminder."""
    phone, pnid = "919500006016", "7776016"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="greeting")
    from app.models.order import Order
    replay_session.add(Order(
        order_number="ORD-2026-0102", client_id=client.id, conversation_id=conv_id, customer_name="Amit",
        customer_phone=phone, delivery_address="x", product_name="Georgette Party Wear", product_sku="SR31045",
        quantity=2, unit_price=1100.0, total_amount=2200.0, payment_method="UPI", status="pending_payment"))
    await replay_session.commit()
    msg = "what about the weather at the venue"
    router.fails(msg, openai.APITimeoutError(request=httpx.Request("POST", "http://x")))

    ev = await turn(replay_http, outbox, msg, phone=phone, pnid=pnid)

    assert "ORD-2026-0102" in body(ev) and "₹2,200" in body(ev) and "riyasarees@paytm" in body(ev)
    assert "rephrase" not in body(ev).lower()


@needs_pg
async def test_G_reply_path_failure_mid_browse_is_stage_aware(replay_http, replay_session, outbox, monkeypatch):
    """Router off + the reply LLM raising 400 json_validate_failed: product card + offer, never 'could you rephrase'."""
    phone, pnid = "919500006017", "7776017"
    client = await seed_store(replay_session, phone=phone, pnid=pnid, router_on=False)
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="product_inquiry",
                     pending_product_sku="SR31045")
    monkeypatch.setattr("app.services.gemini_service.generate_reply", mock.AsyncMock(side_effect=_json_400()))
    monkeypatch.setattr("app.services.llm_intent.classify_turn", mock.AsyncMock(side_effect=_json_400()))

    ev = await turn(replay_http, outbox, "what fabric is used in the border work exactly", phone=phone, pnid=pnid)

    one_reply(ev)
    assert "rephrase" not in body(ev).lower()
    assert "Georgette Party Wear [SR31045]" in body(ev) and "Would you like to order?" in body(ev)


@needs_pg
async def test_G_reply_path_failure_mid_order_re_asks_the_open_question(replay_http, replay_session, outbox, monkeypatch):
    """A crash while an order is being collected re-asks the open slot instead of a generic apology."""
    phone, pnid = "919500006018", "7776018"
    client = await seed_store(replay_session, phone=phone, pnid=pnid, router_on=False)
    await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                     pending_product_sku="SR31045", selected_color="Blue", customer_name="Amit",
                     delivery_address="702 Somerest, Ahmedabad")
    monkeypatch.setattr("app.services.order_pipeline._render_order_reply", mock.AsyncMock(side_effect=openai.APIConnectionError(
        request=httpx.Request("POST", "http://x"))))

    ev = await turn(replay_http, outbox, "M", phone=phone, pnid=pnid)

    assert "continue with your order" in body(ev) and "rephrase" not in body(ev).lower()


@needs_pg
async def test_H_open_order_note_only_when_the_draft_is_live_and_the_reply_is_about_it(replay_http, replay_session, router, outbox):
    """
    The 'your order for X is still open' line is dropped for a payment-stage/stale pin and for a list of OTHER
    products, and kept when the customer asks about the very product being ordered.
    """
    phone, pnid = "919500006019", "7776019"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="SR31045", customer_name="Amit")
    router.when("show me sarees", "search_catalog", {"query": "saree"})
    router.when("tell me about this dress", "show_product", {"sku": "SR31045"})

    other = await turn(replay_http, outbox, "show me sarees", phone=phone, pnid=pnid)
    assert "Cotton Printed Saree [SR33210]" in body(other) and "still open" not in body(other)

    same = await turn(replay_http, outbox, "tell me about this dress", phone=phone, pnid=pnid)
    assert "Georgette Party Wear [SR31045]" in body(same) and "still open" in body(same)

    # a pin left behind at the payment stage never produces the note
    from app.services import conversation_service
    await conversation_service.update_stage(replay_session, conv_id, "payment")
    router.when("any other sarees", "search_catalog", {"query": "saree"})
    stale = await turn(replay_http, outbox, "any other sarees", phone=phone, pnid=pnid)
    assert "Cotton Printed Saree [SR33210]" in body(stale) and "still open" not in body(stale)


@needs_pg
async def test_H_abandoned_order_draft_expires_silently_after_the_flow_ttl(replay_session):
    """Drafts idle past the TTL are reset (no message); fresh drafts and payment-stage conversations are untouched."""
    from app import scheduler
    from app.models.message import Message

    client = await seed_store(replay_session, phone="919500006020", pnid="7776020")
    old = await prime_conv(replay_session, phone="919500006021", client_id=client.id, stage="order_collection",
                           pending_product_sku="SR31045", selected_color="Blue", pending_order_quantity=2)
    fresh = await prime_conv(replay_session, phone="919500006022", client_id=client.id, stage="order_collection",
                             pending_product_sku="SR31045", selected_color="Blue")
    paying = await prime_conv(replay_session, phone="919500006023", client_id=client.id, stage="payment",
                              pending_product_sku="SR31045")
    now = datetime.now(timezone.utc)
    for cid, age in ((old, timedelta(hours=30)), (fresh, timedelta(hours=2)), (paying, timedelta(hours=30))):
        replay_session.add(Message(conversation_id=cid, role="user", content="hi", created_at=now - age))
    await replay_session.commit()

    expired = await scheduler._expire_abandoned_order_drafts(replay_session)

    assert expired == 1
    o, f, p = [await get_conv(replay_session, c) for c in (old, fresh, paying)]
    assert o.current_stage == "greeting" and o.pending_product_sku is None and o.selected_color is None
    assert f.current_stage == "order_collection" and f.pending_product_sku == "SR31045"
    assert p.current_stage == "payment" and p.pending_product_sku == "SR31045"


@needs_pg
@pytest.mark.parametrize("entry", ["name_match", "exact_sku"])
async def test_I_repinning_a_product_keeps_name_and_address_but_resets_product_slots(replay_http, replay_session, router, outbox, entry):
    """A different product pinned by name or SKU resets colour/size/qty/payment — customer_name/address survive."""
    phone, pnid = ("919500006024", "7776024") if entry == "name_match" else ("919500006025", "7776025")
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    # name-match is skipped while variant slots are filled (an order is "active"), so that entry starts from
    # a browsing pin that only carries payment_method; the SKU entry also carries colour/size/quantity.
    slots = {} if entry == "name_match" else dict(selected_color="Blue", selected_size="M", pending_order_quantity=2)
    conv_id = await prime_conv(
        replay_session, phone=phone, client_id=client.id, stage="product_inquiry", pending_product_sku="SR33210",
        payment_method="UPI", customer_name="Amit", delivery_address="702 Somerest, Ahmedabad", **slots,
    )
    text = "Georgette Party Wear" if entry == "name_match" else "SR31045"

    await turn(replay_http, outbox, text, phone=phone, pnid=pnid)

    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR31045"
    assert conv.selected_color is None and conv.selected_size is None and conv.pending_order_quantity is None
    assert conv.payment_method is None
    assert conv.customer_name == "Amit" and conv.delivery_address == "702 Somerest, Ahmedabad"


# ─── follow-up: asking about ANOTHER product mid-order (screenshot "is Red available?" -> "Quantity?") ───

@needs_pg
async def test_J_question_about_another_product_mid_order_offers_a_switch_and_yes_switches(replay_http, replay_session, router, outbox):
    """
    Ordering the saree (open question: Quantity). Customer asks the price of Georgette, then 'is Red available?'
    -> answered about Georgette (not 'that isn't an option / Quantity?'); 'yes' switches the order to Georgette
    with Red already chosen, so the next question is Size, not Quantity.
    """
    phone, pnid = "919500006030", "7776030"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="SR33210", customer_name="Amit",
                               delivery_address="702 Somerest, Ahmedabad")
    router.when("SR31045 price please", "show_product", {"sku": "SR31045"})

    card = await turn(replay_http, outbox, "SR31045 price please", phone=phone, pnid=pnid)
    one_reply(card)
    assert "Georgette Party Wear [SR31045] — ₹1,100" in body(card)
    assert "Would you like to order Georgette Party Wear instead of Cotton Printed Saree? (Yes / No)" in body(card)
    conv = await get_conv(replay_session, conv_id)
    assert conv.current_stage == "awaiting_order_switch_confirm" and conv.interrupted_sku == "SR31045"

    ans = await turn(replay_http, outbox, "is Red available?", phone=phone, pnid=pnid)
    one_reply(ans)
    assert "Yes, Red is available for Georgette Party Wear" in body(ans) and "Quantity?" not in body(ans)
    assert "instead of Cotton Printed Saree" in body(ans)
    assert (await get_conv(replay_session, conv_id)).interrupted_sku == "SR31045"
    assert [c["message"] for c in router.calls] == ["SR31045 price please"]

    sw = await turn(replay_http, outbox, "yes", phone=phone, pnid=pnid)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR31045" and conv.selected_color == "Red"
    assert conv.customer_name == "Amit" and conv.current_stage == "order_collection"
    assert "Size?" in body(sw) and "Quantity?" not in body(sw)


@needs_pg
async def test_J_declining_the_switch_resumes_the_original_question(replay_http, replay_session, router, outbox):
    """'no' to the switch offer returns to the saree's open question; nothing about the order changed."""
    phone, pnid = "919500006031", "7776031"
    client = await seed_store(replay_session, phone=phone, pnid=pnid)
    conv_id = await prime_conv(replay_session, phone=phone, client_id=client.id, stage="order_collection",
                               pending_product_sku="SR33210", customer_name="Amit",
                               delivery_address="702 Somerest, Ahmedabad")
    router.when("is red georgette party wear available?", "show_product",
                {"sku": "SR31045", "filters": {"color": "red"}})

    ans = await turn(replay_http, outbox, "is red georgette party wear available?", phone=phone, pnid=pnid)
    assert "Yes, Red is available for Georgette Party Wear" in body(ans) and "(Yes / No)" in body(ans)
    assert "Want to order it?" not in body(ans)

    back = await turn(replay_http, outbox, "no", phone=phone, pnid=pnid)
    assert "Quantity?" in body(back)
    conv = await get_conv(replay_session, conv_id)
    assert conv.pending_product_sku == "SR33210" and conv.current_stage == "order_collection"
    assert conv.interrupted_sku is None
