"""
Unit tests (no database, no LLM) for the conv=60 v1.5 round (ORD-2026-0075 / 0068):

  1. quantity-per-option lists ("2 purple and one red and one green") read at ₹0 in five languages, partial availability, no example values
  3. exceptions carry plain values (never ORM rows)
  4. WhatsApp labels: unique titles, list (not buttons) for cart disambiguation, numbered text fallback on #131009 / any 400
  5. "remove chair" — not in the cart
  6. stage vs pending orders / the TURN header
  7. upsell: label, once per order, cap
  8. cheap fast paths (compliments, "ok") and weak English greeting markers
  P. turn-trace query diagnostics

The end-to-end behaviour is in ``tests/scenarios/61…65`` and ``tests/replay/test_payment_proof_hardening.py``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import random
import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.agent_actions import AddLine, SplitLine
from app.services import agent_executor as ex
from app.services import agent_fast_paths as fp
from app.services import agent_invariants as inv
from app.services import agent_multivariant, agent_removal, agent_social, agent_templates, language_tracker, turn_trace, wa_labels
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView, VariantView
from app.services.agent_renderer import Renderer, Reply, _TABLES
from tests.test_agent_v3_units import CLIENT, make_cart, make_catalogue


def _product(pid, sku, name, price, category, *, stock=None, has_variants=False):
    """A Product-shaped object."""
    return SimpleNamespace(id=pid, sku=sku, name=name, price=price, category=category, stock=stock, has_variants=has_variants,
                           description=None, is_active=True, delivery_days=None, image_url=None)


def saree_catalogue() -> Catalogue:
    """The conv=60 product: SR51139, colours Navy / Purple / Red (no Green), plus a chair-free shop."""
    saree = ProductView(_product(1, "SR51139", "Banarasi Saree", 1000.0, "Saree", has_variants=True), [
        VariantView(1, "Navy", None, None, 1000.0, 9), VariantView(2, "Purple", None, None, 1000.0, 9), VariantView(3, "Red", None, None, 1000.0, 9),
    ])
    kurti = ProductView(_product(2, "KU1", "Cotton Kurti", 499.0, "Kurti", has_variants=True), [
        VariantView(4, "Red", "M", None, 499.0, 9), VariantView(5, "Red", "L", None, 499.0, 9), VariantView(6, "Blue", "M", None, 499.0, 9),
    ])
    return Catalogue([saree, kurti])


def saree_cart(*, colour="Purple", qty=None) -> CartData:
    """cart=[SR51139 Purple qty=None] — the cart of the failed live turn."""
    cart = make_cart()
    ln = cart.add_line("SR51139", color=colour, qty=qty)
    cart.state.update({"focus_line_id": ln["line_id"], "focus_sku": "SR51139"})
    return cart


def run_fast(text: str, cart: CartData, catalogue: Catalogue | None = None, clarify: dict | None = None):
    """The ₹0 fast path for `text` (None = the planner would be called)."""
    return fp.resolve(text=text, button_id=None, cart=cart, catalogue=catalogue or saree_catalogue(), client=CLIENT, clarify=clarify or {})


def apply(fast, cart: CartData, catalogue: Catalogue | None = None, text: str = ""):
    """Run a fast path's actions through the executor; returns the ExecResult."""
    catalogue = catalogue or saree_catalogue()
    result = ex.ExecResult(cart=cart.copy())
    ctx = ex.ExecContext(user_text=text, language="english", sender_phone="9199", pending_orders=[], grounded_by=fast.grounded_by)
    ctx.unavailable_options = list(fast.unavailable)
    executor = ex.Executor(None, CLIENT, SimpleNamespace(id=1), catalogue, ctx, result, cart)
    issues = asyncio.run(executor.run(fast.actions))
    result.issues = issues
    result.effects.unavailable_options = list(fast.unavailable)
    return result


def lines(result) -> list[tuple]:
    """(colour, size, qty) of every cart line."""
    return [(ln.get("color"), ln.get("size"), ln.get("qty")) for ln in result.cart.lines]


