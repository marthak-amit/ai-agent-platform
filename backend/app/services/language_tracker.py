"""
Agent v3 — which language to ANSWER in, decided per message without an LLM and without flip-flopping.

``signal`` reads ONE message: its script (Gujarati / Devanagari / other Indic) or, for Latin letters, how many words it has from each
language's marker list. Words several languages share ("karo", "size", "order", "price", "ok") are in no list and count for nothing; a
tap, a number, "ok" / "yes" carry no signal at all. ``decide`` turns that signal into the language of the reply:

* a message with no signal keeps the current language (the first message of a conversation: the seller's default language);
* the first clear message of a conversation sets the language;
* a new language needs a STRONG message — native script, ≥ 3 markers, or ≥ 2 markers and none from another language ("Give me
  catalogue") — or TWO consecutive clear messages ("show kurti", then "price please"); one stray word never switches a conversation
  ("ok", "yes", a product name, a Hinglish word in a Gujarati sentence);
* while a name / address / phone is being asked, Latin text is a proper noun or a street: only another SCRIPT switches then.

The tracker state ``{"lang", "conf", "pending", "pending_n"}`` lives in the cart's planner memory (``state.lang_track``), so no
migration is needed; ``Conversation.last_customer_language`` mirrors ``lang`` for everything else that reads it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import language_service as ls

NEUTRAL_WORDS = frozenset({
    "hi", "hii", "hiii", "hello", "hey", "heyy", "hlo", "helo", "ok", "okay", "yes", "no", "thanks", "thx", "cod", "upi",
    "namaste", "namaskar", "namaskaram", "pranam", "jai", "ram",       # a greeting in the customer's own tongue says nothing about the language they will order in
})
# "hi there" / "hello sir" / "good morning": English, but only as a first hint — a weak marker (never switches a running conversation by itself).
_ENGLISH_GREETING_WORDS = frozenset({"hi", "hii", "hiii", "hello", "hey", "heyy", "hlo", "helo", "hy", "morning", "afternoon", "evening", "night"})

_GUJARATI_EXTRA = frozenset("che chhe joiye joi mare tamaru shu ketla kayu nathi aapo kem su hu mane batavo moklo".split())
_HINDI_EXTRA = frozenset("hai chahiye mujhe kya kitna kaun nahi dikhao bhejo aap mera kitne bataiye ye yeh woh hatao nikalo".split())
_ENGLISH_EXTRA = frozenset("""
give me show want the please how much need send tell see what which where under below above between cheaper cheap looking like
do you have has can could would is are was it this that these those with from for my your any some one two three more other another
available catalogue
""".split())
# Ambiguous in any list: two languages (or English and Hinglish) use them every day.
_SHARED = frozenset({"karo", "size", "order", "price", "ok", "okay", "hi", "hello", "yes", "no", "cod", "upi", "pay", "payment", "cancel", "confirm", "do", "me", "hata"})


def _build() -> dict[str, frozenset[str]]:
    """Per-language marker sets with every word that two languages share removed."""
    raw = {
        "gujarati_roman": set(ls._GUJARATI_ROMAN_KEYWORDS) | set(_GUJARATI_EXTRA),
        "hindi_roman": set(ls._HINDI_ROMAN_KEYWORDS) | set(_HINDI_EXTRA),
        "marathi_roman": set(ls._MARATHI_ROMAN_KEYWORDS),
        "english": {w for w in set(ls._ENGLISH_FUNCTION_WORDS) | set(_ENGLISH_EXTRA) if len(w) > 1 or w == "i"},
    }
    # "me" is English only when it is not the Hinglish "mein" short form: keep it as an English marker, drop other shared words.
    shared_between = {w for a, wa in raw.items() for b, wb in raw.items() if a < b for w in wa & wb}
    out = {lang: frozenset(w for w in words if w not in _SHARED and w not in shared_between) for lang, words in raw.items()}
    out["english"] = frozenset(out["english"] | {"me"})
    return out


MARKERS: dict[str, frozenset[str]] = _build()
_WORD_RE = re.compile(r"[a-z']+")
_STRIP_RE = re.compile(r"https?://\S+|\S+@\S+|[0-9]+")


@dataclass(frozen=True)
class Signal:
    """What one message says about the customer's language."""

    lang: str | None = None         # None = no signal
    markers: int = 0                # words of `lang`'s list
    others: int = 0                 # words of any other language's list
    script: bool = False            # decided by the writing system

    @property
    def clear(self) -> bool:
        """Points one way: nothing from another language, or at least twice as many markers as the rest."""
        return self.lang is not None and (self.others == 0 or self.markers >= 2 * self.others)

    @property
    def strong(self) -> bool:
        """Enough on its own to switch a running conversation."""
        return self.lang is not None and self.clear and (self.script or self.markers >= 3 or (self.markers >= 2 and self.others == 0))


