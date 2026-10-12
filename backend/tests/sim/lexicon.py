"""
Multilingual phrase bank for the conversation simulator.

One source of truth: the customer generator (scenarios.py) writes messages from it and the stand-in
NLU (fake_llm.py) recognises them. Each language has: how to ask for a category / price / photos,
yes / no / thanks / cancel / order-status / human / greeting, colour names, and name/address samples.
Languages: english, hindi_roman, hindi_devanagari, gujarati_roman, gujarati_script, marathi,
marathi_roman, bengali, tamil, telugu, kannada, malayalam, punjabi, odia, urdu.
"""

from __future__ import annotations

LANGS = [
    "english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script", "marathi",
    "marathi_roman", "bengali", "tamil", "telugu", "kannada", "malayalam", "punjabi", "odia", "urdu",
]
# Realistic traffic mix for an Indian WhatsApp shop (weights, not %).
LANG_WEIGHTS = {
    "english": 18, "hindi_roman": 22, "hindi_devanagari": 10, "gujarati_roman": 12, "gujarati_script": 12,
    "marathi": 4, "marathi_roman": 3, "bengali": 3, "tamil": 3, "telugu": 3, "kannada": 2, "malayalam": 2,
    "punjabi": 2, "odia": 1, "urdu": 3,
}

# canonical category -> language -> spellings customers use
CATEGORY_WORDS = {
    "saree": {
        "english": ["saree", "sari"], "hindi_roman": ["saree", "sadi"], "hindi_devanagari": ["साड़ी", "साडी"],
        "gujarati_roman": ["saree", "sadi"], "gujarati_script": ["સાડી"], "marathi": ["साडी"],
        "marathi_roman": ["saree", "sadi"], "bengali": ["শাড়ি"], "tamil": ["புடவை", "சேலை"], "telugu": ["చీర"],
        "kannada": ["ಸೀರೆ"], "malayalam": ["സാരി"], "punjabi": ["ਸਾੜੀ"], "odia": ["ଶାଢ଼ୀ"], "urdu": ["ساڑھی"],
    },
    "kurti": {
        "english": ["kurti", "kurta"], "hindi_roman": ["kurti"], "hindi_devanagari": ["कुर्ती"],
        "gujarati_roman": ["kurti"], "gujarati_script": ["કુર્તી"], "marathi": ["कुर्ती"],
        "marathi_roman": ["kurti"], "bengali": ["কুর্তি"], "tamil": ["குர்தி"], "telugu": ["కుర్తీ"],
        "kannada": ["ಕುರ್ತಿ"], "malayalam": ["കുർത്തി"], "punjabi": ["ਕੁੜਤੀ"], "odia": ["କୁର୍ତ୍ତି"], "urdu": ["کرتی"],
    },
    "lehenga": {
        "english": ["lehenga"], "hindi_roman": ["lehenga"], "hindi_devanagari": ["लहंगा"],
        "gujarati_roman": ["lehenga"], "gujarati_script": ["લહેંગા"], "marathi": ["लेहेंगा"],
        "marathi_roman": ["lehenga"], "bengali": ["লেহেঙ্গা"], "tamil": ["லெஹெங்கா"], "telugu": ["లెహంగా"],
        "kannada": ["ಲೆಹೆಂಗಾ"], "malayalam": ["ലെഹംഗ"], "punjabi": ["ਲਹਿੰਗਾ"], "odia": ["ଲେହେଙ୍ଗା"], "urdu": ["لہنگا"],
    },
    "chaniya choli": {
        "english": ["chaniya choli", "ghagra choli"], "hindi_roman": ["chaniya choli"],
        "hindi_devanagari": ["चनिया चोली"], "gujarati_roman": ["chaniya choli"],
        "gujarati_script": ["ચણિયા-ચોળી", "ચણિયા ચોળી"], "marathi": ["चनिया चोली"],
        "marathi_roman": ["chaniya choli"], "bengali": ["ঘাগরা চোলি"], "tamil": ["சனியா சோலி"],
        "telugu": ["చనియా చోళీ"], "kannada": ["ಚನಿಯಾ ಚೋಲಿ"], "malayalam": ["ചണിയ ചോളി"],
        "punjabi": ["ਚਨੀਆ ਚੋਲੀ"], "odia": ["ଚନିଆ ଚୋଲି"], "urdu": ["گھاگھرا چولی"],
    },
}

