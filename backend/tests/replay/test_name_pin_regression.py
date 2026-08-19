"""
Name-match pin regression tests — threshold calibration.

Verifies that product name mentions (even in a fresh conversation with no
prior pinned SKU) reliably trigger the name-match pinner, while true
non-matches (product category not in catalogue) still return an honest
"not found" rather than listing an irrelevant product.

These tests run against a real Postgres instance (replay_ci_test) and are
skipped automatically when Postgres is not reachable.
"""

from __future__ import annotations

import re
import time
import unittest.mock as mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE
from tests.replay.helpers import capture_all, send_message

pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKU_RE = re.compile(r"\b([A-Za-z]{2,4}\d{4,6})\b")
_PRICE_RE = re.compile(r"₹\s*(\d[\d,]*)")


def _extract_skus(text: str) -> set[str]:
    return {m.group(1).upper() for m in _SKU_RE.finditer(text)}


def _extract_prices(text: str) -> set[str]:
    return {m.group(1).replace(",", "") for m in _PRICE_RE.finditer(text)}


def _phone(tag: str) -> str:
    return f"919NMP{tag}"


def _pnid(tag: str) -> str:
    return f"nmp{tag}"


def _wamid(tag: str) -> str:
    return f"wamid.nmp.{tag}.{int(time.time())}"


async def _seed_kanjivaram(
    session: AsyncSession,
    *,
    phone: str,
    pnid: str,
    also_add_georgette: bool = False,
):
    """
    Seed client + 'Kanjivaram Silk Saree' (KS10001, ₹3500).
    Optionally also add 'Georgette Party Wear' (SR31045, ₹1200) as a second product.
    Returns (client, kanjivaram_product[, georgette_product]).
    """
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Saree Test Store",
        email=f"nmp_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
    )
    session.add(client)
    await session.flush()

    kanjivaram = Product(
        client_id=client.id,
        name="Kanjivaram Silk Saree",
        sku="KS10001",
        price=3500.0,
        stock=8,
        is_active=True,
        has_variants=False,
    )
    session.add(kanjivaram)

    georgette = None
    if also_add_georgette:
        georgette = Product(
            client_id=client.id,
            name="Georgette Party Wear",
            sku="SR31045",
            price=1200.0,
            stock=10,
            is_active=True,
            has_variants=False,
        )
        session.add(georgette)

    await session.commit()
    await session.refresh(client)
    await session.refresh(kanjivaram)
    if georgette:
        await session.refresh(georgette)
        return client, kanjivaram, georgette

    return client, kanjivaram


async def _fresh_conv(session: AsyncSession, *, phone: str, client_id: int | None = None):
    """Insert a fresh greeting-stage conversation with no pinned SKU."""
    from app.models.conversation import Conversation

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=client_id,
        current_stage="greeting",
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv


async def _seed_georgette_only(session: AsyncSession, *, phone: str, pnid: str):
    """Seed client with only 'Georgette Party Wear' — no kurti in catalogue."""
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="No Kurti Store",
        email=f"nmp_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=pnid,
        accepts_cod=True,
        hashed_password="x",
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name="Georgette Party Wear",
        sku="SR31045",
        price=1200.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    session.add(product)
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


