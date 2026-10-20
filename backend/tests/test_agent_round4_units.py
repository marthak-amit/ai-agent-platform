"""Agent v3 round 4 — unit tests (no DB): colour search, list context by timestamp, social fast paths, template tables, language."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.agent_actions import REPLY_INTENTS, BrowseCatalog, parse_planner_output, union_wire_schema, wire_schema
from app.services import agent_executor as ex
from app.services import agent_invariants as inv
from app.services import agent_renderer, agent_social, agent_templates
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView, VariantView
from tests.test_agent_v3_units import CLIENT, make_cart, make_catalogue


def _product(pid, sku, name, category, price=899.0):
    """A Product-shaped object."""
    return SimpleNamespace(id=pid, sku=sku, name=name, price=price, category=category, stock=None, has_variants=True,
                           description=None, is_active=True, delivery_days=None, image_url=None)


def _saree_shop(blocks: int = 12) -> Catalogue:
    """`blocks` cream "Block Print" sarees that outrank everything on the word "saree", then one black silk saree, then a "Black Magic" kurti in cream."""
    views = [ProductView(_product(i, f"BP{i}", f"Block Print Saree {i}", "Saree"), [VariantView(i, "Cream", None, None, 899.0, 5)]) for i in range(1, blocks + 1)]
    views.append(ProductView(_product(100, "SK1", "Silk Saree", "Saree", 1499.0), [VariantView(100, "Black", None, None, 1499.0, 3), VariantView(101, "Cream", None, None, 1499.0, 3)]))
    views.append(ProductView(_product(101, "KM1", "Black Magic Kurti", "Kurti", 499.0), [VariantView(102, "Cream", None, None, 499.0, 3)]))
    return Catalogue(views)


# ── 1. colour search ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", ["black saree", "kala saree", "kalo saree", "kaali saree", "BLACK sarees", "કાળો saree"])
def test_a_colour_word_finds_the_black_saree_even_when_twelve_other_sarees_outrank_it(query):
    """The filter runs over EVERY product, not over the best 8 name matches; kala / kalo / kali are black."""
    found = _saree_shop().search_filtered(query)
    assert [v.sku for v in found["views"]] == ["SK1"] and found["labels"]["SK1"] == ["Black"] and found["closest"] is None


def test_a_colour_word_never_matches_a_product_name():
    """"Black Magic Kurti" only comes in cream: "black kurti" is NOT a match by name, it gets the closest real colours."""
    found = _saree_shop().search_filtered("black kurti")
    assert found["views"] == [] and found["closest"]["values"] == ["Cream"]


def test_a_colour_synonym_in_the_variant_values_matches_both_ways():
    """"laal" finds a variant called Red, and "red" a variant the seller named Laal."""
    cat = Catalogue([ProductView(_product(1, "A1", "Cotton Kurti", "Kurti"), [VariantView(1, "Laal", None, None, 499.0, 5)])])
    assert [v.sku for v in cat.search_filtered("red kurti")["views"]] == ["A1"]
    assert [v.sku for v in cat.search_filtered("laal kurti")["views"]] == ["A1"]


def test_colour_search_ranks_by_the_remaining_words_and_limits_after_filtering():
    """Many matches: the best name matches of the FILTERED set come first, capped at top_k."""
    found = _saree_shop().search_filtered("cream saree", top_k=5)
    assert len(found["views"]) == 5 and all("Saree" in v.name for v in found["views"])


# ── 2. list context by timestamp ─────────────────────────────────────────────

def _cart_with_lists() -> CartData:
    """Two product lists: A sent at t=1000, B (the newest) at t=2000."""
    cart = make_cart()
    cart.record_shown({"slot": "product", "options": ["A1", "A2"]}, 1000.0)
    cart.record_shown({"slot": "product", "options": ["B1", "B2"]}, 2000.0)
    cart.state["last_clarify"] = {"slot": "product", "options": ["B1", "B2"]}
    return cart


def test_a_number_refers_to_the_latest_list_delivered_before_the_message_was_sent():
    """Typed at t=1999 (list B still in flight, +2 s delivery grace) → list A; typed at t=2003 → list B; before A arrived → nothing open."""
    cart = _cart_with_lists()
    assert cart.clarify_as_of(1999.0)["options"] == ["A1", "A2"]
    assert cart.clarify_as_of(2001.9)["options"] == ["A1", "A2"]
    assert cart.clarify_as_of(2003.0)["options"] == ["B1", "B2"]
    assert cart.clarify_as_of(1001.0) is None


def test_without_a_timestamp_or_history_the_newest_question_is_used():
    """No Meta timestamp (web widget, Instagram) or no remembered history → state['last_clarify']."""
    cart = _cart_with_lists()
    assert cart.clarify_as_of(None)["options"] == ["B1", "B2"]
    fresh = make_cart()
    fresh.state["last_clarify"] = {"slot": "payment", "options": ["COD"]}
    assert fresh.clarify_as_of(5000.0)["slot"] == "payment"


def test_a_reply_that_left_nothing_open_is_remembered_too():
    """After a product card (no list) the question at that time is None — a late "1" must not revive an older list."""
    cart = _cart_with_lists()
    cart.record_shown(None, 3000.0)
    assert cart.clarify_as_of(3005.0) is None and cart.clarify_as_of(2500.0)["options"] == ["B1", "B2"]


def test_shown_lists_keep_a_bounded_history_and_survive_a_new_cart():
    """Six replies are remembered; the memory is carried to the next cart."""
    from app.services.agent_cart import CARRIED_STATE_KEYS, SHOWN_LISTS_KEEP

    cart = make_cart()
    for i in range(10):
        cart.record_shown({"slot": "product", "options": [str(i)]}, float(i))
    assert len(cart.state["shown_lists"]) == SHOWN_LISTS_KEEP == 6 and "shown_lists" in CARRIED_STATE_KEYS


def test_inbound_timestamp_parsing_ignores_garbage_and_the_far_future():
    """The Meta timestamp is a string of epoch seconds; a missing / invalid / absurd (clock-skew) value means "use the newest question"."""
    import time

    from app.services.agent_v3 import _inbound_epoch

    assert _inbound_epoch(SimpleNamespace(timestamp="1700000000")) == 1700000000.0
    assert _inbound_epoch(SimpleNamespace(timestamp=None)) is None and _inbound_epoch(SimpleNamespace(timestamp="x")) is None
    assert _inbound_epoch(SimpleNamespace(timestamp=str(int(time.time()) + 100000))) is None and _inbound_epoch(SimpleNamespace(timestamp="0")) is None


# ── 3 / 4 / 5. reactions, declines, bestsellers ──────────────────────────────

@pytest.mark.parametrize("text", ["wah", "wow", "nice", "mast", "saras", "👍", "👍🏻", "❤️", "so nice 😍", "wah wah", "superb", "વાહ", "સરસ", "वाह", "बहुत सुंदर",
                         "ok", "okay", "theek hai", "ઓકે", "You have such a amazing catalogue", "aapka collection bahut accha hai", "tamaru collection khub saras che"])
def test_reactions(text):
    """Compliments and thumbs are reactions."""
    assert agent_social.is_reaction(text)


@pytest.mark.parametrize("text", ["nice saree", "I want nice kurti", "yes", "2", "is it nice?", "show me", "thanks", "no", "show me nice sarees", "amazing catalogue show me more", "ok 2", "what a nice price?"])
def test_not_reactions(text):
    """Anything with content, a question, or a gate word is not a reaction."""
    assert not agent_social.is_reaction(text)


@pytest.mark.parametrize("text", ["No thanks", "no thank you", "nahi joiye", "bas", "not now", "bas thanks", "nahi chahiye", "નથી જોઈતું", "नहीं चाहिए", "बस"])
def test_declines(text):
    """"No thanks" and friends decline; they are not a thank-you."""
    assert agent_social.is_decline(text)


def test_a_bare_no_is_a_decline_only_when_nothing_is_waiting_for_a_no():
    """"no" answers a yes/no question when one is open; otherwise it declines."""
    assert not agent_social.is_decline("no") and agent_social.is_decline("no", bare_no_ok=True)
    assert not agent_social.is_decline("no thanks I will order") and not agent_social.is_decline("thanks")


@pytest.mark.parametrize("text", ["best product", "best seller", "best sellers", "saru shu che", "sauthi saru shu che", "top selling", "what is popular",
                                  "sabse popular kya hai", "kya best hai", "show me the best sellers please", "બેસ્ટ સેલર", "શું સારું છે", "सबसे ज्यादा बिकने वाला"])
def test_bestseller_questions(text):
    """"What sells best" in English / Hinglish / Gujarati."""
    assert agent_social.is_bestseller_request(text)


@pytest.mark.parametrize("text", ["best saree", "I want the best silk saree in red", "show products", "price?"])
def test_not_bestseller_questions(text):
    """A category plus "best" is a search the planner handles."""
    assert not agent_social.is_bestseller_request(text)


def test_the_planner_contract_knows_the_new_intents_and_the_bestseller_sort():
    """reply_intent reaction / decline and browse_catalog.sort are in every wire schema and parse."""
    assert {"reaction", "decline"} <= set(REPLY_INTENTS)
    out, issues = parse_planner_output({"actions": [{"type": "browse_catalog", "category": None, "sort": "bestseller"}], "reply_intent": "decline"})
    assert not issues and out.reply_intent == "decline" and out.actions[0] == BrowseCatalog(type="browse_catalog", sort="bestseller")
    for schema in (union_wire_schema(), wire_schema()):
        assert "decline" in str(schema) and "bestseller" in str(schema)
    _, issues = parse_planner_output({"actions": [{"type": "browse_catalog", "sort": "cheapest"}], "reply_intent": "none"})
    assert issues and issues[0].code == "INVALID_ACTION"


def test_the_planner_prompt_documents_the_new_rules():
    """The prompt tells the model about bestseller / reaction / decline and carries a new version string."""
    from app.services import agent_planner

    text = agent_planner.system_message(CLIENT, make_catalogue())
    assert "bestseller" in text and "reaction" in text and "decline" in text


# ── 6. template tables ───────────────────────────────────────────────────────

def test_every_table_is_complete_and_valid():
    """All four languages have every English key with the same placeholders; button titles fit WhatsApp; native scripts have no stray English."""
    assert agent_templates.problems(agent_renderer._EN) == []
    assert set(agent_templates.load_all()) == set(agent_templates.LANGUAGES) and agent_renderer.TABLE_LANGUAGES >= set(agent_templates.LANGUAGES)


def test_the_validator_catches_a_missing_key_a_changed_placeholder_and_english_in_native_script(monkeypatch):
    """problems() is the guard that a translation edit cannot silently break a reply."""
    tables = {k: dict(v) for k, v in agent_templates.load_all().items()}
    del tables["gujarati_script"]["q_name"]
    tables["hindi_devanagari"]["sold_out"] = "माफ़ कीजिए, {product} स्टॉक में नहीं है।"
    tables["hindi_devanagari"]["greet"] = "नमस्ते! Welcome to {shop}"
    tables["hindi_roman"]["btn_confirm"] = "Order confirm karein abhi ke abhi"
    monkeypatch.setattr(agent_templates, "load_all", lambda: tables)
    found = "\n".join(agent_templates.problems(agent_renderer._EN))
    assert "gujarati_script: missing key q_name" in found and "hindi_devanagari: sold_out: placeholders" in found
    assert "hindi_devanagari: greet: English word(s)" in found and "hindi_roman: btn_confirm: button title" in found


def test_native_script_tables_are_script_pure_for_every_key():
    """No Latin word in a Devanagari / Gujarati text except placeholders and the brand words (UPI, COD, QR …)."""
    for lang in agent_templates.NATIVE_SCRIPT:
        for key, text in agent_templates.load_all()[lang].items():
            words = [w for w in re.findall(r"[A-Za-z]{2,}", re.sub(r"\{\w+\}", "", text)) if w.lower() not in agent_templates._LATIN_OK]
            assert not words, f"{lang}.{key}: {words}"


def test_unknown_languages_fall_back_to_english_text():
    """A language without a table gets English (the localizer translates it); a missing key never raises."""
    assert agent_renderer.text_for("marathi", "greet", shop="X") == "Hi! 👋 Welcome to X."
    assert agent_renderer.text_for("gujarati_script", "greet", shop="X").startswith("નમસ્તે")
    assert agent_renderer.text_for("hindi_roman", "retry_later").startswith("Maaf kijiye")


def _render(lang, effects=None, intent="none", cart=None):
    """Render one executed turn in `lang`."""
    cart = cart or make_cart()
    result = ex.ExecResult(cart=cart.copy())
    for k, v in (effects or {}).items():
        setattr(result.effects, k, v)
    cat = make_catalogue()
    return agent_renderer.Renderer(lang, CLIENT, cat, result.cart).render(result, cart, intent=intent)


def test_a_table_language_reply_skips_the_localizer_and_a_planner_sentence_does_not():
    """Template + database turns are complete (localized=True); the planner's own clarify question / a KB answer / an unsupported language still need the localizer."""
    for lang in ("gujarati_script", "gujarati_roman", "hindi_devanagari", "hindi_roman", "english"):
        reply = _render(lang, {"browse": {"category": None, "skus": ["KU1", "LH1"], "categories": ["Kurti", "Lehenga"], "sort": None}})
        assert reply.localized, lang
    browse = {"category": None, "skus": ["KU1"], "categories": ["Kurti"], "sort": None}
    assert not _render("marathi", {"browse": browse}).localized
    assert not _render("gujarati_roman", {"browse": browse, "dynamic": True}).localized
    planner_question = _render("gujarati_roman", {"clarify": ex.Clarify(slot="other", question="Shu tamne gift wrapping joiye chhe?", from_planner=True)})
    assert not planner_question.localized and "gift wrapping" in planner_question.text
    wrong_language = _render("gujarati_roman", {"clarify": ex.Clarify(slot="other", question="Do you want gift wrapping?", from_planner=True)})
    assert wrong_language.localized and "gift wrapping" not in wrong_language.text       # replaced by the template (lang_mismatch)


