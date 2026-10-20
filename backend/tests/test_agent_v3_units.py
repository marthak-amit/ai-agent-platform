"""
Unit tests for agent v3 (no database, no LLM): the action schema, text helpers, catalogue view, cart diff, executor
validation, renderer, invariants, fast paths and the feature flag. The end-to-end behaviour is covered by the YAML scenarios
in ``tests/scenarios``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.schemas.agent_actions import ACTION_TYPES, AddLine, Cancel, ConfirmOrder, PlannerOutput, parse_planner_output, wire_schema
from app.services import agent_executor as ex
from app.services import agent_fast_paths as fp
from app.services import agent_invariants as inv
from app.services import agent_text, agent_v3
from app.services.agent_cart import CartData, changes
from app.services.agent_catalog import Catalogue, ProductView, VariantView
from app.services.agent_renderer import Renderer, Reply


# ── fixtures ─────────────────────────────────────────────────────────────────

def _product(pid, sku, name, price, category, *, stock=None, has_variants=False, description=None):
    """A Product-shaped object."""
    return SimpleNamespace(id=pid, sku=sku, name=name, price=price, category=category, stock=stock, has_variants=has_variants,
                           description=description, is_active=True, delivery_days=None, image_url=None)


def make_catalogue() -> Catalogue:
    """Kurti (colour only, Pink sold out), Lehenga (colour × size), a cheap bag, a pricey saree."""
    kurti = ProductView(_product(1, "KU1", "Cotton Kurti", 499.0, "Kurti", has_variants=True), [
        VariantView(1, "Red", None, None, 499.0, 5), VariantView(2, "Green", None, None, 499.0, 5),
        VariantView(3, "Pink", None, None, 499.0, 0), VariantView(4, "Blue", None, None, 499.0, 2),
    ])
    lehenga = ProductView(_product(2, "LH1", "Cotton Lehenga", 1299.0, "Lehenga", has_variants=True), [
        VariantView(5, "Pink", None, "M", 1299.0, 5) if False else VariantView(5, "Pink", "M", None, 1299.0, 5),
        VariantView(6, "Pink", "XXL", None, 1299.0, 0), VariantView(7, "Blue", "M", None, 1299.0, 5),
        VariantView(8, "Blue", "XXL", None, 1299.0, 3),
    ])
    bag = ProductView(_product(3, "TB1", "Tote Bag", 250.0, "Bags", stock=10))
    saree = ProductView(_product(4, "SR1", "Silk Saree", 2499.0, "Saree", stock=3))
    return Catalogue([kurti, lehenga, bag, saree])


def make_cart(**kw) -> CartData:
    """An empty cart."""
    return CartData(id=1, conversation_id=1, client_id=1, **kw)


CLIENT = SimpleNamespace(id=1, business_name="Riya Boutique", accepts_cod=True, accepts_upi=True, upi_id="shop@upi",
                         delivery_days_min=3, delivery_days_max=7, catalogue_slug="riya")


def run_actions(cart: CartData, actions: list, *, text: str = "", catalogue: Catalogue | None = None):
    """Run actions through the executor (no DB needed for cart actions); returns (ExecResult, issues)."""
    catalogue = catalogue or make_catalogue()
    result = ex.ExecResult(cart=cart.copy())
    ctx = ex.ExecContext(user_text=text, language="english", sender_phone="9199", pending_orders=[])
    executor = ex.Executor(None, CLIENT, SimpleNamespace(id=1), catalogue, ctx, result, cart)
    issues = asyncio.run(executor.run(actions))
    return result, issues


def add(sku, qty=None, **variant):
    """AddLine action."""
    return AddLine(type="add_line", sku=sku, variant=variant, qty=qty)


# ── schema ───────────────────────────────────────────────────────────────────

def _walk(schema, path="$"):
    """Yield (path, object-schema) for every object schema inside a JSON schema."""
    if isinstance(schema, dict):
        types = schema.get("type")
        if types == "object" or (isinstance(types, list) and "object" in types):
            yield path, schema
        for k, v in schema.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _walk(v, f"{path}[{i}]")


def test_wire_schema_is_strict_structured_output_compatible():
    """Every object: additionalProperties false and every property required (Groq strict mode)."""
    for path, obj in _walk(wire_schema()):
        if "properties" in obj:
            assert obj.get("additionalProperties") is False, path
            assert set(obj["required"]) == set(obj["properties"]), path


def test_wire_schema_action_enum_is_the_closed_set():
    """The planner can only name the 16 actions (the 15 specified + send_images)."""
    enum = wire_schema()["properties"]["actions"]["items"]["properties"]["type"]["enum"]
    assert enum == list(ACTION_TYPES) and len(enum) == 16 and "send_images" in enum


def test_parse_planner_output_keeps_valid_actions_and_reports_bad_ones_by_index():
    """One malformed action becomes an INVALID_ACTION issue; the others survive; nulls of the flat form are ignored."""
    out, issues = parse_planner_output({"actions": [
        {"type": "add_line", "sku": "KU1", "variant": {"color": "Red", "size": None, "material": None}, "qty": 2, "query": None},
        {"type": "add_line", "qty": 2},           # sku missing
        {"type": "teleport"},                    # not in the closed set
    ], "reply_intent": "none"})
    assert [a.type for a in out.actions] == ["add_line"] and out.actions[0].variant.color == "Red"
    assert [(i.index, i.code) for i in issues] == [(1, "INVALID_ACTION"), (2, "INVALID_ACTION")]


def test_parse_planner_output_rejects_non_object_plans():
    """Garbage is one INVALID_PLAN issue, not an exception."""
    out, issues = parse_planner_output("nope")
    assert out.actions == [] and issues[0].code == "INVALID_PLAN"


def test_planner_output_allows_at_most_one_terminal_action():
    """confirm + cancel in one plan is invalid."""
    with pytest.raises(ValueError):
        PlannerOutput(actions=[ConfirmOrder(type="confirm_order"), Cancel(type="cancel")])


# ── text helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,ok", [
    ("Which colour would you like?", True), ("It costs ₹500", False), ("Delivery in 3 days", False), ("We have 5 in stock", False),
    ("", False),
])
def test_sanitize_question_blocks_prices_numbers_and_claims(text, ok):
    """Only number-free, claim-free text survives."""
    assert (agent_text.sanitize_question(text) is not None) is ok


def test_numbers_in_reads_digits_and_hinglish_words():
    """'2 laal 1 hara', 'do pink aur ek white', Gujarati digits."""
    assert agent_text.numbers_in("2 laal 1 hara") == {1, 2}
    assert {1, 2} <= agent_text.numbers_in("do pink aur ek white")
    assert agent_text.numbers_in("૩ જોઈએ") == {3}


def test_is_bare_number():
    """Only a message that IS a number."""
    assert agent_text.is_bare_number("2") == 2 and agent_text.is_bare_number("two") == 2
    assert agent_text.is_bare_number("2 red") is None


def test_value_in_text_matches_aliases_and_rejects_invented_values():
    """'laal' says Red, 'gulabi' says Pink, 'red' does not say Green."""
    assert agent_text.value_in_text("color", "Red", "2 laal 1 hara")
    assert agent_text.value_in_text("color", "Green", "2 laal 1 hara")
    assert agent_text.value_in_text("color", "Pink", "gulabi chahiye")
    assert not agent_text.value_in_text("color", "Green", "I want a red kurti")
    assert agent_text.value_in_text("payment", "UPI", "I will pay by gpay")
    assert not agent_text.value_in_text("payment", "COD", "upi")
    assert agent_text.value_in_text("name", "Asha Patel", "my name is asha patel")


@pytest.mark.parametrize("text,yes", [
    ("yes", True), ("haan", True), ("confirm kar do", True), ("ok", True), ("yes but change the address", False),
    ("no", False), ("is it confirmed?", False),
])
def test_looks_like_confirmation(text, yes):
    """An explicit yes only — never with a negation, change or question."""
    assert agent_text.looks_like_confirmation(text) is yes


# ── catalogue view ───────────────────────────────────────────────────────────

def test_product_view_lists_only_in_stock_values_and_real_totals():
    """Pink (stock 0) is not offered; availability sums matching variants."""
    kurti = make_catalogue().get("ku1")
    assert kurti.values("color") == ["Red", "Green", "Blue"]
    assert kurti.values("color", in_stock_only=False) == ["Red", "Green", "Pink", "Blue"]
    assert kurti.available({"color": "Pink"}) == 0 and kurti.available({}) == 12
    lehenga = make_catalogue().get("LH1")
    assert lehenga.needed_attrs == ["color", "size"] and lehenga.values("size", {"color": "Pink"}) == ["M"]


def test_catalogue_get_is_exact_not_prefix():
    """A hallucinated / truncated SKU must not resolve."""
    cat = make_catalogue()
    assert cat.get("KU1") is not None and cat.get("KU") is None and cat.get("nope") is None


def test_resolve_maps_hinglish_colours_and_flags_ambiguity():
    """'laal' → Red; unknown → []; two catalogue colours with one canonical colour → both candidates."""
    kurti = make_catalogue().get("KU1")
    assert kurti.resolve("color", "laal") == ["Red"] and kurti.resolve("color", "gulabi") == ["Pink"]
    assert kurti.resolve("color", "kaala") == []
    two_greens = ProductView(_product(9, "X", "X", 1.0, "c", has_variants=True), [
        VariantView(1, "Dark Green", None, None, 1.0, 1), VariantView(2, "Light Green", None, None, 1.0, 1)])
    assert len(two_greens.resolve("color", "hara")) == 2


# ── cart ─────────────────────────────────────────────────────────────────────

def test_cart_changes_reports_only_what_the_plan_wrote():
    """New line fields, edited fields, payment and name — nothing else."""
    before = make_cart()
    ln = before.add_line("KU1", color="Red", qty=2)
    after = before.copy()
    after.line(ln["line_id"]).update(color="Green")
    after.payment_method = "COD"
    got = {(c.kind, c.field, c.new) for c in changes(before, after)}
    assert got == {("line", "color", "Green"), ("payment", "payment_method", "COD")}


def test_cart_remember_dedupes_and_caps():
    """Memory lists keep each SKU once, most recent last."""
    cart = make_cart()
    for sku in ("a", "b", "a"):
        cart.remember("viewed_skus", sku)
    assert cart.memory("viewed_skus") == ["B", "A"]


# ── executor ─────────────────────────────────────────────────────────────────

def test_add_line_validates_variant_stock_and_sku():
    """Valid add; unknown SKU → UNKNOWN_SKU listing real SKUs; sold-out colour → OUT_OF_STOCK_VARIANT with alternatives."""
    result, issues = run_actions(make_cart(), [add("KU1", 2, color="laal")])
    assert not issues and result.cart.lines[0]["color"] == "Red" and result.cart.lines[0]["qty"] == 2
    _, issues = run_actions(make_cart(), [add("ZZ9")])
    assert issues[0].code == "UNKNOWN_SKU" and any("KU1" in o for o in issues[0].valid_options)
    _, issues = run_actions(make_cart(), [add("KU1", color="Pink")])
    assert issues[0].code == "OUT_OF_STOCK_VARIANT" and issues[0].valid_options == ["Red", "Green", "Blue"]
    _, issues = run_actions(make_cart(), [add("KU1", color="Orange")])
    assert issues[0].code == "INVALID_VARIANT" and issues[0].valid_options == ["Red", "Green", "Blue"]


def test_add_line_quantity_above_stock_is_rejected_with_the_available_count():
    """Blue has 2 → 3 is QTY_EXCEEDS_STOCK(available=2)."""
    _, issues = run_actions(make_cart(), [add("KU1", 3, color="Blue")])
    assert issues[0].code == "QTY_EXCEEDS_STOCK" and issues[0].details["available"] == 2


def test_stock_is_shared_across_lines_of_the_same_variant():
    """A Blue line of 2 uses all of Blue's stock (2): turning a Red line into Blue × 1 does not fit."""
    from app.schemas.agent_actions import UpdateLine

    cart = make_cart()
    cart.add_line("KU1", color="Blue", qty=2)
    red = cart.add_line("KU1", color="Red", qty=1)
    _, issues = run_actions(cart, [UpdateLine(type="update_line", line_id=red["line_id"], fields={"color": "Blue"})])
    assert issues and issues[0].code == "OUT_OF_STOCK_VARIANT" and "Red" in issues[0].valid_options


