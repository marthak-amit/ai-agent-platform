"""
Unit tests (no database) for the chat-flow hardening helpers: cancel / confusion / resume lexicons, draft snapshot
and restore window, "more like that" detection, discount pleas, 3-way switch parsing, template and button hygiene.
The end-to-end behaviour is covered by tests/replay/test_chat_flow_hardening.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import order_recovery as rec


# ── cancel wording ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "cancel", "cancel my order", "please cancel the order", "cancel karo", "order cancel kar do", "nahi chahiye",
    "rehne do", "don't want it", "i do not want this", "stop order", "stop my order", "रद्द करो", "કેન્સલ કરો",
])
def test_explicit_cancel_wording(text):
    """The words that are allowed to cancel directly."""
    assert rec.has_explicit_cancel_keyword(text)


@pytest.mark.parametrize("text", [
    "sorry i cant get it!", "i meant to say i cant got it", "what are you trying to say", "hmm", "no", "stop",
    "don't cancel", "do not cancel it", "can I cancel?", "why was my order cancelled?", "I want to continue this", "",
])
def test_not_explicit_cancel_wording(text):
    """Confusion, apologies, a bare no/stop, a negated cancel and a question about cancelling never count."""
    assert not rec.has_explicit_cancel_keyword(text)


# ── confusion ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "sorry i cant get it!", "i meant to say i cant got it", "I can't understand", "cant understand", "i dont get it",
    "what are you trying to say", "what do you mean", "this is confusing", "I am confused", "samjh nahi aaya",
    "samajh nahi aaya", "kya bol rahe ho", "क्या बोल रहे हो", "સમજાયું નહીં", "not getting it",
])
def test_confusion_phrases(text):
    """'I am lost' in English, Hinglish, Devanagari and Gujarati script."""
    assert rec.is_confused(text)


@pytest.mark.parametrize("text", [
    "I can't get it in blue", "cant find the size chart", "cancel it, I'm confused", "I don't understand why the price is 5599 for this saree and not 4000 like before",
    "red", "2", "yes", "show me kurtis",
])
def test_not_confusion(text):
    """A product question that happens to contain 'can't get', a cancel, or a long real request is not confusion."""
    assert not rec.is_confused(text)


def test_question_marks_only():
    """A bare '?' / '??' / '؟' message, nothing else."""
    assert rec.is_question_marks_only("?") and rec.is_question_marks_only(" ?? ") and rec.is_question_marks_only("؟")
    assert not rec.is_question_marks_only("price?") and not rec.is_question_marks_only("") and not rec.is_question_marks_only("...")


# ── resume ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I want to continue this", "continue", "yes", "ye", "haan", "ok", "restore my order", "i didnt mean to cancel", "chalu rakho",
])
def test_resume_requests(text):
    """Words that bring a cancelled draft back."""
    assert rec.is_resume_request(text)


@pytest.mark.parametrize("text", ["no", "cancel", "show me sarees", "what is the price of the red one in medium size please", ""])
def test_not_resume_requests(text):
    """No / cancel / an unrelated request do not restore anything."""
    assert not rec.is_resume_request(text)


# ── snapshot ─────────────────────────────────────────────────────────────────

