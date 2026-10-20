"""
Agent v3 — social one-liners that never need the planner: reactions ("wah", 👍), declines ("no thanks", "nahi joiye", "bas")
and "what sells best" questions. Whole-message matching over a small multilingual vocabulary (English, Hinglish, Roman and
native-script Gujarati / Hindi / Marathi): a message with any other meaningful word goes to the planner instead.
"""

from __future__ import annotations

from app.services import indic_vocab

_REACTION_WORDS = frozenset(
    "wah wow wowww waah vah vaah wahh nice mast masta saras sars sarus superb awesome amazing great good beautiful lovely cute "
    "gorgeous pretty fab fabulous perfect cool love loved lit fire stunning classy elegant sundar sunder khubsurat khoobsurat "
    "badhiya badiya zabardast shandar jabardast lajawab mastt kya bahut khub khoob bov bahu ekdam ghanu ghano maja mazaa "
    "wahh wooow wowwww nicee niceee ji yaar yar bhai di didi sir mam madam so very too really omg oh ohh ooh aww awww ahh".split()
) | frozenset({
    "वाह", "वाओ", "शानदार", "बढ़िया", "बढ़ीया", "सुंदर", "खूबसूरत", "मस्त", "जबरदस्त", "लाजवाब", "बहुत", "क्या", "अच्छा",
    "વાહ", "સરસ", "મસ્ત", "સુંદર", "ખૂબ", "જબરદસ્ત", "બહુ", "ઘણું", "સરસ", "સારું", "વાઉ",
})
_REACTION_CORE = frozenset(
    "wah wow wowww waah vah vaah wahh nice mast masta saras sars sarus superb awesome amazing great beautiful lovely cute gorgeous "
    "pretty fab fabulous perfect cool love loved stunning classy elegant sundar sunder khubsurat khoobsurat badhiya badiya zabardast "
    "shandar jabardast lajawab mastt omg aww awww wooow wowwww nicee niceee".split()
) | frozenset({"वाह", "वाओ", "शानदार", "बढ़िया", "सुंदर", "खूबसूरत", "मस्त", "जबरदस्त", "लाजवाब", "વાહ", "સરસ", "મસ્ત", "સુંદર", "જબરદસ્ત", "વાઉ"})
_REACTION_EMOJI = frozenset("👍👌😍🥰😊😃😄😁🤩🔥💯❤♥💕💖😘✨🙌👏🤗😻💗💝🙂☺")
_EMOJI_JOINERS = frozenset("\ufe0f\u200d \t\n.!") | frozenset(chr(c) for c in range(0x1F3FB, 0x1F400))     # + skin-tone modifiers

_DECLINE_PHRASES: tuple[tuple[str, ...], ...] = tuple(tuple(p.split()) for p in (
    "no thanks", "no thank you", "no thanku", "no thnx", "no thx", "nope thanks", "not now", "not interested", "maybe later", "nothing else",
    "nahi joiye", "nahi joie", "nai joiye", "na joiye", "nathi joitu", "nathi joiye", "kai nathi joitu", "have nahi joiye", "no joiye",
    "nahi chahiye", "nahin chahiye", "nai chahiye", "na chahiye", "abhi nahi", "abhi nahin", "rehne do", "rahne do", "kuch nahi", "kuchh nahi",
    "nahi dhanyawad", "nahi shukriya", "bas", "bas bas", "bas itna", "bas itna hi", "bas rehne do", "bus", "bas thank you", "bas thanks",
    "bas aatlu", "bas aatlu j", "aatlu j", "bas have", "ok bas", "okay bas", "nahi bas", "no bas", "hu pachi kahish", "pachi",
    "नहीं चाहिए", "नही चाहिए", "अभी नहीं", "बस", "बस इतना", "बस इतना ही", "रहने दो", "नहीं धन्यवाद", "नहीं शुक्रिया",
    "નથી જોઈતું", "નહીં જોઈએ", "નથી જોઈતો", "નથી જોઈતી", "ના જોઈએ", "હમણાં નહીં", "બસ", "બસ આટલું", "ના આભાર", "ના ધન્યવાદ", "નહીં આભાર",
))
_BARE_NO = frozenset({"no", "nope", "nahi", "nahin", "nai", "na", "naa", "nahee", "नहीं", "नही", "ना", "ના", "નહીં", "નથી", "nopee", "noo", "nooo"})