def test_re_adding_an_identical_variant_sets_the_quantity_instead_of_doubling_it():
    """A repeated / repaired plan must be idempotent."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=2)
    result, issues = run_actions(cart, [add("KU1", 2, color="Red")])
    assert not issues and len(result.cart.lines) == 1 and result.cart.lines[0]["qty"] == 2


def test_combo_out_of_stock_lists_real_combinations():
    """Pink/XXL is sold out → 'Pink / M' and 'Blue / XXL' are offered."""
    _, issues = run_actions(make_cart(), [add("LH1", color="Pink", size="XXL")])
    assert issues[0].code == "OUT_OF_STOCK_VARIANT" and set(issues[0].valid_options) == {"Pink / M", "Blue / XXL"}


def test_split_line_requires_parts_to_add_up():
    """3 → 2 + 2 is QTY_MISMATCH; 2 + 1 splits into two lines."""
    cart = make_cart()
    ln = cart.add_line("KU1", qty=3)
    from app.schemas.agent_actions import SplitLine

    bad = SplitLine(type="split_line", line_id=ln["line_id"], parts=[{"variant": {"color": "Red"}, "qty": 2}, {"variant": {"color": "Green"}, "qty": 2}])
    result, issues = run_actions(cart, [bad])
    assert issues[0].code == "QTY_MISMATCH" and issues[0].details == {"expected": 3, "got": 4, "product": "Cotton Kurti", "sku": "KU1"}
    assert len(result.cart.lines) == 1
    good = SplitLine(type="split_line", line_id=ln["line_id"], parts=[{"variant": {"color": "Red"}, "qty": 2}, {"variant": {"color": "Green"}, "qty": 1}])
    result, issues = run_actions(cart, [good])
    assert not issues and [(x["color"], x["qty"]) for x in result.cart.lines] == [("Red", 2), ("Green", 1)]


def test_update_and_remove_unknown_line_are_reported_with_real_ids():
    """UNKNOWN_LINE lists the cart's line ids."""
    from app.schemas.agent_actions import RemoveLine

    cart = make_cart()
    ln = cart.add_line("KU1", qty=1)
    _, issues = run_actions(cart, [RemoveLine(type="remove_line", line_id="nope")])
    assert issues[0].code == "UNKNOWN_LINE" and issues[0].valid_options == [ln["line_id"]]


