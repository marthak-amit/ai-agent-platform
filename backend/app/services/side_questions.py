"""
Side questions and social messages in the middle of a conversation — answered, then the open step resumes.

A customer who is picking a colour will sometimes ask "ketla diwas ma return kari sakai?", "COD available?",
"delivery charge?", say "thank you", "nice", "I'll think about it" or "bye". None of these answers the open
question, and none of them should (a) be mistaken for a product search, (b) be ignored by re-asking the same
question, or (c) be answered with a made-up policy. This module is the one deterministic place that handles them,
for the LLM-router shops and the keyword (legacy) shops alike, at ₹0 and in every supported language:

  detect_topic()          message -> topic (or None) from multilingual keyword lexicons
  build_answer()          topic -> reply text, from the shop's own facts only
  run_side_question_guard()   the pipeline hook: answer + re-ask the open step, never touching order state

Facts come from the Client row (COD / UPI / delivery days / address) or the seller's approved knowledge-base
entries. When the shop has not told us (returns, warranty, delivery charges, ...) the reply says so honestly and
offers the human hand-off — a policy is never invented.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services import command_words as cw
from app.services import order_recovery
from app.services.language_templates import get_template

logger = logging.getLogger(__name__)

# ── lexicons ─────────────────────────────────────────────────────────────────
# Lower-case words / phrases in English, Hinglish, Gujarati (both scripts), Hindi, Marathi, Bengali, Tamil, Telugu,
# Kannada, Malayalam, Punjabi, Odia and Urdu. A single word matches as a whole word, a phrase on word boundaries.

_LEXICON: dict[str, tuple[str, ...]] = {
    "returns": (
        "return", "returns", "returned", "returning", "refund", "refunds", "exchange", "exchanges", "replace", "replacement",
        "money back", "return policy", "refund policy", "exchange policy", "policy", "paisa wapas", "paise wapas",
        "paisa pacha", "paisa pachha", "paisa pacho", "wapas kar", "wapas ho", "vapas kar", "vapas ho", "pachu aapi", "pacho aapi",
        "रिटर्न", "रिफंड", "एक्सचेंज", "रिप्लेसमेंट", "पॉलिसी", "पैसे वापस", "पैसा वापस", "वापसी", "वापस कर", "परतावा", "परत करता", "परत करायचे",
        "રિટર્ન", "રિફંડ", "એક્સચેન્જ", "એક્સચેંજ", "રિપ્લેસમેન્ટ", "પોલિસી", "પૈસા પાછા", "પાછું આપ", "પાછુ આપ", "પરત",
        "ফেরত", "রিটার্ন", "রিফান্ড", "এক্সচেঞ্জ", "পলিসি",
        "ரிட்டர்ன்", "ரீஃபண்ட்", "திரும்ப", "மாற்று", "பாலிசி",
        "రిటర్న్", "రీఫండ్", "ఎక్స్చేంజ్", "పాలసీ", "తిరిగి ఇవ్వ",
        "ರಿಟರ್ನ್", "ರಿಫಂಡ್", "ಎಕ್ಸ್‌ಚೇಂಜ್", "ಪಾಲಿಸಿ", "ಮರಳಿ",
        "റിട്ടേൺ", "റീഫണ്ട്", "എക്സ്ചേഞ്ച്", "പോളിസി", "തിരികെ",
        "ਰਿਟਰਨ", "ਰਿਫੰਡ", "ਐਕਸਚੇਂਜ", "ਪਾਲਿਸੀ", "ਵਾਪਸ ਕ",
        "ରିଟର୍ନ", "ରିଫଣ୍ଡ", "ଏକ୍ସଚେଞ୍ଜ", "ପଲିସି", "ଫେରସ୍ତ",
        "واپسی", "ریٹرن", "ریفنڈ", "ایکسچینج", "پالیسی", "واپس کر",
    ),
    "cod": (
        "cod", "cash on delivery", "pay on delivery", "pay after delivery", "payment on delivery", "cash payment", "cash",
        "delivery par payment", "delivery pe payment", "delivery par paisa", "delivery vakhte payment", "ghar pe paisa",
        "cash on delivery available", "kaash on delivery", "कैश ऑन डिलीवरी", "कॅश ऑन डिलिव्हरी", "कॅश ऑन डिलीवरी", "सीओडी", "कैश", "डिलीवरी पर पेमेंट", "डिलीवरी पर पैसे", "घर पर पैसे",
        "કેશ ઓન ડિલિવરી", "સીઓડી", "કેશ", "ડિલિવરી વખતે પેમેન્ટ", "ડિલિવરી પર પૈસા", "ઘરે પૈસા",
        "ক্যাশ অন ডেলিভারি", "கேஷ் ஆன் டெலிவரி", "క్యాష్ ఆన్ డెలివరీ", "ಕ್ಯಾಶ್ ಆನ್ ಡೆಲಿವರಿ", "ക്യാഷ് ഓൺ ഡെലിവറി",
        "ਕੈਸ਼ ਆਨ ਡਿਲੀਵਰੀ", "କ୍ୟାସ ଅନ ଡେଲିଭରି", "کیش آن ڈیلیوری",
    ),
    "delivery_charge": (
        "delivery charge", "delivery charges", "delivery fee", "delivery fees", "delivery cost", "shipping charge", "shipping charges",
        "shipping cost", "shipping fee", "courier charge", "courier charges", "free delivery", "free shipping", "delivery free",
        "delivery ka charge", "delivery nu charge", "delivery charge che", "delivery kitna", "delivery ketla", "delivery ke paise",
        "डिलीवरी चार्ज", "शिपिंग चार्ज", "डिलीवरी फ्री", "डिलीवरी फ्री है", "कूरियर चार्ज", "डिलीवरी का खर्च",
        "ડિલિવરી ચાર્જ", "શિપિંગ ચાર્જ", "ડિલિવરી ફ્રી", "કુરિયર ચાર્જ", "ડિલિવરીનો ખર્ચ",
        "ডেলিভারি চার্জ", "டெலிவரி கட்டணம்", "డెలివరీ ఛార్జ్", "ಡೆಲಿವರಿ ಶುಲ್ಕ", "ഡെലിവറി ചാർജ്", "ਡਿਲੀਵਰੀ ਚਾਰਜ",
        "ଡେଲିଭରି ଚାର୍ଜ", "ڈیلیوری چارج",
    ),
    "delivery_area": (
        "deliver to", "delivery to", "shipping to", "do you deliver", "deliver in", "delivery in my city", "pincode", "pin code",
        "deliver karte", "deliver karo", "deliver kare", "mara gam", "mere shehar", "mere city", "मेरे शहर", "पिनकोड", "डिलीवर करते",
        "પિનકોડ", "મારા શહેર", "ડિલિવર કરો", "ડિલિવર કરો છો", "ডেলিভারি করেন", "டெலிவரி செய்வீர்களா", "డెలివరీ చేస్తారా",
        "ಡೆಲಿವರಿ ಮಾಡುತ್ತೀರಾ", "ഡെലിവറി ചെയ്യുമോ", "ਡਿਲੀਵਰ ਕਰਦੇ", "ଡେଲିଭରି କରନ୍ତି", "ڈیلیور کرتے",
    ),
    "delivery_time": (
        "delivery time", "how long", "how many days", "how much time", "how soon", "when will it", "when will i get", "when will it arrive",
        "when will it come", "by when", "dispatch", "shipping time", "delivery period", "arrive", "kitne din", "kitna time", "kitna samay",
        "kab tak", "kab milega", "kab aayega", "kab pahunchega", "delivery kab", "ketla diwas", "ketla divas", "ketla din", "kyare malshe",
        "kyare avshe", "kyare pahochshe", "kyare pohchshe", "delivery kyare", "ketlo samay", "kitne dino", "कितने दिन", "कितना समय", "कब तक",
        "कब मिलेगा", "कब आएगा", "कब पहुंचेगा", "डिलीवरी कब", "किती दिवस", "केव्हा मिळेल", "कधी मिळेल", "kiti divas", "kevha milel", "kadhi milel",
        "કેટલા દિવસ", "કેટલો સમય", "ક્યારે મળશે", "ક્યારે આવશે", "ક્યારે પહોંચશે", "ડિલિવરી ક્યારે",
        "কত দিন", "কতদিন", "কবে পাব", "কবে পাবো", "எத்தனை நாள்", "எப்போது கிடைக்கும்", "எப்போது வரும்",
        "ఎన్ని రోజులు", "ఎప్పుడు వస్తుంది", "ఎప్పుడు వచ్చేస్తుంది", "ಎಷ್ಟು ದಿನ", "ಯಾವಾಗ ಬರುತ್ತದೆ", "ಯಾವಾಗ ಸಿಗುತ್ತದೆ",
        "എത്ര ദിവസം", "എപ്പോൾ കിട്ടും", "എപ്പോൾ എത്തും", "ਕਿੰਨੇ ਦਿਨ", "ਕਦੋਂ ਮਿਲੇਗਾ", "ਕਦੋਂ ਆਵੇਗਾ", "କେତେ ଦିନ", "କେବେ ମିଳିବ",
        "کتنے دن", "کب ملے گا", "کب آئے گا", "کتنا وقت",
    ),
    "payment_methods": (
        "pay later", "pay in advance", "advance payment", "partial payment", "part payment", "installment", "installments", "udhaar", "payment options", "payment option", "payment method", "payment methods", "how to pay", "how can i pay", "how do i pay",
        "which payment", "accept card", "card payment", "credit card", "debit card", "net banking", "netbanking", "emi", "gpay", "phonepe",
        "paytm", "payment kaise", "paise kaise", "paisa kaise", "payment kevi rite", "payment kem", "pament kaise", "पेमेंट कैसे",
        "पैसे कैसे", "भुगतान कैसे", "पेमेंट ऑप्शन", "પેમેન્ટ કેવી રીતે", "પૈસા કેવી રીતે", "પેમેન્ટ ઓપ્શન", "કાર્ડ", "પેમેન્ટ કઈ રીતે",
        "পেমেন্ট কীভাবে", "பணம் எப்படி", "పేమెంట్ ఎలా", "ಪೇಮೆಂಟ್ ಹೇಗೆ", "പേയ്മെന്റ് എങ്ങനെ", "ਪੇਮੈਂਟ ਕਿਵੇਂ", "ପେମେଣ୍ଟ କିପରି", "ادائیگی کیسے",
    ),
    "warranty": (
        "warranty", "guarantee", "guaranty", "वारंटी", "गारंटी", "વોરંટી", "ગેરંટી", "ওয়ারেন্টি", "வாரண்டி", "వారంటీ", "ವಾರಂಟಿ",
        "വാറന്റി", "ਵਾਰੰਟੀ", "ୱାରେଣ୍ଟି", "وارنٹی",
    ),
    "quality": (
        "genuine", "authentic", "original", "asli", "quality", "असली", "क्वालिटी", "गुणवत्ता", "ओरिजिनल",
        "ક્વોલિટી", "ગુણવત્તા", "અસલી", "ઓરિજિનલ", "ক্বালিটি", "মান", "தரம்", "నాణ్యత", "ಗುಣಮಟ್ಟ", "ഗുണനിലവാരം", "ਕੁਆਲਿਟੀ", "ଗୁଣବତ୍ତା", "معیار",
    ),
    "location": (
        "your address", "shop address", "store address", "shop location", "where are you", "where is your shop", "where is the shop",
        "location", "your shop", "store", "visit", "dukan kaha", "dukan kahan", "shop kahan", "address kya hai", "dukan kyan", "દુકાન ક્યાં",
        "દુકાન ક્યાં આવેલી", "દુકાનનું સરનામું", "તમારું સરનામું", "दुकान कहाँ", "दुकान का पता", "आपका पता", "दुकान कुठे", "দোকান কোথায়",
        "கடை எங்கே", "షాప్ ఎక్కడ", "ಅಂಗಡಿ ಎಲ್ಲಿ", "കട എവിടെ", "ਦੁਕਾਨ ਕਿੱਥੇ", "ଦୋକାନ କେଉଁଠି", "دکان کہاں",
    ),
    "contact": (
        "contact number", "phone number", "your number", "mobile number of shop", "call you", "can i call", "number do", "number aapo",
        "number dena", "whatsapp number", "customer care", "helpline", "संपर्क नंबर", "फोन नंबर", "आपका नंबर", "नंबर दो", "कस्टमर केयर",
        "સંપર્ક નંબર", "ફોન નંબર", "તમારો નંબર", "નંબર આપો", "કસ્ટમર કેર",
    ),
    "size_guide": (
        "size chart", "size guide", "which size", "what size", "measurement", "measurements", "fitting", "bust", "waist", "kaun sa size",
        "kaunsa size", "kyo size", "kayu size", "kai size", "साइज़ चार्ट", "साइज चार्ट", "कौन सा साइज", "नाप", "માપ", "સાઇઝ ચાર્ટ", "કયું સાઇઝ", "કઈ સાઇઝ",
    ),
    "bulk": (
        "bulk", "wholesale", "bulk order", "reseller", "resell", "dealer", "in quantity", "thok", "थोक", "બલ્ક", "જથ્થાબંધ", "હોલસેલ", "होलसेल",
    ),
    "discount": (
        "discount", "discounts", "any offer", "any offers", "koi offer", "offer hai", "special offer", "festival offer", "cheaper", "less price", "reduce price", "best price", "last price", "final price",
        "bargain", "negotiate", "too expensive", "expensive", "costly", "price kam", "kam karo", "thoda kam", "kam kar do", "mehenga",
        "mahenga", "mangu", "moghu", "ghanu mangu", "ochu karo", "ochhu karo", "छूट", "डिस्काउंट", "कोई ऑफर", "ऑफर है", "महंगा", "कम करो", "थोड़ा कम", "कीमत कम",
        "ડિસ્કાઉન્ટ", "કોઈ ઓફર", "ઓફર છે", "મોંઘું", "મોંઘુ", "ઓછું કરો", "ઓછુ કરો", "થોડું ઓછું", "ભાવ ઓછો", "છૂટ", "ছাড়", "ডিসকাউন্ট", "தள்ளுபடி", "తగ్గింపు",
        "ರಿಯಾಯಿತಿ", "ഡിസ്കൗണ്ട്", "ਛੋਟ", "ରିହାତି", "رعایت", "ڈسکاؤنٹ",
    ),
}

_LEXICON.update({
    "human": (
        "talk to a human", "talk to human", "talk to someone", "talk to a person", "talk to person", "speak to someone", "speak to a human",
        "speak to a person", "speak to human", "human please", "real person", "call me", "call me back", "please call", "contact me",
        "customer support", "owner", "manager", "kisi se baat", "insaan se baat", "aadmi se baat", "malik se baat", "call karo", "call kar do",
        "koi sathe vat", "manas sathe vat", "malik sathe vat", "call karjo", "मुझे कॉल करो", "कॉल करो", "किसी से बात", "इंसान से बात", "मालिक से बात",
        "કોઈ સાથે વાત", "માણસ સાથે વાત", "માલિક સાથે વાત", "કોલ કરો", "મને કોલ કરો", "কথা বলতে চাই", "பேச வேண்டும்", "మాట్లాడాలి", "ಮಾತನಾಡಬೇಕು",
        "സംസാരിക്കണം", "ਗੱਲ ਕਰਨੀ", "କଥା ହେବାକୁ", "بات کرنی",
    ),
    "change_product": (
        "something else", "show me something else", "show something else", "other product", "other products", "different product",
        "another product", "change product", "change the product", "change item", "another one", "other designs", "more designs",
        "kuch aur", "kuch aur dikhao", "doosra product", "dusra product", "dusra dikhao", "biju batavo", "bijo product", "bija products",
        "biju kai", "baki kai chhe", "product badlo", "product badal", "दूसरा प्रोडक्ट", "कुछ और दिखाओ", "कुछ और", "प्रोडक्ट बदल", "और डिज़ाइन", "બીજું બતાવો",
        "બીજુ બતાવો", "બીજો પ્રોડક્ટ", "પ્રોડક્ટ બદલ", "બીજી ડિઝાઇન", "બીજું કંઈક",
        # "show me more like that": the next items of the same search (router_actions.handle_more_like_this)
        "more products", "more like", "some more", "show more", "show me more", "more options", "more items", "more of these",
        "similar products", "similar items", "similar ones", "similar to this", "similar to that", "like that one", "like this one",
        "aur dikhao", "aur batao", "aur dikhana", "aur options", "aur products", "dusre dikhao", "doosre dikhao", "biju batavo",
        "bija batavo", "और दिखाओ", "और बताओ", "और प्रोडक्ट", "બીજા બતાવો", "બીજું જોઈએ",
    ),
    "trust": (
        "is it safe", "is this safe", "is it secure", "legit", "is it genuine shop", "trusted", "trustworthy", "fraud", "scam", "fake", "cheat",
        "cheating", "bharosa", "bharosa kar", "vishwas", "भरोसा", "विश्वास", "धोखा", "ભરોસો", "વિશ્વાસ", "છેતરપિંડી", "ঠিক আছে তো", "நம்பலாமா",
        "నమ్మవచ్చా", "ನಂಬಬಹುದೇ", "വിശ്വസിക്കാമോ", "ਭਰੋਸਾ", "ବିଶ୍ୱାସ", "بھروسہ",
    ),
    "decline": (
        "no thanks", "no thank you", "not interested", "dont want", "don t want", "do not want", "i dont want it", "i do not want it", "nahi chahiye",
        "nahin chahiye", "nahi lena", "nathi joitu", "nathi levu", "na joiye", "naa joiye", "rehne do", "rahevane do", "bas rehne do", "chodo", "chhodo",
        "नहीं चाहिए", "नहीं लेना", "रहने दो", "छोड़ो", "નથી જોઈતું", "નથી લેવું", "રહેવા દો", "છોડો",
    ),
    "ack": (
        "ok", "okay", "k", "kk", "ok ok", "hmm", "hm", "mm", "mhm", "achha", "accha", "acha", "thik", "thik hai", "theek", "theek hai", "thik che",
        "fine", "sure", "alright", "all right", "got it", "noted", "understood", "ठीक", "ठीक है", "अच्छा", "ઠીક", "ઠીક છે", "સારું", "અચ્છા", "ওকে", "சரி",
        "సరే", "ಸರಿ", "ശരി", "ਠੀਕ ਹੈ", "ଠିକ ଅଛି", "ٹھیک ہے",
    ),
})
_COMPILED_EXTRA_TOPICS = ("human", "change_product", "trust", "decline", "ack")

_POSITIVE_WORDS = frozenset(cw.normalize(w) for w in (
    "good", "nice", "great", "perfect", "awesome", "wow", "beautiful", "lovely", "superb", "excellent", "amazing", "cute", "pretty",
    "sundar", "mast", "saras", "sars", "badhiya", "badiya", "zabardast", "shandar", "sunder",
    "बढ़िया", "बढ़ीया", "सुंदर", "शानदार", "ज़बरदस्त", "जबरदस्त", "मस्त", "સરસ", "મસ્ત", "સુંદર", "ઝક્કાસ",
    "চমৎকার", "সুন্দর", "দারুণ", "அருமை", "நன்றாக", "அழகு", "బాగుంది", "అద్భుతం", "ಚೆನ್ನಾಗಿದೆ", "ಸುಂದರ", "നന്നായിട്ടുണ്ട്", "മനോഹരം",
    "ਵਧੀਆ", "ਸੋਹਣਾ", "ଭଲ", "اچھا", "خوبصورت",
))
_POSITIVE_FILLERS = frozenset(cw.normalize(w) for w in (
    "very", "so", "too", "really", "looks", "look", "it", "its", "is", "this", "that", "design", "product", "photo", "pic", "colour",
    "color", "quality", "hai", "che", "chhe", "ye", "yeh", "aa", "a", "ek", "dam", "ji", "lagi", "lage", "lagyu", "laagyu",
    "bahut", "bahu", "ekdum", "khub", "बहुत", "बहोत", "एकदम", "બહુ", "ખૂબ", "بہت", "ବହୁତ", "ok", "okay", "achha", "acha", "theek", "thik",
    "है", "हैं", "छे", "आहे", "છે", "ہے", "ਹੈ", "ଅଛି", "হয়", "ஆம்", "ಇದೆ", "ഉണ്ട്", "ho", "hoy",
))

_THINK_LATER = tuple(cw.normalize(w) for w in (
    "wait", "wait a moment", "hold on", "one minute", "one sec", "just a minute", "ek minute", "ruko", "ruk jao", "thoda ruko", "thodi var", "thodu ruko", "वेट", "रुको", "रुकिए", "एक मिनट", "ठहरिए", "થોડી વાર", "એક મિનિટ", "રાહ જુઓ", "i will think", "i ll think", "ill think", "let me think", "i will let you know", "i ll let you know", "will let you know", "tell you later",
    "will tell you later", "ask my husband", "ask my wife", "ask my mom", "ask my dad", "ask my family", "ask my friend", "talk to my husband",
    "talk to my wife", "check with my husband", "check with my wife", "not now", "maybe later", "later", "some other time", "baad me", "baad mein",
    "abhi nahi", "abhi nahin", "soch ke", "soch kar", "sochke", "sochkar", "pooch ke", "pooch kar", "puchke", "puch ke", "pachi", "pachhi", "pachi kahish",
    "vichari ne", "vichaari ne", "vichari ne kahish", "ghare puchi", "ghare puchhi", "pachi vat karish", "बाद में", "बाद मे", "अभी नहीं", "सोचकर", "सोच कर",
    "सोच के", "पूछकर", "पूछ कर", "घर पर पूछ", "પછી", "પછી કહીશ", "વિચારીને", "પૂછીને", "ઘરે પૂછી", "હમણાં નહીં", "હમણાં નહિ", "અત્યારે નહીં",
    "નિરાંતે", "পরে", "পরে জানাব", "பிறகு", "பிறகு சொல்கிறேன்", "తర్వాత", "తరువాత చెబుతా", "ನಂತರ", "ನಂತರ ಹೇಳುತ್ತೇನೆ", "പിന്നീട്", "ਬਾਅਦ ਵਿੱਚ", "ପରେ", "بعد میں",
))

_QUESTION_STARTERS = frozenset({
    "how", "what", "why", "when", "where", "which", "who", "do", "does", "is", "are", "can", "will", "could", "would",
    "kya", "kem", "ketla", "kitna", "kitne", "kaisa", "kaise", "kaisi", "shu", "su", "kyare", "kab", "kahan", "kaha", "kai",
    "क्या", "कैसे", "कितने", "कितना", "कब", "कहाँ", "શું", "કેમ", "કેટલા", "કેટલો", "ક્યારે", "ક્યાં", "કેવી", "કઈ", "કયું",
})
_QUESTION_WORDS = frozenset({
    "available", "availability", "possible", "hai", "hain", "che", "chhe", "hoy", "milega", "malse", "malshe", "kya", "shu",
    "how", "which", "what", "kaise", "kevi", "kaun", "kaunsa", "कैसे", "कौन", "કેવી", "કઈ", "કયા", "કેવા", "કેવી રીતે",
    "kai", "option", "facility", "accept", "accepted", "allowed", "milti", "milte", "उपलब्ध", "है", "हैं", "मिलेगा", "ઉપલબ્ધ", "છે",
    "મળશે", "ખરું",
})
# "cod please", "UPI karo": an instruction, not a question.
_ANSWER_WORDS = frozenset(cw.normalize(w) for w in (
    "karo", "kardo", "kar do", "chahiye", "chaiye", "joiye", "joie", "please", "pls", "plz", "select", "choose", "rakho", "lenge", "lena",
    "denge", "dunga", "dungi", "aapish", "apish", "karish", "kariye", "ok", "okay", "chalse", "chalshe", "chale", "chalega", "theek", "sahi",
    "करो", "कर दो", "चाहिए", "रखो", "दूँगा", "दूंगा", "દો", "કરો", "જોઈએ", "રાખો", "આપીશ",
))

_FREE_TEXT_SLOTS = frozenset({"customer_name", "delivery_address", "mobile_number"})
_MAX_TOKENS = 40
_SHORT_SOCIAL_TOKENS = 6


def _compile(entries: tuple[str, ...]) -> tuple[frozenset[str], tuple[str, ...]]:
    """(single words, phrases longest-first) of already-normalised lexicon entries."""
    norm = {cw.normalize(e) for e in entries if cw.normalize(e)}
    return (
        frozenset(e for e in norm if " " not in e),
        tuple(sorted((e for e in norm if " " in e), key=len, reverse=True)),
    )


_COMPILED = {topic: _compile(entries) for topic, entries in _LEXICON.items()}
_THINK_WORDS, _THINK_PHRASES = _compile(_THINK_LATER)


def _hits(topic: str, norm: str, words: set[str]) -> bool:
    """True when the normalised message contains one of the topic's words or phrases."""
    single, phrases = _COMPILED[topic]
    if words & single:
        return True
    padded = f" {norm} "
    return any(f" {phrase} " in padded for phrase in phrases)


