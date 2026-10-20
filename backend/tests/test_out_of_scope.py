"""Unit tests for the out-of-scope boundary: schema, prompt, shop block, templates, handler, keyword-path check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.router import RouterAction, RouterDecision
from app.services import (
    conversation_flow, intent_router, llm_client, llm_health, router_actions, shop_context_service as scs,
)
from app.services.language_templates import TEMPLATES, get_template

LANGS = ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script")
OOS_KEYS = ("rt_out_of_scope", "rt_out_of_scope_midorder", "rt_out_of_scope_repeat")


def _profile():
    """A saree shop's profile."""
    client = SimpleNamespace(business_type="clothing", business_description="Ethnic wear: sarees and lehengas")
    products = [
        SimpleNamespace(name="Green Cotton Saree", category="Saree", is_active=True),
        SimpleNamespace(name="Cotton Lehenga", category="Lehenga", is_active=True),
    ]
    return scs.build_profile(client, products, ["Return policy"])


# ── schema / prompt / shop block ─────────────────────────────────────────────

def test_router_decision_accepts_out_of_scope_and_drops_its_hint_value_semantics():
    """The enum knows out_of_scope (anything else would fail validation → fallback)."""
    d = RouterDecision.model_validate(
        {"action": "Out_Of_Scope ", "args": {}, "language": "gu", "confidence": 0.95, "reply_hint": ""}
    )
    assert d.action is RouterAction.OUT_OF_SCOPE and d.language == "gu"
    assert "out_of_scope" in {a.value for a in RouterAction}


def test_prompt_declares_out_of_scope_and_narrows_general_answer():
    """The production prompt lists the action, keeps its reply empty, and ties general_answer to the business."""
    system = intent_router.load_prompt().system
    assert "- out_of_scope" in system and "reply_hint EMPTY" in system
    ga = next(line for line in system.splitlines() if line.startswith("- general_answer"))
    assert "THIS business sells" in ga and "Never for topics unrelated" in ga
    assert 'out_of_scope","args":{}' in system          # worked examples (weather / cricket / joke)
    assert "ALWAYS search_catalog" in system and "Is shoes available?" in system     # availability questions are searches
    assert intent_router.load_prompt().version == "router_v2.6"


def test_shop_block_carries_the_profile_text_only_when_there_is_one():
    """With a profile the SHOP block gains a 'what the business offers' line; without, it is exactly as before."""
    client = SimpleNamespace(
        business_name="Ravi Sarees", accepts_cod=True, accepts_upi=False, upi_id=None,
        delivery_days_min=3, delivery_days_max=7,
    )
    conv = SimpleNamespace(last_customer_language="english")
    plain = intent_router._shop_block(client, conv, None)
    with_profile = intent_router._shop_block(client, conv, None, _profile())
    assert "what the business offers" not in plain and "\n" not in plain
    assert with_profile.startswith(plain)
    assert "what the business offers: sells: clothing" in with_profile and "Green Cotton Saree" in with_profile
    assert "₹" not in with_profile.split("what the business offers")[1]


# ── templates ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", LANGS)
def test_out_of_scope_templates_exist_format_and_carry_no_customer_text(lang):
    """All three keys exist in each hand-written language; business/URL fill in; nothing echoes the customer."""
    for key in OOS_KEYS:
        assert TEMPLATES[lang].get(key), (lang, key)
    full = get_template(lang, "rt_out_of_scope", business="Ravi Sarees", catalogue_url="https://shop.example/ravi")
    assert "Ravi Sarees" in full and "https://shop.example/ravi" in full and "{" not in full
    mid = get_template(lang, "rt_out_of_scope_midorder", business="Ravi Sarees")
    assert "Ravi Sarees" in mid and "{" not in mid
    repeat = get_template(lang, "rt_out_of_scope_repeat", business="Ravi Sarees")
    assert "Ravi Sarees" in repeat and "{" not in repeat