def test_gaps_follow_the_order_line_attrs_then_customer_then_payment():
    """colour → size → quantity → name → address → payment."""
    cat = make_catalogue()
    cart = make_cart()
    cart.add_line("LH1")
    assert [g.slot for g in ex.gaps(cart, cat, CLIENT)][0] == "color"
    cart.lines[0].update(color="Blue")
    assert ex.gaps(cart, cat, CLIENT)[0].slot == "size" and ex.gaps(cart, cat, CLIENT)[0].options == ["M", "XXL"]
    cart.lines[0].update(size="M", qty=1)
    assert [g.slot for g in ex.gaps(cart, cat, CLIENT)] == ["name", "address", "payment"]


def test_a_single_payment_method_is_never_asked():
    """COD-only shop: the method is the shop's policy, not a customer choice."""
    cod_only = SimpleNamespace(**{**CLIENT.__dict__, "accepts_upi": False, "upi_id": None})
    cart = make_cart()
    assert ex.effective_payment(cart, cod_only) == "COD" and ex.effective_payment(cart, CLIENT) is None


def test_confirm_order_needs_summary_on_screen_and_an_explicit_yes():
    """No summary / no yes → NOT_CONFIRMABLE and no order."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=1)
    _, issues = run_actions(cart, [ConfirmOrder(type="confirm_order")], text="yes")
    assert issues[0].code == "NOT_CONFIRMABLE"


def test_register_clarify_hands_over_on_the_third_ask_of_the_same_slot():
    """ask #1 ok, ask #2 (fail 1) ok, ask #3 (fail 2) → handoff; side questions are not attempts."""
    c = ex.Clarify(slot="color", line_id="a")
    cart = make_cart()
    assert [ex.register_clarify(cart, c) is None for _ in range(2)] == [True, True]
    assert "color" in ex.register_clarify(cart, c)
    other = make_cart()
    ex.register_clarify(other, c)
    ex.register_clarify(other, c, count=False)          # a side question / greeting: not an attempt
    ex.register_clarify(other, c, count=False)
    assert ex.register_clarify(other, c) is None
    assert ex.register_clarify(other, ex.Clarify(slot="size", line_id="a")) is None      # another slot restarts the count