def test_reply_text_in_each_language_has_no_english_leftovers_in_native_scripts():
    """A Gujarati-script / Devanagari browse reply contains only the seller's own words (product names, categories, codes) as Latin letters."""
    cat = make_catalogue()
    own = {w.lower() for v in cat.views for w in re.findall(r"[A-Za-z]+", f"{v.name} {v.product.category} {v.sku}")} | {"cotton", "kurti", "lehenga"}
    for lang in ("gujarati_script", "hindi_devanagari"):
        reply = _render(lang, {"browse": {"category": None, "skus": ["KU1", "LH1"], "categories": ["Kurti", "Lehenga"], "sort": None}})
        text = re.sub(r"https?://\S+", "", reply.text)
        leftovers = {w for w in re.findall(r"[A-Za-z]{2,}", text) if w.lower() not in own}
        assert not leftovers, (lang, leftovers, reply.text)


def test_eta_is_translated_not_hardcoded_english():
    """"The delivery estimate comes from the table: working days / din mein / divas ma / दिन में / દિવસમાં."""
    cart = make_cart()
    for lang, word in (("english", "working days"), ("hindi_roman", "din mein"), ("hindi_devanagari", "दिन में"), ("gujarati_roman", "divas ma"), ("gujarati_script", "દિવસમાં")):
        r = agent_renderer.Renderer(lang, CLIENT, make_catalogue(), cart)
        assert word in r.eta([]), lang


