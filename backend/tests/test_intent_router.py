"""Unit tests for the ROUTER_V2 LLM intent router (no DB): schema, flag, prompt, fast paths, guards, ETA, search."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.router import RouterAction, RouterDecision
from app.services import delivery_service, intent_router, llm_client, llm_health, llm_usage_service, router_actions
from app.services.language_templates import TEMPLATES, get_template

SETTINGS_KW = dict(
    database_url="postgresql://u:p@h/d", meta_app_secret="x", meta_verify_token="x",
    whatsapp_access_token="x", whatsapp_phone_number_id="1", _env_file=None,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def variant(color, size, stock, material=None):
    """Fake ProductVariant row."""
    return SimpleNamespace(color=color, size=size, material=material, stock=stock, is_active=True)


def product(sku, name, price=500.0, category="Lehenga", variants=None, stock=5, description=None):
    """Fake Product row (variants=None → simple product)."""
    return SimpleNamespace(
        id=hash(sku) % 10_000, sku=sku, name=name, price=price, category=category, stock=stock, is_active=True,
        has_variants=bool(variants), variants=variants or [], description=description, image_url=None,
        delivery_days=None,
    )


def lehenga():
    """Cotton Lehenga: Pink M (5), Pink XXL (0), Blue M (5), Blue XXL (3)."""
    return product("LH100", "Cotton Lehenga", variants=[
        variant("Pink", "M", 5), variant("Pink", "XXL", 0), variant("Blue", "M", 5), variant("Blue", "XXL", 3),
    ])


def ctx(**kw):
    """Minimal RouterContext for sanitise/handler tests."""
    base = dict(shop="", state="", orders="", candidates="", history="", stage="greeting",
                next_slot=None, pinned_sku=None, candidate_products=[], allowed_skus=set(), order_rows=[])
    base.update(kw)
    return intent_router.RouterContext(**base)


# ── flag ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ids,client_id,expected", [
    ("1", 1, True), ("1", 2, False), ("", 1, False), ("*", 99, True), ("2, 5", 5, True), ("2, 5", 1, False),
])
def test_router_enabled_follows_env_ids(monkeypatch, ids, client_id, expected):
    """With no per-client override, ROUTER_V2_CLIENT_IDS decides ('*' = all, '' = none)."""
    monkeypatch.setattr(intent_router, "get_settings", lambda: SimpleNamespace(router_v2_client_ids=ids))
    assert intent_router.router_enabled(SimpleNamespace(id=client_id, router_v2_enabled=None)) is expected


def test_router_enabled_client_override_beats_env(monkeypatch):
    """Client.router_v2_enabled True/False wins over the env list; None clients are off."""
    monkeypatch.setattr(intent_router, "get_settings", lambda: SimpleNamespace(router_v2_client_ids="1"))
    assert intent_router.router_enabled(SimpleNamespace(id=1, router_v2_enabled=False)) is False
    assert intent_router.router_enabled(SimpleNamespace(id=7, router_v2_enabled=True)) is True
    assert intent_router.router_enabled(None) is False


def test_router_default_is_on_for_client_1_only(monkeypatch):
    """Out of the box: ON for client_id=1 only, confidence threshold 0.5 (the suite-wide env override is cleared here)."""
    monkeypatch.delenv("ROUTER_V2_CLIENT_IDS", raising=False)
    s = Settings(**SETTINGS_KW)
    assert s.router_v2_client_ids == "1" and s.router_confidence_threshold == 0.5


# ── prompt ───────────────────────────────────────────────────────────────────

def test_prompt_has_json_word_placeholders_and_version():
    """JSON mode needs the word 'JSON' in the system prompt; every placeholder is present."""
    p = intent_router.load_prompt()
    assert "JSON" in p.system
    for name in ("shop", "state", "orders", "candidates", "history", "message"):
        assert "{{" + name + "}}" in p.user_template
    assert p.version.startswith("router_v2")
    assert "<!--" not in p.system


def test_prompt_lists_every_action():
    """The SYSTEM prompt documents all twelve actions (so the model can't be missing one)."""
    system = intent_router.load_prompt().system
    for action in RouterAction:
        assert action.value in system


def test_render_user_prompt_fills_placeholders():
    """render_user_prompt substitutes all six blocks; braces inside data are untouched."""
    t = intent_router.load_prompt().user_template
    out = intent_router.render_user_prompt(
        t, shop="S{x}", state="ST", orders="OR", candidates="CA", history="HI", message="MSG")
    assert "{{" not in out and "S{x}" in out and out.rstrip().endswith("Return the JSON object only.")


# ── schema ───────────────────────────────────────────────────────────────────

def test_decision_parses_spec_shape():
    """The spec's example output validates and keeps typed args."""
    d = RouterDecision.model_validate({
        "action": "search_catalog",
        "args": {"query": "saree", "filters": {"color": "green", "max_price": "₹1,000"}},
        "language": "Hinglish", "confidence": 0.9, "reply_hint": "",
    })
    assert d.action == RouterAction.SEARCH_CATALOG and d.args.filters.max_price == 1000.0
    assert d.language == "hinglish" and d.reply_hint is None


def test_decision_coercions_are_forgiving_but_action_is_strict():
    """Sloppy-but-recoverable output is repaired; an unknown action is a validation error."""
    d = RouterDecision.model_validate({
        "action": " Change_Slot ", "args": {"slot": "quantity", "value": 2, "focus": "ETA", "filters": None},
        "language": "english", "confidence": "85",
    })
    assert d.args.value == "2" and d.confidence == pytest.approx(0.85) and d.language == "en"
    assert RouterDecision.model_validate({"action": "order_status", "args": {"focus": "eta"}}).args.focus == "delivery"
    assert RouterDecision.model_validate({"action": "greeting", "args": None}).args.sku is None
    with pytest.raises(ValidationError):
        RouterDecision.model_validate({"action": "book_flight", "args": {}})


def test_unparsable_confidence_forces_clarify_range():
    """Garbage confidence becomes 0 (→ the engine asks a clarifying question)."""
    assert RouterDecision.model_validate({"action": "smalltalk", "confidence": "high"}).confidence == 0.0


# ── sanitise ─────────────────────────────────────────────────────────────────

def _decision(**kw):
    """RouterDecision from kwargs."""
    return RouterDecision.model_validate(kw)


def test_sku_not_in_candidates_is_dropped_and_search_used_instead():
    """A sku the model wasn't given never reaches the engine; with a query it becomes a catalogue search."""
    c = ctx(allowed_skus={"LH100"}, candidate_products=[lehenga()])
    d, notes = intent_router.sanitize_decision(
        _decision(action="show_product", args={"sku": "ZZ99999", "query": "lehenga"}, confidence=0.9), c)
    assert d.action == RouterAction.SEARCH_CATALOG and d.args.sku is None and "dropped_sku=ZZ99999" in notes


def test_sku_dropped_without_query_becomes_low_confidence():
    """No usable sku and nothing to search for → confidence ≤ 0.3 so the engine clarifies."""
    d, _ = intent_router.sanitize_decision(_decision(action="start_order", args={"sku": "NOPE12345"}, confidence=0.9), ctx())
    assert d.confidence <= 0.3


def test_sku_case_is_canonicalised_to_catalogue_spelling():
    """'lh100' from the model is mapped back onto the candidate's real sku."""
    p = lehenga()
    d, _ = intent_router.sanitize_decision(
        _decision(action="show_product", args={"sku": "lh100"}, confidence=0.9),
        ctx(allowed_skus={"LH100"}, candidate_products=[p]))
    assert d.args.sku == "LH100"


def test_answer_slot_without_open_slot_in_order_becomes_change_slot():
    """No question is open but an order is in progress → it's an edit, not an answer."""
    d, notes = intent_router.sanitize_decision(
        _decision(action="answer_slot", args={"slot": "qty", "value": "2"}, confidence=0.9),
        ctx(stage="awaiting_final_confirmation", next_slot=None))
    assert d.action == RouterAction.CHANGE_SLOT and d.args.slot == "quantity"


def test_slot_action_missing_value_is_low_confidence():
    """change_slot with no value can't be executed → clarify."""
    d, _ = intent_router.sanitize_decision(
        _decision(action="change_slot", args={"slot": "size"}, confidence=0.95), ctx(stage="order_collection"))
    assert d.confidence <= 0.3


# ── fast paths ───────────────────────────────────────────────────────────────

async def _fast(text, *, stage="greeting", next_slot=None, conv=None, products=None, vi=None, mtype="text"):
    """Run fast_path_reason with light fakes."""
    conv = conv or SimpleNamespace(pending_choice_skus=None)
    return await intent_router.fast_path_reason(
        conv, SimpleNamespace(type=mtype), text, stage, pinned_product=None, variant_info=vi or {},
        all_products=products or [], next_slot=next_slot,
    )


async def test_fast_path_buttons_and_non_text():
    """Button taps and images/other types never go to the router."""
    assert await _fast("anything", mtype="interactive") == "button"
    assert await _fast("", mtype="image") == "non_text"


async def test_fast_path_exact_sku_and_exact_name():
    """The whole message being a SKU or the exact product name is a ₹0 path."""
    ps = [lehenga()]
    assert await _fast("lh100", products=ps) == "exact_sku"
    assert await _fast("Cotton Lehenga", products=ps) == "exact_name"
    assert await _fast("cotton lehenga price?", products=ps) is None


async def test_fast_path_menu_pick_and_numbers():
    """A number with an open menu is a pick; a bare number outside any order/menu goes to the router."""
    conv = SimpleNamespace(pending_choice_skus='["A","B"]')
    assert await _fast("2", conv=conv) == "menu_pick"
    assert await _fast("2") is None
    assert await _fast("2", stage="order_collection") == "number_reply"


async def test_fast_path_slot_options():
    """Exact option / quantity / payment word the open question expects."""
    vi = {"available_colors": ["Pink", "Blue"], "available_sizes": ["M", "XXL"]}
    assert await _fast("pink", stage="order_collection", next_slot="color", vi=vi) == "slot_option"
    assert await _fast("pink wala", stage="order_collection", next_slot="color", vi=vi) is None
    assert await _fast("cod", stage="order_collection", next_slot="payment_method") == "slot_payment"
    assert await _fast("9876543210", stage="order_collection", next_slot="mobile_number") == "slot_phone"
    assert await _fast("yes", stage="awaiting_final_confirmation") == "gate_word"


async def test_bare_yes_no_is_a_fast_path_only_while_a_prompt_is_open():
    """'yes' after the product card (pinned product) is ₹0; a 'yes' with nothing open goes to the router."""
    pinned = SimpleNamespace(pending_choice_skus=None, pending_product_sku="LH100", interrupted_sku=None)
    assert await _fast("yes", stage="product_inquiry", conv=pinned) == "gate_word"
    assert await _fast("Nahi", stage="product_inquiry", conv=pinned) == "gate_word"
    assert await _fast("yes", stage="greeting") is None


async def test_free_text_slots_are_not_fast_paths():
    """A name/address answer — or 'talk to someone' typed where a name is expected — must reach the router."""
    assert await _fast("I want to talk to someone", stage="order_collection", next_slot="customer_name") is None
    assert await _fast("Amit Kumar", stage="order_collection", next_slot="customer_name") is None


async def test_status_greeting_and_catalogue_paste_routing():
    """Keyword-ish open messages go to the router; the catalogue 'Order' paste stays on the legacy parser."""
    assert await _fast("kab aayega mera parcel") is None
    assert await _fast("hi") is None
    assert await _fast("Product: X\nSKU: LH100\nColor: Pink") == "catalogue_order"


# ── context ──────────────────────────────────────────────────────────────────

def test_history_is_redacted_and_state_never_contains_pii():
    """Phones, emails and pincodes are masked in history; the state block reports name/address as 'set'."""
    rows = [SimpleNamespace(role="user", content="call 9876543210 or a@b.com 395007 https://x.y/z")]
    h = intent_router._history_block(rows)
    assert "9876543210" not in h and "a@b.com" not in h and "395007" not in h and "https" not in h
    conv = SimpleNamespace(
        selected_color="Pink", selected_size=None, selected_material=None, pending_order_quantity=3,
        payment_method=None, customer_name="Amit Marthak", delivery_address="12 MG Road Surat 395007",
        mobile_number="9876543210", summary_shown=False, pending_choice_skus=None)
    state = intent_router._state_block(conv, "order_collection", lehenga(), {}, "size")
    assert "Amit" not in state and "MG Road" not in state and "9876543210" not in state
    assert "name=set" in state and "address=set" in state and "quantity=3" in state and "next_slot: size" in state


def test_candidates_include_typed_sku_and_pinned_product():
    """Typed SKUs lead the candidate list and the pinned product is always in reach."""
    a, b, c = lehenga(), product("SR27754", "Silk Saree", category="Saree"), product("KU11111", "Kurti", category="Kurti")
    picked = intent_router.pick_candidates([a, b, c], "price of sr27754", pinned_product=c)
    assert picked[0] is b and c in picked


# ── LLM call (real chat_json + llm_call, fake provider) ──────────────────────

@pytest.fixture
def provider(monkeypatch, mock_settings):
    """Fake Groq client + captured llm_usage rows; breaker reset."""
    llm_health.reset_state()
    rows: list[dict] = []

    async def _capture(row):
        """Record usage rows instead of persisting."""
        rows.append(dict(row))

    monkeypatch.setattr(llm_usage_service, "_persist_row", _capture)
    fake = MagicMock()
    monkeypatch.setattr(llm_client, "get_client", lambda: fake)
    monkeypatch.setattr(intent_router, "get_settings", lambda: SimpleNamespace(
        llm_model_classifier="classifier-model", router_max_tokens=300))
    fake.rows = rows
    yield fake
    llm_health.reset_state()


def _resp(content, prompt=900, completion=60):
    """Fake chat-completion response."""
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def test_call_router_json_mode_purpose_model_and_usage_row(provider):
    """One call: classifier model, JSON mode with 'JSON' in the prompt, recorded as purpose='router'."""
    provider.chat.completions.create = AsyncMock(return_value=_resp(json.dumps(
        {"action": "order_status", "args": {"focus": "delivery"}, "language": "hi", "confidence": 0.9})))
    call = await intent_router.call_router(ctx(), "kab aayega mera parcel", client_id=1, conversation_id=7)
    assert call.decision.action == RouterAction.ORDER_STATUS and call.decision.args.focus == "delivery"
    kwargs = provider.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "classifier-model" and kwargs["response_format"] == {"type": "json_object"}
    assert "JSON" in kwargs["messages"][0]["content"]
    assert "kab aayega mera parcel" in kwargs["messages"][1]["content"]
    assert [r["purpose"] for r in provider.rows] == ["router"]
    assert provider.rows[0]["client_id"] == 1 and provider.rows[0]["conversation_id"] == 7
    assert provider.rows[0]["prompt_tokens"] == 900


async def test_call_router_invalid_then_valid_retries_once(provider):
    """Invalid JSON → one retry → valid decision; both attempts are billed rows."""
    provider.chat.completions.create = AsyncMock(side_effect=[
        _resp("sorry I cannot"), _resp('{"action":"greeting","args":{},"confidence":0.9}')])
    call = await intent_router.call_router(ctx(), "hi", client_id=1, conversation_id=1)
    assert call.decision.action == RouterAction.GREETING
    assert provider.chat.completions.create.await_count == 2 and len(provider.rows) == 2


async def test_call_router_invalid_twice_is_a_reported_failure(provider):
    """Two invalid outputs → no decision, error 'invalid_json', failure counted (never silent)."""
    provider.chat.completions.create = AsyncMock(return_value=_resp('{"action":"teleport"}'))
    call = await intent_router.call_router(ctx(), "hi", client_id=1, conversation_id=1)
    assert call.decision is None and call.error == "invalid_json"
    assert provider.chat.completions.create.await_count == 2
    assert llm_health.failures_last_hour() >= 1


async def test_call_router_provider_error_and_open_breaker_never_raise(provider, monkeypatch):
    """API errors and an open breaker return an error result so the caller can fall back."""
    provider.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
    call = await intent_router.call_router(ctx(), "hi", client_id=1, conversation_id=1)
    assert call.decision is None and call.error.startswith("llm_error")
    monkeypatch.setattr(llm_health, "should_attempt_llm", lambda: False)   # breaker open, no probe due
    call = await intent_router.call_router(ctx(), "hi", client_id=1, conversation_id=1)
    assert call.decision is None and call.error.startswith("llm_unavailable")


# ── general_answer guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("hint", [
    "Your order is dispatched.", "It costs ₹500.", "Delivery in 3 days!", "Use code SR27754.",
    "We have it in stock.", "You can pay via UPI.", "Rs 200 only", "Order confirmed",
])
def test_guard_rejects_hints_that_state_engine_facts(hint):
    """Prices, numbers, SKUs, stock/delivery/payment/order claims never reach the customer."""
    assert router_actions.guard_reply_hint(hint, []) is None