def _is_question(text: str, norm: str, words: set[str]) -> bool:
    """A question mark, a question opener, or an availability-style word."""
    if "?" in text or "؟" in text:
        return True
    first = norm.split()[0] if norm else ""
    return first in _QUESTION_STARTERS or bool(words & _QUESTION_WORDS)


_OPTION_SLOTS = frozenset({
    "color", "cart_item_color", "size", "cart_item_size", "material", "cart_item_material", "quantity", "cart_item_qty", "payment_method",
})
# Slots where a bare "no"/"ok" is just a non-answer (not a yes/no question, not an address with a saved-address prompt).
_NON_ANSWER_SLOTS = _OPTION_SLOTS | {"customer_name"}
_EMOJI_ACKS = frozenset("👍🙏🙂😊👌✅😃😄🤝")
_WHY_OPENERS = frozenset({"why", "kyu", "kyun", "kyon", "kisliye", "kem", "shu mate", "क्यों", "क्यूँ", "કેમ", "શા માટે"})
_DETAIL_WORDS = frozenset({
    "name", "address", "number", "phone", "mobile", "details", "info", "naam", "nam", "pata", "sarnamu", "nambar", "नाम", "पता", "नंबर", "નામ", "સરનામું", "નંબર",
})
# Order of the shop questions: most specific first.
_SHOP_TOPICS = ("returns", "delivery_charge", "warranty", "bulk", "size_guide", "location", "contact", "payment_methods",
                "delivery_area", "delivery_time", "trust", "discount", "cod", "quality")