# ── 1. quantity per option ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "2 purple and one red and one green",             # the live message, verbatim
    "2 purple 1 red and 1 green",                      # the live retry: no joiner between the first two pairs
    "I want 2 purple and 1 red and 1 green please",
    "2 purple aur ek red aur 1 green",                 # Hinglish
    "do purple aur ek laal aur ek hara",               # Hinglish number words + colour aliases
    "be purple ane ek red ane ek green",               # Gujarati (Roman)
    "૨ purple ane એક red ane ૧ green",                  # Gujarati digits / number words
    "दो पर्पल और एक रेड और 1 green",                     # Devanagari
    "2 purple, 1 red, 1 green",
    "2 Purple\n1 Red\n1 Green",
    "purple 2, red 1, green 1",
])
def test_quantity_list_applies_every_valid_pair_and_reports_the_unavailable_one(text):
    """cart=[Purple qty=None], colours Navy/Purple/Red: Purple 2 + Red 1 are applied, Green is reported — never a generic question, 0 planner calls."""
    cart = saree_cart()
    fast = run_fast(text, cart)
    assert fast is not None and fast.name == "multi_variant", text
    result = apply(fast, cart, text=text)
    assert sorted(lines(result)) == sorted([("Purple", None, 2), ("Red", None, 1)]), text          # the existing Purple line got its quantity, no duplicate
    assert not result.issues
    assert [(u["value"].lower(), u["qty"]) for u in result.effects.unavailable_options][0][0] in ("green", "hara")
    assert all(ln["color"] != "Navy" for ln in result.cart.lines)                                   # Navy was never mentioned


def test_unavailable_option_is_named_with_what_is_available():
    """"Green isn't available for Banarasi Saree. Available: Navy, Purple, Red." — in the customer's language too."""
    cart = saree_cart()
    fast = run_fast("2 purple and one red and one green", cart)
    result = apply(fast, cart)
    reply = Renderer("english", CLIENT, saree_catalogue(), result.cart).render(result, cart)
    assert "Green isn't available for Banarasi Saree. Available: Navy, Purple, Red." in reply.text
    assert "e.g." not in reply.text and "2 Navy" not in reply.text
    hi = Renderer("hindi_roman", CLIENT, saree_catalogue(), result.cart).render(result, cart)
    assert "Green Banarasi Saree mein available nahi hai. Available: Navy, Purple, Red." in hi.text


def test_nothing_valid_still_answers_with_the_real_options_and_changes_nothing():
    """"2 green and 1 pink": nothing the saree comes in → both reported, cart untouched, the open question goes on."""
    cart = saree_cart()
    fast = run_fast("2 green and 1 pink", cart)
    assert fast is not None and fast.actions == [] and len(fast.unavailable) == 2


def test_breakdown_of_the_line_quantity_is_a_split_so_a_wrong_total_is_questioned():
    """"I want 3 kurtis" then "2 red 2 green": one split_line (the engine says 4 ≠ 3); a right total splits the line."""
    cart = make_cart()
    ln = cart.add_line("KU1", qty=3)
    cart.state["focus_line_id"] = ln["line_id"]
    cat = make_catalogue()
    fast = fp.resolve(text="2 red 1 green", button_id=None, cart=cart, catalogue=cat, client=CLIENT)
    assert fast.name == "multi_variant" and isinstance(fast.actions[0], SplitLine)
    result = apply(fast, cart, cat)
    assert sorted(lines(result)) == sorted([("Red", None, 2), ("Green", None, 1)])
    wrong = fp.resolve(text="2 red 2 green", button_id=None, cart=cart, catalogue=cat, client=CLIENT)
    bad = apply(wrong, cart, cat)
    assert [i.code for i in bad.issues] == ["QTY_MISMATCH"] and lines(bad) == [(None, None, 3)]


def test_a_second_attribute_is_carried_to_the_new_lines():
    """Cart [Red, qty open] of a colour×size kurti, "1 M and 1 L": both sizes keep the Red the customer already chose."""
    cat = make_catalogue()
    cart = make_cart()
    ln = cart.add_line("LH1", color="Pink")
    cart.state["focus_line_id"] = ln["line_id"]
    fast = fp.resolve(text="1 M and 1 XXL", button_id=None, cart=cart, catalogue=cat, client=CLIENT)
    assert fast is not None
    got = [(a.variant.color, a.variant.size, a.qty) for a in fast.actions if isinstance(a, AddLine)]
    assert got and all(c == "Pink" for c, _, _ in got)


@pytest.mark.parametrize("text", [
    "red and green", "my name is Riya", "2 kurti and 1 saree", "2 purple", "12 MG Road 2nd floor", "yes", "2", "ok",
    "2 red kurti and 1 blue kurti",              # another product, not "Red" — must never be reported as an unavailable colour
    "1 pink m and 1 blue xxl",                    # colour AND size in one segment: the planner reads combinations
])
def test_things_that_are_not_a_quantity_list_are_left_to_the_other_paths(text):
    """No quantity-per-option shape (a bare 'red and green', a name, an address, one pair) → not this parser."""
    assert agent_multivariant.parse(text, saree_catalogue().get("SR51139")) is None


def test_quantity_list_never_fires_while_a_name_or_address_is_being_typed():
    """An address like "2 Red Rose Society, 1 Main Road" answers the open address question."""
    cart = saree_cart()
    assert fp.multi_variant("2 red and 1 navy", cart, {"slot": "address"}, saree_catalogue()) is None
    assert fp.multi_variant("2 red and 1 navy", cart, {"slot": "color"}, saree_catalogue()) is not None


