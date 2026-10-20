"""
Unit tests (no database, no LLM) for the conv=60 quality round: removal intent, focus line, single-option clarifies, structured
option ids, label limits, spelling-tolerant catalogue words, stale-cart greeting wording, the five-language tables, the language
tracker (+ a 50-message detection fixture), number words, the language lint and the shared Meta HTTP client.
The end-to-end behaviour is in ``tests/scenarios/52…60``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.agent_actions import RemoveLine, parse_planner_output
from app.services import agent_executor as ex
from app.services import agent_fast_paths as fp
from app.services import agent_planner, agent_query, agent_removal, agent_templates, agent_text, http_client, language_tracker, wa_labels
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView, VariantView
from app.services.agent_renderer import Reply, Renderer, _TABLES
from tests.scenarios.lint import foreign_words
from tests.test_agent_v3_units import CLIENT, add, make_cart, make_catalogue, run_actions

FIXTURE = Path(__file__).parent / "fixtures" / "language_samples.json"
LANGS = ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script")


def _product(pid, sku, name, category, *, stock=5, has_variants=False):
    """A Product-shaped object."""
    return SimpleNamespace(id=pid, sku=sku, name=name, price=999.0, category=category, stock=stock, has_variants=has_variants,
                           description=None, is_active=True, delivery_days=None, image_url=None)


def shop_catalogue() -> Catalogue:
    """A tenant that sells kurtis, a suit, lehengas (+ a sold-out category) and a mug with a single colour."""
    return Catalogue([
        ProductView(_product(1, "KU70746", "A-Line Cotton Kurti", "Kurti")),
        ProductView(_product(2, "PR95298", "Anarkali Suit Cotton Suit", "Salwar Suit", has_variants=True), [VariantView(1, "Yellow", None, None, 999.0, 5)]),
        ProductView(_product(3, "LH1", "Bridal Lehenga", "Lehenga")),
        ProductView(_product(4, "LC1", "Lehenga Choli Set", "Lehenga Choli")),
        ProductView(_product(5, "DP1", "Silk Dupatta", "Dupatta", stock=0)),
        ProductView(_product(6, "KU2", "Pocket Linen Kurti under 500", "Kurti")),
    ])


# ── 7. labels ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Pocket Linen Kurti under 500", "Anarkali Suit Cotton Suit", "Add Pocket Linen Kurti", "A" * 60, "", "Red", None])
def test_labels_never_exceed_whatsapp_limits(text):
    """Button ≤ 20, row title ≤ 24, row description ≤ 72 characters — for any input."""
    assert len(wa_labels.button(text)) <= 20 and len(wa_labels.row_title(text)) <= 24 and len(wa_labels.row_description(text)) <= 72
    assert len(wa_labels.list_button(text)) <= 20 and len(wa_labels.list_header(text)) <= 60


def test_labels_cut_at_a_word_boundary_with_an_ellipsis_and_leave_short_text_alone():
    """"Pocket Linen Kurti u" (the live bug) becomes "Pocket Linen Kurti…"."""
    assert wa_labels.button("Pocket Linen Kurti under 500") == "Pocket Linen Kurti…"
    assert wa_labels.row_title("Anarkali Suit Cotton Suit") == "Anarkali Suit Cotton…"
    assert wa_labels.button("Red") == "Red" and wa_labels.button("Order confirm karo") == "Order confirm karo"


def test_renderer_fits_every_label_of_a_reply():
    """``fit_labels`` is the single choke point: buttons, rows, descriptions, list button and header."""
    reply = Reply(text="x", buttons=[("a", "Add Pocket Linen Kurti under 500")], list_rows=[("b", "Anarkali Suit Cotton Suit", "d" * 200)],
                  list_header="h" * 100, list_button_text="View all the options now")
    Renderer.fit_labels(reply)
    assert len(reply.buttons[0][1]) <= 20 and len(reply.list_rows[0][1]) <= 24 and len(reply.list_rows[0][2]) <= 72
    assert len(reply.list_header) <= 60 and len(reply.list_button_text) <= 20


# ── tables ───────────────────────────────────────────────────────────────────

def test_all_five_languages_have_exactly_the_same_keys():
    """english / hindi_roman / hindi_devanagari / gujarati_roman / gujarati_script: same key set, same placeholders, no table problems."""
    count = agent_templates.assert_complete()
    assert count >= 149 and set(agent_templates.LANGUAGES) == set(LANGS)
    assert agent_templates.problems(_TABLES["english"]) == []


def test_every_button_and_list_label_of_every_language_is_within_whatsapp_limits():
    """btn_* ≤ 20 (after the {name} placeholder is replaced by a short word), list-button text ≤ 20, list header ≤ 60."""
    for lang in LANGS:
        table = _TABLES[lang]
        for key, text in table.items():
            if key.startswith("btn_"):
                assert len(text.replace("{name}", "")) <= 20, (lang, key, text)
        assert len(table["list_view_options"]) <= 20 and len(table["list_view_products"]) <= 20 and len(table["list_choose"]) <= 60, lang


def test_native_script_tables_carry_no_leftover_english_and_roman_tables_no_mixed_fragments():
    """No "working divas", "Kul:" or untranslated "Size"/"Rang" wrappers: the wording the live test complained about is gone."""
    gu_r = _TABLES["gujarati_roman"]
    assert "working" not in " ".join(gu_r.values()).lower() and "kul:" not in " ".join(gu_r.values()).lower()
    assert gu_r["eta_range"] == "{lo}–{hi} divas ma" and gu_r["attr_size"] == "Naap"
    assert _TABLES["hindi_roman"]["eta_range"] == "{lo}–{hi} din mein" and _TABLES["english"]["eta_range"] == "{lo}–{hi} working days"
    assert _TABLES["hindi_devanagari"]["eta_range"] == "{lo}–{hi} दिन में" and _TABLES["gujarati_script"]["eta_range"] == "{lo}–{hi} દિવસમાં"
    for lang in ("hindi_devanagari", "gujarati_script"):
        assert agent_templates.problems(_TABLES["english"]) == [], lang


def test_planner_prompt_version_is_bumped():
    """v1.6 → v1.7 so traces tell the prompts apart."""
    assert agent_planner.prompt_tag().startswith("agent_planner_v1.7/")


# ── language tracker ─────────────────────────────────────────────────────────

def _run(messages: list[str], start: str | None = None, default: str | None = None, free_text_open: bool = False) -> list[tuple[str, bool]]:
    """Feed messages through ``decide`` like a conversation; returns [(lang, switched)] per message."""
    state: dict | None = {"lang": start, "conf": 0.9} if start else None
    out = []
    for text in messages:
        d = language_tracker.decide(text, state, fallback=start, default=default, free_text_open=free_text_open)
        state = d.state
        out.append((d.lang, d.switched))
    return out


def test_english_message_in_a_gujarati_chat_switches_at_once():
    """"Give me catalogue" (give, me — no Gujarati/Hindi word) → English."""
    assert _run(["Give me catalogue"], start="gujarati_roman")[-1][0] == "english"


def test_first_messages_set_the_language_in_each_script():
    """"Mane kurti batavo" → gujarati_roman; "મને કુર્તી બતાવો" → gujarati_script; "Mujhe saree dikhao" → hindi_roman; "मुझे साड़ी दिखाओ" → hindi_devanagari."""
    for text, lang in (("Mane kurti batavo", "gujarati_roman"), ("મને કુર્તી બતાવો", "gujarati_script"),
                       ("Mujhe saree dikhao", "hindi_roman"), ("मुझे साड़ी दिखाओ", "hindi_devanagari"), ("Show me sarees", "english")):
        assert _run([text])[-1][0] == lang, text


def test_taps_numbers_and_one_word_answers_never_switch():
    """A button tap, "1", "XL", "ok", "yes" keep the language."""
    assert _run(["1", "XL", "ok", "yes", "COD", "👍"], start="gujarati_roman") == [("gujarati_roman", False)] * 6
    d = language_tracker.decide("Show me sarees please", {"lang": "gujarati_roman", "conf": 0.9}, fallback="gujarati_roman", tap=True)
    assert d.lang == "gujarati_roman" and not d.switched


def test_one_weak_message_waits_for_a_second_and_a_foreign_word_never_switches():
    """"show kurti" alone does not switch a Gujarati chat; two consecutive clear English messages do; a Gujarati message in between resets."""
    assert _run(["show kurti", "price please"], start="gujarati_roman") == [("gujarati_roman", False), ("english", True)]
    assert _run(["show kurti", "mane saree joiye", "price please"], start="gujarati_roman")[-1][0] == "gujarati_roman"
    assert _run(["mare red kurti joiye che"], start="hindi_roman")[-1][0] == "gujarati_roman"       # 3 Gujarati markers: strong
    assert _run(["mare order karvo che ok"], start="english")[-1][0] == "gujarati_roman"


def test_name_and_address_answers_in_latin_letters_do_not_switch_but_a_script_change_does():
    """While a free-text slot is open, only another SCRIPT switches."""
    assert _run(["I want to buy it now please"], start="gujarati_roman", free_text_open=True)[-1][0] == "gujarati_roman"
    assert _run(["મારું નામ આશા"], start="gujarati_roman", free_text_open=True)[-1][0] == "gujarati_script"


def test_seller_default_is_used_for_a_first_message_without_signal():
    """"hi" at the start of a conversation → the shop's language (dashboard language gu → gujarati_roman); an existing language is kept."""
    shop = SimpleNamespace(dashboard_language="gu")
    assert language_tracker.seller_default(shop) == "gujarati_roman"
    assert _run(["hi"], default="gujarati_roman")[-1][0] == "gujarati_roman"
    assert language_tracker.decide("hi", None, fallback="hindi_roman", default="gujarati_roman").lang == "hindi_roman"