_NEEDS_QUESTION = frozenset({"cod", "quality", "location", "contact", "size_guide", "trust"})


def detect_topic(text: str, *, next_slot: str | None = None, saved_address_prompt: bool = True) -> str | None:
    """
    The side-question / social topic of one customer message, or None when it is something else.

    Topics: human, thanks, bye, positive, think_later, ack, decline, change_product, why_details, returns, cod,
    delivery_charge, delivery_area, delivery_time, payment_methods, warranty, quality, trust, location, contact,
    size_guide, bulk, discount.
    Never fires for cancel wording or an order-status question (their own flows own those), nor for a bare payment
    option ("COD"); at the free-text name/address/phone slots only a clear question (or thanks / goodbye) counts, and
    a bare "no"/"ok" only counts at the colour / size / quantity / name / payment questions (and at the address
    question when no saved address was offered — with one on offer "ok"/"no" are its yes/change answers).
    """
    norm = cw.normalize(text)
    if not norm:
        # An emoji-only reply ("👍") at an open question is an acknowledgement.
        return "ack" if next_slot in _NON_ANSWER_SLOTS and text and any(ch in _EMOJI_ACKS for ch in text) else None
    tokens = norm.split()
    if len(tokens) > _MAX_TOKENS or cw.is_cancel(text) or cw.matches("order_status", text):
        return None
    words = set(tokens)
    ack_like = len(tokens) <= 3 and "?" not in text and _hits("ack", norm, words) and (
        norm in _COMPILED["ack"][1] or words <= (_COMPILED["ack"][0] | _POSITIVE_FILLERS | {"ji", "haan", "han"})
    )
    asking = False if ack_like else _is_question(text, norm, words)      # "theek hai" is a statement, not a question
    free_text_slot = next_slot in _FREE_TEXT_SLOTS
    non_answer_slots = _NON_ANSWER_SLOTS | ({"delivery_address"} if not saved_address_prompt else set())
    padded = f" {norm} "

    # ── asking for a person ──
    if _hits("human", norm, words) and len(tokens) <= 8:
        return "human"

    # ── short social messages ──
    if not asking and len(tokens) <= _SHORT_SOCIAL_TOKENS:
        if next_slot in non_answer_slots and (cw.matches_whole("no", text) or _hits("decline", norm, words)):
            return "decline"      # before "thanks": "no thanks" is a decline
        if cw.matches("bye", text):
            return "bye"
        if cw.matches("thanks", text):
            return "thanks"
        if _THINK_WORDS & words or any(f" {p} " in padded for p in _THINK_PHRASES):
            return "think_later"
        if not free_text_slot and len(tokens) <= 4 and words & _POSITIVE_WORDS and words <= (_POSITIVE_WORDS | _POSITIVE_FILLERS):
            return "positive"
        if next_slot in non_answer_slots:
            if ack_like:
                return "ack"
    if free_text_slot and not asking:
        return None

    # ── "why do you need my name?" ──
    if asking and (words & _WHY_OPENERS or any(f" {o} " in padded for o in _WHY_OPENERS if " " in o)) and words & _DETAIL_WORDS:
        return "why_details"
    if _hits("change_product", norm, words) and not any(w in words for w in ("colour", "color", "size", "quantity", "address")):
        return "change_product"

    # ── shop questions, most specific first ──
    for topic in _SHOP_TOPICS:
        if not _hits(topic, norm, words):
            continue
        if topic in _NEEDS_QUESTION and not asking:
            continue
        if topic in ("cod", "payment_methods") and not asking:
            continue
        if topic in ("cod", "payment_methods") and not ("?" in text or "؟" in text) and words & _ANSWER_WORDS:
            continue
        if topic == "delivery_time" and not asking and len(tokens) > 4:
            continue
        return topic
    return None