def test_no_template_in_any_language_carries_example_values():
    """Customers copy examples literally ("e.g. 2 Navy, 1 Purple" → Navy ordered). No table has an example, an {example} slot or a quantity hint."""
    banned = ("e.g.", "jaise 2", "jem ke 2", "જેમ કે 2", "जैसे 2", "(e.g", "(jaise", "(jem ke", "(જેમ કે 2", "(जैसे 2")
    for lang, table in _TABLES.items():
        for key, text in table.items():
            assert "{example}" not in text, (lang, key)
            if key.startswith("q_") and key != "q_change":          # q_change lists WHAT can be changed (slot names), not values
                assert not any(b in text for b in banned), (lang, key, text)
    assert "(e.g. 2)" not in _TABLES["english"]["q_quantity"]
    assert agent_templates.assert_complete() and agent_templates.problems(_TABLES["english"]) == []


def test_split_question_quotes_only_what_the_customer_named():
    """With nothing named: the plain question. With the customer's own options: "How many of each — Red, Green?"."""
    cart = make_cart()
    ln = cart.add_line("KU1", qty=3)
    renderer = Renderer("english", CLIENT, make_catalogue(), cart)
    parts: list[str] = []
    renderer.clarify(ex.Clarify(slot="split", line_id=ln["line_id"], sku="KU1", details={"expected": 3}), Reply(text=""), parts)
    assert parts == ["Which colours and how many of each?"]
    # the executor derives the named options from the customer's message
    named_ctx = ex.ExecContext(user_text="I want red and green", language="english", sender_phone="1", pending_orders=[])
    result = ex.ExecResult(cart=cart.copy())
    asyncio.run(ex.Executor(None, CLIENT, SimpleNamespace(id=1), make_catalogue(), named_ctx, result, cart).run(
        [__import__("app.schemas.agent_actions", fromlist=["AskClarify"]).AskClarify(type="ask_clarify", slot="split", options=["Blue"])]))
    assert result.effects.clarify.details["named"] == ["Red", "Green"]


def test_a_colour_the_customer_never_wrote_is_rejected_as_option_not_from_user():
    """The planner turned "2 red 1 green" into Navy + Purple (or copied an example): reverted, logged as option_not_from_user, the engine asks."""
    cat = saree_catalogue()
    before = saree_cart(colour=None)
    result = ex.ExecResult(cart=before.copy())
    ln = result.cart.lines[0]
    ln["color"], ln["qty"] = "Navy", 2
    ctx = ex.ExecContext(user_text="2 red and 1 purple", language="english", sender_phone="1", pending_orders=[])
    violations = inv.enforce_grounding(before, result, ctx, cat, CLIENT)
    assert "option_not_from_user" in {v.code for v in violations}
    assert result.cart.lines[0]["color"] is None
    # an option the customer DID write, or tapped, passes
    ok = ex.ExecResult(cart=before.copy())
    ok.cart.lines[0]["color"] = "Red"
    assert inv.enforce_grounding(before, ok, ctx, cat, CLIENT) == [] or all(v.code != "option_not_from_user" for v in inv.enforce_grounding(before, ok, ctx, cat, CLIENT))
    tapped = ex.ExecContext(user_text="Navy", language="english", sender_phone="1", pending_orders=[], grounded_by="button")
    nav = ex.ExecResult(cart=before.copy())
    nav.cart.lines[0]["color"] = "Navy"
    assert inv.enforce_grounding(before, nav, tapped, cat, CLIENT) == []


def test_planner_prompt_teaches_quantity_lists_in_five_languages():
    """v1.5: the prompt has the multi-variant rule and examples in English, Hinglish, Gujarati (Roman + digits) and Devanagari."""
    from app.services import agent_planner

    system = agent_planner.load_prompt().system
    assert agent_planner.prompt_tag().startswith("agent_planner_v1.7/")
    for needle in ("QUANTITY PER OPTION", "2 purple aur ek red", "be purple ane ek red", "૨ purple ane એક red", "दो पर्पल और एक रेड", "option_not_from_user"):
        assert needle in system, needle


# ── 3. exceptions carry plain values ─────────────────────────────────────────

