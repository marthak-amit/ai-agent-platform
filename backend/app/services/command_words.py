"""
Multilingual command vocabulary for the deterministic (keyword) parts of the order flow.

The slot machine asks short questions ("Confirm?", "Same or different?", "Reply cancel to stop") and
must understand the answer in the language the customer writes — Gujarati script "હા" / "રદ કરો",
Hindi "हाँ" / "रद्द करें", Tamil "ஆம்" / "ரத்து", and so on. Each command lists lower-case entries
(words or phrases) for English, Hinglish, Hindi, Gujarati (both scripts), Marathi, Bengali, Tamil,
Telugu, Kannada, Malayalam, Punjabi, Odia and Urdu.

Matching works on the normalised message (lower-case, punctuation/emoji removed): a phrase matches
anywhere on word boundaries; a single word matches only as a whole word — "ના" never fires inside a
longer word.
"""

from __future__ import annotations

import unicodedata

_ENTRIES: dict[str, list[str]] = {
    "yes": [
        "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "kk", "confirm", "confirmed", "correct", "right",
        "proceed", "done", "go ahead", "haan", "han", "ha", "haa", "haanji", "ji", "theek", "thik", "sahi", "bilkul",
        "zaroor", "pakka", "barabar", "barobar", "sachu", "haji", "ha ji", "haan ji", "ji haan", "hoy", "hoy ho", "chalel",
        "hyan", "avunu", "houdu", "athe", "sheri", "हाँ", "हां", "हा", "जी", "ठीक", "सही", "बिल्कुल", "ज़रूर", "जरूर",
        "पक्का", "हो", "होय", "હા", "હાજી", "બરાબર", "સાચું", "જરૂર", "ચોક્કસ", "ભલે", "হ্যাঁ", "হ্যা", "ঠিক আছে",
        "ஆம்", "சரி", "అవును", "సరే", "ಹೌದು", "ಸರಿ", "അതെ", "ശരി", "ਹਾਂ", "ਠੀਕ ਹੈ", "ହଁ", "ଠିକ୍ ଅଛି", "جی ہاں", "ٹھیک ہے", "جی",
    ],
    "no": [
        "no", "n", "nope", "nah", "not", "nahi", "nahin", "nhi", "na", "naa", "mat", "nako", "nathi", "नहीं", "नही",
        "नाही", "ना", "मत", "नको", "ના", "નહીં", "નહિ", "નથી", "না", "নয়", "இல்லை", "வேண்டாம்", "కాదు", "వద్దు",
        "ಇಲ್ಲ", "ಬೇಡ", "ഇല്ല", "വേണ്ട", "ਨਹੀਂ", "ਨਾ", "ନା", "ନାହିଁ", "نہیں", "نا",
    ],
    "cancel": [
        "cancel", "cancelled", "canceled", "stop", "abort", "quit", "radd", "cancel karo", "cancel kar do", "cancel kara",
        "order cancel", "nahi chahiye", "nahi lena", "nahi karna", "nahi levu", "have nathi levu", "never mind", "nevermind",
        "रद्द", "कैंसल", "रद्द करो", "रद्द करें", "रद्द करा", "कैंसल करो", "कैंसल करें", "रोक दो", "रुक जाओ", "बंद करो",
        "नहीं चाहिए", "नहीं लेना", "मुझे नहीं चाहिए", "રદ", "રદ કરો", "રદ કરવું", "કેન્સલ", "કેન્સલ કરો", "નથી જોઈતું",
        "નથી લેવું", "હવે નથી લેવું", "રહેવા દો", "বাতিল", "বাতিল করুন", "ரத்து", "ரத்து செய்யுங்கள்", "రద్దు",
        "రద్దు చేయండి", "ರದ್ದು", "ರದ್ದು ಮಾಡಿ", "റദ്ദാക്കുക", "റദ്ദാക്കൂ", "ਰੱਦ", "ਰੱਦ ਕਰੋ", "ବାତିଲ", "ବାତିଲ କରନ୍ତୁ",
        "منسوخ", "منسوخ کریں", "کینسل",
    ],
    "continue": [
        "continue", "proceed", "keep", "resume", "carry on", "go on", "chalu", "chalu rakho", "jari rakho",
        "continue karo", "जारी", "जारी रखें", "जारी रखो", "चालू", "चालू रखो", "जारी ठेवा", "ચાલુ", "ચાલુ રાખો",
        "જારી રાખો", "চালিয়ে যান", "தொடரவும்", "కొనసాగించండి", "ಮುಂದುವರಿಸಿ", "തുടരുക", "ਜਾਰੀ ਰੱਖੋ", "ଜାରି ରଖନ୍ତୁ", "جاری رکھیں",
    ],
    "switch": [
        "switch", "new", "naya", "nayi", "navo", "navu", "new order", "स्विच", "नया", "नया ऑर्डर", "नवीन", "સ્વિચ",
        "નવો", "નવો ઓર્ડર", "નવું", "নতুন", "புதிய", "కొత్త", "ಹೊಸ", "പുതിയ", "ਨਵਾਂ", "ନୂଆ", "نیا",
    ],
    "same": [
        "same", "same for all", "all same", "sab same", "sab ek jaise", "ek jaise", "ek jaisa", "sarkha", "sarkhu",
        "badha sarkha", "ek j", "एक जैसे", "एक जैसा", "सब एक जैसे", "सेम", "सारखे", "सर्व सारखे", "સરખા", "સરખું",
        "બધા સરખા", "એક જ", "સેમ", "একই", "ஒரே மாதிரி", "ఒకేలా", "ಒಂದೇ", "ഒരേ", "ਇੱਕੋ ਜਿਹੇ", "ଏକା", "ایک جیسے", "سب ایک جیسے",
    ],
    "different": [
        "different", "different for each", "alag", "alag alag", "juda", "juda juda", "judu", "अलग", "अलग-अलग",
        "अलग अलग", "वेगळे", "वेगवेगळे", "અલગ", "અલગ અલગ", "જુદા", "જુદા જુદા", "আলাদা", "வெவ்வேறு", "వేర్వేరు",
        "ವಿಭಿನ್ನ", "വ്യത്യസ്ത", "ਵੱਖਰੇ", "ଅଲଗା", "الگ", "الگ الگ",
    ],
    "change": [
        "change", "edit", "update", "modify", "badlo", "badal", "badalna", "badalo", "बदलें", "बदलो", "बदलना",
        "बदल दो", "बदला", "બદલો", "બદલવું", "পরিবর্তন", "மாற்று", "మార్చు", "ಬದಲಿಸಿ", "മാറ്റുക", "ਬਦਲੋ", "ବଦଳାନ୍ତୁ", "تبدیل",
    ],
    "pay_cod": [
        "cod", "cash", "cash on delivery", "pay on delivery", "cash on", "delivery pe payment", "delivery par payment",
        "कैश", "कॅश", "कैश ऑन डिलीवरी", "कॅश ऑन डिलिव्हरी", "डिलीवरी पर", "डिलीवरी पर भुगतान", "नकद", "નગદ", "કેશ",
        "કેશ ઓન ડિલિવરી", "ડિલિવરી પર", "ડિલિવરી પર ચુકવણી", "રોકડા", "রোকড়", "ক্যাশ", "ক্যাশ অন ডেলিভারি", "নগদ",
        "கேஷ்", "ரொக்கம்", "క్యాష్", "నగదు", "ಕ್ಯಾಶ್", "ನಗದು", "ക്യാഷ്", "പണം", "ਕੈਸ਼", "ਨਕਦ", "କ୍ୟାସ", "ନଗଦ", "کیش", "نقد",
    ],
    "pay_upi": [
        "upi", "gpay", "g pay", "google pay", "paytm", "phonepe", "phonepay", "phone pe", "bhim", "online", "online payment",
        "यूपीआई", "ऑनलाइन", "ऑनलाइन पेमेंट", "યુપીઆઈ", "ઓનલાઇન", "ઓનલાઈન", "ઓનલાઇન પેમેન્ટ", "ইউপিআই", "অনলাইন", "யுபிஐ",
        "ஆன்லைன்", "యూపీఐ", "ఆన్‌లైన్", "ಯುಪಿಐ", "ಆನ್‌ಲೈನ್", "യുപിഐ", "ഓൺലൈൻ", "ਯੂਪੀਆਈ", "ਆਨਲਾਈਨ", "ୟୁପିଆଇ", "ଅନଲାଇନ୍", "یو پی آئی",
        "آن لائن",
    ],
    "greeting": [
        "hi", "hii", "hiii", "hello", "hey", "heyy", "helo", "hlo", "hola", "good morning", "good afternoon", "good evening",
        "namaste", "namaskar", "namaskaram", "namaskte", "namaskara", "salam", "assalam", "assalamualaikum", "kem cho",
        "sat sri akal", "sasriakal", "vanakkam", "नमस्ते", "नमस्कार", "हेलो", "हैलो", "हाय", "सलाम", "કેમ છો", "નમસ્તે",
        "નમસ્કાર", "હેલો", "હાય", "নমস্কার", "হ্যালো", "வணக்கம்", "నమస్కారం", "హలో", "ನಮಸ್ಕಾರ", "ಹಲೋ", "നമസ്കാരം",
        "ഹലോ", "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ", "ਹੈਲੋ", "ନମସ୍କାର", "ହେଲୋ", "السلام علیکم", "آداب", "ہیلو", "سلام",
    ],
    "thanks": [
        "thanks", "thank you", "thank you so much", "thanks a lot", "thx", "ty", "shukriya", "dhanyavad", "dhanyavaad",
        "aabhar", "abhar", "dhanyawad", "dhanyavad ahe", "aabhari ahe", "धन्यवाद", "शुक्रिया", "आभार", "आभारी आहे", "आभारी", "थैंक्स", "थैंक यू", "थँक्स", "थँक यू",
        "થેન્ક્સ", "થેન્ક યુ", "ধন্যবাদ", "நன்றி", "આભાર", "ધન્યવાદ", "ધન્યવાદ", "આભારી", "ধন্যবাদ", "நன்றி",
        "ధన్యవాదాలు", "ಧನ್ಯವಾದಗಳು", "നന്ദി", "ਧੰਨਵਾਦ", "ଧନ୍ୟବାଦ", "شکریہ",
    ],
    "bye": ["bye", "goodbye", "alvida", "ok bye", "see you", "बाय", "अलविदा", "બાય", "આવજો", "বিদায়", "பிறகு பார்க்கலாம்"],
    "order_status": [
        "order status", "track my order", "track order", "where is my order", "my order status", "parcel kidhar hai",
        "order kab aayega", "kab aayega", "ऑर्डर स्टेटस", "ऑर्डर की स्थिति", "मेरा ऑर्डर कहाँ है", "पार्सल कब आएगा",
        "ऑर्डर कब आएगा", "ઓર્ડર સ્ટેટસ", "ઓર્ડરની સ્થિતિ", "મારો ઓર્ડર ક્યાં છે", "પાર્સલ ક્યારે આવશે",
        "ઓર્ડર ક્યારે આવશે", "माझी ऑर्डर कुठे आहे", "আমার অর্ডার কোথায়", "என் ஆர்டர் எங்கே", "నా ఆర్డర్ ఎక్కడ",
        "ನನ್ನ ಆರ್ಡರ್ ಎಲ್ಲಿ", "എന്റെ ഓർഡർ എവിടെ", "ਮੇਰਾ ਆਰਡਰ ਕਿੱਥੇ", "ମୋ ଅର୍ଡର", "میرا آرڈر کہاں",
    ],
}

