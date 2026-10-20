"""
Tests (no database) for the 'is this image a payment proof?' decision: ``payment_proof_classifier`` (caption → catalogue watermark →
OCR → vision only when unsure) and the pieces ``payment_inbound`` builds on it (order matching by amount, the denial detector).

The end-to-end behaviour (attach / ask / reroute / withdraw on a real Postgres) is in ``tests/replay/test_payment_proof_hardening.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import catalogue_service, ocr_service, payment_inbound, payment_proof_classifier as clf
from app.services import payment_verification_service as pvs

RECEIPT = "Google Pay\nPayment successful\nPaid to Riya Boutique\n₹1,299\nUPI transaction ID 412345678901\nBanking name RIYA BOUTIQUE"


def _ctx(caption=None, media_url="https://x/y.jpg"):
    """Minimal pipeline ctx carrying one inbound image."""
    return SimpleNamespace(
        message=SimpleNamespace(type="image", image=SimpleNamespace(id="m1", caption=caption)),
        conv=SimpleNamespace(id=1),
        db=object(),
        client=SimpleNamespace(id=9),
        media_url=media_url,
        download_media=AsyncMock(return_value=b"img"),
    )


def _order(number, total, status="pending_payment"):
    """Order-shaped object."""
    return SimpleNamespace(id=int(number[-2:]), order_number=number, total_amount=total, status=status)


@pytest.fixture(autouse=True)
def _no_code_no_vision(monkeypatch):
    """Default: no product code readable on the image, the catalogue has no such product, and vision is never reached."""
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: None)
    monkeypatch.setattr(catalogue_service, "find_product_by_sku", AsyncMock(return_value=None))
    monkeypatch.setattr(clf, "vision_read", AsyncMock(return_value=None))


def _ocr(monkeypatch, text):
    """Make the whole-image OCR return `text` (None = Tesseract unavailable)."""
    monkeypatch.setattr(clf, "ocr_text", AsyncMock(return_value=text))


# ── the classifier ───────────────────────────────────────────────────────────

def test_amounts_are_read_in_indian_and_plain_formats():
    """1,299 · 1299.00 · 1,29,900.50 are rupee amounts; a 12-digit reference is not."""
    assert clf.extract_amounts("₹1,299 paid, ref 412345678901") == [1299.0]
    assert clf.extract_amounts("Rs 1,29,900.50 and 499") == [129900.5]          # the receipt's own amount; a loose 499 is ignored next to it
    assert clf.matches_total([1299.5], 1300.0) and not clf.matches_total([1297.0], 1300.0)         # ±₹1


def test_receipt_text_scores_as_payment_proof():
    """Amount of the order + success wording + a UPI app + a transaction id = a proof, with the evidence listed."""
    score, signals, amounts = clf.score_text(RECEIPT, [1299.0])
    assert score >= 7 and {"utr", "amount_matches_order", "success_wording", "upi_app"} <= set(signals) and amounts == [1299.0]


@pytest.mark.asyncio
async def test_receipt_is_a_confident_payment_proof(monkeypatch):
    """The OCR of a real-looking receipt decides it without a vision call."""
    _ocr(monkeypatch, RECEIPT)
    verdict = await clf.classify(b"img", order_totals=[1299.0])
    assert verdict.kind == clf.PAYMENT_PROOF and verdict.decisive and verdict.amounts == [1299.0] and verdict.source == "ocr"


@pytest.mark.asyncio
async def test_a_picture_without_receipt_text_is_NOT_a_product_photo(monkeypatch):
    """conv=60 ORD-2026-0077: Tesseract read nothing on a dark-mode UPI screenshot and it was filed as a product photo. No text is no evidence."""
    _ocr(monkeypatch, "")
    verdict = await clf.classify(b"img", order_totals=[130000.0])
    assert verdict.kind == clf.UNKNOWN and not verdict.decisive and verdict.ocr_len == 0


@pytest.mark.asyncio
async def test_price_tag_text_is_not_enough_to_call_it_a_payment(monkeypatch):
    """A catalogue picture with a price on it (the same number as the total!) but no receipt wording stays below the bar."""
    _ocr(monkeypatch, "Banarasi Silk Saree\nPrice 1299 only\nFree delivery all over India\nOrder now")
    verdict = await clf.classify(b"img", order_totals=[1299.0])
    assert not verdict.decisive


@pytest.mark.asyncio
async def test_ocr_unavailable_is_unsure_not_a_proof(monkeypatch):
    """No Tesseract / a corrupt image is no evidence either way: the customer is asked instead of guessing."""
    _ocr(monkeypatch, None)
    verdict = await clf.classify(b"img", order_totals=[1299.0])
    assert not verdict.decisive


@pytest.mark.asyncio
async def test_vision_decides_when_ocr_is_unsure(monkeypatch):
    """Inconclusive OCR → the vision model's confident product verdict is used (positive evidence)."""
    _ocr(monkeypatch, None)
    monkeypatch.setattr(clf, "vision_read", AsyncMock(return_value=clf.VisionRead(clf.PRODUCT_PHOTO, 0.93, "saree", ["red"])))
    verdict = await clf.classify(b"img", order_totals=[1299.0])
    assert verdict.kind == clf.PRODUCT_PHOTO and verdict.decisive and verdict.source == "vision" and verdict.vision.search_query() == "red saree"