# ── answers ──────────────────────────────────────────────────────────────────

_KB_QUERIES: dict[str, tuple[str, ...]] = {
    "returns": ("return policy", "refund policy", "exchange policy", "return", "refund", "exchange"),
    "delivery_charge": ("delivery charges", "shipping charges", "free delivery", "shipping"),
    "delivery_area": ("delivery areas", "shipping areas", "pincode", "deliver"),
    "warranty": ("warranty", "guarantee"),
    "quality": ("quality", "original", "genuine"),
    "contact": ("contact number", "phone number", "customer care"),
    "location": ("shop address", "store location", "address"),
    "size_guide": ("size chart", "size guide", "measurements"),
    "bulk": ("bulk order", "wholesale"),
}
# Topics whose answer is a fact the engine already owns (config), so a KB entry must not contradict it.


async def _kb_answer(db, client, user_text: str, topic: str) -> str | None:
    """The seller's approved knowledge-base answer for this topic, if they wrote one (searches the customer's own words first)."""
    if client is None:
        return None
    from app.services import knowledge_service

    for query in (user_text, *_KB_QUERIES.get(topic, ())):
        try:
            entries = await knowledge_service.search_knowledge(client_id=client.id, query=query, db=db)
        except Exception as exc:  # noqa: BLE001 - a KB outage must never block the reply
            logger.warning("side question KB search failed: %s", exc)
            return None
        if entries:
            try:
                entries[0].usage_count = (entries[0].usage_count or 0) + 1
                await db.commit()
            except Exception:  # noqa: BLE001
                pass
            return entries[0].answer
    return None


