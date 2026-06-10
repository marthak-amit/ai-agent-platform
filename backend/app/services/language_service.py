"""
Language detection and instruction generation for multilingual AI replies.

Supports English, Hindi (Devanagari), Hindi (Romanised), Gujarati (script),
Gujarati (Romanised), and Hinglish (default). Detection is script-first then
keyword-based — no external dependencies.

Language codes returned by detect_language():
    "english"           — purely ASCII, English vocabulary
    "hindi_devanagari"  — Devanagari script (Unicode U+0900–U+097F)
    "hindi_roman"       — Hindi words typed in Latin script (Hinglish)
    "gujarati_script"   — Gujarati script (Unicode U+0A80–U+0AFF)
    "gujarati_roman"    — Gujarati words typed in Latin script
    "hinglish"          — mixed Hindi-English (default fallback)
"""

from __future__ import annotations

# ── Keyword lists ──────────────────────────────────────────────────────────────

_GUJARATI_ROMAN_KEYWORDS: list[str] = [
    "kem", "cho", "chhe", "bhaav", "ketlu",
    "kai", "avu", "levo", "apo", "maal",
    "che", "hatu", "hata", "karvo", "joiye",
    "su", "kevi", "rite", "tamaro", "tamare",
    "mane", "apva", "lidhu", "aavyo", "gayo",
]

_HINDI_ROMAN_KEYWORDS: list[str] = [
    "kya", "hai", "hain", "mujhe", "chahiye",
    "kitna", "kitni", "nahi", "haan", "bhai",
    "didi", "acha", "theek", "bilkul", "zaroor",
    "bahut", "bohot", "kaisa", "kaise", "kab",
    "yahan", "wahan", "abhi", "kal", "aaj",
    "dikhao", "lena", "dena", "bata", "batao",
    "accha", "thik", "sahi", "karo", "kar",
]

_ENGLISH_PATTERNS: list[str] = [
    "price", "cost", "how much", "available", "stock",
    "order", "buy", "show", "send", "what", "where",
    "when", "which", "who", "saree", "kurti", "lehenga",
    "product", "colour", "color", "size", "delivery",
    "hello", "hi", "please", "thanks", "thank you",
]

# Common English function words — presence of ANY one strongly suggests English
# when no Hindi/Gujarati keywords are found.
_PURE_ENGLISH_INDICATORS: list[str] = [
    "what", "how", "is", "are", "want", "need", "show",
    "give", "price", "cost", "available", "stock", "order",
    "buy", "will", "can", "do", "the", "a", "an",
    "i", "me", "my", "you", "your", "this", "that",
    "it", "its", "have", "has", "does", "tell", "get",
]


# ── Public API ────────────────────────────────────────────────────────────────


# Short words that are ambiguous across languages.  When seen alone we fall
# back to whatever language the customer used in their previous message.
_AMBIGUOUS_WORDS: frozenset[str] = frozenset([
    "yes", "no", "ok", "okay", "haan", "ha", "nahi", "nah",
    "cod", "upi", "1", "2", "3", "4", "5",
    "confirm", "done", "sure", "fine", "right",
])


def is_ambiguous(message: str) -> bool:
    """Return True if message is a short, language-ambiguous reply (e.g. 'yes', 'COD')."""
    return message.strip().lower() in _AMBIGUOUS_WORDS