_BESTSELLER_PHRASES: tuple[tuple[str, ...], ...] = tuple(tuple(p.split()) for p in (
    "best product", "best products", "best seller", "best sellers", "bestseller", "bestsellers", "best selling", "best sold", "top selling",
    "top seller", "top sellers", "top product", "top products", "top items", "most popular", "most selling", "most sold", "popular products",
    "popular items", "trending", "trending products", "what sells", "what sells most", "what is popular", "whats popular", "most wanted",
    "best item", "best items", "your best", "which is best", "which one is best", "what is best", "whats best", "kya best hai", "best kya hai",
    "best kya", "sabse accha", "sabse acha", "sabse achha", "sabse best", "sabse zyada bikne wala", "sabse zyada bikta", "sabse jyada bikne wala",
    "sabse popular", "famous product", "famous products", "famous kya hai", "kaunsa best hai", "konsa best hai",
    "saru shu che", "saru su che", "saru shu chhe", "sauthi saru", "sauthi saru shu che", "sauthi saru su che", "shu saru che", "su saru che",
    "sauthi vadhare vechay che", "sauthi vadhu vechay che", "best shu che", "best su che", "shu best che", "su best che", "popular shu che",
    "सबसे अच्छा", "सबसे बढ़िया", "सबसे ज्यादा बिकने वाला", "बेस्ट प्रोडक्ट", "बेस्ट सेलर", "सबसे पॉपुलर", "क्या बेस्ट है",
    "શું સારું છે", "સારું શું છે", "સૌથી સારું", "સૌથી સારું શું છે", "સૌથી વધુ વેચાય છે", "બેસ્ટ પ્રોડક્ટ", "બેસ્ટ સેલર", "બેસ્ટ શું છે",
))
_BESTSELLER_FILLERS = frozenset("the a an your you show me tell what which is are please pls plz do have any all ur to us give list of".split())


def _tokens(text: str) -> list[str]:
    """Words of the message (script-safe)."""
    return indic_vocab.tokenize(text or "")


def _only_emoji(text: str, allowed: frozenset[str]) -> bool:
    """True when the message is nothing but characters of `allowed` (plus joiners / spaces / punctuation) and has at least one."""
    seen = False
    for ch in (text or ""):
        if ch in allowed:
            seen = True
        elif ch not in _EMOJI_JOINERS:
            return False
    return seen


# "ok" / "okay" / "fine" / "hmm" / "theek hai": an acknowledgement with nothing to act on. Whole-message match only.
_ACK_PHRASES: tuple[tuple[str, ...], ...] = tuple(tuple(p.split()) for p in (
    "ok", "okay", "okk", "okkk", "okie", "okey", "k", "kk", "kkk", "fine", "alright", "all right", "cool", "hmm", "hm", "hmmm", "hmmmm", "ok ok",
    "ok thanks", "okay thanks", "ok thank you", "ok fine", "okay fine", "ok cool", "ok great", "ok got it", "got it", "noted", "i see", "i see ok",
    "acha", "accha", "achha", "acha ok", "accha ok", "ok acha", "ok accha", "theek hai", "thik hai", "theek", "thik", "thik che", "theek che", "ok thik",
    "ok theek hai", "samajh gaya", "samajh gayi", "samaj gayo", "samaj gayi", "barabar", "barabar che", "saru", "saru che", "ok saru",
    "ठीक", "ठीक है", "अच्छा", "ओके", "ओके ठीक है", "ओके थैंक्स", "समझ गया", "समझ गई", "हम्म",
    "ઓકે", "ઠીક", "ઠીક છે", "સારું", "સારું છે", "બરાબર", "બરાબર છે", "અચ્છા", "સમજાઈ ગયું", "હમ્મ",
))
# A sentence of praise ("you have such a amazing catalogue", "aapka collection bahut accha hai"): the praise word plus ONLY these.
_PRAISE_SENTENCE_CORE = _REACTION_CORE | frozenset({
    "good", "excellent", "fantastic", "wonderful", "brilliant", "impressive", "attractive", "adorable", "accha", "acha", "achha", "sara", "saru",
    "अच्छा", "अच्छी", "अच्छे", "બહુ", "સારું", "સારો", "સારી",
})
_PRAISE_SENTENCE_FILLER = frozenset(
    "you your yours u ur have has had having such a an the so very too really quite truly is are was were be it its this that these those "
    "i we love like liked loving what how wow oh omg ya yaar yar ji sir mam madam bhai di didi and also but just "
    "catalogue catalog collection collections products product designs design store shop stuff work quality looks look items item range variety "
    "aapka aapki aapke apka apki tumhara tumhari tumhare tamaru tamaro tamari tamara mast bahut bohot bahot khub khoob ghanu ghano ekdam hai hain "
    "che chhe chho kya sab sabhi badha badhu "
    "आपका आपकी आपके तुम्हारा तुम्हारी तुम्हारे कलेक्शन प्रोडक्ट डिज़ाइन है हैं सब बहुत "
    "તમારું તમારો તમારી કલેક્શન પ્રોડક્ટ ડિઝાઇન છે બધું બધા".split()
)
_PRAISE_MAX_TOKENS = 9