def test_clarify_for_issue_always_carries_real_options():
    """Never 'not available' without what is available."""
    cat = make_catalogue()
    _, issues = run_actions(make_cart(), [add("KU1", color="Pink")])
    clarify = ex.clarify_for_issue(issues[0], make_cart(), cat, CLIENT)
    assert clarify.slot == "color" and clarify.options == ["Red", "Green", "Blue"]


# ── renderer ─────────────────────────────────────────────────────────────────

def _render(cart, effects=None, applied=None, intent="none", before=None):
    """Render an ExecResult built by hand."""
    result = ex.ExecResult(cart=cart, effects=effects or ex.Effects(), applied=applied or [])
    return Renderer("english", CLIENT, make_catalogue(), cart).render(result, before or make_cart(), intent=intent)


def test_summary_lists_each_line_with_real_prices_and_the_single_eta():
    """Per-line subtotals, total, ETA from client settings, payment, customer."""
    cart = make_cart(payment_method="COD", customer={"name": "Asha", "address": "12 MG Road Surat 395001", "mobile": "9199"})
    cart.add_line("KU1", color="Red", qty=2)
    cart.add_line("KU1", color="Green", qty=1)
    reply = _render(cart, ex.Effects(show_summary=True))
    assert "1. Cotton Kurti (Red) × 2 — ₹998" in reply.text and "2. Cotton Kurti (Green) × 1 — ₹499" in reply.text
    assert "Total: ₹1,497" in reply.text and "3–7 working days" in reply.text and "Cash on Delivery" in reply.text
    assert reply.summary_shown and reply.buttons[0][0] == "v3_confirm"
    assert {998, 499, 1497} <= reply.amounts