def test_manual_review_required_holds_plain_values_not_the_order_row():
    """The 500 (MissingGreenlet): the router read ``exc.order.total_amount`` after rollback. Everything it needs is copied at raise time."""
    from app.services.payment_verification_service import ManualReviewRequired

    order = SimpleNamespace(id=68, order_number="ORD-2026-0068", total_amount=150000)
    exc = ManualReviewRequired(order, 50000)
    del order                                                         # nothing may refer to the row any more
    assert not hasattr(exc, "order")
    assert (exc.order_id, exc.order_number, exc.total, exc.threshold) == (68, "ORD-2026-0068", 150000.0, 50000.0)
    assert exc.payload() == {
        "code": "high_value_confirmation_required", "order_number": "ORD-2026-0068", "order_id": 68, "total": 150000.0, "threshold": 50000.0,
        "message": "Order ORD-2026-0068 is ₹150,000. Verify the money in your bank before approving.",
    }


def test_no_custom_exception_stores_an_orm_object():
    """Rule: never touch ORM attributes after a failed / rolled-back transaction — so no exception class may keep an ORM row in a field."""
    from app.db import Base
    import app.models  # noqa: F401  (registers every model)

    orm_names = {m.class_.__name__ for m in Base.registry.mappers}
    offenders = []
    for path in Path(__file__).resolve().parent.parent.joinpath("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in cls.bases}
            if not any(b.endswith(("Exception", "Error")) or b in {"ManualReviewRequired"} for b in bases):
                continue
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]:
                for arg in fn.args.args + fn.args.kwonlyargs:
                    ann = ast.unparse(arg.annotation).strip("'\"") if arg.annotation is not None else ""
                    if any(name in ann.replace("|", " ").replace("[", " ").replace("]", " ").split() for name in orm_names):
                        stored = any(
                            isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self" for t in n.targets)
                            and isinstance(n.value, ast.Name) and n.value.id == arg.arg
                            for n in ast.walk(fn)
                        )
                        if stored:
                            offenders.append(f"{path.name}:{cls.name}.{arg.arg}")
    assert offenders == []


# ── 4. WhatsApp labels ───────────────────────────────────────────────────────

def test_titles_that_collide_after_shortening_are_made_unique_and_stay_within_limits():
    """The live #131009: three "Floral Print…" buttons. Same-prefix names keep what tells them apart; identical names are numbered."""
    names = ["Floral Print Cotton Saree – Navy", "Floral Print Cotton Saree – Purple", "Floral Print Cotton Saree – Red"]
    got = wa_labels.unique_buttons(names)
    assert len(set(t.lower() for t in got)) == 3 and all(len(t) <= wa_labels.BUTTON_MAX for t in got)
    assert [t.split("– ")[-1] for t in got] == ["Navy", "Purple", "Red"]
    same = wa_labels.unique_row_titles(["Floral Print Cotton Saree", "Floral Print Cotton Saree", "Floral Print Cotton Saree"])
    assert len(set(same)) == 3 and all(len(t) <= wa_labels.ROW_TITLE_MAX for t in same)
    assert wa_labels.unique_buttons(["Confirm", "Change", "Cancel"]) == ["Confirm", "Change", "Cancel"]            # untouched when already fine


def test_unique_labels_property_for_random_names():
    """For any list of up-to-10 names built from a few common words: all titles unique (case-insensitive) and within the limit."""
    rng = random.Random(7)
    words = ["Floral", "Print", "Cotton", "Silk", "Saree", "Kurti", "Navy", "Purple", "Red", "Banarasi", "Zari", "Block"]
    for _ in range(300):
        n = rng.randint(2, 10)
        titles = [" ".join(rng.choice(words) for _ in range(rng.randint(1, 6))) for _ in range(n)]
        for limit, fn in ((wa_labels.BUTTON_MAX, wa_labels.unique_buttons), (wa_labels.ROW_TITLE_MAX, wa_labels.unique_row_titles)):
            out = fn(titles)
            assert len(out) == n and all(0 < len(t) <= limit for t in out), titles
            assert len({t.lower() for t in out}) == n, (titles, out)


def test_reply_labels_are_unique_in_every_interactive_widget():
    """fit_labels (the one place every reply passes) makes button titles and list-row titles unique."""
    reply = Reply(text="x", buttons=[("a", "Add Pocket Linen Kurti"), ("b", "Add Pocket Linen Kurta"), ("c", "Add Pocket Linen Kurti")])
    reply.list_rows = [("r1", "Floral Print Cotton Saree – Navy", "d"), ("r2", "Floral Print Cotton Saree – Red", "d")]
    Renderer.fit_labels(reply)
    assert len({t for _, t in reply.buttons}) == 3 and all(len(t) <= 20 for _, t in reply.buttons)
    assert len({t for _, t, _ in reply.list_rows}) == 2 and all(len(t) <= 24 for _, t, _ in reply.list_rows)