async def build_answer(db, client, conv, pinned_product, topic: str, lang: str, user_text: str, info: dict | None = None) -> str | None:
    """
    Reply text for a topic from the shop's own facts (template in `lang`); never an invented policy.

    `info` (optional out-param): ``info["seller_text"] = True`` when the answer is the seller's own knowledge-base wording — free text
    in whatever language they wrote, unlike the templates — so a caller that skips the reply localizer knows to keep it for this one.
    """
    t = lambda key, **kw: get_template(lang, key, **kw)  # noqa: E731 - tiny local shorthand
    if topic == "thanks":
        return t("sq_thanks")
    if topic == "positive":
        return t("sq_positive")
    if topic == "bye":
        return t("sq_bye")
    if topic == "think_later":
        return t("sq_think_later")
    if topic == "discount":
        return t("sq_discount")       # one statement; the caller adds the single follow-up question
    if topic == "discount_repeat":
        return t("sq_discount_repeat")
    if topic == "ack":
        return t("sq_ack")
    if topic == "decline":
        return t("sq_decline_order")
    if topic == "why_details":
        return t("sq_why_details")
    if topic == "change_product":
        from app.services.router_actions import _catalogue_url

        return t("sq_change_product", catalogue_url=_catalogue_url(client))
    if topic == "cod":
        if getattr(client, "accepts_cod", False):
            limit = getattr(client, "cod_limit", None)
            return t("sq_cod_yes_limit", limit=f"{int(limit):,}") if limit else t("sq_cod_yes")
        return t("sq_cod_no")
    if topic == "payment_methods":
        cod, upi = bool(getattr(client, "accepts_cod", False)), bool(getattr(client, "accepts_upi", True))
        return t("sq_pay_upi_cod") if (cod and upi) else t("sq_pay_cod") if cod else t("sq_pay_upi")
    if topic == "delivery_time":
        from app.services.delivery_service import get_delivery_time_str

        return t("sq_delivery_time", delivery_time=get_delivery_time_str(pinned_product, client) or "3–7 business days")
    if topic == "location" and getattr(client, "business_address", None):
        return t("sq_location", address=" ".join(str(client.business_address).split()))
    kb = await _kb_answer(db, client, user_text, topic)
    if kb:
        if info is not None:
            info["seller_text"] = True
        return kb
    if topic == "quality":
        return t("known_fact_quality")
    if topic == "delivery_area":
        from app.services.delivery_service import get_delivery_time_str

        return t("sq_delivery_time", delivery_time=get_delivery_time_str(pinned_product, client) or "3–7 business days")
    return t("rt_handoff_offer")     # returns / warranty / charges / contact / size / bulk: honest, never invented