def _conv(**kw):
    """A conversation-like object with a Red × 2 saree draft."""
    base = dict(
        pending_product_sku="SR63896", pending_order_quantity=2, selected_color="Red", selected_size=None,
        selected_material=None, customer_name="Amit", delivery_address="702 Somerest", payment_method="UPI",
        summary_shown=True, cart_items=None, cart_variant_mode="same", cart_collection_mode=None, cart_wip_item=None,
        cart_pending_confirmation=None, last_cancelled_draft=None, cancel_confirm_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_snapshot_keeps_slots_sku_cart_and_timestamp():
    """Everything that makes up the draft is saved; empty fields and a False summary flag are not."""
    now = datetime(2026, 10, 17, 15, 26, tzinfo=timezone.utc)
    snap = rec.build_snapshot(_conv(cart_items=[{"sku": "SR63896", "qty": 1}]), now)
    assert snap["ts"] == now.isoformat()
    fields = snap["fields"]
    assert fields["pending_product_sku"] == "SR63896" and fields["selected_color"] == "Red" and fields["pending_order_quantity"] == 2
    assert fields["cart_items"] == [{"sku": "SR63896", "qty": 1}] and fields["summary_shown"] is True
    assert "selected_size" not in fields and "cart_wip_item" not in fields


def test_no_snapshot_without_a_draft():
    """No pinned product -> nothing to save."""
    assert rec.build_snapshot(_conv(pending_product_sku=None)) is None


def test_restore_window_is_thirty_minutes_by_default():
    """Inside 30 minutes the draft is restorable; at 31 it is not; garbage is ignored."""
    now = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
    fresh = {"ts": (now - timedelta(minutes=29)).isoformat(), "fields": {"pending_product_sku": "SR63896"}}
    stale = {"ts": (now - timedelta(minutes=31)).isoformat(), "fields": {"pending_product_sku": "SR63896"}}
    assert rec.restorable_draft(_conv(last_cancelled_draft=fresh), now) is fresh
    assert rec.restorable_draft(_conv(last_cancelled_draft=stale), now) is None
    assert rec.restorable_draft(_conv(last_cancelled_draft={"ts": "nope", "fields": {"pending_product_sku": "X"}}), now) is None
    assert rec.restorable_draft(_conv(last_cancelled_draft={"ts": now.isoformat(), "fields": {}}), now) is None
    assert rec.restorable_draft(_conv(), now) is None


def test_summary_line():
    """'Product Colour, Size × qty' for a single item, every line for a cart."""
    snap = rec.build_snapshot(_conv(selected_size="L"))
    assert rec.summary_line(snap, "Paisley Georgette Saree") == "Paisley Georgette Saree Red, L × 2"
    cart = rec.build_snapshot(_conv(cart_items=[
        {"product_name": "Saree", "color": "Red", "size": None, "qty": 1}, {"product_name": "Necklace", "color": "Antique", "qty": 2},
    ]))
    assert rec.summary_line(cart) == "Saree Red × 1; Necklace Antique × 2"


def test_cancel_confirm_window():
    """The open 'Cancel this order?' question expires after cancel_confirm_ttl_minutes (10)."""
    now = datetime.now(timezone.utc)
    assert rec.cancel_confirm_open(_conv(cancel_confirm_at=now - timedelta(minutes=9)))
    assert not rec.cancel_confirm_open(_conv(cancel_confirm_at=now - timedelta(minutes=11)))
    assert not rec.cancel_confirm_open(_conv())


# ── router_actions / side_questions / pipeline helpers ───────────────────────

@pytest.mark.parametrize("text,expected", [
    ("show me some more products like that", True), ("another one?", True), ("aur dikhao", True), ("something similar", True),
    ("do you have more sizes?", False), ("another colour", False), ("others", False), ("red saree", False),
])
def test_is_more_request(text, expected):
    """'More like that' wording, without catching 'more sizes' or 'another colour' (product questions)."""
    from app.services.router_actions import is_more_request

    assert is_more_request(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("Sure, let's continue!", True), ("You're welcome! 😊", True), ("Sure thing!", True), ("", True),
    ("Would you like to order this?", False), ("Here is the catalogue: https://x.in/shop", False),
    ("I can show you products, take an order or check an order's status.", False), ("We have 3 options", False),
])
def test_is_pure_ack(text, expected):
    """Under 6 words with no question, number or link carries no information."""
    from app.services.router_actions import is_pure_ack

    assert is_pure_ack(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("please", True), ("pls", True), ("Please sir", True), ("please na", True), ("plz plz", True),
    ("please send the catalogue link of sarees", False), ("please?", False), ("yes", False), ("5 please", False), ("", False),
])
def test_discount_plea(text, expected):
    """A bare plea only — no product, number or question in it."""
    from app.services.side_questions import _is_plea

    assert _is_plea(text) is expected