def test_cart_disambiguation_is_a_list_with_full_descriptions_never_buttons():
    """"Which one should I remove?" with three same-name lines → list rows (title ≤ 24, unique) whose description holds name + variant + qty."""
    cat = make_catalogue()
    cart = make_cart()
    for colour in ("Red", "Green", "Blue"):
        cart.add_line("KU1", color=colour, qty=1)
    renderer = Renderer("english", CLIENT, cat, cart)
    clarify = ex.Clarify(slot="remove", options=[ln["line_id"] for ln in cart.lines])
    reply = Reply(text="")
    parts: list[str] = []
    renderer.clarify(clarify, reply, parts)
    renderer.fit_labels(reply)
    assert reply.buttons is None and len(reply.list_rows) == 3
    assert len({t for _, t, _ in reply.list_rows}) == 3 and all(len(t) <= 24 for _, t, _ in reply.list_rows)
    assert all(d.startswith("1× Cotton Kurti (") for _, _, d in reply.list_rows)
    assert all(rid.startswith("v3_rm:") for rid, _, _ in reply.list_rows) and "1. Cotton Kurti (Red)" in parts[0]


def test_numbered_text_fallback_lists_the_options_once():
    """The plain-text version of a refused buttons / list message: the options numbered + "reply with the number" (in the customer's language)."""
    assert wa_labels.numbered_fallback("Which one?", ["A", "B"]) == "Which one?\n\n1. A\n2. B\nReply with the number."
    already = "Which one?\n1. A\n2. B"
    assert wa_labels.numbered_fallback(already, ["A", "B"]) == f"{already}\n\nReply with the number."
    assert "Number bhejkar" in wa_labels.numbered_fallback("Kaunsa?", ["A"], "hindi_roman")


@pytest.mark.asyncio
async def test_a_refused_interactive_message_falls_back_to_numbered_text_and_logs_it(monkeypatch, caplog):
    """Meta #131009 (or any 400) on a buttons message: ``wa_interactive_fallback`` is logged with the code, the customer gets the numbered text."""
    from app.routers import _whatsapp_adapter as adapter
    from app.services import whatsapp_service
    from app.services.order_pipeline import ButtonSpec, PipelineResult

    texts: list[str] = []

    async def _buttons(*a, **k):
        """Meta refuses: duplicate titles."""
        whatsapp_service.last_interactive_error.set({"status": 400, "code": 131009, "message": "Parameter value is not valid"})
        return False

    async def _text(to, body, *a, **k):
        """Record the plain-text fallback."""
        texts.append(body)
        return True

    async def _nonce(db, conv_id, conv):
        """No DB in this unit test."""
        return "abcd1234"

    monkeypatch.setattr(adapter.outbound, "send_buttons", _buttons)
    monkeypatch.setattr(adapter.outbound, "send_text", _text)
    monkeypatch.setattr(adapter, "_rotate_nonce", _nonce)
    result = PipelineResult(text="Which one should I remove?", buttons=[ButtonSpec("v3_rm:a", "Floral Print…"), ButtonSpec("v3_rm:b", "Floral Print…"), ButtonSpec("v3_rm:c", "Chanderi")])
    caplog.set_level(logging.WARNING)
    await adapter.send_pipeline_result(result, db=None, conv=SimpleNamespace(id=5, client_id=1, last_customer_language="english"), sender_phone="9199", pid="p")
    assert len(texts) == 1 and "1. Floral Print" in texts[0] and "2. Floral Print" in texts[0] and "3. Chanderi" in texts[0] and "Reply with the number" in texts[0]
    assert texts[0].count("Floral Print…") == 0 or len(set(l for l in texts[0].splitlines() if l.startswith(("1.", "2.")))) == 2        # the numbered titles differ
    logged = [r.getMessage() for r in caplog.records if "wa_interactive_fallback" in r.getMessage()]
    assert logged and "code=131009" in logged[0] and "kind=buttons" in logged[0]


@pytest.mark.asyncio
async def test_a_refused_list_falls_back_the_same_way(monkeypatch, caplog):
    """The list message takes the same route."""
    from app.routers import _whatsapp_adapter as adapter
    from app.services import whatsapp_service
    from app.services.order_pipeline import ListOptionSpec, PipelineResult

    texts: list[str] = []

    async def _list(*a, **k):
        """Meta refuses."""
        whatsapp_service.last_interactive_error.set({"status": 400, "code": 131009})
        return False

    async def _text(to, body, *a, **k):
        """Record the plain-text fallback."""
        texts.append(body)
        return True

    async def _nonce(db, conv_id, conv):
        """No DB in this unit test."""
        return "abcd1234"

    monkeypatch.setattr(adapter.outbound, "send_list", _list)
    monkeypatch.setattr(adapter.outbound, "send_text", _text)
    monkeypatch.setattr(adapter, "_rotate_nonce", _nonce)
    result = PipelineResult(text="Which one?", list_options=[ListOptionSpec("v3_rm:a", "Saree – Red", "1× Saree (Red)"), ListOptionSpec("v3_rm:b", "Saree – Navy", "1× Saree (Navy)")])
    caplog.set_level(logging.WARNING)
    await adapter.send_pipeline_result(result, db=None, conv=SimpleNamespace(id=5, client_id=1, last_customer_language="gujarati_roman"), sender_phone="9199", pid="p")
    assert "1. Saree – Red" in texts[0] and "2. Saree – Navy" in texts[0] and "Number mokline" in texts[0]
    assert any("wa_interactive_fallback" in r.getMessage() and "kind=list" in r.getMessage() for r in caplog.records)