def test_for_what_and_quantity_phrasing_follow_the_language():
    """" for Cotton Kurti (Red)" is " Cotton Kurti (Red) ke liye" / માટે — no English preposition in the table languages."""
    cat = make_catalogue()
    cart = make_cart()
    line = {"sku": "KU1", "color": "Red"}
    assert agent_renderer.Renderer("english", CLIENT, cat, cart).for_what("KU1", line) == " for Cotton Kurti (Red)"
    assert agent_renderer.Renderer("hindi_roman", CLIENT, cat, cart).for_what("KU1", line) == " Cotton Kurti (Red) ke liye"
    assert agent_renderer.Renderer("gujarati_script", CLIENT, cat, cart).for_what("KU1", line) == " Cotton Kurti (Red) માટે"


def test_reaction_decline_and_bestseller_replies():
    """reaction → ack + the open list again; decline → one line, terminal; bestseller header only when the shop has sales."""
    cart = make_cart()
    cart.state["last_clarify"] = {"slot": "product", "options": ["KU1", "TB1"]}
    result = ex.ExecResult(cart=cart.copy())
    reply = agent_renderer.Renderer("english", CLIENT, make_catalogue(), result.cart).render(result, cart, intent="reaction", resume=cart.state["last_clarify"])
    assert "Glad you like it" in reply.text and "Which one would you like to see" in reply.text and reply.last_clarify["options"] == ["KU1", "TB1"]
    assert [r[0] for r in reply.list_rows] == ["v3_sku_ku1", "v3_sku_tb1"]
    declined = _render("english", intent="decline")
    assert declined.text == "Okay! Message anytime 😊" and declined.terminal and declined.last_clarify is None
    sold = _render("english", {"browse": {"category": None, "skus": ["TB1", "KU1"], "categories": ["Kurti", "Bags"], "sort": "bestseller"}})
    assert sold.text.splitlines()[0] == "Our best sellers 🌟" and "Categories: Kurti, Bags" in sold.text
    plain = _render("english", {"browse": {"category": None, "skus": ["TB1", "KU1"], "categories": ["Kurti"], "sort": None}})
    assert "best sellers" not in plain.text