# ---------------------------------------------------------------------------
# test_real_name_pins
# "Kanjivaram Silk Saree is available?" in a FRESH conversation must pin.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_name_pins(replay_http, replay_session, monkeypatch):
    """
    Name-match pinner must fire when customer names a real product in a fresh
    conversation (no prior pinned SKU).

    "Kanjivaram Silk Saree is available?" → the keyword scorer gives
    KS10001 a score of 6 (kanjivaram+2, silk+2, saree+2). The single-strong
    condition fires and pending_product_sku is set to KS10001.

    The deterministic availability reply must also be sent (we mock the AI to
    return transactional phrasing so the browsing-safety guard fires and uses
    the newly-pinned product to produce the template reply).
    """
    phone = _phone("01")
    pnid = _pnid("01")
    client, product = await _seed_kanjivaram(
        replay_session, phone=phone, pnid=pnid
    )
    await _fresh_conv(replay_session, phone=phone, client_id=client.id)

    # AI leaks transactional phrasing → triggers the browsing safety guard.
    # The guard must use the NEWLY pinned product (pinned THIS turn by the
    # name-match block) to build a deterministic reply.
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=(
            "Kanjivaram Silk Saree [KS10001] — ₹3500. "
            "Deliver to your address? Would you like to order?"
        )),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(
        replay_http, phone,
        "Kanjivaram Silk Saree is available?",
        wamid=_wamid("01a"),
        phone_number_id=pnid,
    )
    assert resp.status_code == 200, resp.text
    assert captured, "No reply was sent"

    # -- Check DB: SKU must be pinned --
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()

    assert conv_row.pending_product_sku == "KS10001", (
        f"Name-match pinner must set pending_product_sku=KS10001, "
        f"got {conv_row.pending_product_sku!r}. "
        f"Reply was: {captured[-1]!r}"
    )

    # -- Check reply: deterministic template from browsing guard --
    reply = captured[-1]
    reply_upper = reply.upper()
    assert "KS10001" in reply_upper, (
        f"Deterministic reply must name SKU KS10001. Got: {reply!r}"
    )
    assert any(p in reply for p in ("3500", "3,500")), (
        f"Deterministic reply must include price ₹3500. Got: {reply!r}"
    )
    assert "order" in reply.lower(), (
        f"Reply must contain order prompt. Got: {reply!r}"
    )
    assert "which item" not in reply.lower(), (
        f"Must NOT produce 'which item' deflect when product was just pinned. Got: {reply!r}"
    )

    print(
        f"\n[NMP-01] test_real_name_pins: sku={conv_row.pending_product_sku!r} "
        f"reply={reply!r} ✓"
    )


# ---------------------------------------------------------------------------
# test_real_name_pins_2
# "Georgette Party Wear is available?" in a FRESH conversation must pin.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_name_pins_2(replay_http, replay_session, monkeypatch):
    """
    Same as test_real_name_pins but for Georgette Party Wear (SR31045).
    Two products in catalogue: Kanjivaram (score 0 for "georgette party wear")
    and Georgette (score 4+). Single-strong fires; SR31045 is pinned.
    """
    phone = _phone("02")
    pnid = _pnid("02")
    client, kanjivaram, georgette = await _seed_kanjivaram(
        replay_session, phone=phone, pnid=pnid, also_add_georgette=True
    )
    await _fresh_conv(replay_session, phone=phone, client_id=client.id)

    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=(
            "Georgette Party Wear [SR31045] — ₹1200. "
            "Deliver to your address? Would you like to order?"
        )),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(
        replay_http, phone,
        "Georgette Party Wear is available?",
        wamid=_wamid("02a"),
        phone_number_id=pnid,
    )
    assert resp.status_code == 200, resp.text
    assert captured, "No reply was sent"

    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()

    assert conv_row.pending_product_sku == "SR31045", (
        f"Name-match pinner must set pending_product_sku=SR31045 for "
        f"'Georgette Party Wear is available?', got {conv_row.pending_product_sku!r}. "
        f"Reply was: {captured[-1]!r}"
    )

    reply = captured[-1]
    assert "SR31045" in reply.upper(), (
        f"Deterministic reply must name SKU SR31045. Got: {reply!r}"
    )
    assert any(p in reply for p in ("1200", "1,200")), (
        f"Deterministic reply must include price ₹1200. Got: {reply!r}"
    )
    assert "order" in reply.lower(), (
        f"Reply must contain order prompt. Got: {reply!r}"
    )

    print(
        f"\n[NMP-02] test_real_name_pins_2: sku={conv_row.pending_product_sku!r} "
        f"reply={reply!r} ✓"
    )