def test_upsell_respects_the_30_percent_cap_and_exclusions():
    """₹250 bag fits a ₹998 cart (cap ₹299); the ₹2,499 saree and ₹1,299 lehenga never; viewed / offered items are excluded."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=2)
    renderer = Renderer("english", CLIENT, make_catalogue(), cart)
    assert renderer.pick_upsell(998).sku == "TB1"
    cart.remember("viewed_skus", "TB1")
    assert Renderer("english", CLIENT, make_catalogue(), cart).pick_upsell(998) is None
    cart.state["viewed_skus"] = []
    cart.remember("offered_upsell", "TB1")
    assert Renderer("english", CLIENT, make_catalogue(), cart).pick_upsell(998) is None
    assert Renderer("english", CLIENT, make_catalogue(), make_cart()).pick_upsell(100) is None


def test_split_question_never_quotes_an_example_the_customer_did_not_write():
    """The conv=60 bug: "e.g. 2 Navy, 1 Purple" was copied literally and Navy was ordered. No example values, ever."""
    cart = make_cart()
    cart.add_line("KU1", qty=3)
    renderer = Renderer("english", CLIENT, make_catalogue(), cart)
    reply = Reply(text="")
    parts: list[str] = []
    renderer.clarify(ex.Clarify(slot="split", line_id=cart.lines[0]["line_id"], sku="KU1", details={"expected": 3}), reply, parts)
    assert parts == ["Which colours and how many of each?"] and not hasattr(renderer, "example_split")
    parts = []       # options the customer named themselves may be quoted back — as a question, without invented numbers
    renderer.clarify(ex.Clarify(slot="split", line_id=cart.lines[0]["line_id"], sku="KU1", details={"named": ["Red", "Green"]}), reply, parts)
    assert parts == ["How many of each — Red, Green?"]


def test_clarify_text_lists_options_and_explains_unavailability():
    """Sold-out colour → reason + the real colours; quantity above stock → the available count."""
    cart = make_cart()
    cart.add_line("KU1", qty=2)
    renderer = Renderer("english", CLIENT, make_catalogue(), cart)
    parts: list[str] = []
    renderer.clarify(ex.Clarify(slot="color", options=["Red", "Green", "Blue"], sku="KU1", line_id=cart.lines[0]["line_id"],
                                reason="OUT_OF_STOCK_VARIANT", details={"product": "Cotton Kurti", "value": "Pink"}), Reply(text=""), parts)
    assert "Cotton Kurti in Pink is sold out" in parts[0] and "Available: Red, Green, Blue" in parts[1]


def test_greeting_with_a_cart_is_a_short_welcome_back_with_two_buttons():
    """"Welcome back! You have 2 items in your cart." + [Continue order] [Start fresh]; no summary, no upsell, state untouched."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=2)
    reply = _render(cart, intent="greeting")
    assert reply.text == "Welcome back! You have 2 items in your cart."
    assert [b[0] for b in reply.buttons] == ["v3_continue", "v3_fresh"] and reply.resume_greeting and not reply.summary_shown
    assert reply.offered_upsell is None


def test_greeting_without_a_cart_is_the_plain_welcome():
    """No cart → the normal welcome + browse prompt."""
    reply = _render(make_cart(), intent="greeting")
    assert "Welcome to Riya Boutique" in reply.text and reply.buttons[0][0] == "v3_browse"


# ── invariants ───────────────────────────────────────────────────────────────

def _ctx(text, grounded=None, last_clarify=None):
    """ExecContext for grounding tests."""
    return ex.ExecContext(user_text=text, language="english", sender_phone="1", grounded_by=grounded, last_clarify=last_clarify)


def test_grounding_reverts_values_the_customer_did_not_say():
    """Planner invented Red × 2 for 'I want a kurti' → both reverted and the engine asks for the colour."""
    cat = make_catalogue()
    before = make_cart()
    result = ex.ExecResult(cart=before.copy())
    result.cart.add_line("KU1", color="Red", qty=2)
    violations = inv.enforce_grounding(before, result, _ctx("I want a kurti"), cat, CLIENT)
    assert {v.code for v in violations} == {"option_not_from_user", "ungrounded_value"}      # colour = option, quantity = value
    assert result.cart.lines[0]["color"] is None and result.cart.lines[0]["qty"] is None
    ex.finish(result, cat, CLIENT, SimpleNamespace(id=1), _ctx("I want a kurti"), before)       # what to ask is decided afterwards
    assert result.effects.clarify.slot == "color"


def test_grounding_accepts_hinglish_values_taps_and_a_yes_to_a_single_offered_option():
    """'2 laal' grounds Red × 2; a tap grounds anything; 'yes' grounds the one option that was offered."""
    cat = make_catalogue()
    before = make_cart()
    result = ex.ExecResult(cart=before.copy())
    result.cart.add_line("KU1", color="Red", qty=2)
    assert inv.enforce_grounding(before, result, _ctx("2 laal"), cat, CLIENT) == []
    assert inv.enforce_grounding(before, result, _ctx("whatever", grounded="button"), cat, CLIENT) == []
    only_red = ex.ExecResult(cart=before.copy())
    only_red.cart.add_line("KU1", color="Red")
    assert inv.enforce_grounding(before, only_red, _ctx("yes", last_clarify={"options": ["Red"]}), cat, CLIENT) == []
    green = ex.ExecResult(cart=before.copy())
    green.cart.add_line("KU1", color="Green")
    assert inv.enforce_grounding(before, green, _ctx("yes", last_clarify={"options": ["Red"]}), cat, CLIENT)


def test_reply_checks_cta_and_unverified_amounts():
    """A statement gets a question appended; a ₹ amount nobody produced replaces the reply by the fallback."""
    cart = make_cart()
    renderer = Renderer("english", CLIENT, make_catalogue(), cart)
    reply = Reply(text="Your kurti is great.")
    assert not inv.ends_with_cta(reply)
    assert {v.code for v in inv.enforce_reply(reply, renderer=renderer, cart=cart, catalogue=make_catalogue(), client=CLIENT)} == {"no_cta"}
    assert inv.ends_with_cta(reply)
    bad = Reply(text="Price is ₹9,999. Which one?", amounts={499})
    assert inv.unverified_amounts(bad) == [9999]
    inv.enforce_reply(bad, renderer=renderer, cart=cart, catalogue=make_catalogue(), client=CLIENT)
    assert "9,999" not in bad.text


