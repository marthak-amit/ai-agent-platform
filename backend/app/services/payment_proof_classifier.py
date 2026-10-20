"""
Is this image a payment proof? — decided BEFORE it is attached to an order.

Three outcomes: ``payment_proof`` · ``product_photo`` · ``unknown``. The rule that matters (conv=60, ORD-2026-0077): **the absence of
receipt text is not evidence of a product photo.** Tesseract misses dark-mode and heavily compressed UPI screenshots, so "OCR read
nothing" says nothing. ``product_photo`` needs POSITIVE product evidence:

    * the catalogue watermark on the picture (our own product photos carry the SKU),
    * a perceptual-hash match against a catalogue image (``image_fingerprint``),
    * a caption that is an enquiry about a product ("is this available?"),
    * the vision model recognising a garment / fabric / model / catalogue picture, with high confidence.

Everything else that is not a receipt is ``unknown`` and the customer is asked ([Payment screenshot] [Product photo]).

Evidence is gathered concurrently — the watermark probe, the whole-image OCR and the vision model run side by side (the vision call is
cancelled the moment the OCR alone is decisive), which is what cuts a 5–9 s image turn to roughly the slowest of the three.

**Context prior** (``prior_payment``): an order is waiting for money and its payment instructions went out ≤ ``PROOF_PRIOR_MINUTES``
ago. Inside that window weaker payment evidence is enough for ``payment_proof`` and a ``product_photo`` verdict must be more certain.

Pure functions (``score_text``) are unit-tested without images; OCR, vision and the watermark probe are injectable seams.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.services.payment_amounts import extract_amounts, read_amounts  # noqa: F401  (extract_amounts re-exported for callers)

logger = logging.getLogger(__name__)

PAYMENT_PROOF = "payment_proof"
PRODUCT_PHOTO = "product_photo"
UNKNOWN = "unknown"
OTHER = UNKNOWN                      # legacy name of the third outcome
KINDS = (PAYMENT_PROOF, PRODUCT_PHOTO, UNKNOWN)

#: A verdict at or above this confidence is acted on; below it the customer is asked.
CONFIDENT = 0.75
#: An amount on the receipt counts as the order's when it is within this many rupees of the total.
AMOUNT_TOLERANCE = 1.0
#: Vision must be at least this sure that a picture is a product (a bit more inside the payment window).
PRODUCT_VISION_MIN = 0.85
PRODUCT_VISION_MIN_IN_PRIOR = 0.92
_OCR_MAX_SIDE = 1600

_CURRENCY_RE = re.compile(r"₹|\brs\.?\s*\d|\binr\b|\brupees?\b", re.IGNORECASE)
_UTR_LABEL_RE = re.compile(
    r"\b(?:utr|rrn|upi\s*ref(?:erence)?(?:\s*(?:no|number|id))?|ref(?:erence)?\s*(?:no|number|id)|txn\s*(?:id|no|number)?|"
    r"transaction\s*(?:id|no|number|ref)|order\s*id)\b[\s:#.\-]*([A-Z0-9]{8,24})", re.IGNORECASE,
)
_TWELVE_DIGITS_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_SUCCESS_RE = re.compile(
    r"\b(?:payment\s+(?:successful|success|completed|done|received|sent|of)|(?:transaction|txn)\s+(?:successful|success|completed)|"
    r"paid\s+(?:to|successfully)|money\s+sent|sent\s+(?:to|successfully)|successfully\s+(?:paid|sent|transferred)|debited|credited|"
    r"transfer(?:red)?\s+(?:successful|success|completed)|completed|successful)\b", re.IGNORECASE,
)
_APP_RE = re.compile(
    r"\b(?:google\s*pay|g\s*pay|gpay|phone\s*pe|phonepe|paytm|bhim|amazon\s*pay|cred|mobikwik|freecharge|payzapp|navi|super\.?money|"
    r"powered\s+by\s+upi|upi\s+(?:id|transaction)|banking\s+name|split\s+expense|view\s+details|share\s+receipt)\b", re.IGNORECASE,
)
_BANK_SMS_RE = re.compile(r"\b(?:a/?c|vpa|imps|neft|rtgs|upi\s*ref|avl\s*bal|available\s*balance|debited\s*by|sent\s*from)\b", re.IGNORECASE)
_CAPTION_PROOF_RE = re.compile(r"\b(?:paid|payment|screenshot|txn|utr|upi|transaction|transferred|done|sent)\b", re.IGNORECASE)
_CAPTION_ENQUIRY_RE = re.compile(
    r"\?|\b(?:available|availability|avail|price|cost|rate|kitna|kitne|kya|chahiye|chahie|milega|milegi|stock|size|colou?r|similar|show|"
    r"dikhao|want|need|catalogue|joiye|ketla|product)\b", re.IGNORECASE,
)


@dataclass
class VisionRead:
    """What the vision model saw: the kind of picture and — for a product — what it shows (for the similarity search)."""

    kind: str
    confidence: float
    category: str | None = None
    colours: list[str] = field(default_factory=list)
    fabric: str | None = None
    keywords: list[str] = field(default_factory=list)
    amount: float | None = None          # the rupee amount the model read on a payment screen (a second source next to OCR)

    def search_query(self) -> str:
        """Words to look the product up with: colour · fabric · category (· keywords), no duplicates."""
        words: list[str] = []
        for part in [*self.colours[:1], self.fabric, self.category, *self.keywords]:
            for w in str(part or "").lower().split():
                if w not in words:
                    words.append(w)
        return " ".join(words[:6])


@dataclass
class ProofVerdict:
    """What an image is, how sure we are, and the evidence."""

    kind: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)       # amounts read from the image (for matching an order)
    source: str = "none"                                     # caption | sku | phash | ocr | vision | none
    ocr_len: int | None = None                               # characters of OCR text (None = OCR unavailable)
    vision_label: str | None = None
    vision_conf: float | None = None
    prior: bool = False                                      # an order was waiting for money and its instructions were recent
    vision: VisionRead | None = None                         # the model's description (reused by the product-photo search)
    sku: str | None = None                                   # the catalogue product the picture is (watermark / hash match)

    @property
    def unreadable(self) -> bool:
        """True when the picture is a blank / black / far-too-dark frame: nothing on it can be read, so the customer is asked to resend it."""
        return "image_blank" in self.signals

    @property
    def decisive(self) -> bool:
        """True when the verdict is sure enough to act on without asking the customer."""
        return self.confidence >= CONFIDENT

    @property
    def decided_by(self) -> str:
        """The evidence the verdict rests on (for the PROOF_CLASSIFY log line)."""
        return self.source


def matches_total(amounts: list[float], total: float) -> bool:
    """True when one of `amounts` is the order total (±₹1)."""
    return any(abs(a - float(total)) <= AMOUNT_TOLERANCE for a in amounts)


def _compact(text: str) -> str:
    """Lower-case text without whitespace — OCR breaks ids across lines and adds spaces."""
    return re.sub(r"\s+", "", (text or "").lower())


def upi_id_in_text(text: str, upi_ids: list[str] | tuple[str, ...]) -> str | None:
    """The shop's UPI id when it appears in the OCR text (exact once spaces are removed, else a near match for OCR slips)."""
    compact = _compact(text)
    if not compact:
        return None
    for raw in upi_ids:
        upi = _compact(raw or "")
        if len(upi) < 6 or "@" not in upi:
            continue
        if upi in compact:
            return raw
        if len(upi) >= 9:
            try:
                from rapidfuzz import fuzz

                if fuzz.partial_ratio(upi, compact) >= 92:
                    return raw
            except Exception:  # noqa: BLE001 - rapidfuzz is optional here
                pass
    return None


