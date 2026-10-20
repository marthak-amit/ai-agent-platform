"""
Manual UPI payment verification — the engine behind "customer pays the seller's
UPI directly, sends a screenshot, seller approves/rejects from the dashboard".

We never collect money. The LLM only *understands* ("paid" intent); everything
that changes state or talks to the customer here is deterministic: state
transitions go through order_state_machine, replies are EN/HI/GU templates.

Concurrency/idempotency model: every state change takes a row lock on the
order (SELECT … FOR UPDATE) and re-reads its status, so a double-clicked
Approve — or an approve racing a reject/expiry — serialises; the loser sees the
new status, performs no stock change and sends no message (`changed=False`).
Approve deducts stock, consumes the reservation, flips the order to paid and
writes the audit row in ONE transaction; customer/owner notifications happen
after the commit and can never roll the payment back.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.conversation import Conversation
from app.models.order import Order
from app.models.order_audit_log import OrderAuditLog
from app.models.payment_proof import (
    PROOF_APPROVED,
    PROOF_PENDING,
    PROOF_REJECTED,
    PROOF_WITHDRAWN,
    PaymentProof,
)
from app.config import get_settings
from app.services import (
    channel_sender,
    media_service,
    order_service,
    realtime_service,
    stock_reservation_service,
)
from app.services.language_templates import format_price, get_template
from app.services.order_state_machine import (
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_PAYMENT_SUBMITTED,
    ORDER_PENDING_PAYMENT,
    PAYMENT_OPEN_STATES,
    InvalidOrderTransition,
    assert_order_transition,
)
from app.services.send_gate import MessageKind

logger = logging.getLogger(__name__)

#: name@handle — handle is the PSP suffix (okaxis, ybl, upi, paytm, …).
UPI_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$")

#: Minimum gap between "verification in progress" acks for extra screenshots.
PROOF_ACK_THROTTLE = timedelta(minutes=10)

#: Waiting longer than this is flagged red in the dashboard.
OVERDUE_AFTER = timedelta(hours=2)

# ─── errors ───────────────────────────────────────────────────────────────────


class ManualReviewRequired(Exception):
    """
    A high-value order cannot be approved until the seller acknowledges the manual review (HTTP 409).

    Carries PLAIN values captured when it is raised — never the ORM ``Order``. The router rolls the transaction back before it
    answers, which expires every ORM instance; reading ``exc.order.total_amount`` after that is a lazy load in async code
    (``MissingGreenlet`` → HTTP 500). Rule for every exception that crosses a transaction boundary: copy what the handler needs.
    """

    def __init__(self, order: "Order", threshold: float):
        self.order_id: int = int(order.id)
        self.order_number: str = str(order.order_number)
        self.total: float = float(order.total_amount or 0)
        self.threshold: float = float(threshold)
        super().__init__(
            f"Order {self.order_number} (₹{int(self.total):,}) is above the high-value limit "
            f"(₹{int(self.threshold):,}): verify the payment in your bank, then approve with confirm_high_value."
        )

    def payload(self) -> dict:
        """The 409 body the dashboard turns into its "verify ₹X in your bank" dialog."""
        return {
            "code": "high_value_confirmation_required", "order_number": self.order_number, "order_id": self.order_id,
            "total": self.total, "threshold": self.threshold,
            "message": f"Order {self.order_number} is ₹{int(self.total):,}. Verify the money in your bank before approving.",
        }


class OrderNotFound(Exception):
    """The order does not exist for this client."""


# ─── small pure helpers ───────────────────────────────────────────────────────


def high_value_threshold(client: Client | None) -> float:
    """Orders above this total need the seller's explicit review: the shop's own limit, else the platform default (₹50,000)."""
    own = getattr(client, "high_value_order_threshold", None)
    if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0:
        return float(own)
    return float(get_settings().high_value_order_threshold)


def is_high_value(order: Order, client: Client | None) -> bool:
    """True when the order total is above the high-value threshold."""
    total = getattr(order, "total_amount", None)
    return isinstance(total, (int, float)) and float(total) > high_value_threshold(client)


def is_valid_upi_id(value: str) -> bool:
    """Return True when value looks like name@handle."""
    return bool(UPI_ID_RE.fullmatch(value or ""))


def build_upi_uri(upi_id: str, payee_name: str | None, amount: float, order_id: int) -> str:
    """
    Build the upi://pay deep link encoded into the QR.

    Format: upi://pay?pa={upi_id}&pn={payee}&am={amount}&cu=INR&tn=Order{order_id}
    """
    return (
        f"upi://pay?pa={quote(upi_id, safe='@')}"
        f"&pn={quote(payee_name or '', safe='')}"
        f"&am={amount:.2f}&cu=INR&tn=Order{order_id}"
    )


_qr_failure_warned = False


def _warn_qr_failure_once(order_id: int, exc: Exception) -> None:
    """Log QR generation failure at WARNING the first time only (text payment message is still sent)."""
    global _qr_failure_warned
    if _qr_failure_warned:
        logger.debug("UPI QR generation failed for order %s: %s", order_id, exc)
        return
    _qr_failure_warned = True
    logger.warning(
        "UPI QR generation failed (order %s): %s: %s — sending the text payment message without a QR. "
        "Further failures are logged at DEBUG.", order_id, type(exc).__name__, exc,
    )


_qr_url_warned = False


def is_public_url(url: str | None) -> bool:
    """
    True when Meta's servers could fetch `url`: an absolute http(s) URL that isn't loopback/private.

    A relative '/uploads/…' path (BACKEND_PUBLIC_URL and R2 both unset) or a localhost URL is rejected
    by the WhatsApp Cloud API when it tries to download the image.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    return not (
        host in ("localhost", "0.0.0.0", "::1") or host.startswith(("127.", "10.", "192.168."))
        or host.endswith((".local", ".internal"))
    )