def test_guard_passes_and_trims_to_two_sentences():
    """A harmless hint passes, trimmed to two sentences."""
    out = router_actions.guard_reply_hint("Cotton breathes well. Pastel shades suit weddings. Enjoy your day!", [])
    assert out == "Cotton breathes well. Pastel shades suit weddings."
    assert router_actions.guard_reply_hint("", []) is None


# ── ETA ──────────────────────────────────────────────────────────────────────

def test_add_business_days_skips_weekends():
    """Fri 9 Oct 2026 + 3 business days = Wed 14 Oct."""
    assert delivery_service.add_business_days(date(2026, 10, 9), 3) == date(2026, 10, 14)
    assert delivery_service.add_business_days(date(2026, 10, 9), 0) == date(2026, 10, 9)


def _order(**kw):
    """Fake Order row."""
    base = dict(status="paid", payment_method="UPI", created_at=datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc),
                paid_at=datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc), confirmed_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_eta_window_from_payment_date_and_client_days():
    """Paid Tue 6 Oct, 3–7 business days → 9 Oct – 15 Oct."""
    client = SimpleNamespace(delivery_days_min=3, delivery_days_max=7)
    eta = delivery_service.order_eta_window(_order(), client, today=date(2026, 10, 7))
    assert eta["state"] == "window" and eta["from"] == date(2026, 10, 9) and eta["to"] == date(2026, 10, 15)