def test_repeat_template_offers_the_phrase_that_triggers_a_human():
    """The hand-off wording matches the phrase the human-request lexicon / rt_handoff_offer already teaches."""
    assert "talk to someone" in get_template("english", "rt_out_of_scope_repeat", business="X")
    assert "talk to someone" in get_template("hindi_roman", "rt_out_of_scope_repeat", business="X")
    assert "किसी से बात करनी है" in get_template("hindi_devanagari", "rt_out_of_scope_repeat", business="X")
    assert "કોઈ સાથે વાત કરવી છે" in get_template("gujarati_script", "rt_out_of_scope_repeat", business="X")


# ── handler ──────────────────────────────────────────────────────────────────

class _RC:
    """Minimal RouterCtx stand-in recording the reply the handler sends."""

    def __init__(self, *, count=0, threshold=None, next_slot=None, stage="greeting", text="Ahmedabad ma su vatavaran chr"):
        """Conversation with `count` off-topic strikes already."""
        self.db = object()
        self.conv = SimpleNamespace(id=7, off_topic_count=count, last_customer_language="gujarati_roman", pending_choice_skus=None)
        self.client = SimpleNamespace(business_name="Ravi Sarees", catalogue_slug="ravi", off_topic_threshold=threshold)
        self.user_text, self.next_slot, self.stage = text, next_slot, stage
        self.replies: list[tuple[str, str]] = []

    async def reply(self, text, route, lang, **_kw):
        """Record and return the text."""
        self.replies.append((route, text))
        return text


@pytest.fixture
def oos_env(monkeypatch):
    """Capture counter writes; neutralise menu clearing and the settings the catalogue URL needs."""
    writes = []

    async def fake_update(db, conv_id, field, value):
        """Record the counter write."""
        writes.append((field, value))

    monkeypatch.setattr(router_actions.conversation_service, "update_order_field", fake_update)
    monkeypatch.setattr(router_actions, "_clear_menu", AsyncMock(return_value=False))
    monkeypatch.setattr(router_actions, "get_settings", lambda: SimpleNamespace(public_shop_base_url="https://shop.example"))
    return writes


def _decision():
    """A confident out_of_scope decision in Gujarati."""
    return RouterDecision(action=RouterAction.OUT_OF_SCOPE, language="gu", confidence=0.95)


async def test_handler_replies_with_the_fixed_template_in_the_customers_language_and_counts(oos_env):
    """First strike: fixed Roman-Gujarati template with the shop name + catalogue link; counter 0 → 1."""
    rc = _RC(count=0)
    text = await router_actions.handle_out_of_scope(rc, _decision())
    assert rc.replies[0][0] == "out_of_scope"
    assert text.startswith("Hu fakt Ravi Sarees ma madad") and "https://shop.example/ravi" in text
    assert "vatavaran" not in text.lower() and "talk to someone" not in text
    assert rc.conv.off_topic_count == 1 and oos_env == [("off_topic_count", 1)]


async def test_third_strike_sends_the_final_notice_and_mutes_the_conversation(oos_env):
    """Strikes 1–2 = plain boundary; the 3rd (default limit) = the ONE final notice + a 15-minute mute (no more LLM)."""
    second = _RC(count=1)
    await router_actions.handle_out_of_scope(second, _decision())
    assert "jawab nahi aapu" not in second.replies[0][1] and second.conv.off_topic_count == 2
    assert getattr(second.conv, "spam_muted_until", None) is None
    third = _RC(count=2)
    await router_actions.handle_out_of_scope(third, _decision())
    text = third.replies[0][1]
    assert "jawab nahi aapu" in text and "15 minute" in text and "talk to someone" in text and "https://shop.example/ravi" in text
    assert third.conv.off_topic_count == 3 and third.conv.spam_muted_until is not None
    assert [f for f, _ in oos_env] == ["off_topic_count", "off_topic_count", "spam_muted_until"]


async def test_handler_honours_the_per_client_threshold(oos_env):
    """Client.off_topic_threshold=2 gives the final notice + mute on the 2nd strike."""
    rc = _RC(count=1, threshold=2)
    await router_actions.handle_out_of_scope(rc, _decision())
    assert "jawab nahi aapu" in rc.replies[0][1] and rc.conv.spam_muted_until is not None