def test_discount_topic_is_detected_and_asks_no_question_itself():
    """The discount statement is one sentence; the caller adds the single follow-up question."""
    from app.services.language_templates import get_template
    from app.services.side_questions import detect_topic

    assert detect_topic("too expensive!") == "discount" and detect_topic("give me some discount!") == "discount"
    for lang in ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script"):
        assert "?" not in get_template(lang, "sq_discount")


@pytest.mark.parametrize("text,both,keep,switch", [
    ("add both", True, False, False), ("both", True, False, False), ("dono add karo", True, False, False),
    ("keep current", False, True, False), ("keep", False, True, False),
    ("switch", False, False, True), ("yes", False, False, True), ("don't switch", False, False, False), ("no", False, False, False),
])
def test_switch_offer_answers(text, both, keep, switch):
    """Typed answers (and button texts) of the Switch / Add both / Keep current offer."""
    from app.services import order_pipeline as op

    assert op._is_add_both(text) is both
    assert op._wants_keep(text) is keep
    assert (not op._is_add_both(text) and op._wants_switch(text)) is switch


def test_switch_offer_has_three_whatsapp_buttons_and_none_elsewhere():
    """WhatsApp gets Switch / Add both / Keep current; Instagram relies on typed answers."""
    from app.services import order_pipeline as op

    wa = op.switch_offer_buttons("english", SimpleNamespace(channel="whatsapp"))
    assert [b.id for b in wa] == ["switch_product", "add_both", "keep_current"]
    assert [b.title for b in wa] == ["Switch", "Add both", "Keep current"]
    assert op.switch_offer_buttons("english", SimpleNamespace(channel="instagram")) is None


def test_addon_button_title_fits_whatsapp_and_matches_the_text_option():
    """'2️⃣ Add <name> (₹…) too' -> 'Add <name>' within 20 characters, in every language."""
    from app.services import order_pipeline as op

    line = "Reply:\n1️⃣ Confirm & pay\n2️⃣ Add golden necklace (₹150,000) too\n3️⃣ Cancel"
    assert op._SUMMARY_ADDON_RE.search(line).group(1) == "golden necklace"
    assert op._addon_button_title("english", "golden necklace") == "Add golden necklace"
    for lang in ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script"):
        title = op._addon_button_title(lang, "Daily Wear Patola Silk Saree")
        assert 0 < len(title) <= 20, (lang, title)
    assert op._SUMMARY_ADDON_RE.search("2️⃣ Daily Wear Saree (₹6,899) bhi add karein").group(1) == "Daily Wear Saree"
    assert op._SUMMARY_ADDON_RE.search("Reply:\n1️⃣ Confirm & pay\n2️⃣ Cancel") is None


# ── templates ────────────────────────────────────────────────────────────────

_NEW_KEYS = [
    "rt_cancel_confirm", "btn_cancel_yes", "btn_cancel_no", "rt_cancel_kept", "rt_confused_prefix", "rt_confused_split",
    "rt_confused_payment", "rt_confused_idle", "rt_restored", "rt_restore_unavailable", "rt_restore_expired", "rt_restore_offer",
    "rt_menu_fallback", "rt_pick_header", "rt_more_header", "rt_more_cheaper_header", "rt_more_none", "rt_more_cheaper_none",
    "switch_offer3", "btn_switch", "btn_add_both", "btn_keep_current", "switch_kept", "switch_add_both_later",
    "switch_add_both_now", "sq_discount", "sq_discount_repeat", "btn_add_item", "ask_cart_colors", "summary_upi_only_label",
    "rt_eta_single", "pay_proof_received_high_value",
]
_LANGS = ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script")


@pytest.mark.parametrize("lang", _LANGS)
def test_new_templates_exist_in_all_five_languages(lang):
    """Every new key is hand-written for English, Hindi (both scripts) and Gujarati (both scripts)."""
    from app.services.language_templates import TEMPLATES

    missing = [k for k in _NEW_KEYS if not TEMPLATES[lang].get(k)]
    assert not missing, (lang, missing)