def test_eta_states_awaiting_overdue_none_and_cod():
    """Unpaid UPI waits; COD starts at creation; late orders are 'overdue'; delivered/cancelled have none."""
    client = SimpleNamespace(delivery_days_min=3, delivery_days_max=7)
    assert delivery_service.order_eta_window(_order(status="pending_payment", paid_at=None), client)["state"] == "awaiting"
    assert delivery_service.order_eta_window(_order(status="new", payment_method="COD", paid_at=None), client,
                                             today=date(2026, 10, 6))["state"] == "window"
    assert delivery_service.order_eta_window(_order(), client, today=date(2026, 12, 1))["state"] == "overdue"
    assert delivery_service.order_eta_window(_order(status="delivered"), client)["state"] == "none"
    assert delivery_service.order_eta_window(_order(status="cancelled"), client)["state"] == "none"


def test_product_delivery_days_override_client_window():
    """Every line having delivery_days → the slowest line wins as a fixed window."""
    client = SimpleNamespace(delivery_days_min=3, delivery_days_max=7)
    assert delivery_service.order_delivery_days(client, [2, 4]) == (4, 4)
    assert delivery_service.order_delivery_days(client, [2, None]) == (3, 7)
    assert delivery_service.order_delivery_days(None, None) == (3, 7)