# ── 5. remove something that is not in the cart ──────────────────────────────

def _removal_cart():
    """Three sarees of different colours + a kurti in the cart."""
    cat = saree_catalogue()
    cart = make_cart()
    for colour in ("Navy", "Purple", "Red"):
        cart.add_line("SR51139", color=colour, qty=1)
    cart.add_line("KU1", color="Red", size="M", qty=1)
    return cat, cart


@pytest.mark.parametrize("text,noun", [("Remove chair", "chair"), ("remove shoes", "shoes"), ("hatao chair", "chair"), ("kadhi nakho chair", "chair"), ("chair nahi chahiye", "chair")])
def test_removing_something_that_is_not_in_the_cart_says_so_without_the_planner(text, noun):
    """conv=60: "Remove chair" asked which of three sarees to remove. Now: "There's no chair in your cart.", cart unchanged, ₹0."""
    cat, cart = _removal_cart()
    path = run_fast(text, cart, cat)
    assert path is not None and path.name == "remove_absent" and path.not_in_cart == noun and path.actions == []
    result = ex.ExecResult(cart=cart.copy())
    result.effects.not_in_cart = path.not_in_cart
    reply = Renderer("english", CLIENT, cat, result.cart).render(result, cart)
    assert reply.text.startswith(f"There's no {noun} in your cart.") and "In your cart" in reply.text and "Banarasi Saree" in reply.text
    assert [b[0] for b in reply.buttons][0] == "v3_remove_menu" and len(result.cart.lines) == 4


def test_the_remove_an_item_button_opens_the_list_of_cart_lines():
    """[Remove an item] → ask_clarify(remove) → the executor offers every cart line."""
    cat, cart = _removal_cart()
    fast = fp.from_button("v3_remove_menu", cart, cat)
    result = apply(fast, cart, cat)
    assert result.effects.clarify.slot == "remove" and len(result.effects.clarify.options) == 4


@pytest.mark.parametrize("text", ["I don't want this, I want shoes", "cancel payment", "cancel", "remove it", "remove address", "hatao", "nahi chahiye", "cancel order please"])
def test_removal_sentences_with_other_content_are_not_reported_as_absent_items(text):
    """Only a short removal + a noun that names nothing in the cart; a new request, 'it', or a non-item word is left to the planner / other paths."""
    cat, cart = _removal_cart()
    removal = agent_removal.detect(text, cart, cat)
    assert removal is None or removal.absent is None


def test_two_matching_lines_still_ask_which_one_and_one_match_removes_it():
    """Disambiguation only when ≥ 2 lines actually match: "remove the saree" (3 sarees) asks; "remove the kurti" (one) removes it."""
    cat, cart = _removal_cart()
    assert run_fast("remove the saree", cart, cat).name in ("remove", "remove_which")
    one = run_fast("remove the kurti", cart, cat)
    assert one.name == "remove" and len(one.actions) == 1


def test_a_removal_phrase_inside_an_address_is_not_a_removal():
    """While the address is being typed ("Drop Point Surat") the words are the answer."""
    cat, cart = _removal_cart()
    assert run_fast("Drop Point Surat", cart, cat, clarify={"slot": "address"}) is None or run_fast("Drop Point Surat", cart, cat, clarify={"slot": "address"}).name != "remove_absent"


# ── 6. stage vs pending orders ───────────────────────────────────────────────

def test_turn_header_shows_the_activity_and_the_pending_orders_separately():
    """"stage=browsing pending_orders=[ORD-2026-0068:payment_submitted]" for an agent-v3 turn; the legacy "a→b" form for the others."""
    head = turn_trace.format_header(60, "payment", "payment", 412, {"stage": "browsing", "pending": ["ORD-2026-0068:payment_submitted"]})
    assert head == "TURN conv=60 stage=browsing pending_orders=[ORD-2026-0068:payment_submitted] 412ms"
    assert turn_trace.format_header(60, "greeting", "order_collection", 9, {"stage": "collecting", "pending": []}) == "TURN conv=60 stage=collecting 9ms"
    assert turn_trace.format_header(60, "a", "b", 5) == "TURN conv=60 stage=a→b 5ms"