def _warn_qr_url_once(url: str) -> None:
    """Warn (once per process) that the payment QR can't be delivered because its URL isn't public."""
    global _qr_url_warned
    if _qr_url_warned:
        return
    _qr_url_warned = True
    logger.warning(
        "Payment QR is generated but its URL %r is not publicly fetchable, so WhatsApp can't download it — "
        "sending the text payment message (with the 'send the screenshot' line) instead of a QR. "
        "Set BACKEND_PUBLIC_URL or configure R2_* to deliver the QR image.", url,
    )


def make_qr_png(data: str) -> bytes:
    """Render `data` as a PNG QR code (pure-python, no image-library dependency)."""
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


_PAID_RE = re.compile(
    r"(?:\b(?:paid|payment\s*(?:done|made|complete|completed|kar\s*diya|kiya|ho\s*gaya|kari\s*didhu|thai\s*gayu)|"
    r"(?:i\s*(?:have|'ve|ve)\s*)paid|done|sent|transferred|kar\s*diya|ho\s*gaya|bhej\s*diya|"
    r"pay\s*kar\s*diya|kari\s*didhu|thai\s*gayu|moklyu|mokli\s*didhu)\b"
    r"|पेमेंट\s*(?:कर\s*दिया|हो\s*गया)|भुगतान\s*कर\s*दिया|पैसे\s*भेज|ચુકવણી\s*કરી|પેમેન્ટ\s*(?:કરી|થઈ))",
    re.IGNORECASE,
)


_NOT_PAID_RE = re.compile(
    r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|did\s+not|have\s+not|has\s+not|yet\s+to|nahi|nahin|nhi|nathi|mat)\b",
    re.IGNORECASE,
)


def is_paid_intent(text: str) -> bool:
    """
    True for a short "I've paid" style message (EN/HI/GU, roman or native script).

    Length-capped so a long sentence that merely contains "done" ("is the
    order done?") is not misread as a payment claim. A negated claim ("I have not paid",
    "payment nahi kiya") is the opposite of a claim and is never one.
    """
    t = (text or "").strip()
    if not t or len(t.split()) > 8:
        return False
    return bool(_PAID_RE.search(t)) and not _NOT_PAID_RE.search(t)


# "This is payment photo" / "payment photo" / "maine pay kar diya" / "screenshot hai" / "aa payment chhe" / "already paid" — the customer
# saying the image(s) they sent ARE the payment (the mirror of the denial below). Deterministic: the planner never sees these.
_PAYMENT_AFFIRMATION_RE = re.compile(
    # English
    r"(?:\b(?:this|that|these|those|it)\s+(?:is|was|are)\s+(?:the\s+|my\s+|a\s+)?(?:payment|paid|proof|receipt)\b)"
    r"|(?:\b(?:it'?s|its)\s+(?:the\s+|my\s+|a\s+)?(?:payment|proof|receipt)\b)"
    r"|(?:^\s*(?:the\s+|my\s+)?payment\s+(?:photo|screenshot|pic|picture|image|proof|receipt|ss)\s*[.!]*\s*$)"
    r"|(?:\b(?:already|just|i\s*(?:have|'ve|ve)?)\s*paid\b)|(?:\bi\s+(?:have\s+)?(?:made|done)\s+(?:the\s+)?payment\b)|(?:\bpayment\s+(?:is\s+)?(?:done|made|complete|completed)\b)"
    r"|(?:^\s*(?:paid|done|payment\s+done)\s*[.!]*\s*$)"
    # Hinglish
    r"|(?:\b(?:maine|maene|mene|main\s*ne|ne)\s+(?:pay|payment|paisa|paise)\s+(?:kar|kiya|bhej)\b)|(?:\b(?:pay|payment)\s+kar\s*(?:diya|di)\b)"
    r"|(?:\bscreenshot\s+(?:hai|h|he|bheja|bhej\s*diya)\b)|(?:\b(?:ye|yeh|yah)\s+(?:to\s+)?payment\s+(?:ka\s+)?(?:screenshot\s+|photo\s+)?(?:hai|h|he)\b)"
    r"|(?:\bpayment\s+(?:ka\s+)?(?:photo|screenshot)\s+(?:hai|h|he)\b)|(?:\bpaisa\s+(?:bhej|de)\s*diya\b)"
    # Gujarati (Roman)
    r"|(?:\bmaine?\s+payment\s+kari\b)|(?:\bpayment\s+kari\s*(?:didhu|didhi|lidhu|nakhyu)\b)|(?:\b(?:aa|a)\s+(?:to\s+)?payment\s+(?:no\s+|nu\s+)?(?:screenshot\s+|photo\s+)?(?:chhe|che|chee)\b)"
    r"|(?:\bscreenshot\s+(?:chhe|che|moklyo|mokli\s*didho)\b)|(?:\bpaisa\s+moklya\b)"
    # Devanagari / Gujarati script
    r"|(?:(?:यह|ये|यही)\s+(?:तो\s+)?पेमेंट\s+(?:का\s+)?(?:स्क्रीनशॉट\s+|फोटो\s+|फ़ोटो\s+)?है)|(?:पेमेंट\s+(?:का\s+)?(?:फोटो|फ़ोटो|स्क्रीनशॉट)(?:\s+है)?\s*$)|(?:स्क्रीनशॉट\s+है)"
    r"|(?:मैंने\s+(?:पेमेंट|पैसे)\s+(?:कर|भेज)\s*(?:दिया|दिए))"
    r"|(?:આ\s+(?:તો\s+)?પેમેન્ટ\s+(?:નો\s+)?(?:સ્ક્રીનશૉટ\s+|સ્ક્રીનશોટ\s+|ફોટો\s+)?છે)|(?:પેમેન્ટ\s+(?:નો\s+)?(?:ફોટો|સ્ક્રીનશૉટ|સ્ક્રીનશોટ)(?:\s+છે)?\s*$)|(?:સ્ક્રીનશૉટ\s+છે)"
    r"|(?:મેં\s+પેમેન્ટ\s+કરી\s*(?:દીધું|દીધી|લીધું))",
    re.IGNORECASE,
)