def score_text(text: str, order_totals: list[float], upi_ids: list[str] | tuple[str, ...] = ()) -> tuple[int, list[str], list[float]]:
    """(payment score, the signals behind it, the amounts read) for the OCR text of an image; higher = more like a payment receipt."""
    text = text or ""
    reading = read_amounts(text)
    amounts = reading.best
    signals: list[str] = []
    score = 0
    has_currency = bool(reading.strong) or bool(_CURRENCY_RE.search(text))
    if _UTR_LABEL_RE.search(text) or _TWELVE_DIGITS_RE.search(text):
        score += 3
        signals.append("utr")
    if any(matches_total(amounts, t) for t in order_totals):
        score += 3 if (has_currency or _SUCCESS_RE.search(text)) else 2
        signals.append("amount_matches_order")
    if _SUCCESS_RE.search(text):
        score += 2
        signals.append("success_wording")
    if _APP_RE.search(text):
        score += 2
        signals.append("upi_app")
    if _BANK_SMS_RE.search(text):
        score += 2
        signals.append("bank_sms")
    if has_currency and amounts:
        score += 1
        signals.append("currency")
    if upi_id_in_text(text, upi_ids):
        score += 4
        signals.append("recipient_upi_id")
    return score, signals, amounts


# ── seams ────────────────────────────────────────────────────────────────────