def test_turn_note_is_per_turn():
    """note() lands in the running turn's header and nowhere else."""
    started = turn_trace.start()
    turn_trace.note(stage="collecting", pending=["ORD-1:pending_payment"])
    assert turn_trace._notes.get() == {"stage": "collecting", "pending": ["ORD-1:pending_payment"]}
    turn_trace.log_turn(started, enabled=False, conv=None, stage_before=None, user_text="x", result=None)
    turn_trace.start()
    assert turn_trace._notes.get() == {}


# ── 7. upsell ────────────────────────────────────────────────────────────────

def _renderer(cart) -> Renderer:
    return Renderer("english", CLIENT, make_catalogue(), cart)


def test_upsell_label_is_a_short_nickname_and_the_price_within_a_button():
    """"+ Chanderi ₹3,199" — not "Add Checks…"; always ≤ 20 characters."""
    r = _renderer(make_cart())
    for name, price, category in (("Chanderi Silk Saree", 3199, "Saree"), ("Office Wear Cotton Palazzo", 1299, "Palazzo"), ("Checks Cotton Shirt", 899, "Shirts"),
                                  ("Supercalifragilistic Kurti", 1000, "Kurti"), ("A", 99, "X")):
        view = ProductView(_product(9, "X", name, price, category, stock=5))
        label = r.upsell_label(view)
        assert label.startswith("+ ") and len(label) <= wa_labels.BUTTON_MAX and f"₹{price:,}" in label, (name, label)
    assert r.upsell_label(ProductView(_product(9, "X", "Chanderi Silk Saree", 3199, "Saree", stock=5))) == "+ Chanderi ₹3,199"


def test_upsell_is_offered_once_per_order_cycle():
    """The first summary may carry it; once it was shown (``upsell_done``) later summaries of the same cart carry none; a new cart starts again."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=2)
    assert _renderer(cart).upsell_allowed(998)
    cart.state["upsell_done"] = True
    assert not _renderer(cart).upsell_allowed(998)
    fresh = make_cart()
    fresh.add_line("KU1", color="Red", qty=2)
    assert _renderer(fresh).upsell_allowed(998)


def test_no_upsell_on_a_big_cart():
    """Above the cap (settings ``agent_upsell_max_cart_total``, ₹10,000 by default; a client may set its own) nobody is nudged for ₹250 more."""
    cart = make_cart()
    cart.add_line("KU1", color="Red", qty=2)
    assert _renderer(cart).upsell_allowed(9999) and not _renderer(cart).upsell_allowed(10001)
    shop = SimpleNamespace(**{**vars(CLIENT), "upsell_max_cart_total": 500})
    assert not Renderer("english", shop, make_catalogue(), cart).upsell_allowed(998)


def test_summary_uses_the_label_and_respects_the_once_rule():
    """The summary's middle button is the short upsell; after ``upsell_done`` it is [Change] instead."""
    cart = make_cart(payment_method="COD", customer={"name": "Asha", "address": "12 MG Road Surat 395001", "mobile": "9199"})
    cart.add_line("KU1", color="Red", qty=2)
    first = _renderer(cart).render(ex.ExecResult(cart=cart, effects=ex.Effects(show_summary=True)), make_cart())
    assert first.buttons[1][0] == "v3_upsell_tb1" and first.buttons[1][1] == "+ Tote ₹250" and first.offered_upsell == "TB1"
    cart.state["upsell_done"] = True
    again = _renderer(cart).render(ex.ExecResult(cart=cart, effects=ex.Effects(show_summary=True)), make_cart())
    assert again.buttons[1][0] == "v3_change" and again.offered_upsell is None and "Many customers also add" not in again.text


# ── 8. cheap fast paths ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "You have such a amazing catalogue", "your collection is so beautiful", "love your designs", "amazing", "nice", "wow", "ok", "okay", "fine", "theek hai",
    "aapka collection bahut accha hai", "tamaru collection khub saras che", "તમારું કલેક્શન ખૂબ સરસ છે", "आपका कलेक्शन बहुत अच्छा है", "ઓકે", "👍", "❤️",
])
def test_compliments_and_acks_are_reactions_not_planner_calls(text):
    """Thanks / compliments / emoji-only / "ok" / "nice" in five languages → the ₹0 reaction path (keeps the current context buttons)."""
    path = run_fast(text, make_cart(), make_catalogue())
    assert path is not None and path.name in ("reaction", "thanks"), (text, path and path.name)
    assert path.actions == []


@pytest.mark.parametrize("text", ["show me nice sarees", "amazing catalogue show me more", "ok 2", "what a nice price?", "nice saree ketla ni che", "I want 2 amazing"])
def test_requests_that_contain_praise_still_go_to_the_planner(text):
    """A request is not a compliment: any extra word that is not praise filler, a number or a question sends it on."""
    path = run_fast(text, make_cart(), make_catalogue())
    assert path is None or path.name != "reaction", text


