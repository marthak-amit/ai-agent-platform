"""
Live check (real Groq key required): does the platform answer in the customer's language and script?

For each of the 15 supported languages it
  1. runs the language detector on a sample customer sentence,
  2. runs a typical English engine reply (product card with a price, SKU and colours) through the reply localizer
     and verifies the result is in the right script and kept every price / SKU / number,
  3. asks the real reply model (generate_reply) the customer's sentence with the language rule and verifies the script.

Run from backend/:
    GROQ_API_KEY=... python scripts/check_language_mirroring.py [--lang gujarati_script,tamil]

Exit code: 0 all good · 1 at least one language failed · 2 no API key. Costs a few paise per language.
The offline equivalent (fake LLM, thousands of conversations) is `python -m tests.sim.run_sim`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.services import gemini_service, language_service, llm_usage_service, reply_localizer  # noqa: E402

# This is a diagnostic: don't require (or write to) the application database for llm_usage rows.
async def _no_usage_row(*args, **kwargs) -> None:
    """Skip persisting llm_usage rows."""


llm_usage_service._persist_row = _no_usage_row

SAMPLES = {
    "english": "do you have chaniya choli?",
    "hindi_roman": "chaniya choli hai kya?",
    "hindi_devanagari": "क्या आपके पास चनिया चोली है?",
    "gujarati_roman": "chaniya choli che?",
    "gujarati_script": "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?",
    "marathi": "तुमच्याकडे चनिया चोली आहे का?",
    "marathi_roman": "tumchyakade chaniya choli ahe ka?",
    "bengali": "আপনার কাছে ঘাগরা চোলি আছে?",
    "tamil": "உங்களிடம் சனியா சோலி இருக்கிறதா?",
    "telugu": "మీ దగ్గర చనియా చోళీ ఉందా?",
    "kannada": "ನಿಮ್ಮ ಬಳಿ ಚನಿಯಾ ಚೋಲಿ ಇದೆಯೇ?",
    "malayalam": "നിങ്ങളുടെ പക്കൽ ചണിയ ചോളി ഉണ്ടോ?",
    "punjabi": "ਕੀ ਤੁਹਾਡੇ ਕੋਲ ਚਨੀਆ ਚੋਲੀ ਹੈ?",
    "odia": "ଆପଣଙ୍କ ପାଖରେ ଚନିଆ ଚୋଲି ଅଛି କି?",
    "urdu": "کیا آپ کے پاس گھاگھرا چولی ہے؟",
}
ENGINE_REPLY = (
    "Navratri Chaniya Choli [CC40001] — ₹1,800\nAvailable colors: Red, Green\n\n"
    "Delivery in 3–7 business days.\nWould you like to order? (Yes / No)"
)
CATALOGUE = "• Navratri Chaniya Choli [CC40001] — ₹1800 [Chaniya Choli]\n  Colors available: Red, Green"
ALLOWED = reply_localizer.allowed_words_from("Navratri Chaniya Choli", "CC40001", "Chaniya Choli", "Red Green")


async def check(lang: str, sentence: str) -> list[str]:
    """Return the list of problems found for one language (empty = pass)."""
    problems: list[str] = []
    detected = language_service.detect_language(sentence)
    if detected != lang:
        problems.append(f"detector said {detected!r}")

    localized = await reply_localizer.localize(ENGINE_REPLY, lang, allowed=ALLOWED)
    print(f"  localized : {localized.replace(chr(10), ' ⏎ ')[:200]}")
    if not language_service.reply_matches_language(localized, lang, ALLOWED):
        problems.append("localized reply is not in the customer's script")
    for fact in ("₹1,800", "CC40001", "3–7"):
        if fact not in localized:
            problems.append(f"localized reply lost {fact!r}")
    if language_service.expected_script(lang) and reply_localizer.stray_latin_words(localized, ALLOWED):
        problems.append(f"English words left: {reply_localizer.stray_latin_words(localized, ALLOWED)}")
    if language_service.roman_language_mismatch(localized, lang):
        problems.append("localized reply is still in the wrong Latin-script language")

    reply = await gemini_service.generate_reply(sentence, history=[], catalogue_context=CATALOGUE, language=lang)
    print(f"  LLM reply : {reply.replace(chr(10), ' ⏎ ')[:200]}")
    if not language_service.reply_matches_language(reply, lang, ALLOWED):
        problems.append("LLM reply is not in the customer's script")
    return problems


async def main(only: list[str]) -> int:
    """Run the checks, print a table, return the exit code."""
    if not get_settings().groq_api_key or get_settings().groq_api_key == "dummy":
        print("GROQ_API_KEY is not set.")
        return 2
    failed = 0
    for lang, sentence in SAMPLES.items():
        if only and lang not in only:
            continue
        print(f"\n[{lang}] {sentence}")
        try:
            problems = await check(lang, sentence)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next language
            problems = [f"exception: {exc}"]
        print("  ✅ ok" if not problems else "  ❌ " + "; ".join(problems))
        failed += bool(problems)
    print(f"\n{failed} language(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="", help="comma-separated language codes (default: all)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main([x for x in args.lang.split(",") if x])))