def test_unavailable_without_options_gets_same_category_alternatives_appended():
    """'sold out' with nothing listed is repaired with what is in stock IN THE SAME CATEGORY — never the first products of the catalogue."""
    base = make_catalogue()
    second_kurti = ProductView(_product(9, "KU2", "Chanderi Kurti", 699.0, "Kurti", stock=4))
    catalogue = Catalogue([*base.views, second_kurti])
    cart = make_cart()
    renderer = Renderer("english", CLIENT, catalogue, cart)
    reply = Reply(text="Sorry, that is sold out. Anything else?", unavailable_sku="KU1")
    violations = inv.enforce_reply(reply, renderer=renderer, cart=cart, catalogue=catalogue, client=CLIENT)
    assert "unavailable_without_options" in {v.code for v in violations}
    assert "Chanderi Kurti" in reply.text and "Silk Saree" not in reply.text and "Tote Bag" not in reply.text
    assert reply.last_clarify["options"] == ["KU2"]


def test_unavailable_without_a_same_category_product_offers_the_browse_prompt_not_random_products():
    """No other kurti in stock → the generic browse prompt, not an unrelated list."""
    cart = make_cart()
    catalogue = make_catalogue()
    reply = Reply(text="Sorry, that is sold out. Anything else?", unavailable_sku="KU1")
    inv.enforce_reply(reply, renderer=Renderer("english", CLIENT, catalogue, cart), cart=cart, catalogue=catalogue, client=CLIENT)
    assert "Lehenga" not in reply.text and "Saree" not in reply.text and not reply.last_clarify


def test_pending_order_is_acknowledged_once_when_a_new_cart_starts():
    """First line added while an order is under review → ack; the same order is not acknowledged twice."""
    order = SimpleNamespace(order_number="ORD-1", status="payment_submitted")
    before, after = make_cart(), make_cart()
    after.add_line("KU1")
    renderer = Renderer("english", CLIENT, make_catalogue(), after)
    reply = Reply(text="Which colour?")
    assert inv.acknowledge_pending_order(reply, before, after, [order], renderer)
    assert reply.text.startswith("Your order #ORD-1 is being verified") and "Meanwhile" in reply.text
    again = Reply(text="Which colour?")
    assert not inv.acknowledge_pending_order(again, before, after, [order], renderer)


def test_stage_follows_the_cart_and_a_pending_order_never_pins_it():
    """browsing → greeting / product_inquiry, collecting → order_collection, confirming → awaiting_final_confirmation,
    order placed → payment (UPI) / completed (COD). An unpaid order is a separate fact: it neither pins the stage to ``payment`` nor
    resets it — browsing with an order pending is plain browsing."""
    cart = make_cart()
    order = SimpleNamespace(order_number="ORD-1", status="pending_payment")
    kw = dict(pending_orders=[], order_created=None, shown_something=False)
    assert inv.stage_to_mirror(cart, current="greeting", **kw) is None
    assert inv.stage_to_mirror(cart, current="greeting", **{**kw, "shown_something": True}) == "product_inquiry"
    cart.add_line("KU1")
    assert inv.stage_to_mirror(cart, current="greeting", **kw) == "order_collection"
    cart.set_stage("confirming")
    assert inv.stage_to_mirror(cart, current="order_collection", **kw) == "awaiting_final_confirmation"
    assert inv.stage_to_mirror(cart, current="x", pending_orders=[], order_created="UPI", shown_something=False) == "payment"
    assert inv.stage_to_mirror(cart, current="x", pending_orders=[], order_created="COD", shown_something=False) == "completed"
    # an old unpaid order: browsing stays browsing (greeting), a conversation left in "payment" by the order is released
    pend = dict(pending_orders=[order], order_created=None, shown_something=False)
    assert inv.stage_to_mirror(make_cart(), current="greeting", **pend) is None
    assert inv.stage_to_mirror(make_cart(), current="payment", **pend) == "greeting"
    assert inv.stage_to_mirror(make_cart(), current="payment", **{**pend, "shown_something": True}) == "product_inquiry"
    cart.set_stage("collecting")
    assert inv.stage_to_mirror(cart, current="payment", **pend) == "order_collection"


# ── fast paths ───────────────────────────────────────────────────────────────

def test_buttons_map_to_actions():
    """Our button ids become the matching actions; foreign ids are not ours."""
    cart, cat = make_cart(), make_catalogue()
    assert fp.from_button("v3_confirm", cart, cat).confirm_ok
    assert fp.from_button("v3_pay_upi", cart, cat).actions[0].method == "UPI"
    assert fp.from_button("v3_order_ku1", cart, cat).actions[0].sku == "KU1"
    assert fp.from_button("confirm_pay", cart, cat) is None


