"""Unit tests for the irrelevant-message guard (no DB): classification, counter, mute, the pipeline guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import conversation_service, order_pipeline, spam_guard


def conv(**kw):
    """A conversation in a browsing stage."""
    base = dict(id=7, current_stage="greeting", off_topic_count=0, spam_muted_until=None, last_customer_language="english")
    base.update(kw)
    return SimpleNamespace(**base)


CLIENT = SimpleNamespace(id=1, business_name="Ravi Sarees", catalogue_slug="ravi", off_topic_threshold=None)


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "😂", "😂😂😂", "🤣 🤣", "🤔", "😡😡", "???", "...", "!!!", "—", "🔥🔥🔥", "hahaha", "HAHAHA", "hehe", "lol", "lmao", "xdd", "haha",
])
def test_noise_is_junk(text):
    """Emoji-only, symbol-only and laughter messages are irrelevant."""
    assert spam_guard.classify_text(text) == "junk"


@pytest.mark.parametrize("text", ["👍", "🙏", "❤️", "👍🏻", "🙏🙏🙏", "😊", "👌 👍", "✅"])
def test_thanks_emojis_are_acknowledgements_not_noise(text):
    """👍🙏❤️ and friends are a normal thanks: never counted, never muted."""
    assert spam_guard.classify_text(text) == "ack"


@pytest.mark.parametrize("text", [
    "saree", "pan mare bija choli jova che", "2", "ok", "hi", "kurti 😂", "👍 thanks", "સાડી", "साड़ी", "COD", "KU84017", "haha nice saree",
])
def test_real_text_is_text(text):
    """Anything with a letter or digit (any script) — even with emoji around it — is a real message."""
    assert spam_guard.classify_text(text) == "text"


def test_empty_text_is_not_junk():
    """An empty string is not noise to react to."""
    assert spam_guard.classify_text("") == "text" and spam_guard.classify_text("   ") == "text"


# ── limits / mute helpers ────────────────────────────────────────────────────

def test_streak_limit_defaults_to_the_setting_and_honours_the_client_override():
    """3 by default; Client.off_topic_threshold wins; never below 1."""
    assert spam_guard.streak_limit(CLIENT) == 3
    assert spam_guard.streak_limit(SimpleNamespace(off_topic_threshold=5)) == 5
    assert spam_guard.streak_limit(SimpleNamespace(off_topic_threshold=0)) == 3


def test_is_muted_and_mute_expired():
    """A future timestamp mutes; a past one is 'expired'; naive timestamps are treated as UTC."""
    now = datetime.now(timezone.utc)
    assert spam_guard.is_muted(conv(spam_muted_until=now + timedelta(minutes=1)))
    assert not spam_guard.is_muted(conv(spam_muted_until=now - timedelta(minutes=1)))
    assert spam_guard.mute_expired(conv(spam_muted_until=now - timedelta(seconds=1)))
    assert not spam_guard.mute_expired(conv(spam_muted_until=now + timedelta(minutes=1))) and not spam_guard.mute_expired(conv())
    naive_future = (now + timedelta(minutes=5)).replace(tzinfo=None)
    assert spam_guard.is_muted(conv(spam_muted_until=naive_future))


@pytest.fixture
def writes(monkeypatch):
    """Capture conversation column writes."""
    out: list = []

    async def fake_update(db, conv_id, field, value):
        """Record the write."""
        out.append((field, value))

    monkeypatch.setattr(conversation_service, "update_order_field", fake_update)
    return out


async def test_register_strike_counts_and_mutes_at_the_limit(writes):
    """Strikes 1 and 2 only count; the 3rd (the limit) starts a 15-minute mute."""
    c = conv()
    first = await spam_guard.register_strike(None, c, CLIENT, may_mute=True)
    second = await spam_guard.register_strike(None, c, CLIENT, may_mute=True)
    assert (first.count, first.reached, first.muted) == (1, False, False) and (second.count, second.muted) == (2, False)
    third = await spam_guard.register_strike(None, c, CLIENT, may_mute=True)
    assert (third.count, third.limit, third.reached, third.muted) == (3, 3, True, True)
    assert 14 * 60 < (c.spam_muted_until - datetime.now(timezone.utc)).total_seconds() <= 15 * 60
    assert [f for f, _ in writes] == ["off_topic_count", "off_topic_count", "off_topic_count", "spam_muted_until"]


async def test_register_strike_without_may_mute_never_mutes(writes):
    """During an order the limit is reached (a human is offered) but nothing is muted."""
    c = conv(off_topic_count=2)
    strike = await spam_guard.register_strike(None, c, CLIENT, may_mute=False)
    assert strike.reached and not strike.muted and c.spam_muted_until is None
    assert [f for f, _ in writes] == ["off_topic_count"]


async def test_register_strike_with_mute_minutes_zero_never_mutes(writes, monkeypatch):
    """IRRELEVANT_MUTE_MINUTES=0 disables the mute entirely."""
    monkeypatch.setattr(spam_guard, "get_settings", lambda: SimpleNamespace(irrelevant_streak_limit=3, irrelevant_mute_minutes=0))
    c = conv(off_topic_count=2)
    assert not (await spam_guard.register_strike(None, c, CLIENT, may_mute=True)).muted and c.spam_muted_until is None


async def test_clear_resets_counter_and_mute_and_writes_only_what_is_set(writes):
    """Clearing a clean conversation writes nothing; a dirty one gets both columns reset."""
    await spam_guard.clear(None, conv())
    assert writes == []
    c = conv(off_topic_count=2, spam_muted_until=datetime.now(timezone.utc))
    await spam_guard.clear(None, c)
    assert c.off_topic_count == 0 and c.spam_muted_until is None
    assert writes == [("off_topic_count", 0), ("spam_muted_until", None)]


# ── the pipeline guard ───────────────────────────────────────────────────────

@pytest.fixture
def guard_env(monkeypatch):
    """Capture saved messages; counted usage; neutral route logging."""
    saved: list = []

    async def fake_save(db, conv_id, role, content, **kw):
        """Record the message."""
        saved.append((role, content))

    async def fake_usage(db, client, conv=None):
        """No-op usage counter."""

    async def fake_update(db, conv_id, field, value):
        """No-op conversation write."""

    monkeypatch.setattr(conversation_service, "save_message", fake_save)
    monkeypatch.setattr(conversation_service, "update_order_field", fake_update)
    monkeypatch.setattr(order_pipeline, "_log_route", lambda *a, **k: None)
    monkeypatch.setattr(order_pipeline, "get_settings", lambda: SimpleNamespace(public_shop_base_url="https://shop.example"))
    return SimpleNamespace(saved=saved, usage=fake_usage)


def msg(text, mtype="text"):
    """A WhatsApp-shaped message object."""
    return SimpleNamespace(type=mtype, text=SimpleNamespace(body=text))


async def run(c, text, stage="greeting", mtype="text", env=None):
    """Run the guard for one message."""
    return await order_pipeline.run_spam_guard(None, c, CLIENT, msg(text, mtype), text, "w", stage, env.usage)


async def test_first_and_second_emoji_get_a_fixed_nudge_with_no_llm(guard_env):
    """😂 → fixed nudge (strike 1, 2); nothing else is called, the counter advances."""
    c = conv()
    r1 = await run(c, "😂😂😂", env=guard_env)
    r2 = await run(c, "😂", env=guard_env)
    for r in (r1, r2):
        assert r is not None and "I didn't understand that" in r.text and "https://shop.example/ravi" in r.text
    assert c.off_topic_count == 2 and c.spam_muted_until is None
    assert [role for role, _ in guard_env.saved] == ["user", "assistant", "user", "assistant"]


async def test_third_emoji_sends_the_final_notice_and_mutes(guard_env):
    """The 3rd junk message: ONE final notice, then muted."""
    c = conv(off_topic_count=2)
    r = await run(c, "🤣🤣", env=guard_env)
    assert "pause replies for about 15 minutes" in r.text and "talk to someone" in r.text and spam_guard.is_muted(c)


async def test_a_muted_conversation_ignores_free_text_silently(guard_env):
    """While muted: a question is saved and NOT answered (skip_send), a thanks emoji too."""
    c = conv(off_topic_count=3, spam_muted_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    for text in ("do you have sarees?", "who is modi", "👍", "😂"):
        r = await run(c, text, env=guard_env)
        assert r is not None and r.skip_send and r.text is None
    assert [role for role, _ in guard_env.saved] == ["user"] * 4


async def test_a_muted_conversation_still_lets_cheap_and_human_requests_through(guard_env):
    """SKU, bare number, yes/no, COD, catalogue paste and 'talk to someone' go on to the normal pipeline (₹0 / handoff)."""
    c = conv(off_topic_count=3, spam_muted_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    for text in ("KU84017", "2", "yes", "COD", "talk to someone", "Hi, I want to order\nSKU: KU84017"):
        assert await run(c, text, env=guard_env) is None, text


async def test_buttons_images_and_voice_always_pass_even_when_muted(guard_env):
    """Only typed text is guarded."""
    c = conv(off_topic_count=3, spam_muted_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    for mtype in ("interactive", "image", "audio"):
        assert await run(c, "[x]", mtype=mtype, env=guard_env) is None


async def test_an_expired_mute_is_lifted_and_the_streak_restarts(guard_env):
    """After the cooldown the next message is processed normally and the counter starts from zero."""
    c = conv(off_topic_count=3, spam_muted_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await run(c, "do you have sarees?", env=guard_env) is None
    assert c.spam_muted_until is None and c.off_topic_count == 0


async def test_thanks_emojis_and_real_text_are_not_guarded(guard_env):
    """👍 / text / numbers fall through to the normal pipeline and count nothing."""
    c = conv()
    for text in ("👍", "🙏🙏", "saree dikhao", "2"):
        assert await run(c, text, env=guard_env) is None
    assert c.off_topic_count == 0


@pytest.mark.parametrize("stage", ["order_collection", "awaiting_final_confirmation", "payment"])
async def test_noise_during_an_order_is_ignored_not_answered_not_muted(guard_env, stage):
    """Mid-order 😂: silent skip, no nudge, no strike, no mute — and a muted flag can never block an order answer."""
    c = conv(current_stage=stage)
    r = await run(c, "😂😂😂", stage=stage, env=guard_env)
    assert r is not None and r.skip_send and r.text is None
    assert c.off_topic_count == 0 and c.spam_muted_until is None
    muted = conv(current_stage=stage, spam_muted_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    assert await run(muted, "Ravi Patel", stage=stage, env=guard_env) is None


async def test_final_notice_is_in_the_customers_language(guard_env):
    """Roman Gujarati customers get the Gujarati notice."""
    c = conv(off_topic_count=2, last_customer_language="gujarati_roman")
    r = await run(c, "😂", env=guard_env)
    assert "jawab nahi aapu" in r.text and "15" in r.text


def test_every_spam_template_exists_in_all_five_languages():
    """rt_junk_nudge / rt_spam_final: present, formatted, no leftover placeholders."""
    from app.services.language_templates import TEMPLATES, get_template

    for lang in ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script"):
        assert TEMPLATES[lang].get("rt_junk_nudge") and TEMPLATES[lang].get("rt_spam_final"), lang
        nudge = get_template(lang, "rt_junk_nudge", catalogue_url="https://x/y")
        final = get_template(lang, "rt_spam_final", business="Ravi Sarees", catalogue_url="https://x/y", minutes=15)
        assert "https://x/y" in nudge and "{" not in nudge
        assert "Ravi Sarees" in final and "15" in final and "https://x/y" in final and "{" not in final