@pytest.mark.asyncio
async def test_catalogue_watermark_means_product_photo_whatever_else_it_shows():
    """Our own SKU watermark of this shop's catalogue → product photo."""
    verdict = await clf.classify(b"img", order_totals=[1299.0], catalogue_sku="SR51139")
    assert verdict.kind == clf.PRODUCT_PHOTO and verdict.confidence > 0.9 and verdict.source == "sku"


@pytest.mark.asyncio
async def test_captions_steer_the_decision(monkeypatch):
    """'available?' on an image is an enquiry; 'paid' / 'UTR …' points at the payment."""
    _ocr(monkeypatch, None)
    assert (await clf.classify(b"img", order_totals=[1.0], caption="available?")).kind == clf.PRODUCT_PHOTO
    proof = await clf.classify(b"img", order_totals=[1.0], caption="payment done UTR 1234")
    assert proof.kind == clf.PAYMENT_PROOF


@pytest.mark.asyncio
async def test_inbound_gate_reads_the_watermark_only_for_this_shops_catalogue(monkeypatch):
    """A SKU-shaped token that is no product of this shop (a UTR on a receipt) is not a catalogue code; an exact match is."""
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: "UTR12345")
    assert await payment_inbound._watermark_sku(_ctx(), b"img") is None
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: "KU76326")
    monkeypatch.setattr(catalogue_service, "find_product_by_sku", AsyncMock(return_value=SimpleNamespace(sku="KU76326")))
    assert await payment_inbound._watermark_sku(_ctx(), b"img") == "KU76326"
    monkeypatch.setattr(catalogue_service, "find_product_by_sku", AsyncMock(return_value=SimpleNamespace(sku="SR27754")))      # prefix-only match
    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: "SR277")
    assert await payment_inbound._watermark_sku(_ctx(), b"img") is None


@pytest.mark.asyncio
async def test_watermark_read_failure_never_raises(monkeypatch):
    """An OCR error must not break the turn."""
    def boom(_):
        """OCR that fails."""
        raise RuntimeError("tesseract missing")

    monkeypatch.setattr(ocr_service, "detect_sku_from_image", boom)
    assert await payment_inbound._watermark_sku(_ctx(), b"img") is None


# ── which order does a screenshot belong to ──────────────────────────────────

def test_one_open_order_gets_the_proof():
    """A single unpaid order needs no matching."""
    a = _order("ORD-2026-0068", 1300)
    assert payment_inbound.pick_order_for_amounts([a], []) == (a, False)


def test_amount_on_the_receipt_picks_the_order_among_several():
    """Two unpaid orders: the receipt's ₹ amount (±1) decides."""
    a, b = _order("ORD-2026-0068", 1300), _order("ORD-2026-0075", 499)
    assert payment_inbound.pick_order_for_amounts([b, a], [1300.0, 412345678901.0]) == (a, False)
    assert payment_inbound.pick_order_for_amounts([b, a], [499.5]) == (b, False)


def test_no_matching_amount_or_two_matches_means_ask():
    """A proof is never filed under a guess: nothing matches / two orders have the same total → ambiguous."""
    a, b = _order("ORD-2026-0068", 1300), _order("ORD-2026-0075", 499)
    assert payment_inbound.pick_order_for_amounts([a, b], [77.0]) == (None, True)
    c = _order("ORD-2026-0076", 1300)
    assert payment_inbound.pick_order_for_amounts([a, c], [1300.0]) == (None, True)


# ── "this is not the payment" ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "This is product not the payment", "No this is not payment", "this is the product", "it's a product", "wrong photo", "sent by mistake",
    "ye payment nahi hai", "yeh payment ka screenshot nahi hai", "ye product hai", "galat photo", "galti se bheja",
    "aa payment nathi", "aa product chhe", "khotu photo", "bhul thi",
    "ये पेमेंट नहीं है", "ये प्रोडक्ट है", "गलत फोटो", "गलती से",
    "આ પેમેન્ટ નથી", "આ પ્રોડક્ટ છે", "ખોટો ફોટો", "ભૂલથી",
])
def test_denials_in_five_languages(text):
    """The customer says the image was not a payment proof."""
    assert pvs.is_payment_denial(text)
    assert not pvs.is_payment_help_request(text), "a denial must never be answered with the payment instruction again"


@pytest.mark.parametrize("text", [
    "Is this product available?", "I haven't paid yet", "payment nahi hua", "this product is nice", "I did not send the payment yet",
    "give me payment details", "kaise pay karu", "ye product kitne ka hai", "aa product ketla ma che?", "paid", "wrong number", "UPI id?",
])
def test_ordinary_payment_and_product_messages_are_not_denials(text):
    """Questions and 'I have not paid' are payment help / product talk, not a withdrawal."""
    assert not pvs.is_payment_denial(text)


def test_payment_help_still_recognised():
    """The carve-out for denials leaves the real help requests alone."""
    for text in ("I haven't paid yet", "give me payment details", "kaise pay karu", "UPI id?"):
        assert pvs.is_payment_help_request(text), text