# ── catalogue search / alternatives ──────────────────────────────────────────

def _catalogue():
    """Small catalogue: sarees (one green ≤1000), a lehenga, a kurti."""
    return [
        product("SR10001", "Green Cotton Saree", 899, "Saree", variants=[variant("Green", None, 4)]),
        product("SR10002", "Red Silk Saree", 2499, "Saree", variants=[variant("Red", None, 2)]),
        product("SR10003", "Blue Cotton Saree", 950, "Saree", variants=[variant("Blue", None, 3)]),
        lehenga(),
        product("KU20001", "Printed Kurti", 450, "Kurti", variants=[variant("Yellow", "L", 6)]),
    ]


def test_search_applies_color_and_price_filters():
    """'green saree under 1000' → only the green saree priced ≤ 1000."""
    f = RouterDecision.model_validate({"action": "search_catalog", "args": {
        "query": "saree", "filters": {"color": "green", "max_price": 1000}}}).args.filters
    hits = router_actions.search_products(_catalogue(), "saree", f)
    assert [p.sku for p in hits] == ["SR10001"]


def test_search_size_filter_uses_in_stock_variants():
    """XXL lehenga: only Blue XXL has stock (Pink XXL is 0)."""
    f = RouterDecision.model_validate({"action": "search_catalog", "args": {"query": "lehenga", "filters": {"size": "XXL", "color": "pink"}}}).args.filters
    assert router_actions.search_products(_catalogue(), "lehenga", f) == []
    f2 = RouterDecision.model_validate({"action": "search_catalog", "args": {"query": "lehenga", "filters": {"size": "xxl", "color": "blue"}}}).args.filters
    assert [p.sku for p in router_actions.search_products(_catalogue(), "lehenga", f2)] == ["LH100"]