def normalize(text: str) -> str:
    """
    Lower-case, drop punctuation/emoji, collapse spaces ('રદ કરો!' → 'રદ કરો', 'अलग-अलग' → 'अलग अलग').

    Indic vowel signs and viramas are combining marks (category M*): they must be KEPT — a regex `\\w`
    would delete them and turn "નાની" into "ન ન".
    """
    chars = [
        ch if (ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith("M")) else " "
        for ch in (text or "").lower()
    ]
    return " ".join("".join(chars).split())


WORDS: dict[str, frozenset[str]] = {}
PHRASES: dict[str, tuple[str, ...]] = {}
for _command, _entries in _ENTRIES.items():
    _norm = {normalize(e) for e in _entries}
    WORDS[_command] = frozenset(e for e in _norm if " " not in e)
    PHRASES[_command] = tuple(sorted((e for e in _norm if " " in e), key=len, reverse=True))


def matches(command: str, text: str) -> bool:
    """
    True when `text` expresses `command` ('yes', 'no', 'cancel', 'continue', 'switch', 'same',
    'different', 'change', 'order_status').

    A phrase matches anywhere on word boundaries; a single word only as a whole word of the message.
    """
    norm = normalize(text)
    if not norm:
        return False
    padded = f" {norm} "
    if any(f" {phrase} " in padded for phrase in PHRASES[command]):
        return True
    return any(word in WORDS[command] for word in norm.split())