def is_payment_affirmation(text: str) -> bool:
    """True when the customer says the image(s) they sent are the payment ("this is payment photo", "payment kari didhu", "already paid")."""
    t = (text or "").replace("’", "'").strip()
    if not t or "?" in t or len(t.split()) > 10:
        return False
    return bool(_PAYMENT_AFFIRMATION_RE.search(t)) and not _NOT_PAID_RE.search(t) and not is_payment_denial(t)


_PAYMENT_STATUS_RE = re.compile(
    r"(?:\b(?:confirm|check|verify|status|update)\b[^.?!\n]{0,24}\bpayments?\b)|(?:\bpayments?\b[^.?!\n]{0,16}\b(?:status|confirm(?:ed|ation)?|verified|verification|received|recieved|check|update)\b)"
    r"|(?:\bdid\s+(?:you|u)\s+(?:get|receive|recieve|see)\b[^.?!\n]{0,24}\bpayments?\b)|(?:\bhave\s+(?:you|u)\s+(?:got|received|recieved)\b[^.?!\n]{0,24}\bpayments?\b)"
    r"|(?:\bpayments?\b[^.?!\n]{0,16}\b(?:mila|mil\s*gaya|aaya|aa\s*gaya|malyu|aavyu)\b)|(?:\bpayment\s+(?:confirm|check)\s+(?:karo|kar\s*do|karjo)\b)"
    r"|(?:पेमेंट[^.?!\n]{0,12}(?:मिला|कन्फर्म|कन्फ़र्म|स्टेटस))|(?:પેમેન્ટ[^.?!\n]{0,12}(?:મળ્યું|કન્ફર્મ|સ્ટેટસ))",
    re.IGNORECASE,
)


def is_payment_status_request(text: str) -> bool:
    """True for "please confirm my payments" / "payment status?" / "did you get my payment" / "payment mila?" (a status question, not a claim)."""
    t = (text or "").strip()
    return bool(t) and len(t.split()) <= 12 and bool(_PAYMENT_STATUS_RE.search(t)) and not is_payment_denial(t)


# Messages from a customer who owes (or says they don't owe) a payment: asking for the details, the UPI id /
# QR, how to pay, or saying they haven't paid / haven't sent the screenshot yet. All are answered by
# re-sending the open order's payment instruction — never by resetting the conversation.
_PAYMENT_HELP_RE = re.compile(
    r"\bpayment\s*(?:details?|info|information|link|process|method|options?|kahan|kaise|kese)\b"
    r"|\b(?:how|where)\s+(?:to|do\s+i|can\s+i|should\s+i|will\s+i)\s+pay\b"
    r"|\b(?:kaise|kese|kem|kaha|kahan)\s+(?:karu\s+)?pay\b|\bpay\s+(?:kaise|kese|kem|kahan)\b"
    r"|\b(?:upi|qr|gpay|phonepe|paytm|scanner)\b"
    r"|\b(?:not|never|haven'?t|didn'?t|did\s+not|have\s+not|yet\s+to)\s+(?:\w+\s+){0,3}?(?:paid|pay|sent|send|screenshot|payment)\b"
    r"|\b(?:payment|pay|paid|screenshot)\s+(?:abhi\s+)?(?:nahi|nahin|nhi|nathi)\b"
    r"|\bnahi\s+(?:kiya|bheja|hua)\b"
    r"|\bsend\s+(?:me\s+)?(?:the\s+)?payment\b|\bgive\s+(?:me\s+)?(?:the\s+)?payment\b"
    r"|\bhow\s+much\s+(?:do\s+i\s+)?(?:to\s+)?pay\b|\bkitna\s+pay\b",
    re.IGNORECASE,
)


# "This is product, not the payment" / "ye payment nahi hai" / "aa payment nathi" / "galat photo" — the customer saying the image they just
# sent is NOT a payment proof. Deliberately strict (a demonstrative + "payment", or an explicit "wrong photo"): "I haven't paid yet" and
# "payment nahi hua" are payment-HELP requests, not denials.
_PAYMENT_DENIAL_RE = re.compile(
    # English
    r"(?:\b(?:not|isn'?t|is\s+not|wasn'?t|was\s+not)\s+(?:a\s+|the\s+|my\s+|any\s+)?payment\b)"
    r"|(?:\b(?:this|that|these|those|it)\s+(?:is|was|are)\s+(?:a\s+|the\s+|my\s+)?(?:product|item|dress|saree|sari|kurti|design|catalogue)\b)"
    r"|(?:\bit'?s\s+(?:a\s+|the\s+|my\s+)?(?:product|item|dress|saree|sari|kurti|design)\b)"
    r"|(?:\bwrong\s+(?:photo|image|pic|picture|screenshot|file|one)\b)|(?:\b(?:by|from)\s+mistake\b)|(?:\bmistakenly\b)"
    r"|(?:\b(?:photo|image|pic|picture)\s+(?:of|is)\s+(?:a\s+|the\s+)?product\b)"
    # Hinglish
    r"|(?:\b(?:ye|yeh|yah|isko|ise)\s+(?:to\s+)?(?:payment|pay|bhugtan)\s+(?:ka\s+)?(?:screenshot\s+)?(?:nahi|nahin|nhi)\b)"
    r"|(?:\bpayment\s+(?:ka\s+)?(?:screenshot\s+)?(?:nahi|nahin|nhi)\s+hai\s+(?:ye|yeh)\b)"
    r"|(?:\b(?:ye|yeh|yah)\s+(?:to\s+)?(?:product|item|kapda|saree|kurti)\s+(?:hai|h|he|ki\s+(?:photo|image|pic))\b)"
    r"|(?:\bgalat\s+(?:photo|image|pic|picture|screenshot)\b)|(?:\bgalti\s+se\b)"
    # Gujarati (Roman)
    r"|(?:\baa\s+(?:to\s+)?(?:payment|pay)\s+(?:no\s+)?(?:screenshot\s+)?nathi\b)|(?:\baa\s+(?:to\s+)?(?:product|item)\s+(?:chhe|che|no\s+(?:photo|image))\b)"
    r"|(?:\bkhot[ua]\s+(?:photo|image|pic|screenshot)\b)|(?:\bbhul\s*(?:thi|thii|se)\b)|(?:\bbhool\s*(?:thi|se)\b)"
    # Devanagari
    r"|(?:(?:ये|यह)\s+(?:पेमेंट|भुगतान|पेमन्ट)\s+(?:का\s+)?(?:स्क्रीनशॉट\s+)?(?:नहीं|नही))|(?:(?:ये|यह)\s+(?:तो\s+)?(?:प्रोडक्ट|आइटम|साड़ी|कुर्ती)\s+है)"
    r"|(?:गलत\s+(?:फोटो|फ़ोटो|इमेज|तस्वीर|स्क्रीनशॉट))|(?:गलती\s+से)"
    # Gujarati script
    r"|(?:આ\s+(?:તો\s+)?(?:પેમેન્ટ|ચુકવણી)\s+(?:નો\s+)?(?:સ્ક્રીનશૉટ\s+|સ્ક્રીનશોટ\s+)?નથી)|(?:આ\s+(?:તો\s+)?(?:પ્રોડક્ટ|આઇટમ|સાડી|કુર્તી)\s+છે)"
    r"|(?:ખોટ[ોુ]\s+(?:ફોટો|ઇમેજ|સ્ક્રીનશૉટ|સ્ક્રીનશોટ))|(?:ભૂલથી)",
    re.IGNORECASE,
)