# ---------------------------------------------------------------------------
# test_absent_still_not_found
# "kurti" when no kurti in catalogue → honest not-found, no phantom
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_absent_still_not_found(replay_http, replay_session, monkeypatch):
    """
    FIX 3 must still work: query with zero relevance to catalogue → not-found.

    Catalogue has only Georgette Party Wear (SR31045). Customer asks "kurti".
    AI hallucinates KU23444 @ ₹1030. Guard detects phantom, checks relevance
    of "kurti" vs [SR31045] → score=0 → returns honest "not found" message
    mentioning what we DO carry.

    This test ensures FIX 3 is NOT broken by any threshold recalibration.
    """
    phone = _phone("03")
    pnid = _pnid("03")
    client, product = await _seed_georgette_only(
        replay_session, phone=phone, pnid=pnid
    )

    # Prime with Georgette pinned from a prior turn — this is the real scenario:
    # customer was discussing Georgette, then pivots to ask about "kurti".
    # _get_catalogue_context will use the pinned SKU → canonical=[Georgette].
    # guard_product_reply then sees score=0 for "kurti" vs Georgette → not-found.
    from app.models.conversation import Conversation as _Conv
    prior_conv = _Conv(
        phone_number=phone,
        channel="whatsapp",
        client_id=client.id,
        current_stage="product_inquiry",
        pending_product_sku="SR31045",
    )
    replay_session.add(prior_conv)
    await replay_session.commit()

    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=(
            "Yes, we have Cotton Kurti [KU23444] — ₹1030. "
            "Available in sizes S, M, L. Would you like to order?"
        )),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(
        replay_http, phone, "kurti",
        wamid=_wamid("03a"),
        phone_number_id=pnid,
    )
    assert resp.status_code == 200, resp.text
    assert captured, "No reply was sent"

    reply = captured[-1]
    skus_in_reply = _extract_skus(reply)
    prices_in_reply = _extract_prices(reply)

    # Phantom must be stripped
    assert "KU23444" not in skus_in_reply, (
        f"Phantom KU23444 must be stripped. Found skus={skus_in_reply} reply={reply!r}"
    )
    assert "1030" not in prices_in_reply, (
        f"Phantom ₹1030 must be stripped. Found prices={prices_in_reply} reply={reply!r}"
    )

    # Must honestly say "not found" / "don't carry"
    reply_lower = reply.lower()
    assert any(phrase in reply_lower for phrase in (
        "don't carry", "don't have", "do not carry", "we currently have",
        "not available", "no kurti", "not found", "nahi hai",
    )), (
        f"Reply must acknowledge kurti is not in catalogue. Got: {reply!r}"
    )

    # Must mention real products we DO carry
    assert "georgette" in reply_lower or "SR31045" in reply.upper(), (
        f"Reply should list real products. Got: {reply!r}"
    )

    print(
        f"\n[NMP-03] test_absent_still_not_found reply: {reply!r}"
        f"\n  ✓ phantom stripped, honest not-found returned"
    )