def signal(text: str | None) -> Signal:
    """The language signal of one typed message (no state, no LLM)."""
    text = (text or "").strip()
    if not text:
        return Signal()
    counts = ls.script_counts(text)
    latin = counts.get("latin", 0)
    native = {k: v for k, v in counts.items() if k != "latin"}
    if native:
        script, n = max(native.items(), key=lambda kv: kv[1])
        if n >= 2 or n * 2 >= latin + sum(native.values()):
            lang = ls._devanagari_lang(text) if script == "devanagari" else ls._SCRIPT_TO_LANG[script]
            return Signal(lang, markers=n, script=True)
    words = _WORD_RE.findall(_STRIP_RE.sub(" ", text.lower()))
    if not words or (len(words) == 1 and words[0] in NEUTRAL_WORDS):
        return Signal()
    score = {lang: sum(1 for w in words if w in markers) for lang, markers in MARKERS.items()}
    best = max(score.values())
    if best == 0:
        if len(words) >= 2 and any(w in _ENGLISH_GREETING_WORDS for w in words) and not any(w in NEUTRAL_WORDS - _ENGLISH_GREETING_WORDS - {"hey"} for w in words):
            return Signal("english", markers=1)        # weak: sets the language of a first message, cannot flip an established one
        return Signal()
    winners = [lang for lang, n in score.items() if n == best]
    if len(winners) > 1:
        return Signal(None, markers=best, others=best)          # a tie: the message does not say
    lang = winners[0]
    return Signal(lang, markers=best, others=sum(score.values()) - best)


@dataclass
class Decision:
    """The language to answer in and how it was reached."""

    lang: str
    confidence: float
    switched: bool
    state: dict = field(default_factory=dict)
    why: str = ""


_DASHBOARD_LANGUAGE = {"en": "english", "hi": "hindi_roman", "gu": "gujarati_roman"}


def seller_default(client) -> str | None:
    """The shop's default language (the dashboard language chosen at sign-up: EN / HI / GU), or None."""
    return _DASHBOARD_LANGUAGE.get(str(getattr(client, "dashboard_language", "") or "").lower())


def _confidence(sig: Signal, previous: float) -> float:
    """0.3–0.99: how sure we are the reply language is right, from this message's markers."""
    if sig.script:
        return 0.95
    return round(min(0.99, max(0.3, 0.55 + 0.12 * sig.markers - 0.1 * sig.others)), 2)


def decide(
    text: str | None, state: dict | None, *, fallback: str | None, default: str | None = None, free_text_open: bool = False, tap: bool = False,
) -> Decision:
    """
    The reply language for this message.

    `state` = the stored tracker state (or None), `fallback` = ``Conversation.last_customer_language`` (used when there is no state),
    `default` = the seller's default (used when there is no state and nothing in the message says), `free_text_open` = a name /
    address / phone question is open, `tap` = a button / list tap (never a language signal).
    """
    st = dict(state or {})
    first = not st.get("lang")
    # A conversation that already has a (non-default) stored language keeps it; a brand-new one starts in the seller's default.
    base = fallback if fallback and ls.normalize_language(fallback) != "english" else (default or fallback)
    current = ls.normalize_language(st.get("lang") or base or "english")
    conf = float(st.get("conf", 0.3))
    sig = Signal() if tap else signal(text)
    if sig.lang and free_text_open and not sig.script:
        sig = Signal()
    if sig.lang is None:
        return Decision(current, conf, False, {"lang": current, "conf": conf, "pending": st.get("pending"), "pending_n": st.get("pending_n", 0)},
                        "no signal" + (" (seller default)" if first and default else ""))
    if sig.lang == current:
        conf = _confidence(sig, conf)
        return Decision(current, conf, False, {"lang": current, "conf": conf, "pending": None, "pending_n": 0}, f"{sig.markers} markers agree")
    if first and sig.clear:
        why, switch = f"first message ({sig.markers} markers)", True
    elif sig.strong:
        why, switch = (f"strong signal ({'script' if sig.script else str(sig.markers) + ' markers'})"), True
    elif sig.clear:
        switch = st.get("pending") == sig.lang
        why = "second consecutive clear message" if switch else "first clear message in a new language (waiting for a second)"
    else:
        return Decision(current, conf, False, {"lang": current, "conf": conf, "pending": st.get("pending"), "pending_n": st.get("pending_n", 0)},
                        f"mixed message ({sig.markers} vs {sig.others}) — kept")
    if switch:
        conf = _confidence(sig, conf)
        return Decision(sig.lang, conf, True, {"lang": sig.lang, "conf": conf, "pending": None, "pending_n": 0}, why)
    return Decision(current, conf, False, {"lang": current, "conf": conf, "pending": sig.lang, "pending_n": 1}, why)