def matches_whole(command: str, text: str) -> bool:
    """True when the ENTIRE normalised message is one of the command's words/phrases ('હા' yes; 'hi, kurti?' no)."""
    norm = normalize(text)
    return bool(norm) and (norm in WORDS[command] or norm in PHRASES[command])


#: Words that may follow a greeting without adding a request: "hello buddy", "hi bro", "hey there", "hello ji", "good morning sir".
GREETING_FILLER = frozenset("""
buddy bro bruh broo dear there ji jii sir sirji madam mam maam ma'am team guys guy everyone everybody all friend friends frnd frnds
bhai bhaiya bhaiyya bhaisaab yaar yar yaara dost boss mate sis sister didi dida ben behen behan bhabhi anna akka saheb sahab saab
shop store seller admin there's folks people hii hiii hello hey hi helo hlo namaste namaskar welcome again
""".split())
_MAX_GREETING_TAIL = 3


def is_greeting_message(text: str) -> bool:
    """
    True when the message is a greeting plus nothing but filler: "hello buddy", "hi bro", "hey there", "hello ji", "good morning sir",
    "namaste didi". The whole message counts (``matches_whole``) or a greeting word / phrase followed by up to three filler words; any
    other word ("hi kurti", "hello price?") makes it a request, not a greeting.
    """
    norm = normalize(text)
    if not norm:
        return False
    if matches_whole("greeting", norm):
        return True
    words = norm.split()
    for size in (3, 2, 1):                                   # the longest greeting phrase first ("good morning")
        head = " ".join(words[:size])
        if len(words) > size and (head in WORDS["greeting"] or head in PHRASES["greeting"]):
            tail = words[size:]
            return len(tail) <= _MAX_GREETING_TAIL and all(w in GREETING_FILLER or w in WORDS["greeting"] for w in tail)
    return False