@pytest.mark.parametrize("lang", _LANGS)
def test_new_button_labels_fit_the_whatsapp_limit(lang):
    """WhatsApp truncates button titles at 20 characters — none of ours may need it."""
    from app.services.language_templates import get_template

    for key in ("btn_cancel_yes", "btn_cancel_no", "btn_switch", "btn_add_both", "btn_keep_current"):
        assert len(get_template(lang, key)) <= 20, (lang, key)


@pytest.mark.parametrize("lang", _LANGS)
def test_new_templates_format_with_their_placeholders(lang):
    """No KeyError / stray braces when the engine fills them."""
    from app.services.language_templates import get_template as t

    samples = [
        t(lang, "rt_restored", summary="X Red × 2"), t(lang, "switch_offer3", product="A", pinned="B"),
        t(lang, "switch_kept", pinned="B"), t(lang, "switch_add_both_later", product="A", pinned="B"),
        t(lang, "switch_add_both_now", product="A"), t(lang, "ask_cart_colors", n=2, colors="Red / Pink"),
        t(lang, "rt_confused_idle", business="Shop", catalogue_url="https://x"), t(lang, "rt_more_none", catalogue_url="https://x"),
        t(lang, "rt_eta_single", date="20 Oct"), t(lang, "btn_add_item", name="Necklace"),
    ]
    assert all(s and "{" not in s and "}" not in s for s in samples), samples


def test_cart_summary_text_options_match_its_two_buttons():
    """The cart summary no longer promises an 'Add another item' option its buttons do not have."""
    from app.services.language_templates import TEMPLATES

    for lang in _LANGS:
        tail = TEMPLATES[lang]["cart_order_summary"].rsplit("1️⃣", 1)[1]
        assert "2️⃣" in tail and "3️⃣" not in tail, (lang, tail)


# ── prompt / schema ──────────────────────────────────────────────────────────

def test_router_prompt_documents_confused_and_the_explicit_cancel_rule():
    """The model is told that confusion is `confused`, never cancel/smalltalk, with worked examples."""
    from app.schemas.router import ROUTER_ACTIONS, RouterDecision
    from app.services import intent_router

    system = intent_router.load_prompt().system
    assert "confused" in ROUTER_ACTIONS
    assert "- confused" in system and "NEVER use cancel_order or smalltalk" in system
    assert '"sorry i cant get it!"' in system and '"action":"confused"' in system
    assert "EXPLICITLY say" in system
    assert RouterDecision.model_validate({"action": "confused", "confidence": 0.9}).action.value == "confused"


# ── payment / catalogue helpers ──────────────────────────────────────────────

def test_high_value_threshold_and_flag():
    """Default ₹50,000; a shop's own limit wins; MagicMock-ish / missing values fall back to the default."""
    from app.services import payment_verification_service as pvs

    assert pvs.high_value_threshold(None) == 50000.0
    assert pvs.high_value_threshold(SimpleNamespace(high_value_order_threshold=None)) == 50000.0
    assert pvs.high_value_threshold(SimpleNamespace(high_value_order_threshold=20000)) == 20000.0
    assert pvs.is_high_value(SimpleNamespace(total_amount=150000), None)
    assert not pvs.is_high_value(SimpleNamespace(total_amount=50000), None)      # the limit itself is not "above"


def test_search_does_not_match_short_words_inside_longer_ones():
    """'got' must not hit 'Gota Patti'; whole words and long substrings still match."""
    from app.services import catalogue_service as cs

    gota = SimpleNamespace(name="Gota Patti Banarasi Dupatta", category="Dupatta", description=None, is_active=True)
    assert cs.search_products_with_scores([gota], "got") == []
    assert cs.search_products_with_scores([gota], "i cant got it") == []
    assert cs.search_products_with_scores([gota], "gota dupatta")[0][1] is gota
    assert cs.search_products_with_scores([gota], "banaras")[0][1] is gota      # 4+ letter prefix still finds it