def _ocr_sync(image_bytes: bytes) -> str | None:
    """Tesseract over the whole image (downscaled); None when OCR itself is unavailable — which is NOT the same as "no text"."""
    try:
        import pytesseract
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(image_bytes))
        if image.mode != "RGB":
            image = image.convert("RGB")
        if max(image.size) > _OCR_MAX_SIDE:
            scale = _OCR_MAX_SIDE / max(image.size)
            image = image.resize((int(image.width * scale), int(image.height * scale)))
        gray = ImageOps.grayscale(image)
        if sum(gray.resize((16, 16)).getdata()) / 256 < 110:          # dark mode (light text on dark): Tesseract wants dark on light
            gray = ImageOps.invert(gray)
        return pytesseract.image_to_string(gray, config="--psm 6") or ""
    except Exception as exc:  # noqa: BLE001 - a missing binary / corrupt image: no evidence, never an exception into the customer's turn
        logger.warning("proof OCR unavailable: %s", exc)
        return None


#: Pictures smaller than this (either side) are never judged blank: a thumbnail / placeholder says nothing about a camera in the dark.
MIN_JUDGED_SIDE = 32


def image_luma_stats(image_bytes: bytes) -> tuple[int, float, float] | None:
    """(highest, mean, standard deviation) of the grey values 0–255 of a downscaled copy; None when the bytes are not an image (or too small to judge)."""
    try:
        import statistics

        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        if min(image.size) < MIN_JUDGED_SIDE:
            return None
        image.draft("L", (128, 128))
        values = list(image.convert("L").resize((64, 64)).tobytes())
        return max(values), statistics.fmean(values), statistics.pstdev(values)
    except Exception:  # noqa: BLE001 - not an image: the other evidence decides
        return None


def is_blank_image(image_bytes: bytes, *, max_luma: int | None = None) -> bool:
    """
    True for a black / blank frame: the brightest pixel is under ``image_blank_max_luma`` (a phone camera in the dark, a covered lens, a failed
    screenshot) or the picture has no contrast at all. conv=60 ORD-2026-0079: the "₹1,758 screenshot" was a frame whose brightest pixel was 6 of
    255 — Tesseract returned 150 characters of noise and its digits became the "amounts" ``[7.0, 2.0]``.
    """
    from app.config import get_settings

    stats = image_luma_stats(image_bytes)
    if stats is None:
        return False
    highest, _mean, deviation = stats
    return highest <= (get_settings().image_blank_max_luma if max_luma is None else max_luma) or deviation < 1.5


async def ocr_text(image_bytes: bytes) -> str | None:
    """OCR text of an image, run off the event loop (None = OCR unavailable)."""
    return await asyncio.to_thread(_ocr_sync, image_bytes)


_VISION_PROMPT = (
    "A shopper sent this image to a clothing shop. Decide what it is. Reply with JSON only:\n"
    '{"kind": "payment_proof" | "product_photo" | "unknown", "confidence": 0.0-1.0, "category": string|null, "colours": [string], '
    '"fabric": string|null, "keywords": [string], "amount": number|null}\n'
    "payment_proof = a screen or screenshot of a UPI / bank payment: an amount, 'paid' / 'successful', a green tick, a transaction id, "
    "an app such as GPay / PhonePe / Paytm / BHIM, or a bank SMS. Dark-mode and cropped screens are common.\n"
    "product_photo = a photo of clothing, a fabric, an accessory, a person wearing clothes, or a catalogue picture.\n"
    "unknown = anything else, or you are not sure (a chat screenshot, a selfie, a document, a blurry picture). "
    "A picture of a phone/app screen is NEVER a product_photo.\n"
    "STRICT: if kind is \"unknown\" then confidence MUST be 0.5 or lower (confidence is how sure you are of the kind you chose). "
    "For payment_proof only, amount = the rupee amount paid as a plain number (1758, not \"₹1,758\"), else null. "
    "For product_photo only, fill category (one lowercase word: saree, kurti, suit, lehenga, dress, bag, shoes …), colours (the main ones, "
    "English), fabric (silk, cotton, georgette … or null) and up to 3 keywords (style words like banarasi, embroidered, printed)."
)