def test_open_question_answers_resolve_without_the_llm():
    """A colour name / alias / number picks the option; a number at the quantity question is the quantity."""
    cart, cat = make_cart(), make_catalogue()
    ln = cart.add_line("KU1", qty=2)
    cart.state["last_clarify"] = {"slot": "color", "options": ["Red", "Green", "Blue"], "line_id": ln["line_id"], "sku": "KU1"}
    for text in ("green", "2", "hara"):
        got = fp.resolve(text=text, button_id=None, cart=cart, catalogue=cat, client=CLIENT)
        assert got.actions[0].fields.color == "Green", text
    cart.state["last_clarify"] = {"slot": "quantity", "options": [], "line_id": ln["line_id"], "sku": "KU1"}
    assert fp.resolve(text="3", button_id=None, cart=cart, catalogue=cat, client=CLIENT).actions[0].fields.qty == 3


def test_free_text_slots_and_unclear_messages_go_to_the_planner():
    """A name at the name question and a long sentence are not fast paths."""
    cart, cat = make_cart(), make_catalogue()
    cart.add_line("KU1", color="Red", qty=2)
    cart.state["last_clarify"] = {"slot": "name", "options": [], "line_id": None, "sku": None}
    assert fp.resolve(text="Asha Patel", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None
    assert fp.resolve(text="I want to talk to someone about delivery", button_id=None, cart=cart, catalogue=cat, client=CLIENT).name == "human"


def test_yes_confirms_only_with_the_summary_on_screen():
    """'yes' with no summary is not a confirmation."""
    cart, cat = make_cart(), make_catalogue()
    cart.add_line("KU1", color="Red", qty=2)
    assert fp.resolve(text="yes", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None
    cart.state.update(stage="confirming", summary_version=cart.version)
    assert fp.resolve(text="yes", button_id=None, cart=cart, catalogue=cat, client=CLIENT).name == "confirm"


# ── flag ─────────────────────────────────────────────────────────────────────

def test_enabled_flag_override_env_list_and_wildcard(monkeypatch):
    """Client.agent_v3_enabled wins; NULL follows AGENT_V3_CLIENT_IDS ('1' default, '*' all, '' none)."""
    from app.config import get_settings

    forced_on, forced_off, plain = SimpleNamespace(id=5, agent_v3_enabled=True), SimpleNamespace(id=1, agent_v3_enabled=False), SimpleNamespace(id=1, agent_v3_enabled=None)
    for env, expect in (("1", True), ("2,3", False), ("*", True), ("", False)):
        monkeypatch.setenv("AGENT_V3_CLIENT_IDS", env)
        get_settings.cache_clear()
        assert agent_v3.enabled(plain) is expect, env
    assert agent_v3.enabled(forced_on) and not agent_v3.enabled(forced_off) and not agent_v3.enabled(None)
    get_settings.cache_clear()


# ── fixes from the live conv=60 test ─────────────────────────────────────────

def test_retry_wait_uses_the_providers_hint_but_never_more_than_the_cap(monkeypatch):
    """"try again in 7.03s" is capped; milliseconds are converted; no hint → the default backoff."""
    from app.services import agent_planner

    monkeypatch.setattr(agent_planner, "RETRY_BACKOFF_SECONDS", 1.5)
    assert agent_planner._retry_wait(RuntimeError("Please try again in 7.034999999s.")) == agent_planner.MAX_RETRY_WAIT_SECONDS
    assert agent_planner._retry_wait(RuntimeError("Please try again in 250ms")) == 1.5
    assert agent_planner._retry_wait(RuntimeError("boom")) == 1.5


def test_call_model_logs_the_raw_output_when_it_is_not_json(monkeypatch, caplog):
    """An unusable model answer is logged (WARNING PLANNER invalid output raw=…) and returns None."""
    import logging

    from app.services import agent_planner, llm_client, llm_usage_service

    async def fake_call(*a, **k):
        """A provider response whose text is not JSON."""
        msg = SimpleNamespace(content="Sure! Here is your plan: nope", reasoning=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="length")],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=7, prompt_tokens_details=SimpleNamespace(cached_tokens=40)))

    monkeypatch.setattr(llm_client, "llm_call", fake_call)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(agent_planner.call_model([{"role": "user", "content": "x"}], client_id=1, conversation_id=1)) is None
    assert "invalid output" in caplog.text and "Here is your plan" in caplog.text and "finish=length" in caplog.text


def test_plural_words_match_singular_products():
    """"sarees" finds the saree; "kurtis" the kurti; names the shop does not sell are not mentioned."""
    from app.services.agent_catalog import with_singulars

    cat = make_catalogue()
    assert "saree" in with_singulars("show me sarees please") and "dress" in with_singulars("dresses")
    assert [v.sku for v in cat.search("sarees")] == ["SR1"]
    assert cat.mentions("kurtis please") and cat.keyword_hit("sarees") and not cat.mentions("shoes") and not cat.keyword_hit("shoes")


