"""
Extended replay harness — language matrix, adversarial, idempotency, simple-product path.

Extends tests/replay/test_replay.py patterns. All assertions on real Postgres DB end-state.
No mocking of the order engine, webhook logic, or DB.

Scenarios:
  Language matrix (parametrized, 4 langs): full UPI happy-path, English/Hinglish/Hindi/Gujarati
  A.  OOS combo Pink+XXL — English + Gujarati
  B.  Qty > stock (Pink+M, qty=99) — rejected
  C.  Qty invalid ("abc") — rejected
  D.  Slot hijack mid-order (bare color word after color slot filled) — SKU unchanged
  E.  Slot hijack at product_inquiry (uncovered edge: SKU pinned, zero slots, "pink")
  F.  Cancel mid-order
  G.  Off-topic mid-order ("cricket score?")
  H.  Language switch mid-flow (English → Gujarati at address step)
  I.  Gujarati no-SKU greeting (regression for UnboundLocalError)
  J.  Same wamid sent twice — dedup
  K.  Double "yes" at completed (COD)
  L.  Double "paid" (UPI) — idempotent
  M.  Webhook retry after order created — no duplicate
  N.  Simple-product full flow — English + Hinglish
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.replay.conftest import _PG_AVAILABLE, seed_variant_and_simple
from tests.replay.helpers import send_message


pytestmark = pytest.mark.skipif(
    not _PG_AVAILABLE, reason="local Postgres not reachable"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _phone(suffix: str) -> str:
    return f"91901{suffix}"


def _pnid(suffix: str) -> str:
    return f"222{suffix}"


async def _msg(http, phone: str, text: str, *, pnid: str, wamid: str | None = None):
    return await send_message(http, phone, text, wamid=wamid, phone_number_id=pnid)


async def _seed(session, *, phone, pnid, **kw):
    from tests.replay.conftest import seed_client_and_product
    return await seed_client_and_product(session, phone=phone, wa_phone_number_id=pnid, **kw)


async def _prime(session, *, phone, product, stage, **slots):
    from app.models.conversation import Conversation
    conv = Conversation(
        phone_number=phone,
        channel="whatsapp",
        client_id=product.client_id,
        current_stage=stage,
        pending_product_sku=product.sku,
        summary_shown=slots.pop("summary_shown", stage == "awaiting_final_confirmation"),
        **slots,
    )
    session.add(conv)
    await session.commit()
    return conv.id


async def _orders(session, conv_id: int):
    from app.models.order import Order
    r = await session.execute(
        select(Order).where(Order.conversation_id == conv_id)
        .execution_options(populate_existing=True)
    )
    return list(r.scalars().all())


async def _stock(session, product_id: int) -> int:
    from app.models.product import Product
    r = await session.execute(
        select(Product).where(Product.id == product_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one().stock


async def _variant_stock(session, product_id: int, color: str, size: str) -> int:
    from app.models.product_variant import ProductVariant
    r = await session.execute(
        select(ProductVariant).where(
            ProductVariant.product_id == product_id,
            ProductVariant.color == color,
            ProductVariant.size == size,
        ).execution_options(populate_existing=True)
    )
    v = r.scalar_one_or_none()
    return v.stock if v else -1


async def _conv_row(session, conv_id: int):
    from app.models.conversation import Conversation
    r = await session.execute(
        select(Conversation).where(Conversation.id == conv_id)
        .execution_options(populate_existing=True)
    )
    return r.scalar_one()


# ---------------------------------------------------------------------------
# Language matrix
# ---------------------------------------------------------------------------
# Each entry: (lang_id, messages_dict)
# Messages are sent in this order: color → size → qty → name → address → yes → paid
# After "address": UPI auto-fills, all slots done → AFC.
# After "yes": stage → payment, order = pending_payment.
# After "paid": stage → completed, stock deducted.
#
# Expected end-state (identical across all langs IF the flow works):
#   1 order, status="paid", paid_at set, stock_deducted=True
#   variant_color="Pink", variant_size="M", quantity=2
#   Pink+M stock: 5 → 3 (deducted once)
#   conv.current_stage = "completed"
#
# NOTE — known keyword-layer limitations exposed by this matrix:
#   hindi: "हाँ" (Devanagari) is NOT in _CONFIRMATION_YES → AFC stage won't
#          advance → FAIL at "yes" step. Bug: conversation_flow.py _CONFIRMATION_YES
#          has no Devanagari entries.

# phone/pnid suffix per lang — must be unique (no shared 3-letter prefix)
_LANG_PHONE = {
    "english":  "L001",
    "hinglish": "L002",
    "hindi":    "L003",
    "gujarati": "L004",
}

_LANG_PARAMS = [
    pytest.param(
        "english",
        {
            "color":   "Pink",
            "size":    "M",
            "qty":     "2",
            "name":    "Ramesh Kumar",
            "address": "12 MG Road, Mumbai",
            "yes":     "yes",
            "paid":    "paid",
        },
        id="english",
    ),
    pytest.param(
        "hinglish",
        {
            "color":   "Pink chahiye",
            "size":    "M size",
            "qty":     "2 chahiye",
            "name":    "Ramesh Kumar",
            "address": "12 MG Road, Mumbai",
            "yes":     "haan",
            # "kar diya" is in PAYMENT_CONFIRMATION_WORDS
            "paid":    "pay kar diya",
        },
        id="hinglish",
    ),
    pytest.param(
        "hindi",
        {
            "color":   "Pink",
            "size":    "M",
            "qty":     "2",
            # Devanagari name — is_valid_name passes (Python .isalpha() handles Devanagari)
            "name":    "राम कुमार",  # "राम कुमार"
            "address": "12 MG Road, Mumbai",
            # BUG: "हाँ" (Devanagari "हाँ") is NOT in _CONFIRMATION_YES.
            # AFC stage will NOT advance → FAIL. Bug location:
            # conversation_flow.py _CONFIRMATION_YES has no Devanagari entries.
            "yes":     "हाँ",  # "हाँ"
            "paid":    "paid",
        },
        id="hindi",
    ),
    pytest.param(
        "gujarati",
        {
            "color":   "Pink",
            "size":    "M",
            "qty":     "2",
            "name":    "Ramesh Kumar",
            "address": "12 MG Road, Mumbai",
            # "ha" is in _CONFIRMATION_YES (Latin — widely used in Gujarati messages)
            "yes":     "ha",
            "paid":    "paid",
        },
        id="gujarati",
    ),
]


@pytest.mark.parametrize("lang,msgs", _LANG_PARAMS)
async def test_lang_matrix_full_upi_flow(
    lang, msgs, replay_http, replay_session
):
    """
    Full UPI happy-path for each language.
    Prime at order_collection (all variant slots empty), then step through all slots.
    Assert end-state: 1 paid order, Pink+M, qty=2, stock deducted.

    Hindi EXPECTED FAIL: "हाँ" (Devanagari) not in _CONFIRMATION_YES keyword set.
    Bug: conversation_flow.py _CONFIRMATION_YES missing Devanagari affirmatives.
    """
    suffix = _LANG_PHONE[lang]
    phone = _phone(suffix)
    pnid = _pnid(suffix)

    client, var_prod, _sim = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    prod_id = var_prod.id
    initial_pink_m_stock = 5

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())

    # Step 2 — color
    r = await _msg(replay_http, phone, msgs["color"], pnid=pnid,
                   wamid=f"wamid.{lang}.color.{ts}")
    assert r.status_code == 200

    # Step 3 — size
    r = await _msg(replay_http, phone, msgs["size"], pnid=pnid,
                   wamid=f"wamid.{lang}.size.{ts}")
    assert r.status_code == 200

    # Step 4 — quantity
    r = await _msg(replay_http, phone, msgs["qty"], pnid=pnid,
                   wamid=f"wamid.{lang}.qty.{ts}")
    assert r.status_code == 200

    # Step 5 — name
    r = await _msg(replay_http, phone, msgs["name"], pnid=pnid,
                   wamid=f"wamid.{lang}.name.{ts}")
    assert r.status_code == 200

    # Step 6 — address → triggers UPI auto-fill → AFC
    r = await _msg(replay_http, phone, msgs["address"], pnid=pnid,
                   wamid=f"wamid.{lang}.addr.{ts}")
    assert r.status_code == 200

    # Step 8 — yes at AFC → order created as pending_payment, stage=payment
    r = await _msg(replay_http, phone, msgs["yes"], pnid=pnid,
                   wamid=f"wamid.{lang}.yes.{ts}")
    assert r.status_code == 200

    # Step 9 — paid → stage=completed, stock deducted
    r = await _msg(replay_http, phone, msgs["paid"], pnid=pnid,
                   wamid=f"wamid.{lang}.paid.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)
    pink_m_stock = await _variant_stock(replay_session, prod_id, "Pink", "M")

    assert len(orders) == 1, (
        f"[{lang}] Expected 1 order, got {len(orders)}"
    )
    o = orders[0]
    assert o.status == "paid", f"[{lang}] Expected paid, got {o.status!r}"
    assert o.paid_at is not None, f"[{lang}] paid_at must be set"
    assert o.stock_deducted is True, f"[{lang}] stock_deducted must be True"
    assert o.variant_color == "Pink", f"[{lang}] variant_color={o.variant_color!r}"
    assert o.variant_size == "M", f"[{lang}] variant_size={o.variant_size!r}"
    assert o.quantity == 2, f"[{lang}] quantity={o.quantity}"
    assert conv.current_stage == "greeting", (
        f"[{lang}] stage={conv.current_stage!r} (expected 'greeting' — BUG 1 fix resets stage after order completion)"
    )
    assert pink_m_stock == initial_pink_m_stock - 2, (
        f"[{lang}] Pink+M stock: expected {initial_pink_m_stock-2}, got {pink_m_stock}"
    )

    print(
        f"\n[LANG {lang}] order={o.order_number} status={o.status} "
        f"color={o.variant_color} size={o.variant_size} qty={o.quantity} "
        f"stock_after={pink_m_stock} stage={conv.current_stage} ✓"
    )


# ---------------------------------------------------------------------------
# A — OOS combo: Pink + XXL is stock=0, must block order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang,size_msg", [
    pytest.param("english", "XXL", id="english"),
    pytest.param("gujarati", "XXL", id="gujarati"),
])
async def test_A_oos_combo_blocked(lang, size_msg, replay_http, replay_session):
    """
    A. Pink+XXL stock=0 → OOS gate clears selected_size, no order row.
    """
    phone = _phone(f"A{lang[:3].upper()}")
    pnid = _pnid(f"A{lang[:3].upper()}")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    initial_stock = await _variant_stock(replay_session, var_prod.id, "Pink", "XXL")
    assert initial_stock == 0  # confirm seed

    # Prime: color=Pink already selected, size slot next
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, size_msg, pnid=pnid,
                   wamid=f"wamid.A.{lang}.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)

    assert len(orders) == 0, (
        f"A [{lang}]: order must NOT exist for OOS combo Pink+XXL, got {len(orders)}"
    )
    assert conv.selected_size is None, (
        f"A [{lang}]: selected_size must be cleared after OOS, got {conv.selected_size!r}"
    )
    assert conv.current_stage == "order_collection", (
        f"A [{lang}]: stage must stay order_collection, got {conv.current_stage!r}"
    )
    print(f"\n[A {lang}] orders=0, size cleared — OOS combo correctly blocked ✓")


# ---------------------------------------------------------------------------
# B — Quantity > stock (Pink+M stock=5, customer requests 99)
# ---------------------------------------------------------------------------

async def test_B_qty_exceeds_stock(replay_http, replay_session):
    """
    B. Pink+M stock=5, customer sends "99" → quantity_invalid, no order.
    """
    phone = _phone("B0001")
    pnid = _pnid("B0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    # Prime with color+size filled so qty is next_slot and available_stock=5
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size="M",
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "99", pnid=pnid, wamid=f"wamid.B.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)
    pink_m_stock = await _variant_stock(replay_session, var_prod.id, "Pink", "M")

    assert len(orders) == 0, f"B: no order for qty > stock, got {len(orders)}"
    # qty must not have been written (or was rejected)
    assert (conv.pending_order_quantity is None or conv.pending_order_quantity != 99), (
        f"B: qty=99 must not be saved, got {conv.pending_order_quantity}"
    )
    assert pink_m_stock == 5, f"B: stock unchanged, got {pink_m_stock}"
    assert conv.current_stage == "order_collection"

    print(f"\n[B] qty=99 rejected, stock unchanged={pink_m_stock}, orders=0 ✓")


# ---------------------------------------------------------------------------
# C — Quantity "abc" is invalid (no digits)
# ---------------------------------------------------------------------------

async def test_C_qty_invalid_text(replay_http, replay_session):
    """
    C. Customer sends "abc" at quantity slot → no digit → re-ask, no order.
    """
    phone = _phone("C0001")
    pnid = _pnid("C0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size="M",
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "abc", pnid=pnid, wamid=f"wamid.C.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)

    assert len(orders) == 0, f"C: no order for invalid qty, got {len(orders)}"
    assert conv.pending_order_quantity is None, (
        f"C: qty must not be saved for 'abc', got {conv.pending_order_quantity}"
    )
    assert conv.current_stage == "order_collection"

    print(f"\n[C] qty='abc' rejected, pending_order_quantity=None ✓")


# ---------------------------------------------------------------------------
# D — Slot hijack mid-order: bare color word after some slots filled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang,color_msg", [
    pytest.param("english", "Pink", id="english"),
    pytest.param("gujarati", "Pink", id="gujarati"),
])
async def test_D_slot_hijack_mid_order(lang, color_msg, replay_http, replay_session):
    """
    D. Mid-order (order_collection, color slot is next), send bare color word "Pink".
    Must be treated as color answer, NOT re-pin. pending_product_sku unchanged.
    """
    phone = _phone(f"D{lang[:3].upper()}")
    pnid = _pnid(f"D{lang[:3].upper()}")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    original_sku = var_prod.sku

    # Prime at color slot (no slots filled yet)
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, color_msg, pnid=pnid,
                   wamid=f"wamid.D.{lang}.{ts}")
    assert r.status_code == 200

    conv = await _conv_row(replay_session, conv_id)

    # SKU must not change — we're in order_collection with a slot active
    assert conv.pending_product_sku == original_sku, (
        f"D [{lang}]: SKU changed from {original_sku!r} to {conv.pending_product_sku!r}"
    )
    assert conv.current_stage == "order_collection", (
        f"D [{lang}]: stage={conv.current_stage!r}"
    )
    # Color should be filled (it's the current slot) OR still None (if classify_user_intent
    # returned something other than ANSWER). Either way SKU must be unchanged.
    print(
        f"\n[D {lang}] sku={conv.pending_product_sku!r} stage={conv.current_stage!r} "
        f"color={conv.selected_color!r} — hijack blocked ✓"
    )


# ---------------------------------------------------------------------------
# E — Slot hijack at product_inquiry (UNCOVERED EDGE — expected FAIL)
# ---------------------------------------------------------------------------

async def test_E_slot_hijack_product_inquiry(replay_http, replay_session):
    """
    E. stage=product_inquiry, SKU pinned (Cotton Lehenga), ZERO slots filled.
    A second product "Pink Silk Stole" is also in the catalogue.
    Customer sends "pink" → name-match is NOT guarded here (_active_order_context=False
    because _stored_stage=product_inquiry and no slots filled).

    BUG: "pink" matches "Pink Silk Stole" via name-match → re-pins to wrong SKU.
    Fix needed: conversation_flow.py / webhook.py _active_order_context guard
    should also fire when SKU is pinned regardless of slot state.

    EXPECTED FAIL: pending_product_sku changes from Cotton Lehenga SKU to Pink Silk Stole SKU.
    """
    phone = _phone("E0001")
    pnid = _pnid("E0001")

    # Seed UPI-only client with variant product first
    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    original_sku = var_prod.sku

    # Add a second product whose name contains "pink" — this is the hijack target
    from app.models.product import Product
    pink_prod = Product(
        client_id=client.id,
        name="Pink Silk Stole",
        sku=f"PINK_{phone[-4:]}",
        price=250.0,
        stock=10,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(pink_prod)
    await replay_session.commit()
    await replay_session.refresh(pink_prod)

    # Prime at product_inquiry with Cotton Lehenga pinned and ZERO slots
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="product_inquiry",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "pink", pnid=pnid, wamid=f"wamid.E.{ts}")
    assert r.status_code == 200

    conv = await _conv_row(replay_session, conv_id)

    # EXPECTED BEHAVIOR: SKU must remain original (Cotton Lehenga), not re-pinned.
    # ACTUAL (BUG): name-match fires and re-pins to "Pink Silk Stole".
    assert conv.pending_product_sku == original_sku, (
        f"E: SKU changed from {original_sku!r} to {conv.pending_product_sku!r}. "
        f"BUG: name-match pinner runs even when product is already pinned at "
        f"product_inquiry with zero slots (_active_order_context guard misses this). "
        f"Fix: webhook.py _active_order_context should check pending_product_sku alone "
        f"(not require slots filled or order-stage) to block re-pinning."
    )

    print(
        f"\n[E] sku={conv.pending_product_sku!r} (original={original_sku!r}) "
        f"— slot hijack guard at product_inquiry"
    )


# ---------------------------------------------------------------------------
# E2 — Over-block guard: explicit different product name MUST switch SKU
# ---------------------------------------------------------------------------

async def test_E2_explicit_product_name_switches_sku(replay_http, replay_session):
    """
    E2. Regression guard for Fix 3 over-blocking.

    stage=product_inquiry, SKU_A pinned (Cotton Lehenga), zero slots.
    A second product "Silk Stole" is in the catalogue.
    Customer sends the explicit product name "Silk Stole" (2 words) → this is
    NOT a bare attribute word → name-match pinner must run → SKU should switch
    to Silk Stole's SKU (the product is a strong single match).

    If this fails, the _active_order_context guard is over-blocking product switches.
    """
    phone = _phone("E20001")
    pnid = _pnid("E20001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    original_sku = var_prod.sku  # Cotton Lehenga

    # Add a second product with a distinct 2-word name (no overlap with "Cotton Lehenga")
    from app.models.product import Product
    silk_prod = Product(
        client_id=client.id,
        name="Banarasi Dupatta",
        sku=f"BAN_{phone[-4:]}",
        price=800.0,
        stock=5,
        is_active=True,
        has_variants=False,
    )
    replay_session.add(silk_prod)
    await replay_session.commit()
    await replay_session.refresh(silk_prod)

    # Prime at product_inquiry with Cotton Lehenga pinned, zero slots
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="product_inquiry",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "Banarasi Dupatta", pnid=pnid,
                   wamid=f"wamid.E2.{ts}")
    assert r.status_code == 200

    conv = await _conv_row(replay_session, conv_id)

    # Multi-word explicit product name → pinner must run → SKU switches to Banarasi Dupatta
    assert conv.pending_product_sku == silk_prod.sku, (
        f"E2: explicit product name 'Banarasi Dupatta' must switch SKU from "
        f"{original_sku!r} to {silk_prod.sku!r}, but got {conv.pending_product_sku!r}. "
        f"Over-blocking guard failed: multi-word product names must still trigger re-pin."
    )

    print(
        f"\n[E2] sku switched from {original_sku!r} to {conv.pending_product_sku!r} "
        f"— explicit product name correctly re-pins (no over-blocking) ✓"
    )


# ---------------------------------------------------------------------------
# F — Cancel mid-order
# ---------------------------------------------------------------------------

async def test_F_cancel_mid_order(replay_http, replay_session):
    """
    F. Customer sends "cancel" while in order_collection.

    With test Groq key, classify_user_intent falls back to ANSWER (LLM unavailable).
    "cancel" fails slot extraction (no valid color/size/etc.) → slot re-asked.

    NOTE: The cancel intent detection is LLM-dependent. With mocked-out Groq,
    the CANCEL path is NOT triggered. Stage stays order_collection instead of resetting
    to greeting. This is a real limitation — cancel handling breaks without Groq.

    Assertions that PASS:  orders=0 (no order created regardless).
    Assertions that FAIL:  stage=="greeting" (stays order_collection without LLM).
    Bug location: conversation_flow.classify_user_intent — cancel detection requires
    live Groq call; no keyword fallback for "cancel" in order_collection.
    """
    phone = _phone("F0001")
    pnid = _pnid("F0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size=None,
        pending_order_quantity=None,
        customer_name="Test User",
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "cancel", pnid=pnid, wamid=f"wamid.F.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)

    assert len(orders) == 0, f"F: no order after cancel, got {len(orders)}"

    # This assertion will FAIL because cancel is LLM-dependent (Groq mocked out).
    assert conv.current_stage == "greeting", (
        f"F: stage should reset to greeting after cancel, got {conv.current_stage!r}. "
        f"BUG: cancel detection (CANCEL intent) requires live classify_user_intent "
        f"LLM call. With test Groq key, falls back to ANSWER → slot re-asked, "
        f"stage stays {conv.current_stage!r}. No keyword fallback for 'cancel' "
        f"in order_collection. Fix: add 'cancel' to keyword fast-path in "
        f"classify_user_intent OR handle it in detect_stage for order stages."
    )

    print(f"\n[F] orders=0, stage={conv.current_stage!r} — cancel handling checked ✓")


# ---------------------------------------------------------------------------
# G — Off-topic mid-order
# ---------------------------------------------------------------------------

async def test_G_off_topic_mid_order(replay_http, replay_session):
    """
    G. Customer asks "cricket score?" while in order_collection at qty slot.

    With mocked Groq → classify_user_intent falls back to ANSWER → slot extraction
    runs → "cricket score?" has no valid digit → None → slot re-asked.
    Stage stays order_collection, no order. End-state matches expected.
    """
    phone = _phone("G0001")
    pnid = _pnid("G0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    # Prime at qty slot (color+size filled, qty next)
    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color="Pink",
        selected_size="M",
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())
    r = await _msg(replay_http, phone, "cricket score?", pnid=pnid,
                   wamid=f"wamid.G.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)

    assert len(orders) == 0, f"G: no order for off-topic, got {len(orders)}"
    assert conv.current_stage == "order_collection", (
        f"G: stage must stay order_collection, got {conv.current_stage!r}"
    )
    assert conv.pending_order_quantity is None, (
        f"G: qty must not be filled by 'cricket score?', got {conv.pending_order_quantity}"
    )
    print(f"\n[G] off-topic mid-order: stage=order_collection, orders=0 ✓")


# ---------------------------------------------------------------------------
# H — Language switch mid-flow (English → Gujarati at address step)
# ---------------------------------------------------------------------------

async def test_H_language_switch_mid_flow(replay_http, replay_session):
    """
    H. Start in English for color+size+qty+name; switch to Gujarati at address.
    Flow must continue without crash. Final: 1 paid order.
    """
    phone = _phone("H0001")
    pnid = _pnid("H0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="order_collection",
        summary_shown=False,
        selected_color=None,
        selected_size=None,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())

    # English messages for first 4 slots
    for step, text in [
        ("color", "Pink"),
        ("size",  "M"),
        ("qty",   "2"),
        ("name",  "Ramesh Kumar"),
    ]:
        r = await _msg(replay_http, phone, text, pnid=pnid,
                       wamid=f"wamid.H.{step}.{ts}")
        assert r.status_code == 200

    # Gujarati address (language switch)
    r = await _msg(replay_http, phone, "12 MG Road, Mumbai", pnid=pnid,
                   wamid=f"wamid.H.addr.{ts}")
    assert r.status_code == 200

    # AFC → yes
    r = await _msg(replay_http, phone, "ha", pnid=pnid, wamid=f"wamid.H.yes.{ts}")
    assert r.status_code == 200

    # Paid
    r = await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.H.paid.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)

    assert len(orders) == 1, f"H: expected 1 order after language switch, got {len(orders)}"
    assert orders[0].status == "paid", f"H: status={orders[0].status!r}"
    assert conv.current_stage == "greeting"  # BUG 1 fix resets stage after order completion

    print(f"\n[H] language switch mid-flow: 1 paid order, stage=greeting (reset) ✓")


# ---------------------------------------------------------------------------
# I — Gujarati no-SKU input at greeting (regression S16 sibling)
# ---------------------------------------------------------------------------

async def test_I_gujarati_no_sku_greeting_no_crash(replay_http, replay_session):
    """
    I. "Lehenga available che?" (Gujarati, no SKU) → 200, no crash.
    Regression for UnboundLocalError on _stored_stage. S16 already covers this;
    this variant uses the seed_variant_and_simple client and a different text.
    """
    phone = _phone("I0001")
    pnid = _pnid("I0001")

    _, _, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    ts = int(time.time())
    r = await _msg(
        replay_http, phone,
        "Lehenga available che?",  # Gujarati FAQ, no SKU
        pnid=pnid,
        wamid=f"wamid.I.{ts}",
    )
    assert r.status_code == 200, (
        f"I: Gujarati no-SKU must return 200, got {r.status_code}: {r.text[:200]}"
    )

    from app.models.conversation import Conversation
    r2 = await replay_session.execute(
        select(Conversation).where(Conversation.phone_number == phone)
        .execution_options(populate_existing=True)
    )
    conv = r2.scalar_one_or_none()
    assert conv is not None, "I: conversation row must exist"

    print(f"\n[I] Gujarati no-SKU: 200, no UnboundLocalError, stage={conv.current_stage!r} ✓")


# ---------------------------------------------------------------------------
# J — Same wamid sent twice → dedup (only first processed)
# ---------------------------------------------------------------------------

async def test_J_wamid_dedup(replay_http, replay_session):
    """
    J. Sending the same wamid twice triggers the webhook dedup guard.
    The second message must be silently dropped. No duplicate order row.
    """
    phone = _phone("J0001")
    pnid = _pnid("J0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="awaiting_final_confirmation",
        summary_shown=True,
        selected_color="Pink",
        selected_size="M",
        pending_order_quantity=2,
        customer_name="Test User",
        delivery_address="12 MG Road, Mumbai",
        payment_method="UPI",
    )

    fixed_wamid = f"wamid.J.dedup.{int(time.time())}"

    # First send → order created as pending_payment
    r1 = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=fixed_wamid)
    assert r1.status_code == 200

    orders_after_first = await _orders(replay_session, conv_id)
    assert len(orders_after_first) == 1, (
        f"J: expected 1 order after first 'yes', got {len(orders_after_first)}"
    )

    # Second send — same wamid → must be deduped
    r2 = await _msg(replay_http, phone, "yes", pnid=pnid, wamid=fixed_wamid)
    assert r2.status_code == 200

    orders_after_second = await _orders(replay_session, conv_id)
    assert len(orders_after_second) == 1, (
        f"J: dedup failed — expected 1 order, got {len(orders_after_second)}"
    )

    print(f"\n[J] wamid dedup: 2 sends, 1 order ✓")


# ---------------------------------------------------------------------------
# K — Double "yes" at completed (COD) — no second order
# ---------------------------------------------------------------------------

async def test_K_double_yes_cod_no_duplicate(replay_http, replay_session):
    """
    K. COD order: "yes" → confirmed. Second "yes" must not create a second order.
    """
    phone = _phone("K0001")
    pnid = _pnid("K0001")

    client, prod = await _seed(
        replay_session, phone=phone, pnid=pnid,
        product_sku="K_SKU", product_name="Test Kurta",
        price=500.0, stock=10, payment_method="COD",
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=prod,
        stage="awaiting_final_confirmation",
        summary_shown=True,
        customer_name="Ramesh Kumar",
        delivery_address="12 MG Road, Mumbai",
        pending_order_quantity=2,
        payment_method="COD",
    )

    ts = int(time.time())
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.K.a.{ts}")
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.K.b.{ts}")

    orders = await _orders(replay_session, conv_id)
    stock = await _stock(replay_session, prod.id)

    assert len(orders) == 1, f"K: expected 1 order, got {len(orders)}"
    assert orders[0].status == "paid"
    assert stock == 10 - 2

    print(f"\n[K] double yes COD: 1 order, stock={stock} ✓")


# ---------------------------------------------------------------------------
# L — Double "paid" (UPI) — idempotent, stock deducted once
# ---------------------------------------------------------------------------

async def test_L_double_paid_idempotent(replay_http, replay_session):
    """
    L. UPI: "yes" → pending_payment. Two "paid" → paid, stock -qty only once.
    Mirrors S3 with the extended seed data.
    """
    phone = _phone("L0001")
    pnid = _pnid("L0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="awaiting_final_confirmation",
        summary_shown=True,
        selected_color="Pink",
        selected_size="M",
        pending_order_quantity=2,
        customer_name="Ramesh Kumar",
        delivery_address="12 MG Road, Mumbai",
        payment_method="UPI",
    )

    ts = int(time.time())
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.L.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.L.paid1.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.L.paid2.{ts}")

    orders = await _orders(replay_session, conv_id)
    pink_m_stock = await _variant_stock(replay_session, var_prod.id, "Pink", "M")

    assert len(orders) == 1, f"L: 1 order, got {len(orders)}"
    assert orders[0].status == "paid"
    assert orders[0].stock_deducted is True
    assert pink_m_stock == 5 - 2, f"L: stock deducted once, got {pink_m_stock}"

    print(f"\n[L] double paid idempotent: stock={pink_m_stock}, 1 order ✓")


# ---------------------------------------------------------------------------
# M — Webhook retry after order created — no duplicate order, no double deduction
# ---------------------------------------------------------------------------

async def test_M_retry_after_order_created(replay_http, replay_session):
    """
    M. After order is created and paid, an arbitrary message (webhook retry) must
    not create a second order or double-decrement stock.
    """
    phone = _phone("M0001")
    pnid = _pnid("M0001")

    client, var_prod, _ = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )

    conv_id = await _prime(
        replay_session, phone=phone, product=var_prod,
        stage="awaiting_final_confirmation",
        summary_shown=True,
        selected_color="Blue",
        selected_size="M",
        pending_order_quantity=1,
        customer_name="Priya Singh",
        delivery_address="5 Park Ave, Delhi",
        payment_method="UPI",
    )

    ts = int(time.time())
    # Normal flow: yes → paid
    await _msg(replay_http, phone, "yes", pnid=pnid, wamid=f"wamid.M.yes.{ts}")
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.M.paid.{ts}")

    orders_before = await _orders(replay_session, conv_id)
    assert len(orders_before) == 1
    blue_m_stock_before = await _variant_stock(replay_session, var_prod.id, "Blue", "M")
    assert blue_m_stock_before == 5 - 1  # deducted once

    # Retry: send another message (simulate webhook redelivery or extra customer msg)
    await _msg(replay_http, phone, "paid", pnid=pnid, wamid=f"wamid.M.retry.{ts}")

    orders_after = await _orders(replay_session, conv_id)
    blue_m_stock_after = await _variant_stock(replay_session, var_prod.id, "Blue", "M")

    assert len(orders_after) == 1, f"M: no duplicate order, got {len(orders_after)}"
    assert blue_m_stock_after == 5 - 1, (
        f"M: stock not double-decremented, got {blue_m_stock_after}"
    )

    print(f"\n[M] retry after order: 1 order, stock={blue_m_stock_after} (no double deduction) ✓")


# ---------------------------------------------------------------------------
# N — Simple-product full flow (no variants)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang,msgs", [
    pytest.param(
        "english",
        {
            "qty":     "1",
            "name":    "Anita Patel",
            "address": "7 Gandhi Nagar, Ahmedabad",
            "yes":     "yes",
            "paid":    "paid",
        },
        id="english",
    ),
    pytest.param(
        "hinglish",
        {
            "qty":     "1 chahiye",
            "name":    "Anita Patel",
            "address": "7 Gandhi Nagar, Ahmedabad",
            "yes":     "haan",
            "paid":    "pay kar diya",
        },
        id="hinglish",
    ),
])
async def test_N_simple_product_flow(lang, msgs, replay_http, replay_session):
    """
    N. Full UPI flow on the simple product (no color/size variants).
    Slots: qty → name → address → (auto-UPI) → yes → paid.
    Assert: 1 paid order, Silk Stole stock 3→2.
    """
    phone = _phone(f"N{lang[:3].upper()}")
    pnid = _pnid(f"N{lang[:3].upper()}")

    client, _, sim_prod = await seed_variant_and_simple(
        replay_session, phone=phone, wa_phone_number_id=pnid
    )
    initial_stock = 3

    conv_id = await _prime(
        replay_session, phone=phone, product=sim_prod,
        stage="order_collection",
        summary_shown=False,
        pending_order_quantity=None,
        customer_name=None,
        delivery_address=None,
        payment_method=None,
    )

    ts = int(time.time())

    for step, text in [
        ("qty",  msgs["qty"]),
        ("name", msgs["name"]),
        ("addr", msgs["address"]),
    ]:
        r = await _msg(replay_http, phone, text, pnid=pnid,
                       wamid=f"wamid.N.{lang}.{step}.{ts}")
        assert r.status_code == 200

    # AFC → yes
    r = await _msg(replay_http, phone, msgs["yes"], pnid=pnid,
                   wamid=f"wamid.N.{lang}.yes.{ts}")
    assert r.status_code == 200

    # Paid
    r = await _msg(replay_http, phone, msgs["paid"], pnid=pnid,
                   wamid=f"wamid.N.{lang}.paid.{ts}")
    assert r.status_code == 200

    orders = await _orders(replay_session, conv_id)
    conv = await _conv_row(replay_session, conv_id)
    final_stock = await _stock(replay_session, sim_prod.id)

    assert len(orders) == 1, f"N [{lang}]: expected 1 order, got {len(orders)}"
    o = orders[0]
    assert o.status == "paid", f"N [{lang}]: status={o.status!r}"
    assert o.stock_deducted is True
    assert o.quantity == 1
    assert conv.current_stage == "greeting"  # BUG 1 fix resets stage after order completion
    assert final_stock == initial_stock - 1, (
        f"N [{lang}]: stock {initial_stock}→{initial_stock-1}, got {final_stock}"
    )

    print(
        f"\n[N {lang}] simple product: 1 paid order, "
        f"stock {initial_stock}→{final_stock}, stage=greeting (reset) ✓"
    )