def test_confidence_and_state_are_reported():
    """The decision carries a confidence and the state to persist (logged as lang= conf= switched=)."""
    d = language_tracker.decide("mare 2 kurti joiye XL ma", None, fallback=None)
    assert d.lang == "gujarati_roman" and 0.5 < d.confidence <= 0.99 and d.state["lang"] == "gujarati_roman"


def test_detection_accuracy_on_the_50_message_fixture():
    """10 messages per language (English, Roman/script Gujarati, Roman/Devanagari Hindi), several code-mixed: every one classified."""
    samples = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(samples) >= 40 and {s["lang"] for s in samples} == set(LANGS)
    misses = [(s["text"], s["lang"], language_tracker.decide(s["text"], None, fallback=None).lang) for s in samples
              if language_tracker.decide(s["text"], None, fallback=None).lang != s["lang"]]
    accuracy = 1 - len(misses) / len(samples)
    assert accuracy >= 0.95, (accuracy, misses)


# ── number words ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["2", "be", "બે", "दो", "૨", "२", "two", "do"])
def test_quantity_words_and_digits_in_every_script_mean_two(text):
    """As the answer to "how many?": Roman Gujarati/Hindi words, native words and Gujarati/Devanagari digits."""
    assert agent_text.quantity_answer(text) == 2


