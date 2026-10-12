"""
End-to-end language mirroring: the customer is answered in the language AND script they wrote in.

    શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?   →   Gujarati-script reply, never English or Hinglish.

Real webhook → pipeline → Postgres. The LLM is faked; the Marathi test also fakes the translator that
reply_localizer uses for languages without hand-written templates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.services import language_service as ls
from app.services import reply_localizer as rl
from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product
from tests.replay.helpers import TINY_PNG, capture_all, send_audio, send_message

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

CATALOGUE_WORDS = frozenset({"navratri", "chaniya", "choli", "replay", "test", "store", "cc40001"})
USER_EXAMPLE = "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?"


@pytest.fixture(autouse=True)
def _no_reply_delay(monkeypatch):
    """Zero the human-like reply delay."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)


async def _shop(session, suffix: str):
    """A shop selling a 'Navratri Chaniya Choli' (category Chaniya Choli)."""
    client, product = await seed_client_and_product(
        session, phone=f"9196000{suffix}", wa_phone_number_id=f"6660{suffix}", product_sku="CC40001",
        product_name="Navratri Chaniya Choli", price=1800.0, stock=10, payment_method="COD",
    )
    product.category = "Chaniya Choli"
    await session.commit()
    return client, f"9196000{suffix}", f"6660{suffix}"


async def _language(session, phone: str) -> str:
    from app.models.conversation import Conversation

    row = await session.execute(select(Conversation).where(Conversation.phone_number == phone).execution_options(populate_existing=True))
    return row.scalar_one().last_customer_language


async def test_pure_gujarati_question_gets_pure_gujarati_answer(replay_http, replay_session, monkeypatch):
    """The user's own example: a Gujarati-script question about chaniya choli → Gujarati-script replies only."""
    _client, phone, pnid = await _shop(replay_session, "01")
    sent = capture_all(monkeypatch)

    resp = await send_message(replay_http, phone, USER_EXAMPLE, phone_number_id=pnid, wamid="wamid.lang.gu.1")
    assert resp.status_code == 200
    assert sent, "the bot must answer"
    for text in sent:
        assert ls.reply_matches_language(text, "gujarati_script", CATALOGUE_WORDS), text
        assert not rl.stray_latin_words(text, CATALOGUE_WORDS), text
    assert "Navratri Chaniya Choli" in " ".join(sent)          # the catalogue product was found from a Gujarati query
    assert await _language(replay_session, phone) == "gujarati_script"

    sent.clear()
    await send_message(replay_http, phone, "હા", phone_number_id=pnid, wamid="wamid.lang.gu.2")       # a bare "yes"
    assert sent and all(ls.reply_matches_language(t, "gujarati_script", CATALOGUE_WORDS) for t in sent), sent


@pytest.mark.parametrize("suffix,message,lang", [
    ("02", "क्या आपके पास चनिया चोली है?", "hindi_devanagari"),
    ("03", "chaniya choli che?", "gujarati_roman"),
    ("04", "chaniya choli hai kya?", "hindi_roman"),
    ("05", "do you have chaniya choli?", "english"),
])
async def test_each_supported_language_is_detected_and_answered_in_kind(replay_http, replay_session, monkeypatch, suffix, message, lang):
    """Language is synced from the very first message, so even the first (template) reply uses it."""
    _client, phone, pnid = await _shop(replay_session, suffix)
    sent = capture_all(monkeypatch)
    resp = await send_message(replay_http, phone, message, phone_number_id=pnid, wamid=f"wamid.lang.{suffix}")
    assert resp.status_code == 200 and sent
    assert await _language(replay_session, phone) == lang
    for text in sent:
        assert ls.reply_matches_language(text, lang, CATALOGUE_WORDS), (lang, text)
        assert not ls.roman_language_mismatch(text, lang), (lang, text)


async def test_customer_switching_language_is_followed(replay_http, replay_session, monkeypatch):
    """English first, Gujarati script next: the next reply is Gujarati; a bare 'ok' keeps whatever was established."""
    _client, phone, pnid = await _shop(replay_session, "06")
    sent = capture_all(monkeypatch)
    await send_message(replay_http, phone, "do you have chaniya choli?", phone_number_id=pnid, wamid="wamid.sw.1")
    assert await _language(replay_session, phone) == "english"
    sent.clear()
    await send_message(replay_http, phone, USER_EXAMPLE, phone_number_id=pnid, wamid="wamid.sw.2")
    assert await _language(replay_session, phone) == "gujarati_script"
    assert sent and all(ls.reply_matches_language(t, "gujarati_script", CATALOGUE_WORDS) for t in sent), sent
    await send_message(replay_http, phone, "ok", phone_number_id=pnid, wamid="wamid.sw.3")
    assert await _language(replay_session, phone) == "gujarati_script"


async def test_marathi_customer_is_answered_through_the_localizer(replay_http, replay_session, monkeypatch):
    """Marathi has no template set: the engine's English reply is translated (facts kept), never sent in English."""
    _client, phone, pnid = await _shop(replay_session, "07")
    sent = capture_all(monkeypatch)
    calls = []

    async def fake_llm_call(purpose, model, messages, *a, **kw):
        """Translator stand-in: Marathi text that keeps the digits/ids of the source."""
        calls.append(purpose)
        source = messages[-1]["content"]
        facts = " ".join(t for t in source.split() if any(ch.isdigit() for ch in t))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=f"आमच्याकडे आहे {facts}".strip()))])

    monkeypatch.setattr("app.services.llm_client.llm_call", fake_llm_call)
    resp = await send_message(replay_http, phone, "तुमच्याकडे चनिया चोली आहे का?", phone_number_id=pnid, wamid="wamid.mr.1")
    assert resp.status_code == 200 and sent
    assert await _language(replay_session, phone) == "marathi"
    assert "translate" in calls
    assert all(ls.reply_matches_language(t, "marathi", CATALOGUE_WORDS) for t in sent), sent


async def test_gujarati_voice_note_is_understood_and_answered_in_gujarati(replay_http, replay_session, monkeypatch):
    """
    A voice note is transcribed BEFORE the pipeline: it finds the product, sets the language, and the reply is
    Gujarati script (it used to reach the pipeline as "[voice note]" and be answered in English). The reply starts
    with what was heard, in italics.
    """
    _client, phone, pnid = await _shop(replay_session, "08")
    sent = capture_all(monkeypatch)

    async def fake_download(media_id, *a, **kw):
        """No network: any bytes will do."""
        return TINY_PNG

    async def fake_transcribe(audio_bytes, filename="audio.ogg", *a, **kw):
        """The Whisper stand-in hears the user's example sentence."""
        return USER_EXAMPLE

    monkeypatch.setattr("app.services.vision_service.download_whatsapp_media", fake_download)
    monkeypatch.setattr("app.services.voice_service.transcribe_voice_note", fake_transcribe)

    resp = await send_audio(replay_http, phone, phone_number_id=pnid, wamid="wamid.voice.gu.1")
    assert resp.status_code == 200
    reply = [t for t in sent if "Navratri" in t]
    assert reply, sent
    assert reply[0].startswith(f"_{USER_EXAMPLE}_")
    body = reply[0].split("\n\n", 1)[1]
    assert ls.reply_matches_language(body, "gujarati_script", CATALOGUE_WORDS)
    assert await _language(replay_session, phone) == "gujarati_script"

    from app.models.message import Message

    rows = (await replay_session.execute(
        select(Message).where(Message.role == "user").execution_options(populate_existing=True)
    )).scalars().all()
    assert rows and all(r.original_type == "audio" for r in rows)