COLOR_WORDS = {
    "Pink": {"english": "pink", "hindi_roman": "pink wala", "hindi_devanagari": "गुलाबी", "gujarati_roman": "gulabi",
             "gujarati_script": "ગુલાબી", "marathi": "गुलाबी", "marathi_roman": "gulabi", "bengali": "গোলাপি",
             "tamil": "இளஞ்சிவப்பு", "telugu": "గులాబీ", "kannada": "ಗುಲಾಬಿ", "malayalam": "പിങ്ക്",
             "punjabi": "ਗੁਲਾਬੀ", "odia": "ଗୋଲାପୀ", "urdu": "گلابی"},
    "Blue": {"english": "blue", "hindi_roman": "neela", "hindi_devanagari": "नीला", "gujarati_roman": "vadli",
             "gujarati_script": "વાદળી", "marathi": "निळा", "marathi_roman": "nila", "bengali": "নীল",
             "tamil": "நீலம்", "telugu": "నీలం", "kannada": "ನೀಲಿ", "malayalam": "നീല", "punjabi": "ਨੀਲਾ",
             "odia": "ନୀଳ", "urdu": "نیلا"},
    "Green": {"english": "green", "hindi_roman": "hara", "hindi_devanagari": "हरा", "gujarati_roman": "lilo",
              "gujarati_script": "લીલો", "marathi": "हिरवा", "marathi_roman": "hirva", "bengali": "সবুজ",
              "tamil": "பச்சை", "telugu": "ఆకుపచ్చ", "kannada": "ಹಸಿರು", "malayalam": "പച്ച", "punjabi": "ਹਰਾ",
              "odia": "ସବୁଜ", "urdu": "ہرا"},
    "Red": {"english": "red", "hindi_roman": "laal", "hindi_devanagari": "लाल", "gujarati_roman": "lal",
            "gujarati_script": "લાલ", "marathi": "लाल", "marathi_roman": "lal", "bengali": "লাল",
            "tamil": "சிவப்பு", "telugu": "ఎరుపు", "kannada": "ಕೆಂಪು", "malayalam": "ചുവപ്പ്", "punjabi": "ਲਾਲ",
            "odia": "ଲାଲ", "urdu": "لال"},
}

# One entry per intent: language -> phrases. "{cat}" is replaced by a category word.
ASK_CATEGORY = {
    "english": ["do you have {cat}?", "show me {cat}", "I am looking for {cat}", "{cat} available?"],
    "hindi_roman": ["{cat} hai kya?", "mujhe {cat} dikhao", "{cat} chahiye", "aapke paas {cat} hai?"],
    "hindi_devanagari": ["क्या आपके पास {cat} है?", "मुझे {cat} दिखाओ", "{cat} चाहिए"],
    "gujarati_roman": ["{cat} che?", "mane {cat} batavo", "tamari pase {cat} chhe?", "{cat} joiye chhe"],
    "gujarati_script": ["શું તમે {cat} પણ વેચી રહ્યા છો?", "{cat} છે?", "મને {cat} બતાવો", "તમારી પાસે {cat} છે?"],
    "marathi": ["तुमच्याकडे {cat} आहे का?", "मला {cat} दाखवा", "{cat} पाहिजे"],
    "marathi_roman": ["tumchyakade {cat} ahe ka?", "mala {cat} dakhva", "{cat} pahije"],
    "bengali": ["আপনার কাছে {cat} আছে?", "আমাকে {cat} দেখান"],
    "tamil": ["உங்களிடம் {cat} இருக்கிறதா?", "{cat} காட்டுங்கள்"],
    "telugu": ["మీ దగ్గర {cat} ఉందా?", "నాకు {cat} చూపించండి"],
    "kannada": ["ನಿಮ್ಮ ಬಳಿ {cat} ಇದೆಯೇ?", "ನನಗೆ {cat} ತೋರಿಸಿ"],
    "malayalam": ["നിങ്ങളുടെ പക്കൽ {cat} ഉണ്ടോ?", "എനിക്ക് {cat} കാണിക്കൂ"],
    "punjabi": ["ਕੀ ਤੁਹਾਡੇ ਕੋਲ {cat} ਹੈ?", "ਮੈਨੂੰ {cat} ਦਿਖਾਓ"],
    "odia": ["ଆପଣଙ୍କ ପାଖରେ {cat} ଅଛି କି?", "ମୋତେ {cat} ଦେଖାନ୍ତୁ"],
    "urdu": ["کیا آپ کے پاس {cat} ہے؟", "مجھے {cat} دکھائیں"],
}