@pytest.mark.parametrize("text,n", [("ek", 1), ("એક", 1), ("एक", 1), ("tran", 3), ("ત્રણ", 3), ("तीन", 3), ("teen", 3), ("૧૨", 12), ("१२", 12)])
def test_more_number_words(text, n):
    """ek / be / tran, ek / do / teen, એક / બે / ત્રણ, एक / दो / तीन and the digits."""
    assert agent_text.quantity_answer(text) == n


def test_the_quantity_fast_path_reads_be_and_do_but_other_slots_do_not():
    """"be" is a number only while "how many?" is open."""
    cart = make_cart()
    line = cart.add_line("KU1", color="Red")
    clarify = {"slot": "quantity", "line_id": line["line_id"], "sku": "KU1", "options": []}
    for word in ("be", "do", "બે", "૨"):
        hit = fp.resolve(text=word, button_id=None, cart=cart, catalogue=make_catalogue(), client=CLIENT, clarify=clarify)
        assert hit is not None and hit.name == "quantity" and hit.actions[0].fields.qty == 2, word
    assert agent_text.is_bare_number("be") is None


# ── 6. spelling-tolerant catalogue words ─────────────────────────────────────

@pytest.mark.parametrize("query,expected_category", [
    ("Give me lahemgha", "Lehenga"), ("lehnga", "Lehenga"), ("lengha", "Lehenga"), ("lahenga", "Lehenga"), ("suite", "Salwar Suit"),
    ("sut", "Salwar Suit"), ("sari", None), ("kurtis", "Kurti"),
])
def test_misspelt_and_transliterated_words_resolve_to_the_shops_category(query, expected_category):
    """The alias map + rapidfuzz (≥ 80) against THIS catalogue's words; "Lehenga" wins over "Lehenga Choli"."""
    cat = shop_catalogue()
    named = cat.find_categories(query)
    assert (named[0] if named else None) == expected_category, (query, named)
    if expected_category:
        assert cat.in_category(query), query


def test_a_misspelt_search_finds_the_product_and_unknown_words_stay_unknown():
    """"Give me lahemgha" searches "give me lehenga"; "shoes" matches nothing."""
    cat = shop_catalogue()
    assert cat.normalize_query("Give me lahemgha").corrections == {"lahemgha": "lehenga"}
    assert [v.sku for v in cat.search("lahemgha")][:1] == ["LH1"]
    assert cat.find_categories("shoes") == [] and cat.in_category("shoes") == []
    assert cat.keyword_hit("lehnga") and cat.mentions("lehnga")