def detect_language(message: str, previous_language: str | None = None) -> str:
    """
    Detect the language of an incoming customer message.

    Detection order (each check is mutually exclusive and short-circuits):
        0. Ambiguous single-word message → return previous_language
        1. Devanagari script count  → "hindi_devanagari"
        2. Gujarati script count    → "gujarati_script"
        3. Gujarati Roman keywords  → "gujarati_roman"
        4. Hindi Roman keywords     → "hindi_roman"
        5. ASCII-only + English vocabulary → "english"
        6. Default                  → "hinglish"

    Args:
        message:           Raw customer message text.
        previous_language: Language detected in the customer's previous turn,
                           used as fallback for ambiguous short words.

    Returns:
        One of the six language codes documented in the module docstring.
    """
    message_stripped = message.strip()

    # 0. Ambiguous short message — inherit previous language so the reply
    #    language doesn't jump around during order collection.
    if message_stripped.lower() in _AMBIGUOUS_WORDS and previous_language:
        return previous_language

    # 1. Devanagari script (pure Hindi)
    devanagari_count = sum(
        1 for char in message_stripped
        if "ऀ" <= char <= "ॿ"
    )
    if devanagari_count > 2:
        return "hindi_devanagari"

    # 2. Gujarati script
    gujarati_count = sum(
        1 for char in message_stripped
        if "઀" <= char <= "૿"
    )
    if gujarati_count > 2:
        return "gujarati_script"

    message_lower = message_stripped.lower()
    words = message_lower.split()

    # 3. Gujarati Romanised keywords
    gujarati_score = sum(1 for w in words if w in _GUJARATI_ROMAN_KEYWORDS)
    if gujarati_score >= 1:
        return "gujarati_roman"

    # 4. Hindi Romanised keywords
    hindi_score = sum(1 for w in words if w in _HINDI_ROMAN_KEYWORDS)
    if hindi_score >= 1:
        return "hindi_roman"

    # 5. Purely ASCII → check for English vocabulary
    is_ascii = all(ord(c) < 128 for c in message_stripped)
    if is_ascii:
        # Fast-path: any pure-English indicator word is enough when no
        # Hindi/Gujarati keywords were found in steps 3 & 4.
        indicator_hit = any(w in _PURE_ENGLISH_INDICATORS for w in words)
        english_score = sum(
            1 for pattern in _ENGLISH_PATTERNS
            if pattern in message_lower
        )
        if indicator_hit or english_score >= 1 or len(words) <= 3:
            return "english"

    # 6. Default: Hinglish
    return "hinglish"


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
    if lang == "english":
        return (
            "╔══════════════════════════════════════╗\n"
            "║  LANGUAGE: ENGLISH ONLY              ║\n"
            "║  NO HINDI. NO GUJARATI. EVER.        ║\n"
            "╚══════════════════════════════════════╝\n"
            "\n"
            "ABSOLUTE RULE: Every single word in your reply must be English.\n"
            "This includes product names, stock phrases, urgency lines, and confirmations.\n"
            "\n"
            "FORBIDDEN — these words will FAIL the reply:\n"
            "  ji, haan, nahi, aap, kya, hai, mein, chahiye, bilkul, zaroor,\n"
            "  acha, theek, bhai, didi, sirf, bacha, abhi, namaste, shukriya,\n"
            "  dikhao, batao, karein, karunga, karungi, accha, thik, pakka,\n"
            "  kem cho, chhe, ketlu, joiye, haa, arre, yaar, bhai, didi.\n"
            "\n"
            "SPECIFIC WRONG → RIGHT EXAMPLES:\n"
            "  ✗ 'Sirf 7 pieces bacha hai'       → ✅ 'Only 7 pieces left'\n"
            "  ✗ 'Haan ji, available hai'         → ✅ 'Yes, it is available'\n"
            "  ✗ 'Bilkul! We have it in stock'    → ✅ 'Absolutely! We have it in stock'\n"
            "  ✗ 'Zaroor! Let me check for you'   → ✅ 'Of course! Let me check for you'\n"
            "  ✗ 'Which color, ji?'               → ✅ 'Which color would you prefer?'\n"
            "  ✗ 'Payment complete karein'        → ✅ 'Please complete payment'\n"
            "\n"
            "Write like a professional English-speaking sales assistant.\n"
            "Clean, simple, professional English only — no exceptions.\n"
            "\n"
            "Example reply:\n"
            '"The Banarasi Silk Saree is priced at ₹2,450 and currently in stock. '
            'Would you like to place an order?"'
        )

    if lang == "hindi_devanagari":
        return (
            "LANGUAGE: Customer is writing in Hindi (Devanagari script).\n"
            "YOU MUST reply in Hindi Devanagari script ONLY.\n"
            "Use 'जी' (ji) naturally as a respectful honorific.\n"
            "Example reply:\n"
            '"बनारसी सिल्क साड़ी की कीमत ₹2,450 है जी। '
            "18 पीस उपलब्ध हैं। क्या आप ऑर्डर करना चाहेंगे?"
            '"'
        )

    if lang == "hindi_roman":
        return (
            "LANGUAGE: Customer is writing in Hinglish "
            "(Hindi words in Roman/English script).\n"
            "YOU MUST reply in Hinglish ONLY.\n"
            "Use 'ji' naturally as a respectful suffix in Hinglish.\n"
            "Example: 'Haan ji, available hai.' / 'Zaroor ji!'\n"
            "Example reply:\n"
            '"Haan ji! Banarasi Silk Saree ₹2,450 mein available hai. '
            "18 pieces stock mein hain. "
            'Order karna chahenge?"'
        )

    if lang == "gujarati_script":
        return (
            "LANGUAGE: Customer is writing in Gujarati script.\n"
            "YOU MUST reply in Gujarati script ONLY.\n"
            "Use 'જી' (ji) naturally as a respectful honorific.\n"
            "Order collection phrases (Gujarati script):\n"
            "  Name:     'તમારું નામ શું છે?'\n"
            "  Address:  'Delivery address શું છે?'\n"
            "  Quantity: 'કેટલા pieces જોઈએ?'\n"
            "  Payment:  'Payment UPI કે COD?'\n"
            "  Confirm:  'શું બધું સાચું છે? Confirm કરો?'\n"
            "Example reply:\n"
            '"હા જી! બનારસી સિલ્ક સાડીની કિંમત ₹2,450 છે. '
            "18 પીસ ઉપલબ્ધ છે. ઓર્ડર કરવા માંગો છો?"
            '"'
        )

    if lang == "gujarati_roman":
        return (
            "LANGUAGE: Customer is writing in Gujarati (Roman script mix).\n"
            "YOU MUST reply in Gujarati-English mix.\n"
            "Use 'ji' naturally as a respectful suffix.\n"
            "Order collection phrases (Gujarati Roman):\n"
            "  Name:     'Tamaro naam shu chhe?'\n"
            "  Address:  'Delivery address shu chhe?'\n"
            "  Quantity: 'Ketla pieces joiye?'\n"
            "  Payment:  'Payment UPI ke COD?'\n"
            "  Confirm:  'Shu badhu sahi chhe? Confirm karo?'\n"
            "Example: 'Haa ji, available che.'\n"
            "Example reply:\n"
            '"Haa ji! Banarasi Silk Saree ₹2,450 ni che. '
            "18 pieces available che. "
            'Order karva maango cho?"'
        )

    # hinglish (default)
    return (
        "LANGUAGE: Reply in Hinglish (friendly Hindi-English mix).\n"
        "Use 'ji' naturally as a respectful suffix.\n"
        "Example reply:\n"
        '"Haan ji! Banarasi Silk Saree ₹2,450 mein available hai. '
        "Stock bhi hai. "
        'Order karein?"'
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
    lang_display = {
        "english": "ENGLISH",
        "hindi_devanagari": "HINDI (Devanagari)",
        "hindi_roman": "HINGLISH (Hindi in Roman script)",
        "gujarati_script": "GUJARATI (Gujarati script)",
        "gujarati_roman": "GUJARATI (Roman mix)",
        "hinglish": "HINGLISH",
    }.get(lang, lang.upper())

    instruction = get_language_instruction(lang)

    ji_rule = (
        "NEVER use 'ji' in this reply — 'ji' is only for Hindi/Gujarati.\n"
        "NEVER use ANY Hindi/Gujarati word — see FORBIDDEN list above.\n"
        if lang == "english"
        else "Use 'ji' naturally as a respectful honorific.\n"
    )

    amount_placeholder_rule = (
        "- NEVER write ₹[amount] or ₹[Amount] — always use the real calculated amount from the order context.\n"
    )

    return (
        "⚠️ CRITICAL LANGUAGE RULE — NEVER BREAK THIS:\n\n"
        f"Detected customer language: {lang}\n\n"
        f"YOU MUST REPLY IN {lang_display} ONLY.\n"
        "DO NOT mix languages.\n"
        "DO NOT reply in Hindi if customer wrote English.\n"
        "DO NOT reply in Gujarati if customer wrote Hindi.\n"
        "MATCH THE CUSTOMER'S LANGUAGE EXACTLY.\n"
        f"{ji_rule}\n"
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
        "- If a detail is not in the catalogue: say 'contact us for details'.\n"
        f"{amount_placeholder_rule}"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
