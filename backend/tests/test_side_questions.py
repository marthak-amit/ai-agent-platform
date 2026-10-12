"""Unit tests for side_questions: topic detection in many languages, what must NOT be a side question, and fact-based answers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import side_questions as sq
from app.services.language_templates import TEMPLATES, get_template


@pytest.mark.parametrize("text,topic", [
    # returns / refund / exchange / policy — the screenshot messages first
    ("ketla diwas ma return kari sakai ka aebi kai policy nathi?", "returns"),
    ("mare return policy joi che", "returns"),
    ("what's your return policy?", "returns"),
    ("can I get a refund?", "returns"),
    ("exchange possible hai kya?", "returns"),
    ("રિટર્ન પોલિસી શું છે?", "returns"),
    ("रिटर्न पॉलिसी क्या है?", "returns"),
    ("वापसी हो सकती है क्या?", "returns"),
    ("திரும்ப பெற முடியுமா?", "returns"),
    ("రిటర్న్ పాలసీ ఏమిటి?", "returns"),
    ("ফেরত দেওয়া যাবে?", "returns"),
    ("واپسی کی پالیسی کیا ہے؟", "returns"),
    # cod / payment
    ("Is COD available?", "cod"),
    ("cod hai kya", "cod"),
    ("COD che?", "cod"),
    ("કેશ ઓન ડિલિવરી ઉપલબ્ધ છે?", "cod"),
    ("कैश ऑन डिलीवरी है क्या?", "cod"),
    ("how can I pay?", "payment_methods"),
    ("payment kaise karna hai?", "payment_methods"),
    ("મને પેમેન્ટ કેવી રીતે કરવું", "payment_methods"),
    # delivery
    ("how many days for delivery?", "delivery_time"),
    ("delivery kitne din mein hogi?", "delivery_time"),
    ("ketla divas ma delivery thase?", "delivery_time"),
    ("ડિલિવરી ક્યારે મળશે?", "delivery_time"),
    ("कब मिलेगा?", "delivery_time"),
    ("delivery charge che?", "delivery_charge"),
    ("is delivery free?", "delivery_charge"),
    ("शिपिंग चार्ज कितना है?", "delivery_charge"),
    ("do you deliver to Surat?", "delivery_area"),
    # other shop questions
    ("is there any warranty?", "warranty"),
    ("shop address?", "location"),
    ("your number?", "contact"),
    ("size chart?", "size_guide"),
    ("do you take bulk orders?", "bulk"),
    ("thoda price kam karo", "discount"),
    ("too expensive", "discount"),
    # social
    ("thank you", "thanks"), ("thanks!", "thanks"), ("shukriya", "thanks"), ("આભાર", "thanks"), ("धन्यवाद", "thanks"), ("நன்றி", "thanks"),
    ("ok bye", "bye"), ("bye", "bye"), ("આવજો", "bye"),
    ("I will think about it", "think_later"), ("pachi vichari ne kahish", "think_later"), ("baad mein batata hoon", "think_later"),
    ("let me ask my husband", "think_later"), ("વિચારીને કહીશ", "think_later"),
    ("nice", "positive"), ("very nice", "positive"), ("bahut badhiya", "positive"), ("સરસ", "positive"), ("बहुत सुंदर", "positive"),
])
def test_detect_topic_recognises_side_questions_in_many_languages(text, topic):
    """detect_topic returns the expected topic for each customer message."""
    assert sq.detect_topic(text) == topic


@pytest.mark.parametrize("text", [
    "Pink", "gold", "M", "XXL", "2", "10", "COD", "cod", "UPI", "cash", "yes", "no", "haan", "ok", "okay", "હા", "ના",
    "cod please", "COD chalega", "UPI karo", "LH10042", "Hiren Patel", "9 Ellis Bridge, Ahmedabad 380006",
    "cancel my order", "cancel my order and refund my money", "order status", "where is my order", "hi", "hello", "kem cho",
    "show me sarees", "tell me about the colours", "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?", "", "😊", "asdkjh qwe",
])
def test_detect_topic_leaves_slot_answers_and_other_flows_alone(text):
    """Option answers, names, addresses, cancel / order-status wording and plain browsing are never side questions."""
    assert sq.detect_topic(text) is None


def test_detect_topic_is_strict_at_free_text_slots():
    """While a name / address / phone is being collected only a clear question counts; an address containing 'return' does not."""
    assert sq.detect_topic("Return Road, Surat 395001", next_slot="delivery_address") is None
    assert sq.detect_topic("Near cash and carry, Pune", next_slot="delivery_address") is None
    assert sq.detect_topic("what is the return policy?", next_slot="delivery_address") == "returns"
    assert sq.detect_topic("thanks", next_slot="customer_name") == "thanks"      # never taken as the name
    assert sq.detect_topic("is COD available?", next_slot="payment_method") == "cod"
    assert sq.detect_topic("COD", next_slot="payment_method") is None


def test_compile_splits_words_and_phrases_longest_first():
    """_compile: single words in a set, multi-word entries as phrases (longest first), normalised."""
    words, phrases = sq._compile(("Return", "Money Back", "paisa wapas kar do"))
    assert words == frozenset({"return"})
    assert phrases == ("paisa wapas kar do", "money back")


def test_hits_matches_words_whole_and_phrases_on_boundaries():
    """_hits: a word only as a whole token, a phrase on word boundaries."""
    assert sq._hits("returns", "i want to return it", set("i want to return it".split()))
    assert not sq._hits("returns", "returnable bag", {"returnable", "bag"})
    assert sq._hits("returns", "paisa wapas kar do", set("paisa wapas kar do".split()))


def test_is_question_detects_marks_openers_and_availability_words():
    """_is_question: '?' / question opener / availability word."""
    assert sq._is_question("cod?", "cod", {"cod"})
    assert sq._is_question("how long", "how long", {"how", "long"})
    assert sq._is_question("cod hai", "cod hai", {"cod", "hai"})
    assert not sq._is_question("cod karo", "cod karo", {"cod", "karo"})


def _client(**kw):
    """A Client stand-in with the settings the answers read."""
    base = dict(accepts_cod=True, accepts_upi=True, cod_limit=None, delivery_days_min=3, delivery_days_max=7, business_address=None, id=1)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_build_answer_uses_shop_settings_for_cod_payment_and_delivery(monkeypatch):
    """COD / payment methods / delivery time are derived from the Client row, per language, never hard-coded."""
    monkeypatch.setattr(sq, "_kb_answer", AsyncMock(return_value=None))
    db = None
    assert "available" in await sq.build_answer(db, _client(), None, None, "cod", "english", "cod?")
    assert "₹5,000" in await sq.build_answer(db, _client(cod_limit=5000), None, None, "cod", "english", "cod?")
    assert "don't offer" in await sq.build_answer(db, _client(accepts_cod=False), None, None, "cod", "english", "cod?")
    assert "UPI or Cash on Delivery" in await sq.build_answer(db, _client(), None, None, "payment_methods", "english", "how to pay")
    assert await sq.build_answer(db, _client(accepts_cod=False), None, None, "payment_methods", "english", "x") == "You can pay by UPI."
    assert "2–4 business days" in await sq.build_answer(db, _client(delivery_days_min=2, delivery_days_max=4), None, None, "delivery_time", "english", "x")
    gujarati = await sq.build_answer(db, _client(), None, None, "delivery_time", "gujarati_script", "x")
    assert "3–7" in gujarati and "business" not in gujarati


async def test_build_answer_never_invents_a_policy(monkeypatch):
    """Returns / warranty / delivery charges: the seller's KB text when present, else the honest hand-off offer."""
    kb = AsyncMock(return_value="Exchange within 7 days.")
    monkeypatch.setattr(sq, "_kb_answer", kb)
    assert await sq.build_answer(None, _client(), None, None, "returns", "english", "return?") == "Exchange within 7 days."
    kb.return_value = None
    for topic in ("returns", "warranty", "delivery_charge", "contact", "size_guide", "bulk"):
        assert await sq.build_answer(None, _client(), None, None, topic, "english", "x") == get_template("english", "rt_handoff_offer")
    assert "7 days" not in get_template("english", "rt_handoff_offer")


