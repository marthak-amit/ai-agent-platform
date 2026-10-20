"""
Native-script / Roman-spelling aliases for product words and colours, plus a script-safe tokenizer.

Shop catalogues are usually written in English ("Green Cotton Saree"), customers are not: "સાડી",
"साड़ी", "புடவை", "sadi", "lehnga". The keyword search and the colour/size slot matching used to
tokenise with `[a-z0-9]+` (dropping every non-Latin word, so a Gujarati query matched *nothing* and fell
back to "the first five products") and with `\\w` (which deletes Indic vowel signs). This module:

  * `tokenize()`        — Unicode-aware words; combining marks are kept.
  * `expand_query()`    — the query's tokens plus the English product words their aliases stand for.
  * `canonical_color()` — "ગુલાબી" / "gulabi" / "गुलाबी" → "pink".

Aliases are exact whole-word matches (never substrings), so a short alias cannot fire inside another word.
"""

from __future__ import annotations

import unicodedata

# canonical English word(s) → aliases (space separated; use _ inside an alias for a multi-word alias)
_CATEGORY_TABLE = {
    "saree": "sari sadi saadi saree sarree साड़ी साडी सारी સાડી શાડી শাড়ি சேலை புடவை చీర ಸೀರೆ സാരി ਸਾੜੀ ଶାଢ଼ୀ ساڑھی ساری",
    "kurti": "kurti kurtis kurta kurtha कुर्ती कुर्ता कुर्त्ता કુર્તી કુર્તા কুর্তি কুর্তা குர்தி కుర్తీ ಕುರ್ತಿ കുർത്തി ਕੁੜਤੀ କୁର୍ତ୍ତି کرتی کرتا",
    "lehenga": "lehenga lehnga lengha ghagra लहंगा लेहेंगा લહેંગા લેંઘા লেহেঙ্গা লেহঙ্গা லெஹெங்கா లెహంగా ಲೆಹೆಂಗಾ ലെഹംഗ ਲਹਿੰਗਾ ଲେହେଙ୍ଗା لہنگا",
    "chaniya choli": "chaniya_choli chania_choli ghagra_choli चनिया_चोली घाघरा_चोली ચણિયા_ચોળી ચણિયાચોળી ચણિયા_ચોલી ચણીયા_ચોલી ઘાઘરા_ચોળી",
    "choli": "choli blouse चोली ब्लाउज़ ब्लाउज ચોળી ચોલી બ્લાઉઝ ব্লাউজ ரவிக்கை బ్లౌజ్ ಬ್ಲೌಸ್ ബ്ലൗസ് ਬਲਾਊਜ਼ چولی بلاؤز",
    "dupatta": "dupatta dupata chunni chunari दुपट्टा चुनरी ચુંદડી દુપટ્ટા ଓଡ଼ଣା دوپٹہ",
    "salwar": "salwar shalwar salvar सलवार शलवार સલવાર সালোয়ার சல்வார் సల్వార్ ಸಲ್ವಾರ್ സൽവാർ ਸਲਵਾਰ شلوار",
    "suit": "suit suits सूट સૂટ সুট சூட் సూట్ ಸೂಟ್ സ്യൂട്ട് ਸੂਟ ସୁଟ୍ سوٹ",
    "dress": "dress dresses frock ड्रेस फ्रॉक ડ્રેસ ફ્રોક ড্রেস ஆடை డ్రెస్ ಡ್ರೆಸ್ ഡ്രസ്സ് ਡਰੈੱਸ ڈریس",
    "gown": "gown gowns गाउन ગાઉન গাউন கவுன் గౌన్ ಗೌನ್ ഗൗൺ ਗਾਊਨ ଗାଉନ୍ گاؤن",
    "shirt": "shirt shirts शर्ट શર્ટ শার্ট சட்டை షర్ట్ ಶರ್ಟ್ ഷർട്ട് ਸ਼ਰਟ ଶାର୍ଟ ଶର୍ଟ شرٹ",
    "tshirt": "tshirt t-shirt tee टीशर्ट ટીશર્ટ টিশার্ট டிஷர்ட் టీషర్ట్ ಟೀಶರ್ಟ್ ടീഷർട്ട് ਟੀਸ਼ਰਟ ٹی_شرٹ",
    "jeans": "jeans jean जीन्स જીન્સ জিন্স ஜீன்ஸ் జీన్స్ ಜೀನ್ಸ್ ജീൻസ് ਜੀਨਜ਼ ଜିନ୍ସ جینز",
    "pant": "pant pants trouser trousers पैंट पेंट पैन्ट પેન્ટ প্যান্ট பேன்ட் ప్యాంటు ಪ್ಯಾಂಟ್ പാന്റ് ਪੈਂਟ ପ୍ୟାଣ୍ଟ پینٹ",
    "top": "top tops टॉप ટોપ টপ டாப் టాప్ ಟಾಪ್ ടോപ്പ് ਟੌਪ ଟପ୍ ٹاپ",
    "shoes": "shoes shoe footwear sandal sandals chappal जूते जूता चप्पल सैंडल બૂટ ચંપલ জুতো செருப்பு చెప్పులు ಚಪ್ಪಲಿ ചെരുപ്പ് ਜੁੱਤੀ ଜୋତା جوتے",
    "bag": "bag bags purse handbag बैग पर्स થેલી બેગ ব্যাগ பை బ్యాగ్ ಬ್ಯಾಗ್ ബാഗ് ਬੈਗ ବ୍ୟାଗ୍ بیگ",
    "jewellery": "jewellery jewelry necklace earrings bangles ज्वैलरी गहने गहना ઘરેણાં જ્વેલરી গয়না நகை నగలు ಆಭರಣ ആഭരണം ਗਹਿਣੇ ଅଳଙ୍କାର زیور",
    "watch": "watch watches घड़ी ઘડિયાળ ঘড়ি கடிகாரம் గడియారం ಗಡಿಯಾರ ഘടികാരം ਘੜੀ ଘଡ଼ି گھڑی",
    "kids": "kids kid children child baby बच्चे बच्चों બાળકો બાળક শিশু குழந்தைகள் పిల్లలు ಮಕ್ಕಳು കുട്ടികൾ ਬੱਚੇ ଶିଶୁ بچے",
    "men": "men mens gents male पुरुष જેન્ટ્સ પુરુષ পুরুষ ஆண்கள் పురుషులు ಪುರುಷ പുരുഷൻ ਮਰਦ ପୁରୁଷ مرد",
    "women": "women womens ladies female महिला लेडीज़ स्त्री સ્ત્રી લેડીઝ মহিলা பெண்கள் మహిళలు ಮಹಿಳೆ സ്ത്രീ ਔਰਤ ମହିଳା خواتین",
    "cotton": "cotton कॉटन सूती કોટન સુતરાઉ কটন காட்டன் కాటన్ ಕಾಟನ್ കോട്ടൺ ਕਾਟਨ କଟନ୍ کاٹن",
    "silk": "silk सिल्क रेशम સિલ્ક રેશમ সিল্ক பட்டு పట్టు ರೇಷ್ಮೆ പട്ട് ਸਿਲਕ ରେଶମ ریشم",
    "bridal": "bridal wedding शादी दुल्हन વેડિંગ લગ્ન বিয়ে திருமணம் పెళ్లి ಮದುವೆ വിവാഹം ਵਿਆਹ ବିବାହ شادی",
}

