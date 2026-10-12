"""
Stand-in for Groq in the conversation simulator (the sandbox cannot reach api.groq.com).

`install()` replaces the provider *client* (llm_client.get_client / gemini_service._get_client) so the
real llm_client stack still runs — JSON mode, retries, reasoning strip, circuit breaker, llm_usage.
The fake answers by task, recognised from the system prompt:

  router     (ROUTER_V2)        rule-based multilingual NLU over the lexicon → router JSON
  classify_turn (legacy)        JSON intent over the catalogue block in the prompt
  reply      (generate_reply)   a short canned answer written in the language the prompt demands
  translate  (reply_localizer)  native-script filler that keeps every fact token verbatim
  classifiers                   cancel / buy-intent / off-topic one-word answers

Chaos knobs (probabilities) inject what production sees: HTTP 429, empty content, invalid JSON, an
English answer to a non-English customer, an English `reply_hint`. The engine must survive all of them.

This tests the ENGINE's handling of LLM output; it says nothing about the real model's quality.
"""

from __future__ import annotations

import json
import random
import re
from types import SimpleNamespace

from tests.sim import lexicon as L

_DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯০১২৩৪৫৬৭৮৯", "0123456789" * 3)


def _norm(text: str) -> str:
    """Lower-case, native digits → ASCII, collapse whitespace."""
    return " ".join((text or "").translate(_DIGITS).lower().split())


def _bank(*tables) -> list[str]:
    """All phrases of the given lexicon tables, lower-cased, longest first."""
    out: set[str] = set()
    for table in tables:
        for phrases in table.values():
            out.update(_norm(p) for p in phrases)
    return sorted(out, key=len, reverse=True)


_CANCEL = _bank(L.CANCEL) + ["cancel", "radd", "रद्द", "रद"]
_STATUS = _bank(L.STATUS) + ["order status", "parcel", "ऑर्डर कहाँ"]
_HUMAN = _bank(L.HUMAN) + ["refund", "damaged", "complaint"]
_THANKS = _bank(L.THANKS)
_GREET = _bank(L.GREET)
_PHOTO = _bank(L.PHOTO)
_ORDER = _bank(L.ORDER_INTENT)
_PRICE = _bank(L.PRICE_Q) + ["price", "kimat", "rate", "कीमत", "કિંમત"]
_FAQ = _bank(L.FAQ)
_YES = {_norm(p) for table in (L.YES,) for phrases in table.values() for p in phrases} | {"ok", "okay", "confirm", "1"}
_NO = {_norm(p) for phrases in L.NO.values() for p in phrases}

# word → canonical category
_CATEGORY_LOOKUP: dict[str, str] = {}
for _cat, _by_lang in L.CATEGORY_WORDS.items():
    for _words in _by_lang.values():
        for _w in _words:
            _CATEGORY_LOOKUP[_norm(_w)] = _cat
# "chaniya choli" must win over "choli"/"chaniya" fragments
_CATEGORY_KEYS = sorted(_CATEGORY_LOOKUP, key=len, reverse=True)

_COLOR_LOOKUP: dict[str, str] = {}
for _color, _by_lang in L.COLOR_WORDS.items():
    _COLOR_LOOKUP[_color.lower()] = _color
    for _w in _by_lang.values():
        _COLOR_LOOKUP[_norm(_w).replace(" wala", "")] = _color
        _COLOR_LOOKUP[_norm(_w)] = _color