def is_ack(text: str) -> bool:
    """A bare acknowledgement — "ok", "okay", "fine", "hmm", "theek hai", "ઓકે" — with nothing to act on."""
    tokens = tuple(_tokens(text))
    return bool(tokens) and "?" not in (text or "") and tokens in _ACK_PHRASES


def is_praise_sentence(text: str) -> bool:
    """
    A compliment written as a sentence: "You have such a amazing catalogue", "aapka collection bahut accha hai", "tamaru collection khub saras che".

    A praise word and nothing else but pronouns / "very" / "collection" / "is" style filler — a request ("show me nice sarees"), a number, a
    question or any product word makes it something for the planner.
    """
    tokens = _tokens(text)
    if not tokens or len(tokens) > _PRAISE_MAX_TOKENS or any(ch in (text or "") for ch in "?؟") or any(t.isdecimal() for t in tokens):
        return False
    if not any(t in _PRAISE_SENTENCE_CORE for t in tokens):
        return False
    return all(t in _PRAISE_SENTENCE_CORE or t in _PRAISE_SENTENCE_FILLER or t in _REACTION_WORDS for t in tokens)


def is_reaction(text: str) -> bool:
    """A compliment / acknowledgement / emoji reaction to what was just shown: "wah", "nice", "wow", "mast", "ok", "so nice 😍", 👍, "you have such a amazing catalogue"."""
    if _only_emoji(text, _REACTION_EMOJI):
        return True
    if is_ack(text) or is_praise_sentence(text):
        return True
    tokens = _tokens(text)
    if not tokens or len(tokens) > 4 or "?" in (text or ""):
        return False
    return all(t in _REACTION_WORDS for t in tokens) and any(t in _REACTION_CORE for t in tokens)


def is_decline(text: str, *, bare_no_ok: bool = False) -> bool:
    """
    "No thanks" / "nahi joiye" / "bas" / "not now": the customer is done — not a thank-you, not an order command.

    A bare "no" counts only when `bare_no_ok` (nothing is open that a "no" would answer).
    """
    tokens = _tokens(text)
    if not tokens or len(tokens) > 5 or "?" in (text or ""):
        return False
    if len(tokens) == 1 and tokens[0] in _BARE_NO:
        return bare_no_ok
    ordered = tuple(tokens)
    return any(ordered == phrase or ordered[: len(phrase)] == phrase and set(ordered[len(phrase):]) <= {"thanks", "thank", "you", "ji", "yaar", "bhai", "hai", "abhi"} for phrase in _DECLINE_PHRASES)


def is_bestseller_request(text: str) -> bool:
    """"best product" / "best seller" / "top selling" / "saru shu che" / "sabse popular kya hai" — asking what the shop sells most."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 8:
        return False
    for phrase in _BESTSELLER_PHRASES:
        n = len(phrase)
        for i in range(len(tokens) - n + 1):
            if tuple(tokens[i:i + n]) == phrase:
                rest = tokens[:i] + tokens[i + n:]
                if all(w in _BESTSELLER_FILLERS or w in {"hai", "che", "chhe", "kya", "shu", "su", "kaun", "kaunsa", "konsa", "ji"} for w in rest):
                    return True
    return False