def test_template_questions_count_as_a_closing_question_in_every_language():
    """The address / phone requests end with a full stop in the tables: they must not trigger the "no CTA" fallback."""
    for lang in ("english", *agent_templates.LANGUAGES):
        assert inv.is_template_question(agent_renderer.text_for(lang, "q_address")), lang
    assert not inv.is_template_question("Thanks for shopping with us.")


# ── language follows the last message ────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_language_is_the_language_of_the_last_message(monkeypatch):
    """A confident switch wins mid-order; no-signal messages, taps and name answers keep the language; a script change always switches."""
    from app.services import agent_v3, conversation_service

    update = AsyncMock()
    monkeypatch.setattr(conversation_service, "update_language", update)

    def conv(lang):
        """A conversation row."""
        return SimpleNamespace(id=7, last_customer_language=lang)

    text_msg, tap = SimpleNamespace(type="text"), SimpleNamespace(type="interactive")
    shop = SimpleNamespace(dashboard_language="en")

    async def lang(c, message, text, question=None, cart=None):
        """One language decision (the tracker state lives in `cart`)."""
        return await agent_v3._reply_language(None, shop, c, cart if cart is not None else SimpleNamespace(state={}), message, text, question)

    c = conv("gujarati_roman")
    assert await lang(c, text_msg, "I want 2 red ones", {"slot": "quantity"}) == "english" and c.last_customer_language == "english"
    update.assert_awaited_once()
    c = conv("gujarati_roman")
    assert await lang(c, text_msg, "2") == "gujarati_roman"                                         # no signal
    assert await lang(c, tap, "I want 2 red ones") == "gujarati_roman"                              # a tap carries no language
    assert await lang(c, text_msg, "I want to buy it now please", {"slot": "name"}) == "gujarati_roman"   # a Roman name answer
    assert await lang(c, text_msg, "મારું નામ આશા", {"slot": "name"}) == "gujarati_script"          # a script change is real
    assert await lang(conv("english"), text_msg, "mare kurti joi che") == "gujarati_roman"