async def test_during_an_order_the_limit_offers_a_human_but_never_mutes(oos_env):
    """Mid-order noise: at the limit the reply offers 'talk to someone', and NO mute is started (an answer must still work)."""
    rc = _RC(count=2, stage="order_collection", next_slot=None)
    await router_actions.handle_out_of_scope(rc, _decision())
    assert "talk to someone" in rc.replies[0][1] and "jawab nahi aapu" not in rc.replies[0][1]
    assert getattr(rc.conv, "spam_muted_until", None) is None
    assert ("spam_muted_until", None) not in oos_env and all(f != "spam_muted_until" for f, _ in oos_env)


async def test_handler_mid_order_prefixes_and_reasks_the_open_question(oos_env, monkeypatch):
    """At an open slot the boundary line is a prefix and the SAME question is re-asked; nothing else is written."""
    seen = {}

    async def fake_reask(rc, lang, prefix_key, route, *, prefix_text=None):
        """Capture what _reask was asked to prefix."""
        seen.update(lang=lang, route=route, prefix=prefix_text)
        return "reasked"

    monkeypatch.setattr(router_actions, "_reask", fake_reask)
    rc = _RC(next_slot="color", stage="order_collection")
    assert await router_actions.handle_out_of_scope(rc, _decision()) == "reasked"
    assert seen["route"] == "out_of_scope" and seen["prefix"].startswith("Hu fakt tamara order ane Ravi Sarees")
    assert oos_env == [("off_topic_count", 1)]


async def test_reset_off_topic_writes_only_when_a_streak_exists(oos_env):
    """On-topic message: clear the counter (one write); with nothing to clear, no DB write at all."""
    streak = SimpleNamespace(id=7, off_topic_count=3)
    await router_actions._reset_off_topic(object(), streak)
    assert streak.off_topic_count == 0 and oos_env == [("off_topic_count", 0)]
    oos_env.clear()
    await router_actions._reset_off_topic(object(), SimpleNamespace(id=7, off_topic_count=0))
    assert oos_env == []