async def test_build_answer_location_uses_the_business_address(monkeypatch):
    """A configured business address is quoted; without one the KB / hand-off path is used."""
    monkeypatch.setattr(sq, "_kb_answer", AsyncMock(return_value=None))
    assert "12 MG Road" in await sq.build_answer(None, _client(business_address="12 MG Road,\n Pune"), None, None, "location", "english", "x")
    assert await sq.build_answer(None, _client(), None, None, "location", "english", "x") == get_template("english", "rt_handoff_offer")


async def test_kb_answer_searches_the_customers_words_then_topic_queries():
    """_kb_answer: first the customer's own text, then the canned topic queries; counts usage; tolerates a KB failure."""
    entry = SimpleNamespace(answer="Returns accepted.", usage_count=0)
    from app.services import knowledge_service

    calls = []

    async def fake_search(client_id, query, db, **kw):
        """Only the third query ('return policy') finds the entry."""
        calls.append(query)
        return [entry] if query == "return policy" else []

    db = SimpleNamespace(commit=AsyncMock())
    original = knowledge_service.search_knowledge
    knowledge_service.search_knowledge = fake_search
    try:
        assert await sq._kb_answer(db, _client(), "ketla diwas ma return thay?", "returns") == "Returns accepted."
        assert calls[:2] == ["ketla diwas ma return thay?", "return policy"] and entry.usage_count == 1

        async def boom(*a, **k):
            """KB outage."""
            raise RuntimeError("db down")

        knowledge_service.search_knowledge = boom
        assert await sq._kb_answer(db, _client(), "x", "returns") is None
        assert await sq._kb_answer(db, None, "x", "returns") is None
    finally:
        knowledge_service.search_knowledge = original


