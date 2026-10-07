"""Unit tests (no database) for invoice helpers: financial year, numbering format, GSTIN, rupee text, PDF."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from reportlab import rl_config

from app.models.sellertalk24_billing import Invoice
from app.services.billing import invoices as inv
from app.services.billing.invoice_pdf import render_invoice_pdf, rupees

UTC = timezone.utc


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 4, 1, 0, 0, tzinfo=UTC), "2026-27"),            # 05:30 IST on 1 Apr: new year
        (datetime(2026, 10, 7, 12, 0, tzinfo=UTC), "2026-27"),
        (datetime(2027, 1, 15, 9, 0, tzinfo=UTC), "2026-27"),           # Jan belongs to the year that began the previous April
        (datetime(2027, 3, 31, 18, 29, tzinfo=UTC), "2026-27"),         # 23:59 IST on 31 Mar: still the old year
        (datetime(2027, 3, 31, 18, 30, tzinfo=UTC), "2027-28"),         # 00:00 IST on 1 Apr: rolled over
        (datetime(2026, 3, 31, 20, 0, tzinfo=UTC), "2026-27"),          # 01:30 IST 1 Apr 2026 even though UTC says 31 Mar
        (datetime(2026, 3, 31, 12, 0, tzinfo=UTC), "2025-26"),
        (datetime(2099, 4, 1, 0, 0, tzinfo=UTC), "2099-00"),            # century rollover of the 2-digit suffix
    ],
)
def test_financial_year_is_judged_in_ist(when, expected):
    assert inv.financial_year(when) == expected


def test_financial_year_treats_a_naive_datetime_as_utc():
    assert inv.financial_year(datetime(2027, 3, 31, 18, 30)) == "2027-28"


def test_format_invoice_number_pads_to_four_digits():
    assert inv.format_invoice_number("ST24", "2026-27", 1) == "ST24/2026-27/0001"
    assert inv.format_invoice_number("ST24", "2026-27", 42) == "ST24/2026-27/0042"
    assert inv.format_invoice_number("ST24", "2026-27", 12345) == "ST24/2026-27/12345"


@pytest.mark.parametrize(
    "gstin, ok",
    [
        ("24ABCDE1234F1Z5", True),
        ("27AAPFU0939F1ZV", True),
        (" 24abcde1234f1z5 ", True),        # case / whitespace tolerated
        ("", False),
        (None, False),
        ("N/A", False),
        ("24ABCDE1234F1Z", False),          # 14 chars
        ("24ABCDE1234F0Z5", False),         # entity number cannot be 0
        ("25ABCDE1234F1Z5", False),         # 25 is not a GST state code
        ("24ABCDE1234F1X5", False),         # 14th char must be Z
    ],
)
def test_is_valid_gstin(gstin, ok):
    assert inv.is_valid_gstin(gstin) is ok


def test_normalise_gstin_uppercases_and_blank_becomes_none():
    assert inv.normalise_gstin(" 24abcde1234f1z5 ") == "24ABCDE1234F1Z5"
    assert inv.normalise_gstin("   ") is None
    assert inv.normalise_gstin(None) is None


def test_state_name_lookup():
    assert inv.state_name("24") == "Gujarat"
    assert inv.state_name("27") == "Maharashtra"
    assert inv.state_name("00") == ""
    assert inv.state_name(None) == ""


@pytest.mark.parametrize(
    "paise, text",
    [(0, "Rs. 0.00"), (50, "Rs. 0.50"), (459900, "Rs. 4,599.00"), (542682, "Rs. 5,426.82"),
     (10000000, "Rs. 1,00,000.00"), (123456789, "Rs. 12,34,567.89"), (-82782, "-Rs. 827.82")],
)
def test_rupees_uses_indian_grouping(paise, text):
    assert rupees(paise) == text


def _invoice(*, intra: bool, credit: int = 0) -> Invoice:
    """A transient (never persisted) invoice shaped like create_invoice would produce."""
    taxable = 459900 - credit
    gst = round(taxable * 0.18)
    return Invoice(
        invoice_number="ST24/2026-27/0001", financial_year="2026-27", seq=1, client_id=1, payment_order_id=1,
        issued_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        buyer_name="Riya Sarees", buyer_email="riya@example.com", buyer_gstin=None if intra else "27AAPFU0939F1ZV",
        buyer_address="12 MG Road\nPune", buyer_state_code=None if intra else "27",
        seller_name="SellerTalk24", seller_gstin="24AAAAA0000A1Z5", seller_address="Ahmedabad", seller_state_code="24",
        sac_code="998314", description="SellerTalk24 Starter plan — 30 days, 1500 conversations", currency="INR",
        gst_rate_bps=1800, intra_state=intra, base_paise=459900, credit_paise=credit, taxable_paise=taxable,
        cgst_paise=gst // 2 if intra else 0, sgst_paise=gst - gst // 2 if intra else 0, igst_paise=0 if intra else gst,
        total_paise=taxable + gst, razorpay_payment_id="pay_123",
    )


@pytest.fixture
def uncompressed_pdf(monkeypatch):
    """Turn off ReportLab page compression so text can be asserted on the raw bytes."""
    monkeypatch.setattr(rl_config, "pageCompression", 0)


def test_pdf_for_an_intra_state_buyer_shows_cgst_sgst_and_no_igst(uncompressed_pdf):
    pdf = render_invoice_pdf(_invoice(intra=True))

    assert pdf.startswith(b"%PDF-")
    for needle in (b"TAX INVOICE", b"ST24/2026-27/0001", b"998314", b"CGST @9%", b"SGST @9%", b"24AAAAA0000A1Z5", b"Rs. 5,426.82"):
        assert needle in pdf, needle
    assert b"IGST" not in pdf


def test_pdf_for_an_inter_state_buyer_shows_igst_only(uncompressed_pdf):
    pdf = render_invoice_pdf(_invoice(intra=False))

    assert b"IGST @18%" in pdf and b"Place of supply: Maharashtra \\(27\\)" in pdf  # PDF strings escape parentheses
    assert b"CGST" not in pdf and b"SGST" not in pdf
    assert b"27AAPFU0939F1ZV" in pdf


def test_pdf_lists_the_upgrade_credit_when_there_is_one(uncompressed_pdf):
    with_credit = render_invoice_pdf(_invoice(intra=True, credit=100000))
    without = render_invoice_pdf(_invoice(intra=True))

    assert b"Less: upgrade credit" in with_credit and b"-Rs. 1,000.00" in with_credit
    assert b"Less: upgrade credit" not in without


def test_pdf_renders_with_missing_optional_details():
    invoice = _invoice(intra=True)
    invoice.seller_gstin = None
    invoice.seller_address = None
    invoice.buyer_address = None
    invoice.razorpay_payment_id = None

    assert render_invoice_pdf(invoice).startswith(b"%PDF-")


def test_series_prefix_marks_test_numbers_and_leaves_live_alone():
    assert inv.series_prefix("ST24", "live") == "ST24"
    assert inv.series_prefix("ST24", "test") == "TEST-ST24"
    assert inv.format_invoice_number(inv.series_prefix("ST24", "test"), "2026-27", 1) == "TEST-ST24/2026-27/0001"
    assert inv.format_invoice_number(inv.series_prefix("ST24", "live"), "2026-27", 1) == "ST24/2026-27/0001"