PRICE_Q = {
    "english": ["what is the price?", "how much?", "price please"],
    "hindi_roman": ["iska price kya hai?", "kitne ka hai?", "rate batao"],
    "hindi_devanagari": ["इसकी कीमत क्या है?", "कितने का है?"],
    "gujarati_roman": ["aani kimat shu che?", "ketla ni che?"],
    "gujarati_script": ["આની કિંમત શું છે?", "કેટલાની છે?"],
    "marathi": ["याची किंमत काय आहे?", "किती रुपयांना?"],
    "marathi_roman": ["yachi kimat kay ahe?", "kiti la ahe?"],
    "bengali": ["দাম কত?", "এর দাম কত"],
    "tamil": ["விலை என்ன?", "இதன் விலை எவ்வளவு?"],
    "telugu": ["ధర ఎంత?", "ఇది ఎంత?"],
    "kannada": ["ಬೆಲೆ ಎಷ್ಟು?", "ಇದರ ಬೆಲೆ ಎಷ್ಟು"],
    "malayalam": ["വില എത്ര?", "ഇതിന്റെ വില എന്താണ്"],
    "punjabi": ["ਕੀਮਤ ਕੀ ਹੈ?", "ਕਿੰਨੇ ਦੀ ਹੈ?"],
    "odia": ["ଦାମ କେତେ?", "ଏହାର ମୂଲ୍ୟ କେତେ"],
    "urdu": ["قیمت کیا ہے؟", "کتنے کی ہے؟"],
}

ORDER_INTENT = {
    "english": ["I want to order this", "I'll take it", "book it"],
    "hindi_roman": ["order karna hai", "ye le lunga", "book karo"],
    "hindi_devanagari": ["ऑर्डर करना है", "मैं यह लूंगा"],
    "gujarati_roman": ["order karvo chhe", "aa levu chhe"],
    "gujarati_script": ["ઓર્ડર કરવો છે", "આ લેવું છે", "હું આ લઈશ"],
    "marathi": ["ऑर्डर करायचा आहे", "मला हे घ्यायचं आहे"],
    "marathi_roman": ["order karaycha ahe", "mala he ghyaych ahe"],
    "bengali": ["অর্ডার করতে চাই", "আমি এটা নেব"],
    "tamil": ["ஆர்டர் செய்ய வேண்டும்", "நான் இதை வாங்குகிறேன்"],
    "telugu": ["ఆర్డర్ చేయాలి", "నేను ఇది తీసుకుంటాను"],
    "kannada": ["ಆರ್ಡರ್ ಮಾಡಬೇಕು", "ನಾನು ಇದನ್ನು ತೆಗೆದುಕೊಳ್ಳುತ್ತೇನೆ"],
    "malayalam": ["ഓർഡർ ചെയ്യണം", "ഞാൻ ഇത് എടുക്കാം"],
    "punjabi": ["ਆਰਡਰ ਕਰਨਾ ਹੈ", "ਮੈਂ ਇਹ ਲਵਾਂਗਾ"],
    "odia": ["ଅର୍ଡର କରିବାକୁ ଚାହେଁ", "ମୁଁ ଏହା ନେବି"],
    "urdu": ["آرڈر کرنا ہے", "میں یہ لوں گا"],
}