def strip_leading_greeting(text: str) -> str:
    """`text` without a greeting at its start ("hi I want kurti" → "I want kurti"); the text unchanged when it is not greeting + request."""
    words = (text or "").split()
    for size in (3, 2, 1):
        head = normalize(" ".join(words[:size]))
        if len(words) > size and (head in WORDS["greeting"] or head in PHRASES["greeting"]):
            return " ".join(words[size:])
    return text


def is_smalltalk_only(text: str) -> bool:
    """True when the whole message is only a greeting / thanks / goodbye / yes / no — no product claim in it."""
    return any(matches_whole(c, text) for c in ("greeting", "thanks", "bye", "yes", "no"))


# The unmistakable "cancel" verbs/nouns. They count in a statement of any length ("ऑर्डर कैंसल कर दो",
# "please cancel my order") but not in a question ("when can I cancel?").
_STRONG_CANCEL = frozenset({
    "cancel", "cancelled", "canceled", "radd", "रद्द", "कैंसल", "रद", "રદ", "કેન્સલ", "বাতিল", "ரத்து", "రద్దు", "ರದ್ದು",
    "റദ്ദാക്കുക", "റദ്ദാക്കൂ", "ਰੱਦ", "ବାତିଲ", "منسوخ", "کینسل",
})
_QUESTION_MARKS = ("?", "؟")


def is_cancel(text: str) -> bool:
    """
    The customer wants to stop / cancel the order.

    A cancel phrase anywhere counts ("order cancel kar do", "મારે હવે નથી લેવું"); a strong cancel word counts in
    any statement but not in a question ("when can I cancel?"); a weaker word ("stop", "quit") only in a short
    message.
    """
    norm = normalize(text)
    if not norm:
        return False
    padded = f" {norm} "
    if any(f" {phrase} " in padded for phrase in PHRASES["cancel"]):
        return True
    words = norm.split()
    if any(w in _STRONG_CANCEL for w in words):
        return not any(mark in (text or "") for mark in _QUESTION_MARKS)
    return len(words) <= 3 and any(word in WORDS["cancel"] for word in words)


def is_no(text: str) -> bool:
    """Negative reply (a plain no, or any cancel wording)."""
    return matches("no", text) or is_cancel(text)


def is_yes(text: str) -> bool:
    """
    Affirmative reply — false when the same message also says no/cancel ('nahi, ok', 'રદ કરો').

    An answer is short: a yes word inside a long sentence ("kitne ka hai ji?") is not a confirmation.
    """
    return len(normalize(text).split()) <= 4 and matches("yes", text) and not is_no(text)