def vision_problem(data: dict) -> str | None:
    """Why the model's JSON cannot be trusted as it stands (None when it is consistent): unknown kind, or "unknown" claimed with high confidence."""
    from app.config import get_settings

    kind = str(data.get("kind", "")).lower().strip()
    if kind == "other":
        kind = UNKNOWN
    if kind not in KINDS:
        return f"kind={kind!r} is not one of {KINDS}"
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return f"confidence={data.get('confidence')!r} is not a number"
    if not 0.0 <= conf <= 1.0:
        return f"confidence={conf} is outside 0..1"
    if kind == UNKNOWN and conf > get_settings().vision_unknown_max_confidence:
        return f"kind=unknown with confidence={conf} (an \"unknown\" verdict cannot be a confident one)"
    return None


def _vision_read_from(data: dict) -> VisionRead | None:
    """A ``VisionRead`` from the model's JSON (None when the kind is not one of ours); an "unknown" verdict's confidence is clamped."""
    from app.config import get_settings

    kind = str(data.get("kind", "")).lower().strip()
    if kind == "other":
        kind = UNKNOWN
    if kind not in KINDS:
        return None
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    if kind == UNKNOWN:
        conf = min(conf, get_settings().vision_unknown_max_confidence)

    def _strs(value) -> list[str]:
        """A list of short lower-case strings from whatever the model returned."""
        if isinstance(value, str):
            value = [value]
        return [str(v).strip().lower() for v in (value or []) if isinstance(v, (str, int, float)) and str(v).strip()][:4]

    category = data.get("category")
    fabric = data.get("fabric")
    amount = _vision_amount(data.get("amount"))
    return VisionRead(
        kind, conf, str(category).strip().lower() if isinstance(category, str) and category.strip() else None,
        _strs(data.get("colours") or data.get("colors")), str(fabric).strip().lower() if isinstance(fabric, str) and fabric.strip() else None,
        _strs(data.get("keywords")), amount,
    )


def _vision_amount(value) -> float | None:
    """The amount the model reported as a plausible rupee value (``"₹1,758"`` and ``1758.0`` both work), else None."""
    from app.services import payment_amounts

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        reading = payment_amounts.read_amounts(f"₹{value.strip().lstrip('₹').strip()}")
        number = reading.best[0] if reading.best else None
    else:
        return None
    return number if number is not None and payment_amounts.MIN_AMOUNT <= number <= payment_amounts.MAX_AMOUNT else None