_WELCOME = {   # reply_hint for thanks, in each language (what a well-behaved model returns)
    "english": "You're welcome! 😊", "hindi_roman": "Koi baat nahi ji! 😊", "hindi_devanagari": "आपका स्वागत है! 😊",
    "gujarati_roman": "Tamaru swagat chhe! 😊", "gujarati_script": "તમારું સ્વાગત છે! 😊", "marathi": "तुमचे स्वागत आहे! 😊",
    "marathi_roman": "Tumche swagat ahe! 😊", "bengali": "আপনাকে স্বাগতম! 😊", "tamil": "நன்றி! 😊",
    "telugu": "మీకు స్వాగతం! 😊", "kannada": "ಸ್ವಾಗತ! 😊", "malayalam": "സ്വാഗതം! 😊", "punjabi": "ਜੀ ਆਇਆਂ ਨੂੰ! 😊",
    "odia": "ସ୍ୱାଗତ! 😊", "urdu": "خوش آمدید! 😊",
}
_ASK_COLOR_REPLY = {
    "english": "Yes, we have it. Which colour would you like?", "hindi_roman": "Ji haan, hamare paas hai. Kaunsa rang chahiye?",
    "hindi_devanagari": "जी हाँ, हमारे पास है। कौन सा रंग चाहिए?", "gujarati_roman": "Haa, amari pase chhe. Kayo rang joiye?",
    "gujarati_script": "હા, અમે ચણિયા ચોળી વેચીએ છીએ. કયો રંગ જોઈએ છે?", "marathi": "हो, आमच्याकडे आहे. कोणता रंग हवा?",
    "marathi_roman": "Ho, amchyakade ahe. Konta rang hava?", "bengali": "হ্যাঁ, আমাদের কাছে আছে। কোন রঙ চান?",
    "tamil": "ஆம், எங்களிடம் உள்ளது. எந்த நிறம் வேண்டும்?", "telugu": "అవును, మా దగ్గర ఉంది. ఏ రంగు కావాలి?",
    "kannada": "ಹೌದು, ನಮ್ಮ ಬಳಿ ಇದೆ. ಯಾವ ಬಣ್ಣ ಬೇಕು?", "malayalam": "ഉണ്ട്, ഞങ്ങളുടെ പക്കൽ ഉണ്ട്. ഏത് നിറം വേണം?",
    "punjabi": "ਹਾਂ ਜੀ, ਸਾਡੇ ਕੋਲ ਹੈ। ਕਿਹੜਾ ਰੰਗ ਚਾਹੀਦਾ ਹੈ?", "odia": "ହଁ, ଆମ ପାଖରେ ଅଛି। କେଉଁ ରଙ୍ଗ ଦରକାର?",
    "urdu": "جی ہاں، ہمارے پاس ہے۔ کون سا رنگ چاہیے؟",
}
_FILLER = {
    "gujarati": "આ છે", "devanagari": "यह है", "bengali": "এটি আছে", "tamil": "இது உள்ளது", "telugu": "ఇది ఉంది",
    "kannada": "ಇದು ಇದೆ", "malayalam": "ഇത് ഉണ്ട്", "gurmukhi": "ਇਹ ਹੈ", "odia": "ଏହା ଅଛି", "arabic": "یہ ہے", "latin": "ok",
}
_SCRIPT_BY_NAME = {"Gujarati": "gujarati", "Devanagari": "devanagari", "Bengali": "bengali", "Tamil": "tamil", "Telugu": "telugu",
                   "Kannada": "kannada", "Malayalam": "malayalam", "Gurmukhi": "gurmukhi", "Odia": "odia", "Arabic (Nastaliq)": "arabic"}
_FACT_TOKEN = re.compile(r"https?://|@|\d|₹|^(UPI|COD|GPay|PhonePe|Paytm|WhatsApp|QR|SKU)\W*$|^\W+$")