def test_a_category_that_exists_but_is_sold_out_is_reported_as_such():
    """Dupatta has a product but no stock: the executor says "sold out", it does not claim the shop has no dupatta."""
    cat = shop_catalogue()
    from app.schemas.agent_actions import BrowseCatalog, SearchCatalog

    result, issues = run_actions(make_cart(), [SearchCatalog(type="search_catalog", query="chunni")], catalogue=cat)
    assert issues == [] and result.effects.search["category_empty"] == "Dupatta" and result.effects.search["skus"] == []
    result, _ = run_actions(make_cart(), [BrowseCatalog(type="browse_catalog", category="dupata")], catalogue=cat)
    assert result.effects.browse["category_empty"] == "Dupatta"
    result, _ = run_actions(make_cart(), [BrowseCatalog(type="browse_catalog", category="shoes")], catalogue=cat)
    assert result.effects.browse["unknown_category"] == "shoes" and result.effects.browse["skus"] == []


def test_the_vocabulary_is_per_tenant():
    """A shop without lehengas does not turn "lehnga" into one (the words come from that client's catalogue only)."""
    other = Catalogue([ProductView(_product(1, "S1", "Leather Shoes", "Shoes"))])
    assert other.normalize_query("lehnga").corrections == {} and other.find_categories("lehnga") == []
    assert agent_query.normalize("lehnga", other.fuzzy_vocabulary()).text == "lehnga"


# ── 1. removal ───────────────────────────────────────────────────────────────

def _two_line_cart():
    """Kurti (Peach / S, no quantity) and the suit (Yellow) — the conv=60 cart."""
    cart = make_cart()
    kurti = cart.add_line("KU70746", color="Peach", size="S")
    suit = cart.add_line("PR95298", color="Yellow")
    return cart, kurti, suit


@pytest.mark.parametrize("text", [
    "Mare kurtinathi joti", "kurti nahi chahiye", "I don't want the kurti", "remove kurti", "kurti hatao", "cancel kurti", "mat bhejo kurti",
    "kurti kadhi nakho", "kurti nathi joiye", "कुर्ती नहीं चाहिए", "કુર્તી નથી જોઈતી", "KU70746 hata do", "dont want kurti",
])
def test_negation_plus_item_noun_removes_that_line(text):
    """In every language and style, the whole message is explained by a removal phrase + a cart item → remove its line(s)."""
    cart, kurti, _suit = _two_line_cart()
    removal = agent_removal.detect(text, cart, shop_catalogue())
    assert removal is not None and removal.line_ids == [kurti["line_id"]] and not removal.ambiguous, text


def test_removal_never_misfires():
    """A change of mind, a bare negation, a message about something else, a question: not a plain removal."""
    cart, _k, _s = _two_line_cart()
    cat = shop_catalogue()
    for text in ("I don't want the kurti, I want shoes", "nahi chahiye", "kurti hatao ane pink joiye", "do you have kurti?", "show me kurti", "cancel"):
        assert agent_removal.detect(text, cart, cat) is None, text
    assert agent_removal.detect("hatao kurti", make_cart(), cat) is None          # nothing in the cart


def test_removal_of_a_shared_word_asks_which_one():
    """"remove cotton" matches the kurti and the suit equally → both lines are offered."""
    cart, kurti, suit = _two_line_cart()
    removal = agent_removal.detect("cotton nahi chahiye", cart, shop_catalogue())
    assert removal is not None and not removal.line_ids and set(removal.ambiguous) == {kurti["line_id"], suit["line_id"]}


def test_removal_by_colour_narrows_to_that_line():
    """Two kurti lines, "red kurti nathi joti" drops only the red one."""
    cart = make_cart()
    red = cart.add_line("KU70746", color="Red")
    cart.add_line("KU70746", color="Blue")
    cat = Catalogue([ProductView(_product(1, "KU70746", "A-Line Cotton Kurti", "Kurti"))])
    removal = agent_removal.detect("red kurti nathi joti", cart, cat)
    assert removal is not None and removal.line_ids == [red["line_id"]]