async def _vision_call(image_bytes: bytes, prompt: str, *, client_id: int | None, conversation_id: int | None) -> str:
    """One vision-model call; the raw final text."""
    import base64

    from app.config import get_settings
    from app.services import llm_client

    b64 = base64.b64encode(image_bytes).decode()
    response = await llm_client.llm_call(
        "proof_classify", get_settings().llm_model_vision,
        [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        client_id=client_id, conversation_id=conversation_id, max_tokens=180, use_breaker=False,
    )
    return llm_client.final_text(response)


_STRICT_RETRY_SUFFIX = (
    "\n\nYour previous reply broke the schema ({problem}). Reply again with ONE JSON object only, exactly the schema above; "
    "kind is one of payment_proof, product_photo, unknown; when kind is unknown, confidence must be 0.5 or lower."
)


async def vision_read(image_bytes: bytes, *, client_id: int | None = None, conversation_id: int | None = None) -> VisionRead | None:
    """
    Ask the vision model what the image is (and, for a product, what it shows); None when vision is off or the call failed.

    The JSON is validated: an unknown ``kind``, a non-number confidence, or "unknown" claimed with high confidence is logged
    (``VISION_SCHEMA_MISMATCH`` with the raw reply), retried ONCE with a strict instruction, and — if it still contradicts itself — clamped
    (an "unknown" verdict never keeps confidence above ``vision_unknown_max_confidence``).
    """
    import json as _json

    from app.services import llm_client, llm_health

    if not llm_health.is_vision_enabled():
        return None
    prompt = _VISION_PROMPT
    try:
        for attempt in (1, 2):
            raw = await _vision_call(image_bytes, prompt, client_id=client_id, conversation_id=conversation_id)
            block = llm_client.first_json_block(raw)
            try:
                data = _json.loads(block) if block else None
            except ValueError:
                data = None
            problem = "no JSON object in the reply" if not isinstance(data, dict) else vision_problem(data)
            if problem is None:
                return _vision_read_from(data)
            logger.warning("VISION_SCHEMA_MISMATCH attempt=%d problem=%s raw=%s", attempt, problem, (raw or "")[:400])
            if attempt == 1:
                prompt = _VISION_PROMPT + _STRICT_RETRY_SUFFIX.format(problem=problem)
                continue
            return _vision_read_from(data) if isinstance(data, dict) else None          # still inconsistent: clamped, never trusted as sure
    except Exception as exc:  # noqa: BLE001
        logger.warning("proof vision classification failed: %s", exc)
    return None


# ── the verdict ──────────────────────────────────────────────────────────────

async def _cancel(task: "asyncio.Task | None") -> None:
    """Cancel a background evidence task and swallow its end."""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _from_ocr(score: int, signals: list[str], amounts: list[float], prior: bool, ocr_len: int | None) -> ProofVerdict | None:
    """A payment-proof verdict when the OCR evidence alone is enough (higher bar outside the payment window), else None."""
    need = 3 if prior else 4
    if score >= 5:
        return ProofVerdict(PAYMENT_PROOF, 0.95 if score >= 7 else 0.9, signals, amounts, "ocr", ocr_len=ocr_len, prior=prior)
    if score >= need:
        return ProofVerdict(PAYMENT_PROOF, 0.8, signals + (["recent_payment_instructions"] if prior and score == 3 else []), amounts, "ocr", ocr_len=ocr_len, prior=prior)
    return None


async def classify(
    image_bytes: bytes | None, *, order_totals: list[float], caption: str | None = None, catalogue_sku: str | None = None,
    sku_probe: Callable[[], Awaitable[str | None]] | None = None, catalogue_match: str | None = None,
    use_vision: bool = True, client_id: int | None = None, conversation_id: int | None = None,
    prior_payment: bool = False, upi_ids: list[str] | tuple[str, ...] = (),
) -> ProofVerdict:
    """
    What is this image?

    `catalogue_sku` / `sku_probe` = a product code of this shop read from the image's watermark: that is a product photo, whatever else it
    shows. `catalogue_match` = a SKU found by perceptual hash. `order_totals` = the totals of the customer's unpaid orders (amount
    matching). `prior_payment` = an order waits for money and its instructions were sent recently. `upi_ids` = the shop's UPI id(s):
    seeing one on the picture is a strong payment signal.
    """
    caption = (caption or "").strip()
    if catalogue_sku:
        return ProofVerdict(PRODUCT_PHOTO, 0.97, [f"catalogue_code:{catalogue_sku}"], [], "sku", prior=prior_payment, sku=catalogue_sku)
    if catalogue_match:
        return ProofVerdict(PRODUCT_PHOTO, 0.92, [f"catalogue_image:{catalogue_match}"], [], "phash", prior=prior_payment, sku=catalogue_match)
    caption_proof = bool(caption and _CAPTION_PROOF_RE.search(caption))
    caption_enquiry = bool(caption and not caption_proof and _CAPTION_ENQUIRY_RE.search(caption))
    if caption_enquiry:
        return ProofVerdict(PRODUCT_PHOTO, 0.9, ["caption_is_an_enquiry"], [], "caption", prior=prior_payment)
    if not image_bytes:
        return ProofVerdict(PAYMENT_PROOF if caption_proof else UNKNOWN, 0.8 if caption_proof else 0.3, ["no_image_bytes"], [], "caption" if caption_proof else "none", prior=prior_payment)

    # A black / blank frame has nothing to read: no OCR, no model call, no amounts — the customer is asked to send it again.
    if await asyncio.to_thread(is_blank_image, image_bytes):
        return ProofVerdict(UNKNOWN, 0.9, ["image_blank"], [], "quality", ocr_len=0, prior=prior_payment)

    sku_task = asyncio.ensure_future(sku_probe()) if sku_probe else None
    ocr_task = asyncio.ensure_future(ocr_text(image_bytes))
    vision_task = asyncio.ensure_future(vision_read(image_bytes, client_id=client_id, conversation_id=conversation_id)) if use_vision else None
    try:
        if sku_task is not None:
            sku = await sku_task
            if sku:
                return ProofVerdict(PRODUCT_PHOTO, 0.97, [f"catalogue_code:{sku}"], [], "sku", prior=prior_payment, sku=sku)
        text = await ocr_task
        score, signals, amounts = score_text(text or "", order_totals, upi_ids)
        if caption_proof:
            score += 4          # the customer wrote "paid" / "UTR …" with the image: as strong as a receipt that matches the order
            signals.append("caption_mentions_payment")
        ocr_len = None if text is None else len(text.strip())
        decided = _from_ocr(score, signals, amounts, prior_payment, ocr_len)
        if decided is not None:
            # OCR is sure it is a receipt but its amount fits none of several unpaid orders: let the model read the amount too (second source).
            if vision_task is not None and len(order_totals) > 1 and not any(matches_total(decided.amounts, t) for t in order_totals):
                with contextlib.suppress(Exception):
                    seen_amount = await asyncio.wait_for(vision_task, timeout=VISION_AMOUNT_WAIT_SECONDS)
                    _adopt_vision_amount(decided, seen_amount, order_totals)
            return decided                           # the vision call (if any) is cancelled in `finally`
        seen = await vision_task if vision_task is not None else None
    finally:
        await _cancel(sku_task)
        await _cancel(ocr_task)
        await _cancel(vision_task)
    return _combine(seen, score, signals, amounts, ocr_len, prior_payment, order_totals)


#: How long a decided-by-OCR receipt waits for the vision model's amount (only when OCR's own amount fits no unpaid order).
VISION_AMOUNT_WAIT_SECONDS = 8.0


def _adopt_vision_amount(verdict: "ProofVerdict", seen: "VisionRead | None", order_totals: list[float]) -> None:
    """Add the vision model's amount to the verdict when it agrees with an unpaid order's total (the amount that agrees wins; a stray one is ignored)."""
    if seen is None or seen.amount is None or not any(matches_total([seen.amount], t) for t in order_totals):
        return
    verdict.amounts = [seen.amount, *[a for a in verdict.amounts if a != seen.amount]]
    verdict.signals = [*verdict.signals, "vision_amount_matches_order"]
    verdict.vision, verdict.vision_label, verdict.vision_conf = seen, seen.kind, seen.confidence


def _combine(
    seen: VisionRead | None, score: int, signals: list[str], amounts: list[float], ocr_len: int | None, prior: bool,
    order_totals: "list[float] | None" = None,
) -> ProofVerdict:
    """OCR was not decisive: weigh what the vision model saw against the (weak) receipt signals."""
    if seen is not None and seen.amount is not None and (not amounts or order_totals and not any(matches_total(amounts, t) for t in order_totals)):
        # The model read an amount the OCR could not (a dark screenshot): it counts as the second source for matching an order.
        amounts = [seen.amount, *amounts]
        signals = signals + ["vision_amount"]
    base = dict(amounts=amounts, ocr_len=ocr_len, prior=prior, vision=seen)
    label, conf = (seen.kind, seen.confidence) if seen is not None else (None, None)
    if seen is not None and seen.kind == PAYMENT_PROOF:
        if seen.confidence >= CONFIDENT or (prior and seen.confidence >= 0.5) or (score >= 2 and seen.confidence >= 0.5):
            return ProofVerdict(PAYMENT_PROOF, max(seen.confidence, 0.8 if (prior or score >= 2) else seen.confidence), signals + ["vision_payment"], source="vision", vision_label=label, vision_conf=conf, **base)
    if seen is not None and seen.kind == PRODUCT_PHOTO:
        bar = PRODUCT_VISION_MIN_IN_PRIOR if prior else PRODUCT_VISION_MIN
        if seen.confidence >= bar and score < 2:           # a picture with receipt-like text is not settled by vision alone
            return ProofVerdict(PRODUCT_PHOTO, seen.confidence, signals + ["vision_product"], source="vision", vision_label=label, vision_conf=conf, **base)
        signals = signals + ["vision_product_not_sure_enough" if score < 2 else "vision_product_but_receipt_text"]
    if score >= 3:      # weak receipt evidence and nothing contradicting it
        return ProofVerdict(PAYMENT_PROOF, 0.6, signals, source="ocr", vision_label=label, vision_conf=conf, **base)
    return ProofVerdict(UNKNOWN, 0.4, signals or (["no_evidence"] if seen is None else ["vision_unsure"]), source="vision" if seen is not None else "none", vision_label=label, vision_conf=conf, **base)