# ── pipeline hook ────────────────────────────────────────────────────────────

_NO_RESUME = frozenset({"bye", "think_later"})     # don't nag a customer who just said goodbye / "let me think"


async def run_side_question_guard(
    db, conv, client, message, user_text: str, sender_phone: str, wamid: str | None, stored_stage: str,
    language: str, record_usage,
):
    """
    Answer a side question / social message and resume the open step. Returns a PipelineResult, or None to carry on.

    Order state is never touched: the reply is saved as an ordinary assistant message (so the stage machine still
    recognises the re-asked question) and the next customer message continues from exactly where the flow was.
    """
    if getattr(message, "type", "text") != "text" or client is None:
        return None
    # A pending yes/no prompt (switch product, continue/switch interrupt) is recognised from the last assistant
    # message — answering a side question over it would lose that prompt, so those stages keep their own flow.
    if (stored_stage or "").startswith("awaiting_") and stored_stage != "awaiting_final_confirmation":
        return None
    if getattr(conv, "interrupted_sku", None):
        return None
    from app.services import catalogue_service, conversation_flow, customer_service, intent_router, order_pipeline as op, router_actions as ra

    pinned = None
    if conv.pending_product_sku:
        pinned = await catalogue_service.find_product_by_sku(db, client.id, conv.pending_product_sku)
    variant_info: dict = {}
    if pinned is not None:
        try:
            variant_info = await catalogue_service.get_product_variant_info(db, pinned)
        except Exception as exc:  # noqa: BLE001
            logger.error("side question variant_info error: %s", exc)
    in_order = pinned is not None and stored_stage in intent_router.ORDER_SLOT_STAGES
    if conv.channel == "whatsapp" and not getattr(conv, "mobile_number", None):
        conv.mobile_number = sender_phone      # same in-memory fill as the router front door: the sender IS the number
    next_slot = conversation_flow.get_next_required_slot(conv, variant_info) if in_order else None

    # One colour / size / material only: the question is "Only Grey. Would you like Grey? (Yes / No)", so a bare
    # yes / no IS the answer (taken or declined by the order pipeline), not a decline / acknowledgement aside.
    _single_key = {"color": "available_colors", "size": "available_sizes", "material": "available_materials"}.get(next_slot or "")
    if _single_key and len(variant_info.get(_single_key) or []) == 1 and (cw.is_yes(user_text) or cw.is_no(user_text)):
        return None

    profile = None
    if next_slot == "delivery_address":
        try:
            profile = await customer_service.get_customer(db, client_id=client.id, phone=sender_phone)
        except Exception:  # noqa: BLE001
            profile = None
    context_open = bool(in_order or pinned is not None or stored_stage in intent_router.ORDER_ACTIVE_STAGES)
    memo = _fresh_memo(conv)
    if order_recovery.restorable_draft(conv) is not None:
        context_open = True
    topic = None
    if order_recovery.is_confused(user_text) or (order_recovery.is_question_marks_only(user_text) and context_open):
        topic = "confused"      # "I can't get it" / "what are you trying to say" / "?": re-ask simply, never cancel or guess
    if topic is None:
        topic = detect_topic(
            user_text, next_slot=next_slot, saved_address_prompt=bool(profile is not None and getattr(profile, "address", None)),
        )
    if memo and memo.get("topic") in ("discount", "discount_followup"):
        # "please" / a second "give me a discount" right after a discount refusal is that conversation continuing —
        # not a yes to "Would you like to order X?".
        if topic is None and _is_plea(user_text):
            topic = "discount_followup"
        elif topic == "discount":
            topic = "discount_followup"
    # An unmistakable "cancel" while the order is being filled ends the draft — deterministically, so it also works
    # when the LLM is down (the router / classifier would otherwise be the only thing that can cancel it).
    if topic is None and in_order and next_slot and stored_stage == "order_collection" and cw.is_cancel(user_text):
        topic = "cancel"
    if topic is None:
        if memo is not None:
            await _forget_topic(db, conv)       # the conversation moved on: "please" later is not a discount follow-up
        return None
    if topic == "delivery_time" and not in_order and await _has_open_order(db, client, conv, sender_phone):
        return None       # "when will it come?" about an order the customer already has → the order-status flow
    lang = ra.template_language(None, user_text, conv)
    logger.info("SIDE_QUESTION conv=%s topic=%s stage=%s slot=%s", conv.id, topic, stored_stage, next_slot)

    in_summary = pinned is not None and stored_stage == "awaiting_final_confirmation"
    resume_slot = in_order and bool(next_slot) and topic not in _NO_RESUME
    if profile is None and (resume_slot or in_summary) and topic not in _NO_RESUME:
        try:
            profile = await customer_service.get_customer(db, client_id=client.id, phone=sender_phone)
        except Exception:  # noqa: BLE001
            profile = None
    rc = ra.RouterCtx(
        db=db, conv=conv, client=client, message=message, user_text=user_text, sender_phone=sender_phone, wamid=wamid,
        stage=stored_stage, language=language, record_usage=record_usage, ctx=None, all_products=[], pinned_product=pinned,
        variant_info=variant_info, next_slot=next_slot, profile=profile,
    )
    if topic == "human":
        return (await ra.handle_handoff(rc, None)).early_result
    if topic == "cancel":
        return (await ra.handle_cancel_order(rc, None)).early_result
    if topic == "confused":
        return (await ra.handle_confused(rc, None)).early_result
    if topic == "change_product" and not in_order and stored_stage not in intent_router.ORDER_ACTIVE_STAGES:
        # "another one?" / "aur dikhao" / "show me some more like that": the next items of the same search.
        return (await ra.handle_more_like_this(rc, lang)).early_result

    followup = topic == "discount_followup"
    answer = await build_answer(db, client, conv, pinned, "discount_repeat" if followup else topic, lang, user_text)
    if not answer:
        return None
    await ra._clear_menu(db, conv)      # an open "which one? 1/2/3" menu is closed by any unrelated question
    route = f"side_{topic}"
    if topic in ("discount", "discount_followup"):
        await _remember_topic(db, conv, topic, (memo or {}).get("n", 0) + 1 if memo else 1)
    elif memo is not None:
        await _forget_topic(db, conv)
    if followup and not resume_slot and not in_summary:
        # Fixed price said once more, politely — and the useful next step: similar items that cost less.
        reference = pinned or await ra._reference_product(rc, await _catalogue(db, client))
        return (await ra.handle_more_like_this(
            rc, lang, cheaper_than=(reference.price if reference is not None else None) or 0,
            header_key="rt_more_cheaper_header", none_key="rt_more_cheaper_none", prefix_text=answer, route=route,
        )).early_result
    if resume_slot:
        outcome = await ra._reask(rc, lang, None, route, prefix_text=answer)
    elif in_summary and topic not in _NO_RESUME:
        summary = ""
        try:
            summary = await op._render_order_reply(
                action="show_summary", conv=conv, db=db, client=client, next_slot=None, variant_info=variant_info,
                customer_profile=profile, available_stock=None, declined_saved_address=False, lang=lang,
            )
        except Exception as exc:  # noqa: BLE001 - the answer alone is still a good reply
            logger.warning("side question: could not re-render the summary conv=%s: %s", conv.id, exc)
        text = f"{answer}\n\n{summary}" if summary else answer
        result = await ra._send_with_buttons(db, conv, client, stored_stage, text, None, pinned, variant_info) if summary else None
        outcome = await rc.reply(text, route, lang, result=result)
    else:
        text = answer
        if pinned is not None and topic not in _NO_RESUME and stored_stage not in intent_router.ORDER_SLOT_STAGES and _offer_open(conv):
            text = f"{answer}\n\n{get_template(lang, 'would_you_like_to_order_named', name=pinned.name, sku=pinned.sku)}"
        outcome = await rc.reply(text, route, lang)
    return outcome.early_result