def test_alternatives_prefer_same_category_and_nearby_price_and_exclude_oos():
    """Nothing matches 'green saree under 500' → nearest sarees by price; sold-out items never offered."""
    cat = _catalogue()
    cat.append(product("SR10009", "Sold Out Saree", 100, "Saree", variants=[variant("Green", None, 0)]))
    f = RouterDecision.model_validate({"action": "search_catalog", "args": {
        "query": "saree", "filters": {"color": "green", "max_price": 500}}}).args.filters
    assert router_actions.search_products(cat, "saree", f) == []
    alts = router_actions.closest_alternatives(cat, "saree", f, k=3)
    assert alts and all(p.category == "Saree" for p in alts) and "SR10009" not in [p.sku for p in alts]
    assert alts[0].sku == "SR10001"            # matches the colour and is the closest to the price


# ── variant answers / slot validation ────────────────────────────────────────

def _filters(**kw):
    """RouterFilters from kwargs."""
    return RouterDecision.model_validate({"action": "show_product", "args": {"filters": kw}}).args.filters


def test_canonical_product_text_uses_sku_when_legacy_can_match_it_else_unique_name():
    """AB12345-shaped SKUs are sent as-is; other SKU shapes fall back to the unique exact product name."""
    ok = product("SR20001", "Green Saree")
    odd = product("VR_0001", "Silk Stole")
    twin_a, twin_b = product("A-1", "Stole"), product("A-2", "Stole")
    allp = [ok, odd, twin_a, twin_b]
    assert router_actions.canonical_product_text(ok, allp) == "SR20001"
    assert router_actions.canonical_product_text(odd, allp) == "Silk Stole"
    assert router_actions.canonical_product_text(twin_a, allp) == "A-1"      # ambiguous name → keep the sku


