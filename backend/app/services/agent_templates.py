"""
Agent v3 — the pre-translated reply tables.

``app/services/agent_templates/<language>.json`` holds EVERY static string of the renderer in five languages: ``english``, Hinglish
(``hindi_roman``), Hindi (``hindi_devanagari``) and Gujarati (``gujarati_roman`` / ``gujarati_script``), so a reply to those
customers is complete without the LLM localizer. ``english.json`` is the reference table: the renderer's ``_EN``. The tables are
loaded once when the renderer is imported; ``assert_complete()`` (run at startup by ``main._warm_agent_templates``) raises when a
language is missing or its keys differ from English, and ``problems()`` checks the rest: the same ``{placeholders}``, native-script
tables free of stray English words (only the brand words in ``_LATIN_OK`` and the placeholders), every button title within WhatsApp's
20 characters, list-button text within 20 and list headers within 60. A language missing a key falls back to English for that key
(and ``problems()`` says so) — it never crashes a reply.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).with_name("agent_templates")
LANGUAGES: tuple[str, ...] = ("english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script")
REFERENCE = "english"
NATIVE_SCRIPT = frozenset({"hindi_devanagari", "gujarati_script"})
_LATIN_OK = frozenset({"upi", "cod", "qr", "ok", "id", "yes", "no", "whatsapp", "e", "g", "m", "xl", "pincode"})
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
BUTTON_MAX = 20
LIST_BUTTON_MAX = 20
LIST_HEADER_MAX = 60


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict[str, str]]:
    """{language: {key: text}} for every table file found (a missing / unreadable file is logged and skipped)."""
    tables: dict[str, dict[str, str]] = {}
    for lang in LANGUAGES:
        path = TEMPLATE_DIR / f"{lang}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.error("agent templates: cannot read %s: %s", path.name, exc)
            continue
        tables[lang] = {str(k): str(v) for k, v in data.items()}
    return tables


def problems(english: dict[str, str]) -> list[str]:
    """Everything wrong with the loaded tables relative to `english` (empty = all good)."""
    out: list[str] = []
    tables = load_all()
    for lang in LANGUAGES:
        table = tables.get(lang)
        if table is None:
            out.append(f"{lang}: table missing")
            continue
        for key in english:
            if key not in table:
                out.append(f"{lang}: missing key {key}")
        for key in table:
            if key not in english:
                out.append(f"{lang}: unknown key {key}")
        for key, text in table.items():
            want = english.get(key)
            if want is None:
                continue
            if set(_PLACEHOLDER.findall(text)) != set(_PLACEHOLDER.findall(want)):
                out.append(f"{lang}: {key}: placeholders {sorted(set(_PLACEHOLDER.findall(text)))} != {sorted(set(_PLACEHOLDER.findall(want)))}")
            if key.startswith("btn_") and len(_PLACEHOLDER.sub("", text)) > BUTTON_MAX and "{" not in text:
                out.append(f"{lang}: {key}: button title {len(text)} chars > {BUTTON_MAX}")
            if key in ("list_view_options", "list_view_products") and len(text) > LIST_BUTTON_MAX:
                out.append(f"{lang}: {key}: list button text {len(text)} chars > {LIST_BUTTON_MAX}")
            if key == "list_choose" and len(text) > LIST_HEADER_MAX:
                out.append(f"{lang}: {key}: list header {len(text)} chars > {LIST_HEADER_MAX}")
            if lang in NATIVE_SCRIPT:
                stray = [w for w in _LATIN_WORD.findall(_PLACEHOLDER.sub("", text)) if w.lower() not in _LATIN_OK]
                if stray:
                    out.append(f"{lang}: {key}: English word(s) {stray} in a native-script text")
    return out


def assert_complete() -> int:
    """
    Every language of ``LANGUAGES`` has a table and exactly the English key set. Returns the key count.

    Raises AssertionError naming what is missing / extra — run at startup so a half-translated release is loud, and by the test suite.
    """
    tables = load_all()
    missing = [lang for lang in LANGUAGES if lang not in tables]
    assert not missing, f"agent templates: no table for {missing}"
    reference = set(tables[REFERENCE])
    for lang in LANGUAGES:
        keys = set(tables[lang])
        assert keys == reference, (
            f"agent templates: {lang} differs from {REFERENCE} — missing {sorted(reference - keys)[:8]}, extra {sorted(keys - reference)[:8]}"
        )
    return len(reference)


def warm(english: dict[str, str]) -> None:
    """Startup hook: assert the five tables are complete, log what was loaded and every problem. Logs instead of raising (never blocks startup)."""
    tables = load_all()
    try:
        count = assert_complete()
    except AssertionError as exc:
        count = len(english)
        logger.error("%s", exc)
    issues = problems(english)
    logger.info("agent templates loaded: %d language(s) %s, %d keys each", len(tables), sorted(tables), count)
    for issue in issues[:30]:
        logger.error("agent templates: %s", issue)