def is_payment_denial(text: str) -> bool:
    """True when the customer says the image they sent is not a payment proof ("this is product not the payment", "ye payment nahi hai")."""
    text = (text or "").replace("’", "'")
    return "?" not in text and bool(_PAYMENT_DENIAL_RE.search(text))


def is_payment_help_request(text: str) -> bool:
    """
    True when the message asks for payment details / how to pay, or says the customer hasn't paid yet.

    A denial of the image just sent ("this is not the payment") is NOT a help request: re-sending the payment instruction to someone who is
    telling us the screenshot was the wrong picture is the loop conv=60 got stuck in.
    """
    if is_payment_denial(text):
        return False
    return bool(_PAYMENT_HELP_RE.search(text or ""))


def actor_label(user) -> str:
    """Human-readable audit label for a staff user ('system' when None)."""
    return getattr(user, "email", None) or "system"


def _lang(conv: Conversation | None) -> str:
    """Template language bucket for a conversation."""
    return getattr(conv, "last_customer_language", None) or "english"


def render_items(order: Order) -> str:
    """One bullet per line item: '• Name (Color/Size) × qty = ₹x'."""
    items = getattr(order, "line_items", None) or []
    lines = []
    if items:
        for li in items:
            variant = "/".join(p for p in (li.variant_color, li.variant_size, li.variant_material) if p)
            label = f"{li.product_name} ({variant})" if variant else li.product_name
            lines.append(f"• {label} × {li.quantity} = {format_price(li.subtotal)}")
    else:
        variant = "/".join(p for p in (order.variant_color, order.variant_size) if p)
        label = f"{order.product_name} ({variant})" if variant else order.product_name
        lines.append(f"• {label} × {order.quantity} = {format_price(order.total_amount)}")
    return "\n".join(lines)


# ─── payment instruction (sent when an order enters pending_payment) ──────────


@dataclass
class PaymentInstruction:
    """Deterministic payment message: text, optional QR image, and the closing line."""

    text: str
    qr_url: str | None
    closing: str


def instruction_text(order: Order, client: Client, lang: str) -> str:
    """Order summary + amount + UPI ID (+ seller's custom note) + the next-steps block, in the customer's language."""
    payee = getattr(client, "upi_display_name", None)
    extra = getattr(client, "payment_instructions", None)
    amount = format_price(order.total_amount)
    body = get_template(
        lang, "pay_instruction",
        order_number=order.order_number,
        items=render_items(order),
        amount=amount,
        upi_id=client.upi_id,
        payee_line=f"\n{payee}" if payee else "",
        extra=f"\n{extra}" if extra else "",
    )
    # Spell out the three steps so the customer knows what happens next (pay -> screenshot -> we verify).
    return f"{body}\n\n{get_template(lang, 'pay_steps', amount=amount)}"


async def build_payment_instruction(
    db: AsyncSession, order: Order, client: Client, conv: Conversation | None
) -> PaymentInstruction:
    """
    Build the order summary + amount + UPI ID text and the generated UPI QR.

    The QR encodes upi://pay?…&am={total}&tn=Order{id}. If generation or
    upload fails the seller's static QR (Client.upi_qr_url) is used instead;
    if neither exists the closing line is folded into the text so the
    instruction still ends with "send the screenshot".
    """
    lang = _lang(conv)
    if conv is not None:
        conv.last_payment_instruction_at = datetime.now(timezone.utc)      # the "recent payment instructions" prior of the proof classifier
    payee = getattr(client, "upi_display_name", None)
    text = instruction_text(order, client, lang)
    # WhatsApp can't always render the QR (private URL, suppressed image): the deep link keeps a one-tap
    # route to pay with the exact amount and order reference.
    try:
        link = build_upi_uri(client.upi_id, payee, order.total_amount, order.id)
        text = f"{text}\n\n{get_template(lang, 'pay_upi_link', upi_link=link)}"
    except Exception as exc:
        logger.debug("UPI deep link skipped for order %s: %s", order.id, exc)
    closing = get_template(lang, "pay_send_screenshot")

    qr_url: str | None = None
    try:
        png = make_qr_png(build_upi_uri(client.upi_id, payee, order.total_amount, order.id))
        qr_url = media_service.absolute_url(
            await media_service.store_media(client.id, png, "image/png", folder="payment-qr")
        )
    except Exception as exc:
        _warn_qr_failure_once(order.id, exc)
    if qr_url is not None and not is_public_url(qr_url):
        _warn_qr_url_once(qr_url)
        qr_url = None
    if qr_url is None and getattr(client, "upi_qr_url", None):
        static_url = media_service.absolute_url(client.upi_qr_url)
        if is_public_url(static_url):
            qr_url = static_url
        else:
            _warn_qr_url_once(static_url)

    if qr_url is None:
        text = f"{text}\n\n{closing}"
    return PaymentInstruction(text=text, qr_url=qr_url, closing=closing)