YES = {
    "english": ["yes", "yes please", "sure"], "hindi_roman": ["haan", "haan ji", "theek hai"],
    "hindi_devanagari": ["हाँ", "जी हाँ", "ठीक है"], "gujarati_roman": ["ha", "ha ji", "barabar"],
    "gujarati_script": ["હા", "હા જી", "બરાબર"], "marathi": ["होय", "हो"], "marathi_roman": ["hoy", "ho"],
    "bengali": ["হ্যাঁ", "ঠিক আছে"], "tamil": ["ஆம்", "சரி"], "telugu": ["అవును", "సరే"],
    "kannada": ["ಹೌದು", "ಸರಿ"], "malayalam": ["അതെ", "ശരി"], "punjabi": ["ਹਾਂ", "ਠੀਕ ਹੈ"],
    "odia": ["ହଁ", "ଠିକ୍ ଅଛି"], "urdu": ["جی ہاں", "ٹھیک ہے"],
}
NO = {
    "english": ["no", "no thanks"], "hindi_roman": ["nahi", "nahi chahiye"], "hindi_devanagari": ["नहीं", "नहीं चाहिए"],
    "gujarati_roman": ["na", "nathi joitu"], "gujarati_script": ["ના", "નથી જોઈતું"], "marathi": ["नाही", "नको"],
    "marathi_roman": ["nahi", "nako"], "bengali": ["না", "চাই না"], "tamil": ["இல்லை", "வேண்டாம்"],
    "telugu": ["కాదు", "వద్దు"], "kannada": ["ಇಲ್ಲ", "ಬೇಡ"], "malayalam": ["ഇല്ല", "വേണ്ട"],
    "punjabi": ["ਨਹੀਂ", "ਨਹੀਂ ਚਾਹੀਦਾ"], "odia": ["ନା", "ଦରକାର ନାହିଁ"], "urdu": ["نہیں", "نہیں چاہیے"],
}
THANKS = {
    "english": ["thanks", "thank you so much"], "hindi_roman": ["shukriya", "dhanyavaad"],
    "hindi_devanagari": ["धन्यवाद", "शुक्रिया"], "gujarati_roman": ["aabhar"],
    "gujarati_script": ["આભાર", "ધન્યવાદ"], "marathi": ["धन्यवाद", "आभारी आहे"], "marathi_roman": ["dhanyavad"],
    "bengali": ["ধন্যবাদ"], "tamil": ["நன்றி"], "telugu": ["ధన్యవాదాలు"], "kannada": ["ಧನ್ಯವಾದಗಳು"],
    "malayalam": ["നന്ദി"], "punjabi": ["ਧੰਨਵਾਦ"], "odia": ["ଧନ୍ୟବାଦ"], "urdu": ["شکریہ"],
}
GREET = {
    "english": ["hi", "hello", "good morning"], "hindi_roman": ["namaste", "hello bhai"],
    "hindi_devanagari": ["नमस्ते", "हेलो"], "gujarati_roman": ["kem cho", "namaste"],
    "gujarati_script": ["કેમ છો?", "નમસ્તે"], "marathi": ["नमस्कार", "कसे आहात?"], "marathi_roman": ["namaskar"],
    "bengali": ["নমস্কার", "হ্যালো"], "tamil": ["வணக்கம்"], "telugu": ["నమస్కారం"], "kannada": ["ನಮಸ್ಕಾರ"],
    "malayalam": ["നമസ്കാരം"], "punjabi": ["ਸਤ ਸ੍ਰੀ ਅਕਾਲ"], "odia": ["ନମସ୍କାର"], "urdu": ["السلام علیکم", "آداب"],
}
CANCEL = {
    "english": ["cancel my order", "I don't want this anymore"], "hindi_roman": ["order cancel kar do", "nahi lena"],
    "hindi_devanagari": ["ऑर्डर कैंसल कर दो", "मुझे नहीं चाहिए"], "gujarati_roman": ["order cancel karo", "have nathi levu"],
    "gujarati_script": ["ઓર્ડર રદ કરો", "મારે હવે નથી લેવું"], "marathi": ["ऑर्डर रद्द करा"], "marathi_roman": ["order cancel kara"],
    "bengali": ["অর্ডার বাতিল করুন"], "tamil": ["ஆர்டரை ரத்து செய்யுங்கள்"], "telugu": ["ఆర్డర్ రద్దు చేయండి"],
    "kannada": ["ಆರ್ಡರ್ ರದ್ದು ಮಾಡಿ"], "malayalam": ["ഓർഡർ റദ്ദാക്കുക"], "punjabi": ["ਆਰਡਰ ਰੱਦ ਕਰੋ"],
    "odia": ["ଅର୍ଡର ବାତିଲ କରନ୍ତୁ"], "urdu": ["آرڈر منسوخ کریں"],
}
STATUS = {
    "english": ["where is my order?", "order status"], "hindi_roman": ["mera order kab aayega?", "parcel kidhar hai"],
    "hindi_devanagari": ["मेरा ऑर्डर कहाँ है?", "पार्सल कब आएगा"], "gujarati_roman": ["maru order kyare aavshe?"],
    "gujarati_script": ["મારો ઓર્ડર ક્યાં છે?", "મારું પાર્સલ ક્યારે આવશે?"], "marathi": ["माझी ऑर्डर कुठे आहे?"],
    "marathi_roman": ["majha order kuthe ahe?"], "bengali": ["আমার অর্ডার কোথায়?"], "tamil": ["என் ஆர்டர் எங்கே?"],
    "telugu": ["నా ఆర్డర్ ఎక్కడ ఉంది?"], "kannada": ["ನನ್ನ ಆರ್ಡರ್ ಎಲ್ಲಿದೆ?"], "malayalam": ["എന്റെ ഓർഡർ എവിടെ?"],
    "punjabi": ["ਮੇਰਾ ਆਰਡਰ ਕਿੱਥੇ ਹੈ?"], "odia": ["ମୋ ଅର୍ଡର କେଉଁଠି?"], "urdu": ["میرا آرڈر کہاں ہے؟"],
}
HUMAN = {
    "english": ["I want to talk to someone", "my item was damaged, I want a refund"],
    "hindi_roman": ["owner se baat karni hai", "mera item kharab aaya, refund chahiye"],
    "hindi_devanagari": ["मुझे किसी से बात करनी है", "सामान खराब आया है, रिफंड चाहिए"],
    "gujarati_roman": ["malik sathe vaat karvi chhe"], "gujarati_script": ["મારે માલિક સાથે વાત કરવી છે", "સામાન ખરાબ આવ્યો છે"],
    "marathi": ["मला मालकाशी बोलायचं आहे"], "marathi_roman": ["malkashi bolaych ahe"], "bengali": ["আমি মালিকের সাথে কথা বলতে চাই"],
    "tamil": ["நான் உரிமையாளரிடம் பேச வேண்டும்"], "telugu": ["నేను యజమానితో మాట్లాడాలి"],
    "kannada": ["ನಾನು ಮಾಲೀಕರೊಂದಿಗೆ ಮಾತನಾಡಬೇಕು"], "malayalam": ["എനിക്ക് ഉടമയോട് സംസാരിക്കണം"],
    "punjabi": ["ਮੈਂ ਮਾਲਕ ਨਾਲ ਗੱਲ ਕਰਨੀ ਹੈ"], "odia": ["ମୁଁ ମାଲିକ ସହିତ କଥା ହେବାକୁ ଚାହେଁ"], "urdu": ["مجھے مالک سے بات کرنی ہے"],
}
PHOTO = {
    "english": ["send photos", "can I see pictures?"], "hindi_roman": ["photo bhejo", "pic dikhao"],
    "hindi_devanagari": ["फोटो भेजो", "तस्वीर दिखाओ"], "gujarati_roman": ["photo moklo"], "gujarati_script": ["ફોટો મોકલો", "તસવીર બતાવો"],
    "marathi": ["फोटो पाठवा"], "marathi_roman": ["photo pathva"], "bengali": ["ছবি পাঠান"], "tamil": ["புகைப்படம் அனுப்புங்கள்"],
    "telugu": ["ఫోటో పంపండి"], "kannada": ["ಫೋಟೋ ಕಳುಹಿಸಿ"], "malayalam": ["ഫോട്ടോ അയയ്ക്കൂ"], "punjabi": ["ਫੋਟੋ ਭੇਜੋ"],
    "odia": ["ଫଟୋ ପଠାନ୍ତୁ"], "urdu": ["تصویر بھیجیں"],
}
FAQ = {
    "english": ["do you have cash on delivery?", "what is your return policy?"],
    "hindi_roman": ["cod available hai?", "return policy kya hai?"], "hindi_devanagari": ["क्या कैश ऑन डिलीवरी है?"],
    "gujarati_roman": ["cod che?", "return policy shu che?"], "gujarati_script": ["શું કેશ ઓન ડિલિવરી છે?", "રિટર્ન પોલિસી શું છે?"],
    "marathi": ["कॅश ऑन डिलिव्हरी आहे का?"], "marathi_roman": ["cod ahe ka?"], "bengali": ["ক্যাশ অন ডেলিভারি আছে?"],
    "tamil": ["கேஷ் ஆன் டெலிவரி உள்ளதா?"], "telugu": ["క్యాష్ ఆన్ డెలివరీ ఉందా?"], "kannada": ["ಕ್ಯಾಶ್ ಆನ್ ಡೆಲಿವರಿ ಇದೆಯೇ?"],
    "malayalam": ["ക്യാഷ് ഓൺ ഡെലിവറി ഉണ്ടോ?"], "punjabi": ["ਕੀ ਕੈਸ਼ ਆਨ ਡਿਲੀਵਰੀ ਹੈ?"], "odia": ["କ୍ୟାସ ଅନ ଡେଲିଭରି ଅଛି କି?"],
    "urdu": ["کیا کیش آن ڈیلیوری ہے؟"],
}
PAY_UPI = {l: ["UPI"] for l in LANGS}
PAY_COD = {
    "english": ["COD", "cash on delivery"], "hindi_roman": ["COD", "cash on delivery"], "hindi_devanagari": ["COD", "कैश ऑन डिलीवरी"],
    "gujarati_roman": ["COD", "cash on delivery"], "gujarati_script": ["COD", "કેશ ઓન ડિલિવરી"], "marathi": ["COD"],
    "marathi_roman": ["COD"], "bengali": ["COD"], "tamil": ["COD"], "telugu": ["COD"], "kannada": ["COD"],
    "malayalam": ["COD"], "punjabi": ["COD"], "odia": ["COD"], "urdu": ["COD"],
}