def test_variant_answer_reports_available_sizes_from_db():
    """'XXL hai?' on a lehenga whose Pink XXL is sold out but Blue XXL isn't."""
    p = lehenga()
    assert "available" in router_actions._variant_answer("english", p, _filters(size="xxl")).lower()
    no = router_actions._variant_answer("english", p, _filters(size="XXL", color="Pink"))
    assert "isn't available" in no and "Available sizes: M" in no
    assert router_actions._variant_answer("english", p, _filters(color="Green")).endswith("Available colors: Pink, Blue.")
    assert router_actions._variant_answer("english", product("X1", "Plain"), _filters(size="L")) is None


def test_canonical_answer_validates_against_real_options():
    """Option slots map onto catalogue spellings; quantity is digits; unaccepted payment modes are rejected."""
    rc = SimpleNamespace(
        variant_info={"available_colors": ["Pink", "Blue"], "available_sizes": ["M", "XXL"], "available_materials": []},
        client=SimpleNamespace(accepts_cod=False, accepts_upi=True))
    assert router_actions.canonical_answer(rc, "color", "pink") == "Pink"
    assert router_actions.canonical_answer(rc, "color", "green") is None
    assert router_actions.canonical_answer(rc, "size", "xxl") == "XXL"
    assert router_actions.canonical_answer(rc, "quantity", "2") == "2"
    assert router_actions.canonical_answer(rc, "quantity", "two") is None
    assert router_actions.canonical_answer(rc, "payment_method", "COD") is None
    assert router_actions.canonical_answer(rc, "payment_method", "upi") == "UPI"
    assert router_actions.canonical_answer(rc, "confirmation", "haan") == "yes"
    assert router_actions.canonical_answer(rc, "variant_mode", "same for all") == "same"