async def raise_missing_upi_alert(db: AsyncSession, client: Client, conv: Conversation | None) -> None:
    """
    A customer reached the payment step but the seller has no UPI ID.

    Stamps Client.payment_setup_alert_at (dashboard banner), pushes a realtime
    event, and pings the owner on WhatsApp — the latter at most once an hour. A simulator conversation raises
    nothing: the owner is the one testing.
    """
    if conv is not None and getattr(conv, "is_sandbox", False):
        return
    now = datetime.now(timezone.utc)
    previous = client.payment_setup_alert_at
    client.payment_setup_alert_at = now
    await db.commit()
    realtime_service.publish(client.id, "payment_setup_required", {"conversation_id": getattr(conv, "id", None)})
    if previous is not None and (now - previous) < timedelta(hours=1):
        return
    try:
        from app.services import outbound

        if client.phone:
            await outbound.send_owner_text(
                client.phone,
                "⚠️ A customer is ready to pay but you haven't added your UPI ID yet. "
                "Add it in Settings → Payment details so orders can continue.",
                phone_number_id=client.whatsapp_phone_number_id,
                access_token=client.whatsapp_access_token,
            )
    except Exception as exc:
        logger.warning("Missing-UPI owner ping failed for client %s: %s", client.id, exc)


# ─── internals ────────────────────────────────────────────────────────────────


async def _lock_order(db: AsyncSession, client_id: int, order_id: int) -> Order:
    """Fetch the order FOR UPDATE (scoped to the client), refreshing any cached state."""
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.client_id == client_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise OrderNotFound(order_id)
    return order


async def _conversation_for(db: AsyncSession, order: Order) -> Conversation | None:
    """The conversation an order belongs to, if any."""
    if not order.conversation_id:
        return None
    return (
        await db.execute(select(Conversation).where(Conversation.id == order.conversation_id))
    ).scalar_one_or_none()


def _audit(
    db: AsyncSession, order: Order, *, actor_user, actor_name: str, action: str,
    from_status: str | None, to_status: str | None, detail: dict | None = None,
) -> None:
    """Queue an audit-log row in the caller's transaction."""
    db.add(OrderAuditLog(
        client_id=order.client_id, order_id=order.id,
        actor_user_id=getattr(actor_user, "id", None), actor_name=actor_name,
        action=action, from_status=from_status, to_status=to_status, detail=detail,
    ))


async def _pending_proofs(db: AsyncSession, order_id: int) -> list[PaymentProof]:
    """Still-pending proofs of an order, locked for the decision."""
    return list(
        (
            await db.execute(
                select(PaymentProof)
                .where(PaymentProof.order_id == order_id, PaymentProof.status == PROOF_PENDING)
                .with_for_update()
            )
        ).scalars().all()
    )


async def _notify_customer(
    db: AsyncSession, client: Client, conv: Conversation | None, key: str, **kwargs
) -> channel_sender.SendOutcome | None:
    """
    Send a deterministic payment template to the customer.

    Deliberately NOT subject to the bot-pause flag: approve/reject/cancel are
    seller decisions, and the customer must hear about them even while the
    seller is handling the chat by hand. Still subject to opt-out/block/24h.
    """
    event = {"pay_confirmed": "payment_approved", "pay_rejected": "payment_rejected", "pay_cancelled": "payment_cancelled"}.get(key, key)
    if conv is None or getattr(conv, "is_sandbox", False):
        logger.info("notify_%s order=%s sent=false reason=%s", event, kwargs.get("order_number"), "no_conversation" if conv is None else "sandbox")
        return None
    text = get_template(_lang(conv), key, **kwargs)
    outcome = await channel_sender.send_to_conversation(
        db, client, conv, text=text,
        kind=MessageKind.UTILITY_TEMPLATE, sender_type="system",
    )
    # One line per seller decision: the customer is told (or why not) — the dashboard's "customer notified" flag is built from the same outcome.
    logger.info(
        "notify_%s order=%s sent=%s%s", event, kwargs.get("order_number"), str(bool(getattr(outcome, "sent", False))).lower(),
        "" if getattr(outcome, "sent", False) else f" reason={getattr(outcome, 'reason', None)}",
    )
    return outcome


async def _reset_conversation_after_terminal(db: AsyncSession, conv: Conversation | None) -> None:
    """Return a payment-stage conversation to a clean greeting state (best-effort)."""
    if conv is None or (conv.current_stage or "greeting") not in ("payment", "completed"):
        return
    try:
        from app.services.order_pipeline import _reset_order_slots_after_completion

        await _reset_order_slots_after_completion(db, conv.id, conv)
    except Exception as exc:
        logger.error("Conversation reset after terminal order failed (conv=%s): %s", conv.id, exc)


# ─── customer submits a screenshot ────────────────────────────────────────────


