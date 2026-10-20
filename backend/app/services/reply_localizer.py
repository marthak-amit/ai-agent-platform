"""
Last-mile language guarantee: every reply leaves the platform in the customer's own language.

The deterministic templates (language_templates.py) are hand-written for English, Hindi (Roman +
Devanagari) and Gujarati (Roman + script). Everything else — Marathi, Bengali, Tamil, Telugu, Kannada,
Malayalam, Punjabi, Odia, Urdu — and any stray English text produced by a code path that has no
translation for the customer's language is translated here, once, at the single point every inbound
turn passes through (`order_pipeline.handle_inbound_message`).

Safety rules (the engine owns the facts, the translator only changes the words):
  * a reply already in the right script is returned untouched (no LLM call, ₹0);
  * prices, numbers, SKUs, order ids, URLs, UPI ids and brand words are extracted before translating
    and must all survive in the output — otherwise the original text is kept;
  * the translation must itself be in the required script;
  * any failure (LLM down, breaker open, rate limit, bad output) falls back to the original text.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict

from app.config import get_settings
from app.services import language_service as _lang
from app.services import llm_client, llm_health

logger = logging.getLogger(__name__)

# Tokens that must appear verbatim in the translation: digits (prices, quantities, ids, phone numbers),
# order/SKU codes, URLs, e-mail / UPI ids.
_FACT_RE = re.compile(r"https?://\S+|\S+@\S+|[A-Za-z]{0,6}[-_]?\d[\w\-/.,]*")

_CACHE_MAX = 2000
_cache: "OrderedDict[tuple[str, str], str]" = OrderedDict()

_SYSTEM_ROMAN = (
    "You rewrite WhatsApp shop messages in {name}, typed with ordinary English (Latin) letters — the way customers "
    "and shopkeepers chat.\n"
    "Rules:\n"
    "- Output ONLY the rewritten message — no quotes, notes or explanations.\n"
    "- Keep common English shop words (order, payment, delivery, size, colour) where people really use them.\n"
    "- Copy EXACTLY: numbers, ₹ amounts, SKU/order codes, URLs, UPI ids, emoji, *bold* markers, line breaks, "
    "product names, and the words UPI, COD, GPay, PhonePe, Paytm, WhatsApp, QR.\n"
    "- Do not add, remove or change any fact, amount or date. Same short, polite tone."
)

_SYSTEM = (
    "You translate WhatsApp shop messages into {name}, written in {script} script.\n"
    "Rules:\n"
    "- Output ONLY the translation — no quotes, notes or explanations.\n"
    "- Pure {name}: no English or Hinglish words, except the things listed next.\n"
    "- Copy EXACTLY, character for character: numbers, ₹ amounts, SKU/order codes, URLs, UPI ids, "
    "emoji, *bold* markers, line breaks, and the words UPI, COD, GPay, PhonePe, Paytm, WhatsApp, QR.\n"
    "- Do not add, remove or change any fact, amount, date or product name; product names stay as written.\n"
    "- Keep the same tone: short, polite, shop-assistant style."
)


def _mostly_non_latin(text: str) -> bool:
    """True when most letters of `text` are not Latin — a whole Gujarati/Hindi/... reply sent to an English customer."""
    counts = _lang.script_counts(_URLISH.sub(" ", text or ""))
    total = sum(counts.values())
    return total > 0 and counts.get("latin", 0) / total < 0.4


def _facts(text: str) -> list[str]:
    """Verbatim tokens (digits, ids, URLs) the translation has to keep."""
    return [m.group(0).rstrip(".,") for m in _FACT_RE.finditer(text or "")]


def _facts_preserved(source: str, translated: str) -> bool:
    """True when every fact token of `source` is present in `translated`."""
    return all(fact in translated for fact in _facts(source))


def _cache_get(lang: str, text: str) -> str | None:
    """LRU read."""
    hit = _cache.get((lang, text))
    if hit is not None:
        _cache.move_to_end((lang, text))
    return hit


def _cache_put(lang: str, text: str, value: str) -> None:
    """LRU write."""
    _cache[(lang, text)] = value
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# Latin words that may legitimately stay inside a native-script reply (payment brands, ids, units).
_ALWAYS_ALLOWED = frozenset({
    "upi", "cod", "gpay", "phonepe", "paytm", "whatsapp", "qr", "sku", "ord", "id", "pdf", "http", "https", "www", "com",
    "xxl", "xl", "xs", "ok", "pin", "pincode", "bhim", "pay", "net", "in",
})
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_URLISH = re.compile(r"https?://\S+|\S+@\S+|\b[A-Za-z]{2,6}[-_]?\d+\b")


def allowed_words_from(*texts: str | None) -> frozenset[str]:
    """Lower-cased Latin words found in the given texts (catalogue names, the customer's own name/address...)."""
    out: set[str] = set()
    for text in texts:
        out.update(w.lower() for w in _LATIN_WORD.findall(text or ""))
    return frozenset(out)


def stray_latin_words(text: str, allowed: frozenset[str] = frozenset()) -> list[str]:
    """
    English words sitting inside a native-script reply ("સાઈઝ" expected, "size" written).

    Ignored: URLs / ids / SKUs, payment brands, and anything in `allowed` (product names the seller wrote in
    English, the customer's own Latin-script name/address, the shop name).
    """
    cleaned = _URLISH.sub(" ", text or "")
    return [w for w in _LATIN_WORD.findall(cleaned) if w.lower() not in _ALWAYS_ALLOWED and w.lower() not in allowed]


def needs_translation(text: str | None, lang: str | None, allowed: frozenset[str] = frozenset()) -> bool:
    """
    True when `text` is not (purely) in the language the customer wrote in.

    Wrong script → yes. Native-script languages are held to "pure": English words that are neither a brand,
    an id nor a name from the catalogue / the customer's own message make the reply impure → translate.
    """
    if not text or not text.strip():
        return False
    if not _lang.reply_matches_language(text, lang, allowed):
        return True
    if _lang.expected_script(lang) is not None:
        return bool(stray_latin_words(text, allowed))
    return _lang.roman_language_mismatch(text, lang)


async def localize(
    text: str | None, lang: str | None, *, client_id=None, conversation_id=None, allowed: frozenset[str] = frozenset(),
) -> str | None:
    """
    Return `text` in `lang`; translate via the LLM only when it is not already in the right script.

    Never raises and never returns something unsafe: on any problem the original text comes back.
    """
    if text is None or not needs_translation(text, lang, allowed):
        return text
    code = _lang.normalize_language(lang)
    if code == "english" and not _lang.roman_language_mismatch(text, code) and not _mostly_non_latin(text):
        # A few Indic letters in an English reply (an Indic product name) — keep as is.
        return text
    cached = _cache_get(code, text)
    if cached is not None:
        return cached
    name, script, _ = _lang.LANGUAGES[code]
    roman = _lang.expected_script(code) is None
    system = (_SYSTEM_ROMAN if roman else _SYSTEM).format(name=name, script=script)
    settings = get_settings()
    try:
        response = await llm_client.llm_call(
            "translate", settings.llm_model_reply,
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            client_id, conversation_id, max_tokens=max(200, min(900, len(text) * 3)), temperature=0.1,
        )
        translated = llm_client.final_text(response).strip().strip('"').strip()
    except llm_health.LLMUnavailableError:
        return text
    except Exception as exc:  # noqa: BLE001 - a reply in the wrong language beats no reply
        logger.warning("reply localizer: translation to %s failed: %s", code, str(exc)[:160])
        return text
    if (
        not translated or not _facts_preserved(text, translated) or not _lang.reply_matches_language(translated, code, allowed)
        # never accept a translation that is MORE English than the original
        or (_lang.expected_script(code) is not None
            and len(stray_latin_words(translated, allowed)) > len(stray_latin_words(text, allowed)))
    ):
        logger.warning("reply localizer: rejected %s translation (facts/script check failed)", code)
        return text
    # Keep WhatsApp bold markers balanced: if the source had any and the output lost them, that is fine; if the
    # output has an odd number it would render as stray asterisks.
    if translated.count("*") % 2:
        translated = translated.replace("*", "")
    _cache_put(code, text, translated)
    return translated


# Buttons whose tap is mapped from the button *id* (webhook.py), so the visible label can be translated
# freely. Every other button/list row is a product, colour or size whose TITLE is matched back against
# the catalogue when tapped — those labels must stay exactly as the catalogue spells them.
_STATIC_BUTTON_IDS = frozenset({
    "confirm_pay", "cancel_order", "confirm_no", "paid_done", "offer_yes", "offer_no", "addr_change", "upi", "cod", "bank_transfer",
    "cancel_yes", "cancel_no", "switch_product", "add_both", "keep_current", "add_crosssell",
})


async def localize_result(
    result, lang: str | None, *, client_id=None, conversation_id=None, allowed: frozenset[str] = frozenset(),
):
    """
    Localize the customer-visible strings of a PipelineResult in place and return it.

    Covers the body text, pre-texts, list header/button, image captions, the image fallback text and
    the labels of the fixed action buttons (Confirm / Cancel / I've paid / Yes / No). Product, colour
    and size buttons and list rows keep their catalogue spelling (see _STATIC_BUTTON_IDS).
    """
    if result is None or getattr(result, "skip_send", False):
        return result
    code = _lang.normalize_language(lang)

    async def _tr(value):
        """Translate one string (None/empty untouched)."""
        return (
            await localize(value, code, client_id=client_id, conversation_id=conversation_id, allowed=allowed)
            if value else value
        )

    result.text = await _tr(result.text)
    result.image_fallback_text = await _tr(result.image_fallback_text)
    result.list_header = await _tr(result.list_header)
    result.list_button_text = await _tr(result.list_button_text)
    if result.pre_texts:
        result.pre_texts = [await _tr(t) for t in result.pre_texts]
    if result.images:
        result.images = [(url, await _tr(caption)) for url, caption in result.images]
    if result.pre_images:
        result.pre_images = [(url, await _tr(caption)) for url, caption in result.pre_images]
    for button in result.buttons or []:
        if button.id in _STATIC_BUTTON_IDS and button.title:
            translated = await _tr(button.title)
            if translated and len(translated) <= 20:   # WhatsApp button titles are capped at 20 characters
                button.title = translated
    return result