# ---------------------------------------------------------------------------
# test_pinned_then_yes_starts_order
# After a real-name pin, "yes" → order_collection / first slot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pinned_then_yes_starts_order(replay_http, replay_session, monkeypatch):
    """
    After the name-match pinner fires and sets the SKU, the customer replies
    "yes". The stage must advance to order_collection (first slot question).

    We prime the conversation with:
    - pending_product_sku = KS10001 (as if the pinner fired on the previous turn)
    - current_stage = product_inquiry
    - An "order offer" in DB history so detect_stage sees the agent asked

    Then "yes" → classify_buy_intent returns True → order_collection.
    """
    phone = _phone("04")
    pnid = _pnid("04")
    client, product = await _seed_kanjivaram(
        replay_session, phone=phone, pnid=pnid
    )

    # Prime: product pinned, agent already asked "would you like to order?"
    from app.models.conversation import Conversation
    from app.models.message import Message
    from datetime import datetime, timezone, timedelta

    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=client.id,
        current_stage="product_inquiry",
        pending_product_sku="KS10001",
    )
    replay_session.add(conv)
    await replay_session.flush()

    replay_session.add(Message(
        conversation_id=conv.id,
        role="model",
        content=(
            "Kanjivaram Silk Saree [KS10001] — ₹3,500 is available. "
            "Would you like to order?"
        ),
        created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    ))
    await replay_session.commit()
    await replay_session.refresh(conv)

    monkeypatch.setattr(
        "app.services.whatsapp_service._raw_send_text_message",
        mock.AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.conversation_flow.classify_buy_intent",
        mock.AsyncMock(return_value=True),
    )

    resp = await send_message(
        replay_http, phone, "yes",
        wamid=_wamid("04a"),
        phone_number_id=pnid,
    )
    assert resp.status_code == 200, resp.text

    conv_r = await replay_session.execute(
        select(Conversation)
        .where(Conversation.id == conv.id)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()

    assert conv_row.current_stage == "order_collection", (
        f"'yes' after order offer must enter order_collection, "
        f"got {conv_row.current_stage!r}"
    )
    assert conv_row.pending_product_sku == "KS10001", (
        f"SKU must remain KS10001 through 'yes', got {conv_row.pending_product_sku!r}"
    )

    print(
        f"\n[NMP-04] test_pinned_then_yes_starts_order: "
        f"stage={conv_row.current_stage!r} sku={conv_row.pending_product_sku!r} ✓"
    )


# ---------------------------------------------------------------------------
# test_gujarati_fallback_reply_stays_gujarati
#
# Captured bug: a customer's FIRST message ever ("તમે સારી વેચો છો?" — Gujarati
# script, no product named) got an English "Which item are you interested in?"
# reply. Root cause: the browsing-safety-net guard (order_pipeline.py, fires
# when the AI leaks transactional phrasing in a browsing stage) built its
# fallback reply as a hardcoded English f-string, ignoring the language
# detected for the current turn entirely. On a customer's first-ever message,
# conv.last_customer_language is still None, so any code that reads only
# that column (and not the freshly detected `language`) silently defaults to
# English on turn one — regardless of what script the customer actually used.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gujarati_fallback_reply_stays_gujarati(replay_http, replay_session, monkeypatch):
    """
    A Gujarati-script message with no named product must still get a
    Gujarati-localized fallback reply when the browsing-safety guard fires —
    never the hardcoded English "Which item are you interested in?" deflect.
    """
    phone = _phone("05")
    pnid = _pnid("05")
    client, product = await _seed_kanjivaram(replay_session, phone=phone, pnid=pnid)
    await _fresh_conv(replay_session, phone=phone, client_id=client.id)

    # AI leaks transactional phrasing → triggers the browsing-safety guard.
    # No product is named in the customer's message, so no SKU gets pinned —
    # this hits the guard's "no pinned product" fallback branch, which is
    # exactly the branch that was hardcoded to English.
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value=(
            "Sure! What is your delivery address? Please pay ₹500 via UPI ID: test@upi."
        )),
    )

    captured = capture_all(monkeypatch)

    resp = await send_message(
        replay_http, phone,
        "તમે સારી વેચો છો?",  # "Do you sell good things?" — Gujarati script
        wamid=_wamid("05a"),
        phone_number_id=pnid,
    )
    assert resp.status_code == 200, resp.text
    assert captured, "No reply was sent"

    reply = captured[-1]
    reply_lower = reply.lower()

    assert "which item" not in reply_lower, (
        f"Fallback guard must not fall back to the hardcoded English deflect "
        f"for a Gujarati-script first message. Got: {reply!r}"
    )
    assert "i'd be happy to help" not in reply_lower, (
        f"Fallback guard reply is still the hardcoded English string. Got: {reply!r}"
    )
    # The customer wrote in Gujarati SCRIPT, so the reply must use the
    # script-accurate "which_item" template (language_templates.
    # GUJARATI_SCRIPT_TEMPLATES), not the romanized one — assert on its
    # distinctive Gujarati-script tail.
    assert "કયું આઇટમ જોઈએ છે" in reply, (
        f"Reply must use the Gujarati SCRIPT which_item template, not the "
        f"romanized fallback. Got: {reply!r}"
    )

    # Confirm the conversation's persisted language was actually set from this
    # turn's detection (gujarati_script), not left at the "english" default.
    from app.models.conversation import Conversation
    conv_r = await replay_session.execute(
        select(Conversation)
        .where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv_row = conv_r.scalar_one()
    assert conv_row.last_customer_language == "gujarati_script", (
        f"Detected language must persist as gujarati_script, got {conv_row.last_customer_language!r}"
    )

    print(f"\n[NMP-05] test_gujarati_fallback_reply_stays_gujarati reply={reply!r} ✓")