async def find_open_payment_order(db: AsyncSession, conversation_id: int) -> Order | None:
    """Latest non-COD order of the conversation still in the payment flow."""
    return (
        await db.execute(
            select(Order)
            .where(
                Order.conversation_id == conversation_id,
                Order.status.in_(tuple(PAYMENT_OPEN_STATES)),
                Order.payment_method != "COD",
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def find_open_payment_orders(db: AsyncSession, client_id: int, conversation_id: int, customer_phone: str | None = None, *, limit: int = 6) -> list[Order]:
    """
    Every non-COD order of this customer still in the payment flow (newest first): the conversation's own, plus — when the phone is known —
    the same customer's orders from another conversation. A real screenshot is matched to ONE of them by amount (see ``payment_proof_classifier``).
    """
    cond = Order.conversation_id == conversation_id
    if customer_phone:
        cond = or_(cond, Order.customer_phone == customer_phone)
    rows = await db.execute(
        select(Order)
        .where(Order.client_id == client_id, Order.status.in_(tuple(PAYMENT_OPEN_STATES)), Order.payment_method != "COD", cond)
        .order_by(Order.created_at.desc()).limit(limit)
    )
    return list(rows.scalars().all())


async def recent_payment_orders(
    db: AsyncSession, client_id: int, conversation_id: int, customer_phone: str | None = None, *, days: int = 7, limit: int = 6,
) -> list[Order]:
    """This customer's non-COD orders of the last `days` that are (or were) in the payment flow — unpaid, under review or paid — newest first."""
    cond = Order.conversation_id == conversation_id
    if customer_phone:
        cond = or_(cond, Order.customer_phone == customer_phone)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await db.execute(
        select(Order).where(
            Order.client_id == client_id, Order.status.in_((ORDER_PENDING_PAYMENT, ORDER_PAYMENT_SUBMITTED, ORDER_PAID)),
            Order.payment_method != "COD", Order.created_at >= since, cond,
        ).order_by(Order.created_at.desc()).limit(limit)
    )
    return list(rows.scalars().all())


@dataclass
class Withdrawal:
    """Plain values of a withdrawn proof (never the ORM rows: the transaction is over by the time a caller reads them)."""

    order_id: int
    order_number: str
    proof_id: int
    order_back_to_pending: bool        # no other pending proof was left, so the order is waiting for a payment screenshot again


#: A denial only withdraws a proof the customer sent recently.
WITHDRAW_WINDOW = timedelta(hours=6)


async def withdraw_last_proof(db: AsyncSession, client: Client, conversation_id: int, *, reason: str = "customer says the image is not a payment proof") -> Withdrawal | None:
    """
    The customer says the last image they sent as payment proof was NOT one ("this is product not the payment").

    The newest still-pending proof of the conversation becomes ``withdrawn`` (the row and an audit entry stay — nothing is deleted), so it drops out
    of the seller's pending queue; when no other pending proof remains the order goes back ``payment_submitted → pending_payment``. Returns None when
    there is no recent pending proof (the denial then needs no action). One transaction, row-locked, idempotent.
    """
    proof = (await db.execute(
        select(PaymentProof)
        .where(PaymentProof.client_id == client.id, PaymentProof.conversation_id == conversation_id, PaymentProof.status == PROOF_PENDING)
        .order_by(PaymentProof.id.desc()).limit(1).with_for_update()
    )).scalar_one_or_none()
    if proof is None:
        return None
    created = proof.created_at if proof.created_at is None or proof.created_at.tzinfo else proof.created_at.replace(tzinfo=timezone.utc)
    if created is not None and datetime.now(timezone.utc) - created > WITHDRAW_WINDOW:
        return None
    order = await _lock_order(db, client.id, proof.order_id)
    others = int((await db.execute(
        select(func.count()).select_from(PaymentProof).where(
            PaymentProof.order_id == order.id, PaymentProof.status == PROOF_PENDING, PaymentProof.id != proof.id)
    )).scalar_one())
    proof.status = PROOF_WITHDRAWN
    proof.reviewed_at = datetime.now(timezone.utc)
    proof.reviewed_by_name = "customer"
    proof.reject_reason = reason
    from_status = order.status
    reverted = False
    if order.status == ORDER_PAYMENT_SUBMITTED and others == 0:
        assert_order_transition(order.status, ORDER_PENDING_PAYMENT)
        order.status = ORDER_PENDING_PAYMENT
        order.payment_submitted_at = None
        reverted = True
    _audit(db, order, actor_user=None, actor_name="customer", action="proof_withdrawn", from_status=from_status, to_status=order.status,
           detail={"proof_id": proof.id, "reason": reason})
    out = Withdrawal(order_id=int(order.id), order_number=str(order.order_number), proof_id=int(proof.id), order_back_to_pending=reverted)
    conv_id = proof.conversation_id
    await db.commit()          # plain values were copied above: the commit expires every ORM instance
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": out.order_id, "conversation_id": conv_id, "decision": "withdrawn", "reviewed_by": "customer",
    })
    return out


@dataclass
class ProofSubmission:
    """Outcome of attaching a screenshot to an order."""

    proof: PaymentProof
    transitioned: bool  # True when this proof moved the order pending_payment → payment_submitted
    high_value: bool = False  # the order is above the high-value limit: manual review, no automatic confirmation


async def submit_proof(
    db: AsyncSession, client: Client, conv: Conversation, order_id: int, *,
    message_id: int | None, media_url: str, reclassified: bool = False,
) -> ProofSubmission:
    """
    Attach a customer screenshot to an order.

    pending_payment → payment_submitted on the first proof; extra proofs while
    already payment_submitted just add a row. The state change, proof row and
    audit entry commit together. No vision model is ever run on the image.
    """
    order = await _lock_order(db, client.id, order_id)
    now = datetime.now(timezone.utc)
    transitioned = False
    if order.status == ORDER_PENDING_PAYMENT:
        assert_order_transition(order.status, ORDER_PAYMENT_SUBMITTED)
        _audit(db, order, actor_user=None, actor_name="customer", action="payment_submitted",
               from_status=order.status, to_status=ORDER_PAYMENT_SUBMITTED)
        order.status = ORDER_PAYMENT_SUBMITTED
        order.payment_submitted_at = now
        transitioned = True
    elif order.status != ORDER_PAYMENT_SUBMITTED:
        raise InvalidOrderTransition(order.status, ORDER_PAYMENT_SUBMITTED)
    high_value = is_high_value(order, client)
    if high_value and not getattr(order, "requires_manual_review", False):
        order.requires_manual_review = True
        _audit(db, order, actor_user=None, actor_name="system", action="manual_review_required",
               from_status=order.status, to_status=order.status,
               detail={"total": float(order.total_amount or 0), "threshold": high_value_threshold(client)})

    proof = PaymentProof(
        order_id=order.id, client_id=client.id, conversation_id=conv.id,
        message_id=message_id, media_url=media_url, status=PROOF_PENDING, reclassified_by_customer=reclassified,
    )
    db.add(proof)
    if reclassified:
        # The customer told us an image we had not taken as a proof IS the payment: keep that on the audit trail.
        _audit(db, order, actor_user=None, actor_name="customer", action="proof_reclassified_by_customer",
               from_status=order.status, to_status=order.status, detail={"message_id": message_id})
    await db.commit()
    await db.refresh(proof)
    realtime_service.publish(client.id, "payment_submitted", {
        "order_id": order.id, "conversation_id": conv.id,
        "proof_id": proof.id, "additional": not transitioned,
    })
    return ProofSubmission(proof=proof, transitioned=transitioned, high_value=high_value)


def proof_ack_due(conv: Conversation, now: datetime | None = None) -> bool:
    """True when the throttled 'verification in progress' ack may be sent (once per 10 min)."""
    last = conv.last_proof_ack_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - last) >= PROOF_ACK_THROTTLE