async def test_reset_off_topic_leaves_a_running_mute_alone(oos_env):
    """An exact SKU / menu number must not lift a mute (or its counter): it ends by time or when staff resume."""
    from datetime import datetime, timedelta, timezone

    muted = SimpleNamespace(id=7, off_topic_count=3, spam_muted_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    await router_actions._reset_off_topic(object(), muted)
    assert muted.off_topic_count == 3 and oos_env == []


def test_out_of_scope_is_registered_in_the_handler_table():
    """Every RouterAction has a handler — a missing one would crash the turn into the keyword fallback."""
    assert set(router_actions._HANDLERS) == set(RouterAction)


# ── keyword path: is_off_topic_message ───────────────────────────────────────

def _resp(text):
    """Fake chat response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1),
    )


async def test_off_topic_check_skips_the_llm_when_the_message_uses_the_shops_words(monkeypatch):
    """A message sharing a word with the shop profile is on-topic — and costs no LLM call."""
    llm = AsyncMock(return_value=_resp("YES"))
    monkeypatch.setattr(llm_client, "chat", llm)
    assert await conversation_flow.is_off_topic_message("silk saree kaise dhoye", "greeting", shop_profile=_profile()) is False
    llm.assert_not_called()


async def test_off_topic_check_sends_the_shop_context_to_the_llm(monkeypatch):
    """A message with no shop word goes to the classifier WITH what the business offers; YES → off-topic."""
    llm = AsyncMock(return_value=_resp("YES"))
    monkeypatch.setattr(llm_client, "chat", llm)
    assert await conversation_flow.is_off_topic_message(
        "Ahmedabad ma su vatavaran chr", "greeting", shop_profile=_profile(),
    ) is True
    prompt = llm.call_args.args[1][0]["content"]
    assert "What the business offers: sells: clothing" in prompt and "Green Cotton Saree" in prompt
    assert "Hindi, Gujarati or Marathi written in English letters" in prompt


async def test_off_topic_check_without_a_profile_behaves_as_before(monkeypatch):
    """No profile → generic store wording, still fail-open (error → False + counted)."""
    llm = AsyncMock(return_value=_resp("NO"))
    monkeypatch.setattr(llm_client, "chat", llm)
    assert await conversation_flow.is_off_topic_message("what is flutter framework", "greeting") is False
    assert "The business is an online store." in llm.call_args.args[1][0]["content"]
    llm_health.reset_state()

    async def boom(*_a, **_k):
        """Provider error."""
        raise RuntimeError("429")

    monkeypatch.setattr(llm_client, "chat", boom)
    assert await conversation_flow.is_off_topic_message("what is flutter framework", "greeting", shop_profile=_profile()) is False
    assert llm_health.failures_last_hour() == 1
    llm_health.reset_state()


async def test_off_topic_check_short_messages_never_off_topic(monkeypatch):
    """'ok', 'hi' and similar are never judged (no LLM, no profile needed)."""
    llm = AsyncMock(return_value=_resp("YES"))
    monkeypatch.setattr(llm_client, "chat", llm)
    assert await conversation_flow.is_off_topic_message("ok", "greeting", shop_profile=_profile()) is False
    llm.assert_not_called()


# ── search miss: not sold / out of stock ─────────────────────────────────────

def _item(sku, name, category, *, stock=5):
    """Fake simple (variant-less) catalogue product."""
    return SimpleNamespace(
        id=hash(sku) % 10_000, sku=sku, name=name, price=500.0, category=category, stock=stock, is_active=True,
        has_variants=False, variants=[], description=None, delivery_days=None,
    )


KURTI_SHOP = [
    _item("KU1", "A-Line Chanderi Kurti", "Kurti"), _item("KU2", "A-Line Cotton Kurti", "Kurti"),
    _item("SR1", "Red Silk Saree", "Saree", stock=0), _item("SR2", "Green Cotton Saree", "Saree"),
]


def test_closest_alternatives_is_empty_when_nothing_in_the_catalogue_resembles_the_query():
    """'shoes' at a kurti shop: no suggestions at all (it used to fall back to every in-stock product)."""
    from app.schemas.router import RouterFilters

    assert router_actions.closest_alternatives(KURTI_SHOP, "shoes", RouterFilters()) == []


def test_closest_alternatives_still_works_for_filter_misses_and_filter_only_requests():
    """A real product type with an impossible filter, or no query at all (filters only), still gets nearby options."""
    from app.schemas.router import RouterFilters

    by_type = router_actions.closest_alternatives(KURTI_SHOP, "kurti", RouterFilters(max_price=10))
    assert {p.sku for p in by_type} == {"KU1", "KU2"}
    assert router_actions.closest_alternatives(KURTI_SHOP, "", RouterFilters(max_price=10))


def test_out_of_stock_matches_only_when_the_sold_out_product_is_the_better_match():
    """'red silk saree' names the sold-out saree → reported; 'saree' alone ties with the in-stock one → not reported."""
    assert [p.sku for p in router_actions.out_of_stock_matches(KURTI_SHOP, "red silk saree")] == ["SR1"]
    assert router_actions.out_of_stock_matches(KURTI_SHOP, "saree") == []
    assert router_actions.out_of_stock_matches(KURTI_SHOP, "shoes") == []
    assert router_actions.out_of_stock_matches(KURTI_SHOP, "") == []
    only_dead = [_item("SR1", "Red Silk Saree", "Saree", stock=0)]
    assert [p.sku for p in router_actions.out_of_stock_matches(only_dead, "saree")] == ["SR1"]


@pytest.mark.parametrize("lang", LANGS)
def test_search_miss_templates_exist_in_every_language_and_never_take_customer_text(lang):
    """rt_search_oos names a catalogue product; rt_not_stocked is generic (no placeholders at all)."""
    oos = get_template(lang, "rt_search_oos", product="Red Silk Saree")
    assert "Red Silk Saree" in oos and "{" not in oos
    generic = TEMPLATES[lang]["rt_not_stocked"]
    assert generic and "{" not in generic