_COLOR_TABLE = {
    "red": "red laal lal laall lalrang रेड રેડ लाल લાલ লাল சிவப்பு ఎరుపు ಕೆಂಪು ചുവപ്പ് ਲਾਲ ଲାଲ لال سرخ",
    "blue": "blue neela ब्लू બ્લૂ બ્લુ neeli nila neelo vadli वाडली नीला नीली निळा નીલો વાદળી নীল நீலம் నీలం ನೀಲಿ നീല ਨੀਲਾ ନୀଳ نیلا",
    "green": "green hara ग्रीन ગ્રીન hari lilo hirva हरा हरी हिरवा લીલો লীলো সবুজ பச்சை ఆకుపచ్చ ಹಸಿರು പച്ച ਹਰਾ ସବୁଜ ہرا سبز",
    "pink": "pink gulabi पिंक પિંક gulaabi गुलाबी ગુલાબી গোলাপি இளஞ்சிவப்பு గులాబీ ಗುಲಾಬಿ പിങ്ക് ਗੁਲਾਬੀ ଗୋଲାପୀ گلابی",
    "yellow": "yellow peela येलो યેલો pila pivla pilo peelo पीला पिवळा પીળો হলুদ மஞ்சள் పసుపు ಹಳದಿ മഞ്ഞ ਪੀਲਾ ହଳଦିଆ پیلا",
    "black": "black kala ब्लैक બ્લેક kaala kaali kalo kaalo kalu kali kaalu kaloo काला काली काळा કાળો কালো கருப்பு నలుపు ಕಪ್ಪು കറുപ്പ് ਕਾਲਾ କଳା کالا",
    "white": "white safed व्हाइट વ્હાઇટ safeda dholo dhoLo dhawal सफेद पांढरा સફેદ સફેદો সাদা வெள்ளை తెలుపు ಬಿಳಿ വെള്ള ਚਿੱਟਾ ଧଳା سفید",
    "orange": "orange narangi ऑरेंज ઓરેન્જ नारंगी केशरी નારંગી কমলা ஆரஞ்சு నారింజ ಕಿತ್ತಳೆ ഓറഞ്ച് ਸੰਤਰੀ କମଳା نارنجی",
    "purple": "purple jamuni पर्पल પર્પલ वायलेट વાયોલેટ baingani jambli बैंगनी जामुनी જાંબલી বেগুনি ஊதா ఊదా ನೇರಳೆ വയലറ്റ് ਬੈਂਗਣੀ ବାଇଗଣି جامنی",
    "maroon": "maroon marun मैरून મરૂન মেরুন மெரூன் మెరూన్ ಮರೂನ್ മറൂൺ ਮਰੂਨ ମେରୁନ ماروں",
    "brown": "brown bhura bhoora bhuro भूरा તપખીરી ભૂરો বাদামী பழுப்பு గోధుమ ಕಂದು തവിട്ട് ਭੂਰਾ ଖଇରି بھورا",
    "grey": "grey gray slate rakhodi ग्रे स्लेटी રાખોડી ধূসর சாம்பல் బూడిద ಬೂದು ചാരം ਸਲੇਟੀ ଧୂସର سرمئی",
    "golden": "golden gold sunehri सुनहरी गोल्डन સોનેરી গোল্ডেন தங்க బంగారు ಚಿನ್ನ സ്വർണ്ണ ਸੁਨਹਿਰੀ ସୁନେଲି سنہری",
    "silver": "silver chandi चांदी सिल्वर ચાંદી সিলভার வெள்ளி వెండి ಬೆಳ್ಳಿ വെള്ളി ਚਾਂਦੀ ରୂପା چاندی",
    "cream": "cream क्रीम ક્રીમ ক্রিম கிரீம் క్రీమ్ ಕ್ರೀಮ್ ക്രീം ਕਰੀਮ କ୍ରିମ୍ کریم",
    "beige": "beige बेज બેજ বেজ பீஜ் బేజ్ ಬೀಜ್ ബീജ് ਬੇਜ ବେଜ୍ بیج",
    "navy": "navy navy_blue नेवी નેવી নেভি நேவி నేవీ ನೇವಿ നേവി ਨੇਵੀ ନେଭି نیوی",
    "peach": "peach आड़ू पीच પીચ পীচ பீச் పీచ్ ಪೀಚ್ പീച്ച് ਪੀਚ ପିଚ୍ پیچ",
    "magenta": "magenta मैजेंटा મેજેન્ટા ম্যাজেন্টা மெஜந்தா మెజెంటా ಮೆಜೆಂಟಾ മജെന്റ ਮੈਜੈਂਟਾ ମାଜେଣ୍ଟା میجینٹا",
}