# ── templates / logging ──────────────────────────────────────────────────────

def test_every_router_template_exists_in_all_five_languages_and_formats():
    """rt_* keys are present in EN, Hindi (roman + devanagari) and Gujarati (roman + script), none empty."""
    used = {k for k in TEMPLATES["english"] if k.startswith("rt_")}
    assert len(used) >= 55
    for lang in ("english", "hindi_roman", "hinglish", "gujarati_roman", "hindi_devanagari", "gujarati_script"):
        missing = [k for k in used if not TEMPLATES[lang].get(k)]
        assert not missing, (lang, missing)


def test_router_templates_never_contain_user_text_placeholders():
    """Not-found / clarify templates only take product facts — nothing that could echo the customer's text."""
    for lang in TEMPLATES:
        assert "{query}" not in TEMPLATES[lang]["rt_not_found"]
        assert "{text}" not in TEMPLATES[lang]["rt_clarify_open"]
    assert get_template("gujarati_script", "rt_handoff") and get_template("hindi_devanagari", "rt_handoff")


def test_log_line_has_all_fields_and_redacts_pii_slot_values(caplog):
    """ROUTER conv=.. action=.. conf=.. args=.. fast_path=.. ms=.. — and an address value never hits the log."""
    from app.schemas.router import RouterTrace

    trace = RouterTrace(action="answer_slot", confidence=0.93, args={"slot": "address", "value": "12 MG Road"}, ms=412)
    with caplog.at_level(logging.INFO, logger="app.services.router_actions"):
        router_actions._log_line(42, trace, "router_v2.1")
    line = caplog.records[-1].getMessage()
    for part in ("ROUTER conv=42", "action=answer_slot", "conf=0.93", "fast_path=-", "ms=412"):
        assert part in line
    assert "MG Road" not in line and "<redacted>" in line


def test_template_language_prefers_script_then_router_language():
    """Devanagari/Gujarati script wins; otherwise the router's language code maps to a template language."""
    conv = SimpleNamespace(last_customer_language="english")
    d = lambda lang: RouterDecision.model_validate({"action": "greeting", "language": lang})  # noqa: E731
    assert router_actions.template_language(d("en"), "hello", conv) == "english"
    assert router_actions.template_language(d("gu"), "kem cho", conv) == "gujarati_roman"
    assert router_actions.template_language(d("en"), "मेरा ऑर्डर कहाँ है", conv) == "hindi_devanagari"
    assert router_actions.template_language(d("hinglish"), "order kaha hai", conv) == "hinglish"
