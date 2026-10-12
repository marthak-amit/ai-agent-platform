"""
Language detection and instruction generation for multilingual AI replies.

Principle: the customer is answered in the language AND script they wrote in. Pure Gujarati script
in → pure Gujarati script out; Hinglish in → Hinglish out; Tamil in → Tamil out, and so on.

Detection is script-first (every major Indic script + Urdu), then keyword-based for Roman-script
Hindi / Gujarati / Marathi, then English. No external dependencies.

Language codes returned by detect_language():
    "english"           — English
    "hindi_devanagari"  — Hindi in Devanagari script
    "hindi_roman"       — Hindi / Hinglish typed in Latin script
    "gujarati_script"   — Gujarati script
    "gujarati_roman"    — Gujarati typed in Latin script
    "marathi"           — Marathi (Devanagari)
    "marathi_roman"     — Marathi typed in Latin script
    "bengali" "tamil" "telugu" "kannada" "malayalam" "punjabi" "odia" "urdu" — native script

NOTE: "hinglish" is intentionally NOT a return value (legacy DB rows may still hold it). Mixed
Hindi-English in Roman script maps to "hindi_roman".

Languages with a hand-written template set live in language_templates.TEMPLATES; every other code
is served by reply_localizer (LLM translation of the deterministic reply, with fact protection).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Script ranges ─────────────────────────────────────────────────────────────

_SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "devanagari": (0x0900, 0x097F),
    "bengali": (0x0980, 0x09FF),
    "gurmukhi": (0x0A00, 0x0A7F),
    "gujarati": (0x0A80, 0x0AFF),
    "odia": (0x0B00, 0x0B7F),
    "tamil": (0x0B80, 0x0BFF),
    "telugu": (0x0C00, 0x0C7F),
    "kannada": (0x0C80, 0x0CFF),
    "malayalam": (0x0D00, 0x0D7F),
    "arabic": (0x0600, 0x06FF),
}
# Devanagari/Arabic extended blocks that also occur in real messages.
_EXTRA_RANGES: dict[str, tuple[int, int]] = {
    "devanagari": (0xA8E0, 0xA8FF),
    "arabic": (0x0750, 0x077F),
}
# Presentation forms (ligatures some keyboards emit).
_PRESENTATION: tuple[int, int] = (0xFB50, 0xFDFF)

_SCRIPT_TO_LANG: dict[str, str] = {
    "bengali": "bengali",
    "gurmukhi": "punjabi",
    "gujarati": "gujarati_script",
    "odia": "odia",
    "tamil": "tamil",
    "telugu": "telugu",
    "kannada": "kannada",
    "malayalam": "malayalam",
    "arabic": "urdu",
}

# language code → (English name, script name used in prompts, expected script key or None for Latin)
LANGUAGES: dict[str, tuple[str, str, str | None]] = {
    "english": ("English", "Latin", None),
    "hindi_roman": ("Hinglish (Hindi in Roman script)", "Latin", None),
    "hindi_devanagari": ("Hindi", "Devanagari", "devanagari"),
    "gujarati_roman": ("Gujarati in Roman script", "Latin", None),
    "gujarati_script": ("Gujarati", "Gujarati", "gujarati"),
    "marathi": ("Marathi", "Devanagari", "devanagari"),
    "marathi_roman": ("Marathi in Roman script", "Latin", None),
    "bengali": ("Bengali", "Bengali", "bengali"),
    "tamil": ("Tamil", "Tamil", "tamil"),
    "telugu": ("Telugu", "Telugu", "telugu"),
    "kannada": ("Kannada", "Kannada", "kannada"),
    "malayalam": ("Malayalam", "Malayalam", "malayalam"),
    "punjabi": ("Punjabi", "Gurmukhi", "gurmukhi"),
    "odia": ("Odia", "Odia", "odia"),
    "urdu": ("Urdu", "Arabic (Nastaliq)", "arabic"),
}

_LEGACY_ALIASES = {"hinglish": "hindi_roman", "hindi": "hindi_roman", "gujarati": "gujarati_script",
                   "en": "english", "hi": "hindi_roman", "gu": "gujarati_script", "mr": "marathi",
                   "bn": "bengali", "ta": "tamil", "te": "telugu", "kn": "kannada", "ml": "malayalam",
                   "pa": "punjabi", "or": "odia", "ur": "urdu"}


def normalize_language(lang: str | None) -> str:
    """Fold legacy / ISO spellings into a known language code; unknown or empty → 'english'."""
    if not isinstance(lang, str):
        return "english"
    value = lang.strip().lower()
    value = _LEGACY_ALIASES.get(value, value)
    return value if value in LANGUAGES else "english"


def expected_script(lang: str | None) -> str | None:
    """Script key a reply in `lang` must be written in (None = Latin script languages)."""
    return LANGUAGES[normalize_language(lang)][2]


def script_counts(text: str) -> dict[str, int]:
    """Count letters per script ('latin' included) — digits, punctuation, emoji and symbols are ignored."""
    counts: dict[str, int] = {}
    for ch in text or "":
        cp = ord(ch)
        if cp < 0x250:
            if ch.isalpha():
                counts["latin"] = counts.get("latin", 0) + 1
            continue
        if not ch.isalpha() and not (0x0900 <= cp <= 0x0D7F):
            # Indic combining marks (matras, viramas) are not isalpha(); keep them for the script count.
            continue
        for name, (lo, hi) in _SCRIPT_RANGES.items():
            if lo <= cp <= hi or (name in _EXTRA_RANGES and _EXTRA_RANGES[name][0] <= cp <= _EXTRA_RANGES[name][1]):
                counts[name] = counts.get(name, 0) + 1
                break
        else:
            if _PRESENTATION[0] <= cp <= _PRESENTATION[1]:
                counts["arabic"] = counts.get("arabic", 0) + 1
    return counts


# ── Roman-script keyword lists ────────────────────────────────────────────────
# Words shared between languages ("ha", "nahi", "bhav", "kal", "do", "me") are deliberately left out of
# every list: they carry no signal, and the previous language decides.

_GUJARATI_ROMAN_KEYWORDS: frozenset[str] = frozenset("""
kem cho chho chhe chhu chu che chhi tame tamne tamaro tamari tamaru tamara tamare mane amne amare
ketlu ketli ketla kevu kevi kevo kimat bhaav joiye joie joi shu su kyare nathi saru saras majama maja
aapo apo apjo aapjo aapsho levo levu lidhu lidho karvo karvu karjo karsho moklo mokalo batavo batavjo
jovu mari maru mara hatu hata hati avjo aavjo aavo aavyo aavyu gayo gayu thayu thashe malshe malse
pachhi atyare ahiya chokkas barabar
""".split())

_HINDI_ROMAN_KEYWORDS: frozenset[str] = frozenset("""
kya hai hain hoon chahun chahu shukriya dhanyavaad mujhe mujhko chahiye chahie chahta chahti kitna kitni kitne nahi nahin nhi
haanji bhai didi bhaiya acha accha achha theek thik bilkul zaroor bahut bohot bahot kaisa kaisi kaise
kab yahan yaha wahan waha abhi aaj dikhao dikhana dikha lena lunga lungi dena dedo dijiye kijiye
bata batao bataiye kar karo karna karni karein karenge mera meri mere aap aapka
aapki aapke apka apki tum tumhara tumhari hum hamara hamare wala wali vala vali mein bhejo bhej bhejiye
bhijwao hoga hogi honge milega milegi milta milti rahe raha rahi kaun kahan kaha kyun kyu kidhar kuch
sirf pasand liye liya diya diye gaya gayi
""".split())

# Weak Hindi cues: count half, so a stronger Gujarati/Marathi cue in the same message wins ("ahe ka").
_HINDI_ROMAN_WEAK: frozenset[str] = frozenset({"ka", "ki"})

# Gujarati 'is/are' forms are the strongest Gujarati cue (Hindi 'mein' + Gujarati 'chhe' → Gujarati).
_GUJARATI_COPULA: frozenset[str] = frozenset({"che", "chhe", "cho", "chho", "chu", "chhu"})

_MARATHI_ROMAN_KEYWORDS: frozenset[str] = frozenset("""
ahe aahe ahet aahet kay mala tula tumhi tumhala kiti pahije pahijet hava havi havet nako kuthe kasa
kashi kase mazha majha maza majhi tumcha tumchi tumche amhi dya dakhva dakhav sanga sangu hoy pathva kara baghun
""".split())

_MARATHI_DEVANAGARI_MARKERS: frozenset[str] = frozenset("""
आहे आहेत आहात आहेस काय मला तुला तुम्ही तुमच्या तुमची तुमचा तुमचे किती पाहिजे पाहिजेत हवे हवी हवा हव
नको कुठे कसे कशी कसा माझा माझी माझे आम्ही आमच्या द्या दाखवा सांगा होय मिळेल मिळतील नाही आणि पाठवा करा बघा रद्द
""".split())
_HINDI_DEVANAGARI_MARKERS: frozenset[str] = frozenset("""
है हैं हूँ हूं क्या मुझे मुझको चाहिए चाहिये कितना कितनी कितने नहीं नही हाँ हां आप आपका आपकी आपके मेरा मेरी
मेरे तुम हम हमारा हमारे वाला वाली कीमत दाम दीजिए दिखाइए बताइए भेजिए कहाँ कहां कब कौन
क्यों कुछ सिर्फ बहुत अभी
""".split())

# Function words that are unambiguously English (no Roman Hindi/Gujarati/Marathi word collides with these).
_ENGLISH_FUNCTION_WORDS: frozenset[str] = frozenset("""
what how is are was were want need give will can the a an i my you your this that it its have has does did
tell get please much many which where when who why with for from of and or not any some there here about
would could should dont we our us they them he she his her show instead than then if more less just also actually really very
""".split())
# English shop words that Hinglish/Gujarati speakers use constantly: they only count half.
_ENGLISH_COMMERCE_WORDS: frozenset[str] = frozenset("""
price cost available stock order buy send sell delivery cancel confirm status address name quantity size
color colour payment pay track looking like
""".split())

_ENGLISH_GREETINGS = frozenset({"hi", "hii", "hiii", "hello", "hey", "heyy", "hlo", "helo", "hy"})

# Short words that carry no language signal on their own — the previous language decides.
_AMBIGUOUS_WORDS: frozenset[str] = frozenset([
    "yes", "no", "ok", "okay", "haan", "ha", "nahi", "nah", "han", "haa", "k", "kk",
    "cod", "upi", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "confirm", "done", "sure", "fine", "right", "paid", "cancel", "thanks", "thank you", "thx",
])

_WORD_RE = re.compile(r"[a-z']+")
_STRIP_RE = re.compile(r"https?://\S+|\S+@\S+|[0-9]+")


@dataclass(frozen=True)
class Detection:
    """Result of detect(): the language code and whether the message itself actually said so."""

    lang: str
    confident: bool


def is_ambiguous(message: str) -> bool:
    """Return True if message is a short, language-ambiguous reply (e.g. 'yes', 'COD', '2')."""
    return (message or "").strip().lower() in _AMBIGUOUS_WORDS


def _devanagari_lang(text: str, previous: str | None = None) -> str:
    """Hindi vs Marathi for Devanagari text, by function-word markers (no marker → previous, else Hindi)."""
    tokens = re.findall(r"[\u0900-\u097F]+", text)
    mr = sum(1 for t in tokens if t in _MARATHI_DEVANAGARI_MARKERS)
    hi = sum(1 for t in tokens if t in _HINDI_DEVANAGARI_MARKERS)
    # Marathi-only forms inside words ("तुमच्या", "आपल्या", "ची"/"चा" possessives, "ळ" letter).
    mr += sum(1 for cue in ("ळ", "च्या", "ल्या", "ाचे", "ाची") if cue in text)
    if mr == hi and previous in ("marathi", "hindi_devanagari"):
        return previous
    return "marathi" if mr > hi else "hindi_devanagari"


def detect(message: str, previous_language: str | None = None) -> Detection:
    """
    Detect the language of a customer message and say how sure we are.

    Order: script (any Indic script / Urdu) → Roman keywords (Gujarati / Marathi / Hindi) → English.
    A message that carries no signal (a name, an address, "ok", "COD", "2", a lone product word) is
    NOT confident and inherits `previous_language` — the language must not flip on "Rajesh Patel".

    Args:
        message:           Raw customer message text.
        previous_language: Language of the customer's previous turn.

    Returns:
        Detection(lang, confident).
    """
    text = (message or "").strip()
    prev = normalize_language(previous_language) if previous_language else None
    fallback = prev or "english"
    if not text:
        return Detection(fallback, False)

    lowered = text.lower()
    if lowered in _AMBIGUOUS_WORDS:
        return Detection(fallback, False)

    # 1. Script. Any non-Latin script with ≥2 letters (or ≥ half of all letters, e.g. "હા") wins.
    counts = script_counts(text)
    latin = counts.get("latin", 0)
    native = {k: v for k, v in counts.items() if k != "latin"}
    if native:
        script, n = max(native.items(), key=lambda kv: kv[1])
        total = latin + sum(native.values())
        if n >= 2 or n * 2 >= total:
            if script == "devanagari":
                return Detection(_devanagari_lang(text, prev), True)
            return Detection(_SCRIPT_TO_LANG[script], True)

    # 2. Roman script keywords.
    cleaned = _STRIP_RE.sub(" ", lowered)
    words = _WORD_RE.findall(cleaned)
    gu = sum(1 for w in words if w in _GUJARATI_ROMAN_KEYWORDS) + 0.5 * sum(1 for w in words if w in _GUJARATI_COPULA)
    mr = sum(1 for w in words if w in _MARATHI_ROMAN_KEYWORDS)
    hi = sum(1 for w in words if w in _HINDI_ROMAN_KEYWORDS) + 0.5 * sum(1 for w in words if w in _HINDI_ROMAN_WEAK)
    en = sum(1 for w in words if w in _ENGLISH_FUNCTION_WORDS) + 0.5 * sum(1 for w in words if w in _ENGLISH_COMMERCE_WORDS)
    best = max(gu, mr, hi)
    if best >= 1 and best >= en:
        winners = [lang for lang, score in (("gujarati_roman", gu), ("marathi_roman", mr), ("hindi_roman", hi)) if score == best]
        if len(winners) == 1:
            return Detection(winners[0], True)
        if prev in winners:
            return Detection(prev, True)
        # "che" (gu) vs "hai" (hi) tie with no history — Hindi is the larger audience.
        return Detection("hindi_roman" if "hindi_roman" in winners else winners[0], True)

    # 3. English. Leaving an established non-English language needs real English grammar (two function
    #    words, or most of the message), not just shop words like "name price" in a garbled message.
    if prev and prev != "english":
        strong = sum(1 for w in words if w in _ENGLISH_FUNCTION_WORDS)
        if strong >= 2 or (strong >= 1 and strong * 2 >= len(words)):
            return Detection("english", True)
    elif en >= 2 or (en >= 1 and len(words) <= 6):
        return Detection("english", True)
    if len(words) == 1 and words[0] in _ENGLISH_GREETINGS:
        # A bare "hi" keeps an established language; only a first message becomes English.
        return Detection(prev, False) if prev else Detection("english", True)

    # 4. No signal (name, address, lone product word, gibberish, pure digits/emoji).
    return Detection(prev or "english", False)


def detect_language(message: str, previous_language: str | None = None) -> str:
    """
    Detect the language of an incoming customer message (see detect()).

    Args:
        message:           Raw customer message text.
        previous_language: Language detected in the customer's previous turn — returned when the
                           message itself carries no language signal.

    Returns:
        One of the language codes documented in the module docstring.
    """
    return detect(message, previous_language).lang


_PROTECTED_RE = re.compile(
    r"https?://\S+|\S+@\S+|[A-Za-z]{0,6}[-_]?\d[\w\-/.,]*|₹\s*[\d,.]+|\b(?:UPI|COD|GPay|PhonePe|Paytm|WhatsApp|SKU|QR)\b"
)


def reply_matches_language(text: str, lang: str, ignore_words: frozenset[str] = frozenset()) -> bool:
    """
    True when `text` is written in the script `lang` requires.

    Native-script languages need mostly native letters (codes, prices, URLs and brand words are
    ignored); Latin-script languages (English, Hinglish, Roman Gujarati/Marathi) must contain no Indic
    or Arabic script at all. Text with no letters left (emoji, numbers) always passes.
    """
    letters_text = _PROTECTED_RE.sub(" ", text or "")
    if ignore_words:
        # Product names / the customer's own Latin-script words must not count against a native-script reply.
        letters_text = re.sub(r"[A-Za-z]+", lambda m: " " if m.group(0).lower() in ignore_words else m.group(0), letters_text)
    counts = script_counts(letters_text)
    total = sum(counts.values())
    if total == 0:
        return True
    want = expected_script(lang)
    if want is None:
        return counts.get("latin", 0) / total >= 0.9
    return counts.get(want, 0) / total >= 0.6


# Languages whose model-written text we can screen for forbidden claims (price/stock/delivery words).
_CLAIM_GUARDED = frozenset({"english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script",
                            "marathi", "marathi_roman"})


_ROMAN_INDIC = frozenset({"hindi_roman", "gujarati_roman", "marathi_roman"})


def roman_language_mismatch(text: str, lang: str | None) -> bool:
    """
    True when a Latin-script reply is clearly in the WRONG language for the customer.

    Scripts cannot tell English from Hinglish from Roman Gujarati, so the keyword detector is used: an English
    reply (two+ real English function words) to a Hinglish / Roman-Gujarati / Roman-Marathi customer, or a
    Hinglish/Gujarati reply to an English customer, or Hindi where Gujarati was asked. Short texts (product
    lines, one-word answers) never count — too little evidence.
    """
    code = normalize_language(lang)
    if expected_script(code) is not None:
        return False
    cleaned = _PROTECTED_RE.sub(" ", text or "")
    words = _WORD_RE.findall(cleaned.lower())
    if len(words) < 5:
        return False
    found = detect(cleaned)
    if not found.confident or found.lang == code:
        return False
    if code in _ROMAN_INDIC:
        if found.lang == "english":
            return sum(1 for w in words if w in _ENGLISH_FUNCTION_WORDS) >= 2
        return found.lang in _ROMAN_INDIC
    # code == english: any confident Roman-Indic reply is a mismatch
    return found.lang in _ROMAN_INDIC


def has_claim_guard(lang: str | None) -> bool:
    """True when router_actions.guard_reply_hint knows the price/stock/delivery vocabulary of `lang`."""
    return normalize_language(lang) in _CLAIM_GUARDED


# Terms that stay in Latin letters even inside a native-script reply: codes, brands, payment words.
_KEEP_LATIN = "SKU/order codes, numbers, ₹ amounts, URLs, UPI ids and brand names (UPI, COD, GPay, PhonePe, Paytm, WhatsApp)"

_NATIVE_EXAMPLES: dict[str, tuple[str, str]] = {
    # code: (customer sentence, correct reply) — shows the model "same language, same script, no English"
    "gujarati_script": ("શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?", "હા, અમે ચણિયા ચોળી વેચીએ છીએ. કયો રંગ જોઈએ છે?"),
    "hindi_devanagari": ("क्या आप लहंगा भी बेचते हैं?", "जी हाँ, हम लहंगा बेचते हैं। कौन सा रंग चाहिए?"),
    "marathi": ("तुमच्याकडे साडी आहे का?", "हो, आमच्याकडे साडी आहे. कोणता रंग हवा आहे?"),
    "bengali": ("আপনার কাছে শাড়ি আছে কি?", "হ্যাঁ, আমাদের কাছে শাড়ি আছে। কোন রঙ চান?"),
    "tamil": ("உங்களிடம் புடவை இருக்கிறதா?", "ஆம், எங்களிடம் புடவை உள்ளது. எந்த நிறம் வேண்டும்?"),
    "telugu": ("మీ దగ్గర చీర ఉందా?", "అవును, మా దగ్గర చీర ఉంది. ఏ రంగు కావాలి?"),
    "kannada": ("ನಿಮ್ಮ ಬಳಿ ಸೀರೆ ಇದೆಯೇ?", "ಹೌದು, ನಮ್ಮ ಬಳಿ ಸೀರೆ ಇದೆ. ಯಾವ ಬಣ್ಣ ಬೇಕು?"),
    "malayalam": ("നിങ്ങളുടെ പക്കൽ സാരി ഉണ്ടോ?", "ഉണ്ട്, ഞങ്ങളുടെ പക്കൽ സാരി ഉണ്ട്. ഏത് നിറം വേണം?"),
    "punjabi": ("ਕੀ ਤੁਹਾਡੇ ਕੋਲ ਸਾੜੀ ਹੈ?", "ਹਾਂ ਜੀ, ਸਾਡੇ ਕੋਲ ਸਾੜੀ ਹੈ। ਕਿਹੜਾ ਰੰਗ ਚਾਹੀਦਾ ਹੈ?"),
    "odia": ("ଆପଣଙ୍କ ପାଖରେ ଶାଢ଼ୀ ଅଛି କି?", "ହଁ, ଆମ ପାଖରେ ଶାଢ଼ୀ ଅଛି। କେଉଁ ରଙ୍ଗ ଦରକାର?"),
    "urdu": ("کیا آپ کے پاس ساڑھی ہے؟", "جی ہاں، ہمارے پاس ساڑھی ہے۔ کون سا رنگ چاہیے؟"),
}


def get_language_instruction(lang: str) -> str:
    """
    Return a system-prompt snippet that instructs the AI to reply in *lang*.

    The snippet is injected at the top of every system prompt so language
    compliance is the first constraint the model sees.

    Args:
        lang: Language code as returned by detect_language().

    Returns:
        Instruction string including an example reply in that language.
    """
    lang = normalize_language(lang)

    if lang == "english":
        return (
            "LANGUAGE: ENGLISH ONLY.\n"
            "Every word of the reply must be English — no Hindi, Gujarati or other-language words\n"
            "(no 'ji', 'haan', 'kya', 'hai', 'chahiye', 'bilkul', 'zaroor', 'kem cho', 'chhe').\n"
            "  ✗ 'Haan ji, available hai' → ✅ 'Yes, it is available'\n"
            "  ✗ 'Payment complete karein' → ✅ 'Please complete the payment'\n"
            'Example reply: "Product XYZ001 is priced at ₹999 and is in stock. Would you like to order?"'
        )

    if lang in _NATIVE_EXAMPLES:
        name, script, _ = LANGUAGES[lang]
        question, answer = _NATIVE_EXAMPLES[lang]
        return (
            f"LANGUAGE: the customer is writing in {name}. Reply in PURE {name}, written in {script} script.\n"
            f"Every word must be {name} in {script} letters. Do NOT mix in English or Hinglish words, and do NOT\n"
            f"write {name} words in Latin/Roman letters. The only things that may stay in Latin letters are "
            f"{_KEEP_LATIN}.\n"
            f"Write product types (saree, kurti, lehenga, chaniya choli...) the way a native {name} speaker "
            f"would in {script} script — never leave them in English.\n"
            "Be polite and natural, like a shop-owner chatting with a customer.\n"
            f"Example — customer: {question}\n"
            f"Example — correct reply: {answer}"
        )

    if lang == "hindi_roman":
        return (
            "LANGUAGE: Customer is writing Hindi / Hinglish in Roman (English) letters.\n"
            "Reply in Hinglish in Roman letters ONLY — do not use Devanagari or Gujarati script.\n"
            "Use 'ji' naturally as a respectful suffix.\n"
            'Example reply: "Haan ji! Product XYZ001 ₹999 mein available hai. Order karna chahenge?"'
        )

    if lang == "gujarati_roman":
        return (
            "LANGUAGE: Customer is writing Gujarati in Roman (English) letters.\n"
            "Reply in Gujarati written in Roman letters ONLY (e.g. 'chhe', 'tamne', 'joiye') — do not use "
            "Gujarati script, Devanagari or Hindi words like 'hai'/'kya'.\n"
            "Use 'ji' naturally as a respectful suffix.\n"
            'Example reply: "Haa ji! Product XYZ001 ₹999 ni chhe. Order karva mango cho?"'
        )

    # marathi_roman
    return (
        "LANGUAGE: Customer is writing Marathi in Roman (English) letters.\n"
        "Reply in Marathi written in Roman letters ONLY (e.g. 'ahe', 'tumhala', 'pahije') — no Devanagari, "
        "no Hindi words like 'hai'/'kya'.\n"
        'Example reply: "Ho! Product XYZ001 ₹999 la ahe. Order karaycha ahe ka?"'
    )


def build_language_rule(lang: str) -> str:
    """
    Build the CRITICAL_LANGUAGE_RULE block to prepend to every system prompt.

    This block appears BEFORE any business instructions so the model treats
    language compliance as the highest-priority constraint.

    Args:
        lang: Language code as returned by detect_language().

    Returns:
        Formatted rule string ready to prepend to a system prompt.
    """
    _lang = normalize_language(lang)
    lang_display = LANGUAGES[_lang][0].upper()
    instruction = get_language_instruction(_lang)

    ji_rule = (
        "NEVER use 'ji' in this reply — 'ji' is only for Hindi/Gujarati.\n"
        if _lang == "english"
        else ""
    )

    amount_placeholder_rule = (
        "- NEVER write ₹[amount] or ₹[Amount] — always use the real calculated amount from the order context.\n"
    )

    return (
        "⚠️ CRITICAL LANGUAGE RULE — NEVER BREAK THIS:\n\n"
        f"Detected customer language: {_lang}\n\n"
        f"YOU MUST REPLY IN {lang_display} ONLY.\n"
        "Answer in the SAME language and SAME script the customer used — pure, with no other language mixed in.\n"
        "The catalogue data below may be in English: translate its meaning into the customer's language, but keep "
        "product SKUs, numbers and ₹ prices exactly as given.\n"
        f"{ji_rule}"
        f"{instruction}\n"
        "\n"
        "⚠️ CONCISENESS RULE — NEVER BREAK THIS:\n"
        "- Maximum 2 sentences per reply.\n"
        "- Maximum 30 words per reply.\n"
        "- ONE question per reply only — never ask two things at once.\n"
        "- Never explain what you are doing.\n"
        "- Never say 'I apologize' or 'I'm sorry'.\n"
        "- Never repeat information already given in this conversation.\n"
        "\n"
        "⚠️ HALLUCINATION RULE — NEVER BREAK THIS:\n"
        "- NEVER invent colors, sizes, or materials not listed in the catalogue.\n"
        "- NEVER promise same-day dispatch or specific dispatch times.\n"
        "- NEVER say 'our team will call you' — you complete orders in this chat.\n"
        "- NEVER mention discounts not explicitly listed in the catalogue.\n"
        "- NEVER invent product features. Only state what catalogue data contains.\n"
        "- If a detail is not in the catalogue: say 'contact us for details' (in the customer's language).\n"
        f"{amount_placeholder_rule}"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