def test_offer_open_only_while_browsing_with_a_pinned_product():
    """_offer_open: a pinned product in a browsing stage = the 'Would you like to order?' moment."""
    assert sq._offer_open(SimpleNamespace(pending_product_sku="X1", current_stage="product_inquiry"))
    assert not sq._offer_open(SimpleNamespace(pending_product_sku=None, current_stage="product_inquiry"))
    assert not sq._offer_open(SimpleNamespace(pending_product_sku="X1", current_stage="order_collection"))


def test_every_side_question_template_exists_in_all_five_template_languages():
    """The sq_* keys are hand-written for English, Hindi (both scripts) and Gujarati (both scripts)."""
    keys = [k for k in TEMPLATES["english"] if k.startswith("sq_")]
    assert len(keys) >= 12
    for lang in ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script"):
        for key in keys:
            assert TEMPLATES[lang].get(key), (lang, key)


async def test_run_side_question_guard_ignores_non_text_and_prompt_stages():
    """run_side_question_guard: no work for images, for pending yes/no prompts, or for an interrupted order."""
    conv = SimpleNamespace(pending_product_sku="X1", interrupted_sku=None, current_stage="awaiting_switch_confirm")
    args = (None, conv, _client(), SimpleNamespace(type="text"), "return policy?", "91999", None)
    assert await sq.run_side_question_guard(*args, "awaiting_switch_confirm", "english", None) is None
    assert await sq.run_side_question_guard(None, conv, _client(), SimpleNamespace(type="image"), "x", "91999", None, "order_collection", "english", None) is None
    conv.interrupted_sku = "Y2"
    assert await sq.run_side_question_guard(*args, "order_collection", "english", None) is None
    assert await sq.run_side_question_guard(None, conv, None, SimpleNamespace(type="text"), "x", "91999", None, "order_collection", "english", None) is None