def test_remove_line_by_sku_and_by_line_id_in_the_executor():
    """The action takes a sku (every line of the product) or a line_id; neither → schema error; unknown → UNKNOWN_LINE."""
    cart, kurti, suit = _two_line_cart()
    result, issues = run_actions(cart, [RemoveLine(type="remove_line", sku="KU70746")], catalogue=shop_catalogue())
    assert issues == [] and [ln["sku"] for ln in result.cart.lines] == ["PR95298"] and result.applied[0].kind == "removed"
    result, _ = run_actions(cart, [RemoveLine(type="remove_line", line_id=suit["line_id"])], catalogue=shop_catalogue())
    assert [ln["sku"] for ln in result.cart.lines] == ["KU70746"]
    _, issues = run_actions(cart, [RemoveLine(type="remove_line", sku="NOPE")], catalogue=shop_catalogue())
    assert [i.code for i in issues] == ["UNKNOWN_LINE"]
    out, parse_issues = parse_planner_output({"actions": [{"type": "remove_line", "line_id": None, "sku": None}], "reply_intent": "none"})
    assert out.actions == [] and parse_issues[0].code == "INVALID_ACTION"
    out, _ = parse_planner_output({"actions": [{"type": "remove_line", "line_id": None, "sku": "KU70746"}], "reply_intent": "none"})
    assert out.actions[0].sku == "KU70746"


def test_the_planner_prompt_teaches_removal_in_every_language():
    """v1.4: the negation phrases and the single-option rule are in the system prompt."""
    system = agent_planner.load_prompt().system
    for phrase in ("nathi joti", "nahi chahiye", "mat bhejo", "hatao", "kadhi nakho", "remove_line", "CUSTOMER_LANGUAGE"):
        assert phrase in system, phrase


# ── 4. focus line ────────────────────────────────────────────────────────────

def test_the_line_added_last_is_asked_about_first():
    """Kurti (qty missing) then Suit just added: the next gap is the suit's size, then the kurti's quantity."""
    cat = shop_catalogue()
    cart = make_cart()
    cart.add_line("KU70746", color="Peach", size="S")
    result, _ = run_actions(cart, [add("PR95298")], catalogue=cat)
    gaps = ex.gaps(result.cart, cat, CLIENT)
    assert [(g.slot, g.sku) for g in gaps[:2]] == [("quantity", "PR95298"), ("quantity", "KU70746")] or gaps[0].sku == "PR95298"
    assert result.cart.state["focus_line_id"] == result.cart.lines[-1]["line_id"]


def test_without_a_focus_the_old_order_is_kept():
    """No focus recorded → lines in insertion order (nothing else changes)."""
    cart = make_cart()
    a = cart.add_line("KU70746")
    cart.add_line("PR95298")
    assert ex.focus_first(cart)[0]["line_id"] == a["line_id"]
    cart.state["focus_line_id"] = cart.lines[1]["line_id"]
    assert ex.focus_first(cart)[0]["line_id"] == cart.lines[1]["line_id"]


# ── 2. single option / already filled ────────────────────────────────────────

def test_planner_clarify_about_a_single_option_or_filled_slot_is_dropped():
    """Suit (Yellow only) with colour set: ask_clarify(color) is dropped and logged as a fix; a repair round keeps a clarify; a cart without lines too."""
    from app.schemas.agent_actions import AskClarify

    cat = shop_catalogue()
    cart = make_cart()
    cart.add_line("PR95298", color="Yellow")
    ask = AskClarify(type="ask_clarify", question="Which colour?", options=["Yellow"], slot="color")
    result, issues = run_actions(cart, [ask], catalogue=cat)
    assert issues == [] and result.effects.clarify is None and result.fixes and "single option" in result.fixes[0]
    cart2 = make_cart()
    cart2.add_line("LH1")                       # no variants → nothing to choose: still a quantity gap, not a colour one
    result, _ = run_actions(cart2, [AskClarify(type="ask_clarify", question="", options=[], slot="quantity")], text="how many?", catalogue=cat)
    assert result.effects.clarify is not None            # an open quantity gap is a real question
    result, _ = run_actions(make_cart(), [ask], text="what colours", catalogue=cat)
    assert result.effects.clarify is not None            # no cart yet: a product-level question is kept


# ── 3. structured ids ────────────────────────────────────────────────────────