# ── discount follow-ups ("please" after a refusal) ───────────────────────────

_FOLLOWUP_WINDOW = timedelta(minutes=10)
_PLEA_WORDS = frozenset(cw.normalize(w) for w in (
    "please", "pls", "plz", "pleaz", "pleasee", "pleaseee", "pleas", "kindly", "request", "kripya", "meherbani", "mehrbani", "prarthna",
    "plis", "प्लीज", "प्लीज़", "कृपया", "मेहरबानी", "પ્લીઝ", "કૃપા", "મહેરબાની",
))
_PLEA_FILLERS = frozenset(cw.normalize(w) for w in (
    "sir", "madam", "maam", "bhai", "bhaiya", "ji", "na", "naa", "yaar", "once", "bas", "ek", "baar", "just", "very", "so", "much",
    "a", "little", "bit", "thoda", "thodu", "kuch", "kam", "karo", "kardo", "do", "dedo", "de", "aapo", "apo", "ok", "okay", "yes", "ha", "haan",
))


def _is_plea(text: str) -> bool:
    """A bare "please" / "pls sir" / "please na" — no product, number or question in it."""
    tokens = cw.normalize(text).split()
    if not tokens or len(tokens) > 4 or any(ch.isdigit() for ch in text) or "?" in text:
        return False
    return bool(set(tokens) & _PLEA_WORDS) and set(tokens) <= (_PLEA_WORDS | _PLEA_FILLERS)