def color_aliases(canonical: str) -> list[tuple[str, str]]:
    """Every (alias, canonical colour) pair for one canonical colour name ("red" → [("laal","red"), ("lal","red"), ...])."""
    return [(alias, target) for alias, target in _COLOR_ALIAS.items() if target == canonical]


def normalize_token(text: str) -> str:
    """Lower-case a word and fold the Unicode variants keyboards produce (NFC, nukta forms, ZWJ/ZWNJ removed)."""
    out = unicodedata.normalize("NFC", (text or "").lower()).replace("‌", "").replace("‍", "")
    return out


def _build(table: dict[str, str]) -> dict[str, str]:
    """alias (normalised, '_' → ' ') → canonical English."""
    out: dict[str, str] = {}
    for canonical, aliases in table.items():
        for alias in aliases.split():
            out[normalize_token(alias.replace("_", " "))] = canonical
    return out


_CATEGORY_ALIAS = _build(_CATEGORY_TABLE)
_COLOR_ALIAS = _build(_COLOR_TABLE)
_MAX_PHRASE_WORDS = max(a.count(" ") + 1 for a in list(_CATEGORY_ALIAS) + list(_COLOR_ALIAS))


def tokenize(text: str) -> list[str]:
    """
    Words of `text` in any script, lower-cased, in order.

    Letters, digits and combining marks (Indic vowel signs, viramas, nuktas) stay inside a word; everything
    else — punctuation, emoji, hyphens — separates words.
    """
    words, current = [], []
    for ch in normalize_token(text):
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def _lookup(tokens: list[str], table: dict[str, str]) -> list[str]:
    """Canonical words for every alias (single or multi-word, longest first) found in `tokens`."""
    found: list[str] = []
    i = 0
    while i < len(tokens):
        for span in range(min(_MAX_PHRASE_WORDS, len(tokens) - i), 0, -1):
            canonical = table.get(" ".join(tokens[i:i + span]))
            if canonical:
                found.append(canonical)
                i += span
                break
        else:
            i += 1
    return found


def expand_query(text: str) -> list[str]:
    """
    The English product words a (possibly native-script) query stands for, in order.

    "શું તમે ચણિયા-ચોળી પણ વેચી રહ્યા છો?" → ["chaniya choli"];  "लाल साड़ी चाहिए" → ["red", "saree"] (colours
    included, since they are valid catalogue keywords too). Words that are already Latin are not repeated here.
    """
    tokens = tokenize(text)
    return _lookup(tokens, _CATEGORY_ALIAS) + _lookup(tokens, _COLOR_ALIAS)


def canonical_colors(text: str) -> list[str]:
    """Every known colour named in `text`, in order ("2 laal 1 hara" → ["red", "green"]); [] when none."""
    return _lookup(tokenize(text), _COLOR_ALIAS)


def canonical_color(text: str) -> str | None:
    """'ગુલાબી' / 'gulabi' / 'Pink wala' → 'pink' (None when the text names no known colour)."""
    colors = _lookup(tokenize(text), _COLOR_ALIAS)
    return colors[0] if colors else None