def _resp(content: str):
    """An openai-shaped chat completion carrying `content`."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=40, total_tokens=240),
    )


class FakeGroq:
    """Drop-in for the AsyncOpenAI client: `.chat.completions.create(**kwargs)`."""

    def __init__(self, seed: int = 7, *, p_429: float = 0.0, p_empty: float = 0.0, p_bad_json: float = 0.0,
                 p_wrong_lang: float = 0.0, p_low_conf: float = 0.0, translate_down: bool = False) -> None:
        """Seeded RNG + chaos probabilities (all 0 = a perfect model). translate_down: every translation call fails."""
        self.translate_down = translate_down
        self.rng = random.Random(seed)
        self.p = dict(p_429=p_429, p_empty=p_empty, p_bad_json=p_bad_json, p_wrong_lang=p_wrong_lang, p_low_conf=p_low_conf)
        self.calls: dict[str, int] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    # -- plumbing ----------------------------------------------------------------
    def _chaos(self, key: str) -> bool:
        """True with probability p[key]."""
        return self.rng.random() < self.p[key]

    async def _create(self, **kw):
        """Dispatch one chat completion by task."""
        messages = kw["messages"]
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""
        kind = self._kind(system, user, kw)
        self.calls[kind] = self.calls.get(kind, 0) + 1
        if kind == "translate" and self.translate_down:
            raise RuntimeError("Error code: 503 - translator unavailable (simulated)")
        if self._chaos("p_429"):
            raise RuntimeError("Error code: 429 - rate_limit_exceeded (simulated)")
        if self._chaos("p_empty"):
            return _resp("")
        handler = getattr(self, f"_do_{kind}")
        return _resp(handler(system, user, messages))

    @staticmethod
    def _kind(system: str, user: str, kw: dict) -> str:
        """Which task is this call?"""
        if "understanding layer of SellerTalk24" in system:
            return "router"
        if system.startswith("You translate WhatsApp shop messages"):
            return "translate"
        if "RESPOND WITH ONLY A JSON OBJECT" in system:
            return "turn"
        if "CRITICAL LANGUAGE RULE" in system:
            return "reply"
        if "Classify into exactly one of" in user:
            return "classify"
        if "Is the customer clearly expressing intent to BUY" in user:
            return "buy"
        if "completely unrelated to shopping" in user:
            return "offtopic"
        if kw.get("response_format"):
            return "json"
        return "reply"

    # -- understanding -----------------------------------------------------------
    @staticmethod
    def _parse_ctx(user: str) -> dict:
        """Pull stage / pinned / next slot / candidates / orders out of the router user prompt."""
        ctx = {"stage": "greeting", "pinned": None, "next_slot": None, "options": [], "cands": [], "orders": 0}
        for line in user.splitlines():
            if line.startswith("stage: "):
                ctx["stage"] = line[7:].strip()
            m = re.match(r"pinned_product: (.+?) \| sku=(\S+)", line)
            if m:
                ctx["pinned"] = m.group(2)
            m = re.match(r"next_slot: (\w+)(?: \| allowed options: (.*))?", line)
            if m:
                ctx["next_slot"] = m.group(1)
                ctx["options"] = [o.strip() for o in (m.group(2) or "").split(",") if o.strip()]
            m = re.match(r"- (.+?) \| sku=(\S+) \| ₹[\d,]+ \| (.*)", line)
            if m:
                ctx["cands"].append({"name": m.group(1), "sku": m.group(2), "category": m.group(3).split(" | ")[0]})
            if line.startswith("- ORD") or re.match(r"- \S+-\d+-\d+ \| status", line):
                ctx["orders"] += 1
        return ctx

    @staticmethod
    def _contains(msg: str, bank: list[str]) -> bool:
        """Substring match; very short phrases must be the whole message (no 'na' in 'banana')."""
        return any((p == msg) if len(p) <= 3 else (p in msg) for p in bank)

    def _category_of(self, msg: str) -> str | None:
        """Canonical category word found in the message (longest spelling first)."""
        for key in _CATEGORY_KEYS:
            if key in msg:
                return _CATEGORY_LOOKUP[key]
        return None

    def _understand(self, message: str, ctx: dict) -> dict:
        """One router decision for `message` (the NLU the real model provides)."""
        msg = _norm(message)
        out = {"action": "smalltalk", "args": {}, "language": "en", "confidence": 0.92, "reply_hint": ""}
        slot, in_order = ctx["next_slot"], ctx["stage"] in ("order_collection", "awaiting_final_confirmation", "payment")
        if self._contains(msg, _HUMAN):
            out["action"] = "handoff_human"
        elif self._contains(msg, _CANCEL) and (in_order or ctx["orders"] or ctx["pinned"]):
            out["action"] = "cancel_order"
        elif self._contains(msg, _STATUS) and ctx["orders"]:
            out["action"] = "order_status"
            out["args"] = {"focus": "delivery" if any(w in msg for w in ("kab", "when", "ક્યારે", "कब", "kyare")) else "status"}
        elif slot and slot in ("color", "size", "material"):
            options = {o.lower(): o for o in ctx["options"]}
            value = options.get(msg) or options.get(_COLOR_LOOKUP.get(msg.replace(" wala", ""), "").lower())
            if value:
                out.update(action="answer_slot", args={"slot": slot, "value": value})
            else:
                out.update(action="smalltalk", confidence=0.3)
        elif slot in ("quantity", "cart_item_qty") and re.fullmatch(r"\d{1,3}", msg):
            out.update(action="answer_slot", args={"slot": "quantity", "value": msg})
        elif slot == "payment_method":
            if "upi" in msg or "online" in msg:
                out.update(action="answer_slot", args={"slot": "payment_method", "value": "UPI"})
            elif "cod" in msg or "cash" in msg or "કેશ" in msg or "कैश" in msg:
                out.update(action="answer_slot", args={"slot": "payment_method", "value": "COD"})
            else:
                out.update(confidence=0.3)
        elif slot in ("name", "customer_name"):
            out.update(action="answer_slot", args={"slot": "name", "value": message.strip()})
        elif slot in ("address", "delivery_address"):
            out.update(action="answer_slot", args={"slot": "address", "value": message.strip()})
        elif slot in ("phone", "mobile_number") and re.fullmatch(r"\+?\d[\d\s-]{8,13}", msg):
            out.update(action="answer_slot", args={"slot": "phone", "value": msg})
        elif self._contains(msg, _THANKS):
            out["reply_hint"] = _WELCOME.get(self._guess_lang(message), "")
        elif self._contains(msg, _GREET) and not self._category_of(msg):
            out["action"] = "greeting"
        elif self._contains(msg, _PHOTO) and (ctx["pinned"] or ctx["cands"]):
            sku = ctx["pinned"] or ctx["cands"][0]["sku"]
            out.update(action="show_product", args={"sku": sku, "want_photo": True})
        elif self._contains(msg, _ORDER) and (ctx["pinned"] or ctx["cands"]):
            out.update(action="start_order", args={"sku": ctx["pinned"] or ctx["cands"][0]["sku"]})
        elif self._category_of(msg):
            out.update(action="search_catalog", args={"query": self._category_of(msg)})
        elif self._contains(msg, _FAQ):
            out.update(action="faq", args={"query": "cod" if "cod" in msg or "cash" in msg or "कैश" in msg or "કેશ" in msg else "return"})
        elif self._contains(msg, _PRICE) and ctx["pinned"]:
            out.update(action="show_product", args={"sku": ctx["pinned"]})
        elif msg in _YES and ctx["pinned"]:
            out.update(action="start_order", args={"sku": ctx["pinned"]})
        else:
            out["confidence"] = 0.3
        if self._chaos("p_low_conf"):
            out["confidence"] = 0.2
        return out

    @staticmethod
    def _guess_lang(message: str) -> str:
        """Language of a message via the production detector (the fake 'model' just reads the script)."""
        from app.services.language_service import detect_language

        return detect_language(message)

    # -- task handlers -----------------------------------------------------------
    def _do_router(self, system: str, user: str, messages) -> str:
        """Router JSON for the CUSTOMER MESSAGE in the prompt."""
        original = next((m["content"] for m in messages if isinstance(m["content"], str) and "CUSTOMER MESSAGE:" in m["content"]), user)
        user = original
        message = user.split("CUSTOMER MESSAGE:", 1)[1].split("Return the JSON object only.")[0].strip()
        decision = self._understand(message, self._parse_ctx(user))
        if self._chaos("p_wrong_lang") and decision["reply_hint"]:
            decision["reply_hint"] = "You're welcome!"
        if self._chaos("p_bad_json"):
            return "Sure! Here you go: {not json"
        return json.dumps(decision, ensure_ascii=False)

    def _do_turn(self, system: str, user: str, messages) -> str:
        """Legacy classify_turn JSON: SKU/category from the catalogue block in the system prompt."""
        msg = _norm(user)
        skus = re.findall(r"\[([A-Za-z0-9_\-]+)\] —", system)
        cat = self._category_of(msg)
        lines = [ln for ln in system.splitlines() if "[" in ln and "—" in ln]
        picked = [re.search(r"\[([^\]]+)\]", ln).group(1) for ln in lines if cat and cat.split()[0] in ln.lower()]
        if self._chaos("p_bad_json"):
            return "not json at all"
        if picked:
            return json.dumps({"intent": "LIST_PRODUCTS" if len(picked) > 1 else "ASK_PRODUCT", "sku": picked[0] if len(picked) == 1 else None,
                               "skus": picked if len(picked) > 1 else None, "slots": {}, "question_topic": None})
        if self._contains(msg, _PRICE) and skus:
            return json.dumps({"intent": "ASK_PRICE", "sku": skus[0], "skus": None, "slots": {}, "question_topic": None})
        return json.dumps({"intent": "CHITCHAT", "sku": None, "skus": None, "slots": {}, "question_topic": None})

    def _do_reply(self, system: str, user: str, messages) -> str:
        """Free-text reply in the language the prompt asks for (or English when chaos says the model drifted)."""
        m = re.search(r"Detected customer language: (\w+)", system)
        lang = m.group(1) if m else "english"
        if self._chaos("p_wrong_lang"):
            lang = "english"
        return _ASK_COLOR_REPLY.get(lang, _ASK_COLOR_REPLY["english"])

    def _do_translate(self, system: str, user: str, messages) -> str:
        """Native-script filler with every fact token (digits, ₹, ids, URLs, brand words) kept verbatim."""
        m = re.search(r"written in (.+?) script", system)
        filler = _FILLER.get(_SCRIPT_BY_NAME.get(m.group(1) if m else "", ""), _FILLER["latin"])
        out_lines = []
        for line in user.split("\n"):
            toks, i = [], 0
            for tok in line.split(" "):
                if not tok or _FACT_TOKEN.search(tok):
                    toks.append(tok)
                else:
                    toks.append(filler.split(" ")[i % 2])
                    i += 1
            out_lines.append(" ".join(toks))
        return "\n".join(out_lines)

    def _do_classify(self, system: str, user: str, messages) -> str:
        """classify_user_intent: CANCEL on cancel words, else ANSWER."""
        said = _norm(user.split("Customer replied:", 1)[-1].split("\n\nClassify", 1)[0])
        return "CANCEL" if self._contains(said, _CANCEL) else "ANSWER"

    def _do_buy(self, system: str, user: str, messages) -> str:
        """classify_buy_intent: YES for order phrases / yes words."""
        said = _norm(user.split("Customer message:", 1)[-1].split("\n")[0].strip(" '"))
        return "YES" if self._contains(said, _ORDER) or said in _YES else "NO"

    def _do_offtopic(self, system: str, user: str, messages) -> str:
        """is_off_topic_message: never off-topic."""
        return "NO"

    def _do_json(self, system: str, user: str, messages) -> str:
        """Any other JSON-mode call (cart breakdown etc.): an empty-but-valid object."""
        return json.dumps({"items": [], "inferred_split": False})


def install(monkeypatch, fake: FakeGroq) -> FakeGroq:
    """Route every provider call (chat + the gemini_service helper) to `fake`."""
    monkeypatch.setattr("app.services.llm_client.get_client", lambda: fake)
    monkeypatch.setattr("app.services.gemini_service._get_client", lambda: fake)
    return fake