# ─── seller decisions ─────────────────────────────────────────────────────────


@dataclass
class DecisionResult:
    """
    Outcome of approve / reject / cancel.

    changed:           False when the order was already in the target state
                       (idempotent replay) — nothing was written or sent.
    customer_notified: True/False when a template was attempted, None when not.
    notify_failure:    send-gate/transport reason when the template wasn't delivered.
    """

    order: Order
    changed: bool
    customer_notified: bool | None = None
    notify_failure: str | None = None


def _apply_outcome(result: DecisionResult, outcome: channel_sender.SendOutcome | None) -> None:
    """Fold a SendOutcome into a DecisionResult."""
    if outcome is None:
        return
    result.customer_notified = outcome.sent
    result.notify_failure = None if outcome.sent else outcome.reason


async def approve(
    db: AsyncSession, client: Client, order_id: int, user, *, confirm_high_value: bool = False,
) -> DecisionResult:
    """
    payment_submitted → paid. Idempotent.

    A high-value order (total above `high_value_threshold`) is never approved by a plain click: it raises
    `ManualReviewRequired` until the seller passes `confirm_high_value=True` (recorded in the audit trail), so no
    "payment confirmed" message can go out for it without a deliberate manual review.

    One transaction: lock order → validate edge → approve pending proofs →
    deduct stock + consume reservation → mark paid → audit → commit. Then
    (only if this call made the change) message the customer, ping the owner,
    send the invoice and reset the conversation.

    Raises:
        OrderNotFound, InvalidOrderTransition (e.g. approving a pending_payment
        order that has no screenshot, or a cancelled one).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_PAID:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_PAID)

    now = datetime.now(timezone.utc)
    name = actor_label(user)
    if (getattr(order, "requires_manual_review", False) or is_high_value(order, client)) and getattr(order, "manual_review_cleared_at", None) is None:
        if not confirm_high_value:
            raise ManualReviewRequired(order, high_value_threshold(client))
        order.requires_manual_review = True
        order.manual_review_cleared_at = now
        _audit(db, order, actor_user=user, actor_name=name, action="manual_review_cleared",
               from_status=order.status, to_status=order.status, detail={"total": float(order.total_amount or 0)})
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_APPROVED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now

    if not order.stock_deducted:
        await order_service.apply_stock_deduction(db, order)
    order.stock_deducted = True
    from_status = order.status
    order.status = ORDER_PAID
    order.payment_status = "paid"
    order.paid_at = now
    order.confirmed_at = order.confirmed_at or now
    _audit(db, order, actor_user=user, actor_name=name, action="payment_approved",
           from_status=from_status, to_status=ORDER_PAID)
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "approved", "reviewed_by": name,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_confirmed", order_number=order.order_number))
    await order_service.run_post_paid_side_effects(db, order, client)
    await _reset_conversation_after_terminal(db, conv)
    return result


async def reject(
    db: AsyncSession, client: Client, order_id: int, user, reason: str | None = None
) -> DecisionResult:
    """
    payment_submitted → pending_payment. Idempotent.

    Stock stays reserved (the customer may simply re-send a clearer screenshot).
    A repeated reject on an order already back in pending_payment is a no-op.

    Raises:
        OrderNotFound, InvalidOrderTransition (order is paid or cancelled).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_PENDING_PAYMENT:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_PENDING_PAYMENT)

    reason = (reason or "").strip() or None
    now = datetime.now(timezone.utc)
    name = actor_label(user)
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_REJECTED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now
        proof.reject_reason = reason
    from_status = order.status
    order.status = ORDER_PENDING_PAYMENT
    _audit(db, order, actor_user=user, actor_name=name, action="payment_rejected",
           from_status=from_status, to_status=ORDER_PENDING_PAYMENT, detail={"reason": reason})
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "rejected", "reviewed_by": name, "reason": reason,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_rejected", reason_part=f": {reason}" if reason else ""))
    return result


async def cancel(
    db: AsyncSession, client: Client, order_id: int, user, reason: str | None = None,
    *, source: str = "seller",
) -> DecisionResult:
    """
    pending_payment | payment_submitted → cancelled. Idempotent.

    Releases the stock reservation (nothing was deducted) and rejects any
    still-pending proofs. `source` is 'seller' or 'expiry' (audit detail).

    Raises:
        OrderNotFound, InvalidOrderTransition (order is already paid).
    """
    order = await _lock_order(db, client.id, order_id)
    if order.status == ORDER_CANCELLED:
        return DecisionResult(order, changed=False)
    assert_order_transition(order.status, ORDER_CANCELLED)

    reason = (reason or "").strip() or None
    now = datetime.now(timezone.utc)
    name = actor_label(user)
    for proof in await _pending_proofs(db, order.id):
        proof.status = PROOF_REJECTED
        proof.reviewed_by = getattr(user, "id", None)
        proof.reviewed_by_name = name
        proof.reviewed_at = now
        proof.reject_reason = reason or "order cancelled"
    await stock_reservation_service.release_for_order(db, order)
    from_status = order.status
    order.status = ORDER_CANCELLED
    order.cancelled_at = now
    order.cancel_reason = reason
    _audit(db, order, actor_user=user, actor_name=name, action="order_cancelled",
           from_status=from_status, to_status=ORDER_CANCELLED,
           detail={"reason": reason, "source": source})
    await db.commit()

    result = DecisionResult(order, changed=True)
    conv = await _conversation_for(db, order)
    realtime_service.publish(client.id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "cancelled", "reviewed_by": name, "reason": reason,
    })
    _apply_outcome(result, await _notify_customer(
        db, client, conv, "pay_cancelled",
        order_number=order.order_number, reason_part=f" {reason}" if reason else ""))
    await _reset_conversation_after_terminal(db, conv)
    return result