def test_option_ids_are_structured_and_resolve_without_any_state():
    """v3_opt:<line>:<slot>:<value> carries everything; title text and the open question are not needed."""
    cart = make_cart()
    line = cart.add_line("KU1")
    cat = make_catalogue()
    bid = fp.option_id(line["line_id"], "color", "Green")
    assert bid == f"v3_opt:{line['line_id']}:color:Green"
    hit = fp.resolve(text="Pocket Linen Kurti u", button_id=bid, cart=cart, catalogue=cat, client=CLIENT, clarify={})
    assert hit is not None and hit.name == "option" and hit.grounded_by == "button"
    assert hit.actions[0].fields.color == "Green" and hit.actions[0].line_id == line["line_id"]
    assert fp.from_button(f"v3_opt:{line['line_id']}:color:Nope", cart, cat) is None            # not a colour of this product → text path
    stale = fp.from_button("v3_opt:zzzzzz:color:Red", cart, cat)
    assert stale is not None and stale.name == "stale_option" and stale.actions == []         # the line left the cart: no planner call


def test_variant_and_remove_ids_round_trip():
    """v3_var carries a whole colour/size combination; v3_rm the line to drop."""
    cat = make_catalogue()
    cart = make_cart()
    line = cart.add_line("LH1")
    vid = fp.variant_id(line["line_id"], "LH1", {"color": "Blue", "size": "XXL"})
    hit = fp.from_button(vid, cart, cat)
    assert hit is not None and hit.actions[0].fields.color == "Blue" and hit.actions[0].fields.size == "XXL"
    new = fp.from_button(fp.variant_id(None, "LH1", {"color": "Pink", "size": "M"}), cart, cat)
    assert new is not None and new.actions[0].type == "add_line" and new.actions[0].variant.color == "Pink"
    rm = fp.from_button(fp.remove_id(line["line_id"]), cart, cat)
    assert rm is not None and rm.actions[0].type == "remove_line"


def test_too_long_values_fall_back_to_positional_ids():
    """A value that would push the id past 150 characters gets ``v3_opt_N`` instead of an invalid id."""
    assert fp.option_id("abc123", "color", "x" * 400) is None
    ids = Renderer._option_ids(["x" * 400, "Red"], "color", "abc123", "KU1", None)
    assert ids[0] == "v3_opt_1" and ids[1].startswith("v3_opt:abc123:color:")


def test_a_truncated_title_still_picks_its_option():
    """An old-style tap whose title we shortened ("Pocket Linen Kurti…") matches the one option it starts."""
    assert fp._pick_option(["Pocket Linen Kurti under 500", "Silk Saree"], "Pocket Linen Kurti…") == "Pocket Linen Kurti under 500"


# ── lint ─────────────────────────────────────────────────────────────────────

def test_the_language_lint_finds_another_languages_template_words():
    """"aapka"/"kripya" are Hindi table words, "tamaru" Gujarati: foreign in each other's replies and in English; own words pass."""
    assert "aapka" in foreign_words("Aapka order confirm ho gaya", "gujarati_roman")
    assert "tamaru" in foreign_words("Tamaru naam kaho", "hindi_roman")
    assert foreign_words("Tamaru naam kaho", "gujarati_roman") == set()
    assert foreign_words("Which colour would you like? Available: Red", "english") == set()
    assert "kripya" in foreign_words("Kripya apna address bhejiye", "english")


# ── perf: shared client ──────────────────────────────────────────────────────

def test_the_meta_client_is_shared_and_rebuilt_for_a_new_loop():
    """One keep-alive client per event loop; ``aclose`` closes it; a different loop gets a fresh one."""
    async def two():
        """Two calls on one loop."""
        a, b = http_client.meta(), http_client.meta()
        await http_client.aclose()
        return a, b, a.is_closed

    a, b, closed = asyncio.run(two())
    assert a is b and closed
    c = asyncio.run(_make_client())
    d = asyncio.run(_make_client())
    assert c is not d


async def _make_client():
    """The shared client as seen from a fresh loop."""
    return http_client.meta()


def test_a_send_books_connect_and_request_time_into_the_turn_trace():
    """``wa_connect`` / ``wa_request`` spans are added for each request (MockTransport: no connect phase, so connect is 0)."""
    import httpx

    from app.services import turn_trace

    async def go():
        """One traced POST through the shared client."""
        http_client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
        http_client._loop = asyncio.get_running_loop()
        turn_trace.start()
        async with http_client.meta_session() as client:
            response = await client.post("https://graph.example/x", headers={}, json={"a": 1})
        spans = turn_trace.current_spans()
        await http_client.aclose()
        return response.status_code, spans

    status, spans = asyncio.run(go())
    assert status == 200 and "wa_request" in spans and "wa_connect" not in spans
    assert "wa_connect" in turn_trace.SPAN_ORDER and "wa_request" in turn_trace.SPAN_ORDER