def test_bare_gate_words_only():
    """≤3 words and no product noun: gate. A sentence with its own content is not."""
    cat = make_catalogue()
    for text in ("yes", "cancel", "ok confirm", "haan"):
        assert fp.is_bare_gate(text, cat), text
    for text in ("I don't want to buy this, I want to buy shoes", "cancel kurti", "yes but first show me the saree collection"):
        assert not fp.is_bare_gate(text, cat), text


def test_cancel_and_yes_fast_paths_ignore_sentences_with_content():
    """The explicit-cancel / confirm / offer fast paths do not fire on longer messages naming a product."""
    cart, cat = make_cart(), make_catalogue()
    cart.add_line("KU1", color="Red", qty=2)
    assert fp.resolve(text="cancel", button_id=None, cart=cart, catalogue=cat, client=CLIENT).name == "cancel"
    assert fp.resolve(text="I don't want this, I want a saree", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None
    cart.state.update(stage="confirming", summary_version=cart.version)
    assert fp.resolve(text="yes show me the saree", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None


def test_image_words_are_a_fast_path_only_for_a_product_in_focus():
    """"photo dikhao" with a focus product → send_images; without focus, or naming another product → planner."""
    cart, cat = make_cart(), make_catalogue()
    assert fp.resolve(text="photo dikhao", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None
    cart.state["focus_sku"] = "KU1"
    got = fp.resolve(text="photo dikhao", button_id=None, cart=cart, catalogue=cat, client=CLIENT)
    assert got.name == "images" and got.actions[0].sku == "KU1"
    assert fp.resolve(text="images of the saree please", button_id=None, cart=cart, catalogue=cat, client=CLIENT) is None


def test_welcome_back_buttons_are_fast_paths():
    """Continue order = no actions; Start fresh = confirmed cancel."""
    cart, cat = make_cart(), make_catalogue()
    assert fp.from_button("v3_continue", cart, cat).actions == []
    fresh = fp.from_button("v3_fresh", cart, cat)
    assert fresh.actions[0].type == "cancel" and fresh.cancel_confirmed


def test_single_available_value_is_selected_for_the_customer():
    """One in-stock size → set and announced; two colours → left to the customer; a sold-out only-value is not selected."""
    wallet = ProductView(_product(7, "WL1", "Wallet", 550.0, "Acc", has_variants=True), [
        VariantView(1, "Black", "One Size", None, 550.0, 3), VariantView(2, "Brown", "One Size", None, 550.0, 3)])
    cat = Catalogue([wallet])
    cart = make_cart()
    cart.add_line("WL1")
    result = ex.ExecResult(cart=cart)
    assert ex.autofill_single_options(result, cat)
    assert cart.lines[0]["size"] == "One Size" and cart.lines[0]["color"] is None
    assert result.applied[-1].kind == "autofilled" and result.applied[-1].fields == ["size"]
    wallet.variants[0].available = 0
    wallet.variants[1].available = 0
    cart2 = make_cart()
    cart2.add_line("WL1")
    assert not ex.autofill_single_options(ex.ExecResult(cart=cart2), cat)


def test_upsell_is_the_same_for_a_cart_and_seeded_by_its_id():
    """Repeated summaries of one cart keep one pick; the pick is a stable function of the cart id."""
    cat = make_catalogue()
    extra = ProductView(_product(9, "TB2", "Mini Pouch", 200.0, "Bags", stock=5))
    cat.views.append(extra)
    cat._by_sku["TB2"] = extra
    picks = {}
    for cart_id in (1, 2, 3, 4, 5, 6):
        cart = CartData(id=cart_id, conversation_id=1, client_id=1)
        cart.add_line("KU1", color="Red", qty=2)
        first = Renderer("english", CLIENT, cat, cart).pick_upsell(998)
        again = Renderer("english", CLIENT, cat, cart).pick_upsell(998)
        assert first.sku == again.sku
        cart.state["upsell"] = {"cart_id": cart_id, "sku": first.sku}
        cart.remember("offered_upsell", first.sku)           # offered for THIS cart: shown again, not "declined"
        assert Renderer("english", CLIENT, cat, cart).pick_upsell(1497).sku == first.sku
        picks[cart_id] = first.sku
    assert set(picks.values()) <= {"TB1", "TB2"}


def test_cancel_with_other_content_acknowledges_and_continues():
    """[cancel, search] → "I've cancelled this order." followed by the search answer, not only the cancel text."""
    cart = make_cart()
    effects = ex.Effects(cancel="cart", search={"query": "shoes", "skus": []})
    reply = _render(cart, effects)
    assert reply.text.startswith("✅ I've cancelled this order.") and "We don't have shoes. We have: Kurti, Lehenga, Bags, Saree." in reply.text
    assert reply.buttons[0][0] == "v3_browse"


def test_an_empty_plan_for_a_catalogue_word_becomes_a_search():
    """The planner returned nothing for "mare choli joi che"-style text that names a product → search; free-text slots excepted."""
    cat = make_catalogue()
    assert ex.keyword_search_outcome(cat, "show me sarees")
    assert ex.keyword_search_outcome(cat, "shoes") is None
    assert ex.keyword_search_outcome(cat, "") is None