def _fresh_memo(conv) -> dict | None:
    """Conversation.last_side_topic while it is recent enough to matter, else None."""
    memo = getattr(conv, "last_side_topic", None)
    if not isinstance(memo, dict):
        return None
    try:
        ts = datetime.fromisoformat(str(memo.get("ts")))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return memo if datetime.now(timezone.utc) - ts <= _FOLLOWUP_WINDOW else None


async def _remember_topic(db, conv, topic: str, n: int) -> None:
    """Remember the side topic just answered (and how many times in a row) for follow-ups like "please"."""
    from app.services import conversation_service

    memo = {"topic": topic, "n": n, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        await conversation_service.update_order_field(db, conv.id, "last_side_topic", memo)
        conv.last_side_topic = memo
    except Exception as exc:  # noqa: BLE001 - a memo is a nicety
        logger.error("last_side_topic write error conv=%s: %s", conv.id, exc)


async def _forget_topic(db, conv) -> None:
    """The conversation moved on: drop the side-topic memo."""
    from app.services import conversation_service

    if getattr(conv, "last_side_topic", None) is None:
        return
    try:
        await conversation_service.update_order_field(db, conv.id, "last_side_topic", None)
        conv.last_side_topic = None
    except Exception as exc:  # noqa: BLE001
        logger.error("last_side_topic clear error conv=%s: %s", conv.id, exc)


async def _catalogue(db, client) -> list:
    """The shop's active products (the side-question context does not carry them)."""
    from app.services import catalogue_service

    return await catalogue_service.list_products(db, client.id, is_active=True)


async def _has_open_order(db, client, conv, sender_phone: str) -> bool:
    """True when this customer already has an order that is not cancelled (so 'when will it come?' means that order)."""
    from sqlalchemy import or_, select

    from app.models.order import Order

    row = await db.execute(
        select(Order.id).where(
            Order.client_id == client.id, Order.status != "cancelled",
            or_(Order.customer_phone == sender_phone, Order.conversation_id == conv.id),
        ).limit(1)
    )
    return row.first() is not None


def _offer_open(conv) -> bool:
    """True while a product is pinned from browsing and no order is being collected (the 'Would you like to order?' moment)."""
    return bool(getattr(conv, "pending_product_sku", None)) and getattr(conv, "current_stage", None) in (
        "product_inquiry", "qualification", "objection_handling", "offer_making", "greeting",
    )