# name / address samples per language (names and addresses are proper nouns; script follows the customer)
NAMES = {
    "english": ["Rajesh Patel", "Priya Sharma", "Amit Shah"], "hindi_roman": ["Rohit Verma", "Sunita Devi"],
    "hindi_devanagari": ["रोहित वर्मा", "सुनीता देवी"], "gujarati_roman": ["Hiren Patel", "Jigna Mehta"],
    "gujarati_script": ["હિરેન પટેલ", "જિજ્ઞા મહેતા"], "marathi": ["सुनील पाटील", "अनिता जाधव"], "marathi_roman": ["Sunil Patil"],
    "bengali": ["সুমন দাস", "রিয়া সেন"], "tamil": ["முருகன் குமார்", "லக்ஷ்மி"], "telugu": ["రమేష్ రెడ్డి", "లక్ష్మి"],
    "kannada": ["ರಮೇಶ್ ಗೌಡ", "ಲಕ್ಷ್ಮಿ"], "malayalam": ["അനിൽ കുമാർ", "ലക്ഷ്മി"], "punjabi": ["ਗੁਰਪ੍ਰੀਤ ਸਿੰਘ", "ਸਿਮਰਨ ਕੌਰ"],
    "odia": ["ସୁନୀଲ ଦାସ", "ପ୍ରିୟା ସାହୁ"], "urdu": ["محمد عامر", "عائشہ خان"],
}
ADDRESSES = {
    "english": ["12 MG Road, Surat 395001", "Flat 4B, Shanti Apartments, Pune 411001"],
    "hindi_roman": ["45 Gandhi Nagar, Jaipur 302001"], "hindi_devanagari": ["४५ गांधी नगर, जयपुर ३०२००१"],
    "gujarati_roman": ["9 Ellis Bridge, Ahmedabad 380006"], "gujarati_script": ["૯ એલિસ બ્રિજ, અમદાવાદ ૩૮૦૦૦૬", "12 નવરંગપુરા, અમદાવાદ 380009"],
    "marathi": ["१२ शिवाजी नगर, पुणे ४११००५"], "marathi_roman": ["12 Shivaji Nagar, Pune 411005"],
    "bengali": ["১২ পার্ক স্ট্রিট, কলকাতা ৭০০০১৬"], "tamil": ["12 காந்தி தெரு, சென்னை 600001"],
    "telugu": ["12 గాంధీ రోడ్, హైదరాబాద్ 500001"], "kannada": ["12 ಎಂಜಿ ರೋಡ್, ಬೆಂಗಳೂರು 560001"],
    "malayalam": ["12 എംജി റോഡ്, കൊച്ചി 682001"], "punjabi": ["12 ਮਾਲ ਰੋਡ, ਅੰਮ੍ਰਿਤਸਰ 143001"],
    "odia": ["12 ଗାନ୍ଧୀ ରୋଡ, ଭୁବନେଶ୍ୱର 751001"], "urdu": ["12 مال روڈ، لاہوری گیٹ دہلی 110006"],
}
CHANGE_WORDS = {
    "english": ["change"], "hindi_roman": ["change", "badlo"], "hindi_devanagari": ["बदलें", "बदलो"], "gujarati_roman": ["change", "badlo"],
    "gujarati_script": ["બદલો"], "marathi": ["बदला", "बदलायचे"], "marathi_roman": ["change"], "bengali": ["পরিবর্তন"],
    "tamil": ["மாற்று"], "telugu": ["మార్చు"], "kannada": ["ಬದಲಿಸಿ"], "malayalam": ["മാറ്റുക"], "punjabi": ["ਬਦਲੋ"],
    "odia": ["ବଦଳାନ୍ତୁ"], "urdu": ["تبدیل"],
}
PHONE = "9876543210"

# Expected canonical category for NLU/tests
CATEGORY_KEYS = list(CATEGORY_WORDS)