@pytest.mark.parametrize("text,slot,topic", [
    ("no", "color", "decline"), ("not interested", "size", "decline"), ("no thanks", "quantity", "decline"), ("no", "customer_name", "decline"),
    ("ok", "color", "ack"), ("hmm", "size", "ack"), ("okay", "payment_method", "ack"), ("theek hai", "quantity", "ack"), ("👍", "color", "ack"),
    ("talk to a human", None, "human"), ("call me", "color", "human"), ("કોઈ સાથે વાત કરવી છે", None, "human"), ("किसी से बात करनी है", None, "human"),
    ("show me something else", "color", "change_product"), ("kuch aur dikhao", None, "change_product"), ("બીજું બતાવો", None, "change_product"),
    ("is it safe?", None, "trust"), ("is this a fraud?", "delivery_address", "trust"),
    ("why do you need my name?", "customer_name", "why_details"), ("address kyu chahiye?", "delivery_address", "why_details"),
    ("wait", "quantity", "think_later"), ("hold on", None, "think_later"), ("ruko", "color", "think_later"),
    ("can I pay later?", "payment_method", "payment_methods"), ("advance payment karna padega?", None, "payment_methods"),
])
def test_detect_topic_covers_negative_neutral_and_other_mid_flow_messages(text, slot, topic):
    """Declines, acknowledgements, hand-off requests, change-product, trust, why-details and pause words resolve at the right step."""
    assert sq.detect_topic(text, next_slot=slot) == topic


@pytest.mark.parametrize("text,slot", [
    ("no", None), ("no", "delivery_address"), ("ok", "delivery_address"), ("ok", None), ("ok", "confirmation"), ("no", "confirmation"),
    ("no", "variant_mode"), ("yes", "color"), ("no size", "size"), ("free size", "size"), ("Sundar", "customer_name"), ("Sundar Patel", "customer_name"),
    ("nice", "customer_name"), ("nahi chahiye", "quantity"), ("cancel", "color"),
])
def test_detect_topic_does_not_hijack_real_answers(text, slot):
    """A yes/no question's answer, a name like 'Sundar', and cancel wording (own flow) are never side questions."""
    assert sq.detect_topic(text, next_slot=slot) is None


def test_detect_topic_decline_and_ack_at_the_address_question_depend_on_the_saved_address_prompt():
    """With a saved address on offer 'ok'/'no' are its answers; without one they are non-answers."""
    assert sq.detect_topic("no", next_slot="delivery_address", saved_address_prompt=True) is None
    assert sq.detect_topic("no", next_slot="delivery_address", saved_address_prompt=False) == "decline"
    assert sq.detect_topic("ok", next_slot="delivery_address", saved_address_prompt=False) == "ack"


async def test_build_answer_for_the_new_topics(monkeypatch):
    """ack / decline / why_details / change_product answers come from templates; the catalogue link is the shop's own."""
    monkeypatch.setattr(sq, "_kb_answer", AsyncMock(return_value=None))
    client = _client(catalogue_slug="my-shop")
    assert await sq.build_answer(None, client, None, None, "ack", "english", "ok") == "Sure! 🙂"
    assert "reply 'cancel'" in await sq.build_answer(None, client, None, None, "decline", "english", "no")
    assert "invoice" in await sq.build_answer(None, client, None, None, "why_details", "english", "why name")
    change = await sq.build_answer(None, client, None, None, "change_product", "english", "something else")
    assert "/my-shop" in change
    assert "रद्द" in await sq.build_answer(None, client, None, None, "decline", "hindi_devanagari", "no")
    assert "રદ" in await sq.build_answer(None, client, None, None, "decline", "gujarati_script", "no")
    assert await sq.build_answer(None, client, None, None, "trust", "english", "is it safe?") == get_template("english", "rt_handoff_offer")


def test_offer_as_a_verb_is_not_a_discount_question():
    """'Do you offer gift wrapping?' is not about discounts; 'any offer?' is."""
    assert sq.detect_topic("Do you offer gift wrapping for this?") is None
    assert sq.detect_topic("any offer going on?") == "discount"