async def cancel_by_customer(db: AsyncSession, order: Order) -> None:
    """
    The customer cancelled an unpaid order in chat (pipeline cancel paths).

    Mirrors cancel() for the pending_payment → cancelled edge — status, stamp,
    reservation release and audit row commit together — but sends no template
    (the bot's own cancel reply already told the customer) and does not touch
    orders that already carry a screenshot (payment_submitted): those need the
    seller to decide, since money may have moved.
    """
    assert_order_transition(order.status, ORDER_CANCELLED)
    order.status = ORDER_CANCELLED
    order.cancelled_at = datetime.now(timezone.utc)
    order.cancel_reason = "cancelled by customer"
    await stock_reservation_service.release_for_order(db, order)
    _audit(db, order, actor_user=None, actor_name="customer", action="order_cancelled",
           from_status=ORDER_PENDING_PAYMENT, to_status=ORDER_CANCELLED, detail={"source": "customer"})
    await db.commit()
    realtime_service.publish(order.client_id, "payment_reviewed", {
        "order_id": order.id, "conversation_id": order.conversation_id,
        "decision": "cancelled", "reviewed_by": "customer", "reason": order.cancel_reason,
    })


async def expire_stale_orders(db: AsyncSession, now: datetime | None = None) -> int:
    """
    Cancel pending_payment orders unpaid past their client's payment_expiry_hours.

    payment_submitted orders are NEVER auto-expired: a screenshot is waiting on
    the seller, and cancelling under a customer who already paid would be
    wrong — those are surfaced as overdue in the dashboard instead.

    Returns:
        Number of orders cancelled.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Order.id, Order.client_id, Order.created_at, Client.payment_expiry_hours)
            .join(Client, Client.id == Order.client_id)
            .where(Order.status == ORDER_PENDING_PAYMENT, Order.payment_method != "COD")
        )
    ).all()
    cancelled = 0
    for order_id, client_id, created_at, expiry_hours in rows:
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if now - created_at < timedelta(hours=expiry_hours or 24):
            continue
        client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one()
        try:
            res = await cancel(db, client, order_id, None, "payment not received in time", source="expiry")
            cancelled += int(res.changed)
        except (InvalidOrderTransition, OrderNotFound):
            await db.rollback()
        except Exception as exc:
            await db.rollback()
            logger.error("expire_stale_orders: order %s failed: %s", order_id, exc)
    return cancelled


# ─── dashboard queries ────────────────────────────────────────────────────────


@dataclass
class PendingPayment:
    """One order with its proofs, shaped for the verification page."""

    order: Order
    channel: str | None
    proofs: list[PaymentProof] = field(default_factory=list)
    waiting_since: datetime | None = None


async def count_pending(db: AsyncSession, client_id: int) -> int:
    """Orders currently waiting for seller verification (sidebar badge)."""
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Order).where(
                    Order.client_id == client_id, Order.status == ORDER_PAYMENT_SUBMITTED
                )
            )
        ).scalar_one()
    )


async def list_payments(
    db: AsyncSession, client_id: int, *, status: str = PROOF_PENDING,
    date_from: date | None = None, date_to: date | None = None,
    search: str | None = None, limit: int = 50, offset: int = 0,
) -> list[PendingPayment]:
    """
    Orders with payment proofs in `status`, newest-waiting first (oldest first for pending).

    status 'pending'  → orders currently payment_submitted (dated by submission time);
    'approved' / 'rejected' → orders having a proof with that decision (dated by review time).
    """
    pending = status == PROOF_PENDING
    date_col = PaymentProof.created_at if pending else PaymentProof.reviewed_at
    stmt = (
        select(Order)
        .join(PaymentProof, PaymentProof.order_id == Order.id)
        .where(Order.client_id == client_id, PaymentProof.status == status)
    )
    if pending:
        stmt = stmt.where(Order.status == ORDER_PAYMENT_SUBMITTED)
    if date_from:
        stmt = stmt.where(date_col >= datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(date_col < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Order.order_number.ilike(like), Order.customer_name.ilike(like),
            Order.customer_phone.ilike(like),
        ))
    order_by = func.min(date_col).asc() if pending else func.max(date_col).desc()
    stmt = stmt.group_by(Order.id).order_by(order_by).limit(limit).offset(offset)
    orders = list((await db.execute(stmt)).scalars().all())
    if not orders:
        return []

    ids = [o.id for o in orders]
    proofs = (
        await db.execute(
            select(PaymentProof)
            .where(PaymentProof.order_id.in_(ids), PaymentProof.status == status)
            .order_by(PaymentProof.created_at.asc())
        )
    ).scalars().all()
    by_order: dict[int, list[PaymentProof]] = {}
    for p in proofs:
        by_order.setdefault(p.order_id, []).append(p)

    conv_ids = [o.conversation_id for o in orders if o.conversation_id]
    channels = dict(
        (await db.execute(
            select(Conversation.id, Conversation.channel).where(Conversation.id.in_(conv_ids))
        )).all()
    ) if conv_ids else {}

    out = []
    for o in orders:
        plist = by_order.get(o.id, [])
        out.append(PendingPayment(
            order=o, channel=channels.get(o.conversation_id), proofs=plist,
            waiting_since=(plist[0].created_at if plist else o.payment_submitted_at),
        ))
    return out