def test_ok_at_the_summary_still_confirms_and_at_a_name_question_is_not_a_reaction():
    """"ok" is a yes where a yes is expected, and may be a name where one is being asked."""
    cart = make_cart(payment_method="COD", customer={"name": "Asha", "address": "12 MG Road Surat 395001", "mobile": "9199"})
    cart.add_line("KU1", color="Red", qty=2)
    cart.set_stage("confirming")
    cart.state["summary_version"] = cart.version
    assert run_fast("ok", cart, make_catalogue()).name == "confirm"
    assert run_fast("nice", make_cart(), make_catalogue(), clarify={"slot": "name"}) is None


@pytest.mark.parametrize("text", ["good morning", "hello sir", "hey there"])
def test_greetings_are_weak_english_markers(text):
    """"good morning" / "hello sir" count as English — weakly: they set a first message's language but never flip a running conversation."""
    sig = language_tracker.signal(text)
    assert sig.lang == "english" and sig.markers == 1 and not sig.strong
    assert language_tracker.decide(text, None, fallback=None).lang == "english"
    assert language_tracker.decide(text, {"lang": "gujarati_roman", "conf": 0.9}, fallback="gujarati_roman").lang == "gujarati_roman"


def test_native_greetings_and_gujarati_openers_keep_their_meaning():
    """"kem cho" → gujarati_roman at once; "namaste" says nothing; a greeting plus a clear language keeps that language."""
    assert language_tracker.decide("kem cho", None, fallback=None).lang == "gujarati_roman"
    for text in ("namaste", "hi", "hello", "hey"):
        assert language_tracker.signal(text).lang is None
    assert language_tracker.decide("namaste", None, fallback="hindi_roman").lang == "hindi_roman"
    assert language_tracker.decide("hello mane pink joiye", None, fallback=None).lang == "gujarati_roman"
    assert language_tracker.decide("Good morning, mujhe saree chahiye", None, fallback=None).lang == "hindi_roman"


# ── P. turn-trace query diagnostics ──────────────────────────────────────────

def test_statement_keys_group_the_round_trips_by_table():
    """"SELECT messages", "INSERT cost_log_entries" — what a repeated-statement list is built from."""
    assert turn_trace.statement_key("SELECT messages.id, messages.content FROM messages WHERE messages.conversation_id = $1") == "SELECT messages"
    assert turn_trace.statement_key("INSERT INTO cost_log_entries (a) VALUES ($1)") == "INSERT cost_log_entries"
    assert turn_trace.statement_key("UPDATE conversation_cart SET lines=$1") == "UPDATE conversation_cart"
    assert turn_trace.summarise_statement("SELECT a, b,\n c FROM orders WHERE x = 1") == "SELECT … FROM orders WHERE x = 1"


def test_db_line_lists_the_slowest_statements_and_the_repeated_ones():
    """The TURN block's ``DB`` line: how many statements, the 3 slowest, and kinds run 3+ times (N+1 suspects)."""
    queries = [(0.3, "SELECT a FROM orders WHERE x"), (0.2, "SELECT m FROM messages"), (0.01, "SELECT m FROM messages"), (0.02, "SELECT m FROM messages"),
               (0.5, "SELECT c FROM conversation_cart")]
    top = sorted(queries, reverse=True)[:3]
    line = turn_trace.format_db_line(len(queries), 1030, top, turn_trace.repeated_statements(queries))
    assert line.startswith("DB   : 5 statements 1030ms") and "500ms SELECT … FROM conversation_cart" in line and "repeated: SELECT messages×3" in line


@pytest.mark.asyncio
async def test_emit_pending_computes_the_cost_line_after_the_send(monkeypatch, caplog):
    """The COST ledger queries run in ``emit_pending`` (after the reply was sent), not before it."""
    calls: list[str] = []

    async def cost():
        """Records when it ran."""
        calls.append("cost")
        return {"turn": 0.15, "session": 0.3, "lifetime": 1.0}

    started = turn_trace.start()
    caplog.set_level(logging.INFO)
    turn_trace.log_turn(started, enabled=True, conv=SimpleNamespace(id=7, current_stage="greeting"), stage_before="greeting", user_text="hi",
                        result=SimpleNamespace(text="Hello", buttons=None), defer=True, cost_fn=cost)
    assert calls == [] and not [r for r in caplog.records if r.getMessage().startswith("TURN conv=")]
    await turn_trace.emit_pending()
    block = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TURN conv=")][0]
    assert calls == ["cost"] and "COST turn=₹0.15 session=₹0.30 lifetime=₹1.00" in block
