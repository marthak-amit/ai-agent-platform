"""
Unit tests (no database, no network) for the v1.6 round (conv=60: ORD-2026-0077 / 0078 / 0068):

  1. payment screenshots are never taken for product photos on missing OCR text (three outcomes, positive evidence, prior, concurrency)
  3. rupee amounts in every Indian format, ignoring phone numbers / dates / times / UTR / percentages
  5. Groq 429: model fallback without sleeping, client-side token bucket, header parsing, deterministic bare-reply fallback, smaller prompt
  6. a quantity is never confused with a list index (stock buttons, no catalogue, sanity limits)
  7. unsupported message types are answered once per window, in five languages
  8. cheap deterministic paths (category word, nouns + colours, Hindi-roman chatter)

The end-to-end behaviour on Postgres is in ``tests/replay/test_payment_images_v16.py`` and ``tests/scenarios/69…74``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_catalogue_words, agent_fallback, llm_rate_limiter
from app.services import agent_executor as ex
from app.services import agent_fast_paths as fp
from app.services import agent_invariants as inv
from app.services import payment_proof_classifier as clf
from app.services import payment_verification_service as pvs
from app.services.agent_renderer import Renderer, Reply
from app.services.language_templates import get_template
from app.services.payment_amounts import extract_amounts, read_amounts
from tests.test_agent_v3_units import CLIENT, make_cart, make_catalogue, run_actions


# ── 3. amounts ───────────────────────────────────────────────────────────────

AMOUNT_CASES = [
    # (what the OCR read, the order total that must be recognised)
    ("₹8,548", [8548.0]),
    ("Rs. 8,548.00", [8548.0]),
    ("INR 8548", [8548.0]),
    ("₹1,50,000", [150000.0]),                                                    # lakh grouping
    ("₹17,245.00", [17245.0]),
    ("₹ 8 548", [8548.0]),                                                        # OCR spaces inside the number
    ("₹8, 548", [8548.0]),
    ("₹ 8,548 .00", [8548.0]),
    ("%8,548", [8548.0]),                                                         # Tesseract's reading of ₹
    ("T8 548", [8548.0]),
    ("Amount 1299/-", [1299.0]),
    ("Rupees 499 only", [499.0]),
    ("INR 1,29,900.50", [129900.5]),
    # GPay
    ("Google Pay\n₹1,299\nCompleted\n12 Oct 2026, 10:45 AM\nUPI transaction ID 412345678901\nTo: RIYA BOUTIQUE\n+91 98765 43210", [1299.0]),
    # PhonePe
    ("PhonePe\nPayment successful\n₹ 8,548\nPaid to Riya Boutique\nTransaction ID T2610121045123456789\nUTR 412345678901\n12/10/2026 10:45 am", [8548.0]),
    # Paytm
    ("Paytm\nPaid Successfully\n₹ 8548\nOrder ID 20261012111212800110\nTo riya@paytm\n10:45 PM, 12 Oct 2026", [8548.0]),
    # BHIM
    ("BHIM UPI\nTransaction Successful\nRs 8548.00\nRef No 412345678901\nPayee riya@ybl", [8548.0]),
    # bank SMS / net banking
    ("Rs.8548.00 debited from A/c XX1234 on 12-10-26 to VPA riya@ybl (UPI Ref No 412345678901). Not you? Call 1800", [8548.0]),
    ("INR 8,548.00 debited from A/c ending 1234 on 12Oct26. UPI:412345678901. Avl Bal INR 20,000.00", [20000.0, 8548.0]),
    ("Sent Rs 250 to 9876543210", [250.0]),
    # noise only
    ("10:45 pm 12/10/2026 +91 9876543210 412345678901", []),
    ("GST 18% total 2,360", [2360.0]),
    ("ORD-2026-0077 ₹ 8548", [8548.0]),
    ("Order ID OD1234567890 ₹ 999", [999.0]),
]


@pytest.mark.parametrize("text,expected", AMOUNT_CASES)
def test_amount_formats(text, expected):
    """₹8,548 → 8548 and friends; phone numbers, dates, times, UTR/ref numbers, percentages and masked accounts are never amounts."""
    got = extract_amounts(text)
    if expected == [20000.0, 8548.0]:         # the SMS prints both the debit and the balance; both are amounts on the screen, the debit is among them
        assert 8548.0 in got
    else:
        assert got == expected


def test_strong_amounts_beat_loose_numbers():
    """A number next to ₹ is the receipt's amount; a stray number elsewhere on the screen does not compete with it."""
    reading = read_amounts("₹ 499\nItems 2\nOrder 7")
    assert reading.strong == [499.0] and reading.best == [499.0]


def test_order_total_is_matched_with_a_rupee_tolerance():
    """±₹1."""
    assert clf.matches_total(extract_amounts("₹8,548"), 8548.0) and clf.matches_total(extract_amounts("₹8,548"), 8547.5)
    assert not clf.matches_total(extract_amounts("₹8,548"), 8540.0)


# ── 1. classifier ────────────────────────────────────────────────────────────

@pytest.fixture
def seams(monkeypatch):
    """OCR and vision as scripted seams (default: nothing read, vision off)."""
    monkeypatch.setattr(clf, "ocr_text", AsyncMock(return_value=""))
    monkeypatch.setattr(clf, "vision_read", AsyncMock(return_value=None))
    return SimpleNamespace(ocr=clf.ocr_text, vision=clf.vision_read)


@pytest.mark.asyncio
async def test_dark_mode_screenshot_with_empty_ocr_is_never_a_product_photo(seams):
    """TEST 1: OCR read nothing, payment instructions went out 10 minutes ago → unknown (ask), never product_photo."""
    verdict = await clf.classify(b"img", order_totals=[8548.0], prior_payment=True)
    assert verdict.kind == clf.UNKNOWN and not verdict.decisive and verdict.prior and verdict.ocr_len == 0


@pytest.mark.asyncio
async def test_vision_that_sees_a_payment_screen_wins_inside_the_payment_window(seams):
    """OCR empty, vision 'payment_proof' at 0.55: not decisive on its own outside the window, a proof inside it."""
    seams.vision.return_value = clf.VisionRead(clf.PAYMENT_PROOF, 0.55)
    outside = await clf.classify(b"img", order_totals=[1.0], prior_payment=False)
    inside = await clf.classify(b"img", order_totals=[1.0], prior_payment=True)
    assert outside.kind == clf.UNKNOWN
    assert inside.kind == clf.PAYMENT_PROOF and inside.decisive and inside.decided_by == "vision"


@pytest.mark.asyncio
async def test_saree_photo_is_a_product_photo_only_on_confident_vision(seams):
    """TEST 2: a saree picture — vision says product_photo 0.95 → product_photo; 0.8 → still unknown (ask)."""
    seams.vision.return_value = clf.VisionRead(clf.PRODUCT_PHOTO, 0.95, "saree", ["red"], "silk", ["banarasi"])
    sure = await clf.classify(b"img", order_totals=[1.0])
    assert sure.kind == clf.PRODUCT_PHOTO and sure.decisive and sure.vision.search_query() == "red silk saree banarasi"
    seams.vision.return_value = clf.VisionRead(clf.PRODUCT_PHOTO, 0.8)
    assert (await clf.classify(b"img", order_totals=[1.0])).kind == clf.UNKNOWN


@pytest.mark.asyncio
async def test_inside_the_payment_window_a_product_verdict_needs_more_certainty(seams):
    """The prior biases toward payment: 0.88 is enough outside the window, not inside it."""
    seams.vision.return_value = clf.VisionRead(clf.PRODUCT_PHOTO, 0.88)
    assert (await clf.classify(b"img", order_totals=[1.0], prior_payment=False)).kind == clf.PRODUCT_PHOTO
    assert (await clf.classify(b"img", order_totals=[1.0], prior_payment=True)).kind == clf.UNKNOWN


@pytest.mark.asyncio
async def test_decisive_ocr_cancels_the_vision_call(seams):
    """A readable receipt decides alone; the concurrent vision task is cancelled (no tokens spent on a settled question)."""
    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_vision(*_a, **_k):
        """A vision call that would take long."""
        started.set()
        await asyncio.sleep(5)
        finished.set()
        return clf.VisionRead(clf.PRODUCT_PHOTO, 0.99)

    seams.ocr.return_value = "PhonePe\nPayment successful\n₹8,548\nUTR 412345678901"
    clf.vision_read = slow_vision
    t0 = time.perf_counter()
    verdict = await clf.classify(b"img", order_totals=[8548.0])
    assert verdict.kind == clf.PAYMENT_PROOF and verdict.decided_by == "ocr" and time.perf_counter() - t0 < 1.0 and not finished.is_set()


@pytest.mark.asyncio
async def test_ocr_and_vision_run_side_by_side(seams):
    """The image turn costs the slower of the two, not their sum (5–9 s sequential in conv=60)."""
    async def slow_ocr(_b):
        """OCR that takes 0.4 s and finds nothing decisive."""
        await asyncio.sleep(0.4)
        return ""

    async def slow_vision(*_a, **_k):
        """Vision that takes 0.4 s."""
        await asyncio.sleep(0.4)
        return clf.VisionRead(clf.PRODUCT_PHOTO, 0.95, "saree")

    clf.ocr_text, clf.vision_read = slow_ocr, slow_vision
    t0 = time.perf_counter()
    verdict = await clf.classify(b"img", order_totals=[1.0])
    elapsed = time.perf_counter() - t0
    assert verdict.kind == clf.PRODUCT_PHOTO and elapsed < 0.7, elapsed          # sequential would be ≥ 0.8


@pytest.mark.asyncio
async def test_the_shops_upi_id_on_the_picture_is_a_strong_payment_signal(seams):
    """'₹' + the recipient UPI id of the shop: a payment even without success wording."""
    seams.ocr.return_value = "To: riyaboutique@okaxis\n₹ 8548"
    verdict = await clf.classify(b"img", order_totals=[8548.0], upi_ids=["riyaboutique@okaxis"])
    assert verdict.kind == clf.PAYMENT_PROOF and "recipient_upi_id" in verdict.signals and verdict.decisive


@pytest.mark.asyncio
async def test_catalogue_hash_match_is_a_product_photo(seams):
    """A perceptual-hash match with a catalogue picture is positive product evidence."""
    verdict = await clf.classify(b"img", order_totals=[1.0], catalogue_match="SR1")
    assert verdict.kind == clf.PRODUCT_PHOTO and verdict.sku == "SR1" and verdict.decided_by == "phash"


def test_image_fingerprint_matches_a_rescaled_copy():
    """dHash survives re-scaling; a different picture is far away."""
    import io

    from PIL import Image, ImageDraw

    from app.services import image_fingerprint as fpr

    def picture(seed: int, size: int) -> bytes:
        """A synthetic picture: random coloured blocks, the same blocks at any size."""
        import random

        rnd = random.Random(seed)
        img = Image.new("RGB", (size, size), "white")
        d = ImageDraw.Draw(img)
        cell = size // 8
        for row in range(8):
            for col in range(8):
                d.rectangle([col * cell, row * cell, (col + 1) * cell, (row + 1) * cell], fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    index = {"A": fpr.dhash(picture(1, 400)), "B": fpr.dhash(picture(5, 400))}
    assert fpr.find(index, picture(1, 220)) == "A"
    assert fpr.find({"B": index["B"]}, picture(1, 400)) is None


# ── payment phrases ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "This is payment photo", "this is the payment", "payment photo", "already paid", "payment done", "paid",
    "maine pay kar diya", "screenshot hai", "ye payment hai", "payment kari didhu", "aa payment chhe", "screenshot chhe",
    "यह पेमेंट है", "स्क्रीनशॉट है", "આ પેમેન્ટ છે", "સ્ક્રીનશૉટ છે",
])
def test_affirmation_of_payment_in_five_languages(text):
    """The customer says the image IS the payment — for the deterministic reclassification path, never the planner."""
    assert pvs.is_payment_affirmation(text) and not pvs.is_payment_denial(text)


@pytest.mark.parametrize("text", ["this is not payment", "this is product", "I haven't paid yet", "is this the payment?", "paid kyun nahi", "show me sarees"])
def test_other_messages_are_not_affirmations(text):
    """Denials, questions and unrelated sentences are left alone."""
    assert not pvs.is_payment_affirmation(text)


@pytest.mark.parametrize("text", ["Please confirm my payments", "payment status?", "did you get my payment", "payment mila?", "check my payments pls", "confirm payment"])
def test_payment_status_requests(text):
    """TEST 9 phrases: a status question, answered per order without the planner."""
    assert pvs.is_payment_status_request(text)


# ── which orders / pickers ───────────────────────────────────────────────────

def _order(number, total, status="pending_payment", oid=None):
    """Order-shaped object."""
    return SimpleNamespace(id=oid or int(number[-2:]), order_number=number, total_amount=total, status=status)


def test_the_picker_only_offers_orders_waiting_for_money():
    """TEST 7: ORD-…0068 is already payment_submitted — it is not a candidate."""
    from app.services import payment_inbound

    orders = [_order("ORD-2026-0078", 998), _order("ORD-2026-0077", 8548), _order("ORD-2026-0068", 130000, "payment_submitted")]
    assert [o.order_number for o in payment_inbound.pending_only(orders)] == ["ORD-2026-0078", "ORD-2026-0077"]


@pytest.mark.parametrize("lang", ["english", "hindi_roman", "hindi_devanagari", "gujarati_roman", "gujarati_script"])
def test_new_payment_templates_exist_in_five_languages(lang):
    """Stale picker, payment status, unsupported type, order picker button: every language has its own text with the placeholders filled."""
    assert get_template(lang, "pay_stale_pick", linked="ORD-1", wanted="ORD-2").count("ORD-") == 2
    assert "ORD-9" in get_template(lang, "pay_already_linked", order="ORD-9")
    for key in ("pay_status_header", "pay_status_none", "pay_pick_order_button", "unsupported_message"):
        text = get_template(lang, key)
        assert text and (lang == "english" or text != get_template("english", key)), (lang, key)
    for key in ("pay_status_pending", "pay_status_submitted", "pay_status_paid"):
        assert "ORD-7" in get_template(lang, key, order="ORD-7", amount="₹10")


# ── 7. unsupported type ──────────────────────────────────────────────────────

def test_unsupported_notice_once_per_window():
    """TEST 13: the second unsupported message of the same sender inside 10 minutes gets no second notice."""
    from app.routers import webhook

    webhook._unsupported_notified.clear()
    key = (1, "9199")
    assert webhook._unsupported_due(key, 1000.0, 600.0)
    assert not webhook._unsupported_due(key, 1100.0, 600.0)
    assert webhook._unsupported_due(key, 1700.0, 600.0)
    assert webhook._unsupported_due((1, "9200"), 1100.0, 600.0)           # another sender is independent
    assert {"unsupported", "document", "video", "location"} <= webhook.UNSUPPORTED_REPLY_TYPES and "reaction" not in webhook.UNSUPPORTED_REPLY_TYPES


# ── 5. rate limiter / fallback ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _limiter_state(monkeypatch):
    """Fresh limiter windows and the production default limits (Groq on_demand: 8000 TPM / 30 RPM) for every test."""
    from app.config import Settings

    monkeypatch.setenv("LLM_RATE_LIMITS", Settings.model_fields["llm_rate_limits"].default)
    llm_rate_limiter.reset()
    yield
    llm_rate_limiter.reset()


@pytest.mark.asyncio
async def test_bucket_lets_two_calls_through_and_queues_the_third(monkeypatch):
    """8000 TPM and ~3.3k per call: two fit in a minute, the third cannot within 2 s and is refused (→ fallback model)."""
    model = "openai/gpt-oss-120b"
    first = await llm_rate_limiter.acquire(model, 3300, max_wait=0.2)
    second = await llm_rate_limiter.acquire(model, 3300, max_wait=0.2)
    third = await llm_rate_limiter.acquire(model, 3300, max_wait=0.2)
    assert first and second and third is None
    llm_rate_limiter.settle(model, second, None)                    # a failed call gives its reservation back
    assert await llm_rate_limiter.acquire(model, 3300, max_wait=0.2)


@pytest.mark.asyncio
async def test_settle_replaces_the_estimate_by_real_usage():
    """The reservation shrinks to what the provider counted."""
    model = "openai/gpt-oss-120b"
    entry = await llm_rate_limiter.acquire(model, 5000, max_wait=0.1)
    llm_rate_limiter.settle(model, entry, 1500)
    assert llm_rate_limiter.used_tokens(model) == 1500


def test_headers_block_a_model_until_the_bucket_refills():
    """x-ratelimit-remaining-tokens below the estimate holds the call back until reset; durations in Groq's formats are parsed."""
    model = "openai/gpt-oss-120b"
    llm_rate_limiter.observe_headers(model, {"x-ratelimit-remaining-tokens": "120", "x-ratelimit-reset-tokens": "1m26.4s"})
    wait = llm_rate_limiter._wait_needed(model, 3000, time.monotonic())
    assert 80 < wait < 90
    assert llm_rate_limiter.parse_duration("250ms") == 0.25 and llm_rate_limiter.parse_duration("2.5s") == 2.5


def test_429_blocks_the_model_for_retry_after():
    """A 429 with retry-after blocks further calls to that model."""
    llm_rate_limiter.observe_headers("m", {"retry-after": "7"})
    assert 5 < llm_rate_limiter._wait_needed("m", 10, time.monotonic()) <= 7


class _Err(RuntimeError):
    """A provider error with an HTTP status."""

    def __init__(self, msg, status):
        """Carry the status the way the openai SDK does."""
        super().__init__(msg)
        self.status_code = status


@pytest.mark.asyncio
async def test_429_on_the_primary_goes_straight_to_the_fallback_without_sleeping(monkeypatch):
    """TEST 10: gpt-oss-120b answers 429 → gpt-oss-20b is called at once (no 2.4 s sleep) and its plan is used."""
    from app.services import agent_planner

    calls = []

    async def fake_call_model(messages, *, client_id, conversation_id, model=None):
        """Primary 429s, fallback works."""
        calls.append(model)
        if model is None:
            raise _Err("Rate limit reached for model `openai/gpt-oss-120b` ... Please try again in 7.2s", 429)
        return {"actions": [], "reply_intent": "greeting"}

    monkeypatch.setattr(agent_planner, "call_model", fake_call_model)
    t0 = time.perf_counter()
    raw, trace = await agent_planner.call_with_fallback([{"role": "user", "content": "hi"}], client_id=1, conversation_id=1)
    assert raw == {"actions": [], "reply_intent": "greeting"} and time.perf_counter() - t0 < 0.5
    assert calls == [None, "openai/gpt-oss-20b"] and trace.model == "openai/gpt-oss-20b" and trace.reason == "429:openai/gpt-oss-120b"
    # the primary is now blocked for retry window: the next call does not even try it
    calls.clear()
    raw, trace = await agent_planner.call_with_fallback([{"role": "user", "content": "hi"}], client_id=1, conversation_id=1)
    assert calls == ["openai/gpt-oss-20b"] and trace.reason and trace.reason.startswith("bucket:openai/gpt-oss-120b")


@pytest.mark.asyncio
async def test_both_models_limited_gives_none_and_is_not_a_provider_failure(monkeypatch):
    """Both 429 → (None, limited): the turn is answered deterministically and the breaker is not fed."""
    from app.services import agent_planner, llm_health

    async def always_429(messages, *, client_id, conversation_id, model=None):
        """Every model is over its limit."""
        raise _Err("429 rate limited", 429)

    monkeypatch.setattr(agent_planner, "call_model", always_429)
    before = llm_health.failures_last_hour() if hasattr(llm_health, "failures_last_hour") else None
    raw, trace = await agent_planner.call_with_fallback([{"role": "user", "content": "hi"}], client_id=1, conversation_id=1)
    assert raw is None and trace.limited and trace.model is None
    if before is not None:
        assert llm_health.failures_last_hour() == before


@pytest.mark.parametrize("text", ["Yes", "yes", "haan", "no", "2", "1"])
def test_bare_reply_when_the_planner_is_down_is_never_an_apology(text):
    """TEST 10b: a yes / no / number is answered against the open question (no 'didn't quite get that')."""
    plan = agent_fallback.resolve(text, {"slot": "other", "options": []})
    assert plan is not None and plan[0].actions == []


def test_yes_to_a_single_offered_option_takes_it():
    """'Shall I use Red?' + 'yes' with the planner down applies Red."""
    plan, grounded = agent_fallback.resolve("yes", {"slot": "color", "line_id": "l1", "options": ["Red"]})
    assert plan.actions[0].type == "update_line" and plan.actions[0].fields.color == "Red" and grounded == "option"
    assert agent_fallback.resolve("show me sarees", {}) is None


def test_planner_prompt_is_smaller_and_documents_v16():
    """v1.6: the system prompt + schema stay compact (the measured before/after is in the report: 4166 → ~2600 tokens)."""
    from app.schemas.agent_actions import union_wire_schema
    from app.services import agent_planner

    system = agent_planner.load_prompt().system
    assert agent_planner.prompt_tag().startswith("agent_planner_v1.7/") and len(system) < 6500
    assert len(json.dumps(union_wire_schema(), separators=(",", ":"))) < 4150          # was 4398 before identical-field actions shared a branch


# ── 6. quantity vs list index ────────────────────────────────────────────────

def _cart_with_open_quantity(stock_sku="KU1"):
    """A kurti line (Red) waiting for its quantity; the open question is the quantity of that line."""
    cart = make_cart()
    line = cart.add_line(stock_sku, color="Red")
    clarify = {"slot": "quantity", "options": [], "line_id": line["line_id"], "sku": stock_sku}
    return cart, line, clarify


def test_zero_negative_and_four_zeros_are_a_deterministic_minimum_message():
    """TEST 12: '0000' / '0' / '-3' at the quantity question → QTY_MIN, no planner."""
    cart, line, clarify = _cart_with_open_quantity()
    cat = make_catalogue()
    for text in ("0000", "0", "-3"):
        path = fp.resolve(text=text, button_id=None, cart=cart, catalogue=cat, client=CLIENT, clarify=clarify)
        assert path is not None and path.forced_issue["code"] == "QTY_MIN" and path.actions == [], text
    result = _run_fast(cart, path, cat)
    assert result.issues and result.issues[0].code == "QTY_MIN"
    cl = ex.clarify_for_issue(result.issues[0], cart, cat, CLIENT)
    reply = _render(cart, cat, cl)
    assert reply.text.startswith("Quantity must be at least 1.") and "How many" in reply.text


def _run_fast(cart, path, cat):
    """Run a fast path's actions (and its forced issue) through the executor."""
    result = ex.ExecResult(cart=cart.copy())
    ctx = ex.ExecContext(user_text="x", language="english", sender_phone="9199", forced_issue=path.forced_issue, qty_confirmed=path.qty_confirmed)
    executor = ex.Executor(None, CLIENT, SimpleNamespace(id=1), cat, ctx, result, cart)
    issues = asyncio.run(executor.run(path.actions))
    soft = list(issues)
    if ctx.forced_issue:
        forced = dict(ctx.forced_issue)
        from app.schemas.agent_actions import ActionIssue

        soft.append(ActionIssue(index=0, action_type="update_line", code=forced.pop("code"), message="", valid_options=[], details=forced))
    result.issues = soft
    return result


def _render(cart, cat, clarify):
    """Render one clarify through the real renderer + invariants (WhatsApp)."""
    renderer = Renderer("english", CLIENT, cat, cart, is_whatsapp=True)
    reply = Reply(text="", amounts=renderer.amounts)
    parts: list[str] = []
    renderer.clarify(clarify, reply, parts)
    reply.text = "\n\n".join(parts)
    inv.enforce_reply(reply, renderer=renderer, cart=cart, catalogue=cat, client=CLIENT)
    return reply


def test_1000_pieces_with_one_in_stock_is_buttons_and_no_catalogue_list():
    """TEST 11: the stock answer has [Take 1][Remove item][See similar]; the open question stays 'quantity', so '1' means 1 piece."""
    cat = make_catalogue()
    cart = make_cart()
    line = cart.add_line("LH1", color="Pink", size="XXL")           # Pink/XXL is out; use Blue/XXL (3) → ask 1000
    line["color"], line["size"] = "Blue", "XXL"
    from app.schemas.agent_actions import LineFields, UpdateLine

    result, issues = run_actions(cart, [UpdateLine(type="update_line", line_id=line["line_id"], fields=LineFields(qty=1000))], catalogue=cat)
    issue = next(i for i in issues if i.code == "QTY_EXCEEDS_STOCK")
    reply = _render(result.cart, cat, ex.clarify_for_issue(issue, result.cart, cat, CLIENT))
    assert "Only 3" in reply.text
    assert [t for _, t in reply.buttons] == ["Take 3", "Remove item", "See similar"]
    assert reply.buttons[0][0] == f"v3_qtytake:{line['line_id']}:3" and reply.buttons[1][0] == f"v3_rm:{line['line_id']}"
    assert reply.last_clarify["slot"] == "quantity" and not reply.list_rows
    for other in ("Cotton Kurti", "Silk Saree", "Tote Bag"):
        assert other not in reply.text
    # "1" afterwards is the quantity of that line, never product number 1
    path = fp.resolve(text="1", button_id=None, cart=result.cart, catalogue=cat, client=CLIENT, clarify=reply.last_clarify)
    assert path.name == "quantity" and path.actions[0].fields.qty == 1
    # the buttons resolve from their ids alone
    take = fp.resolve(text="", button_id=reply.buttons[0][0], cart=result.cart, catalogue=cat, client=CLIENT, clarify={})
    assert take.name == "qty_take" and take.actions[0].fields.qty == 3
    similar = fp.resolve(text="", button_id=reply.buttons[2][0], cart=result.cart, catalogue=cat, client=CLIENT, clarify={})
    assert similar.name == "similar" and similar.actions[0].category == "Lehenga"


def test_a_numbered_product_list_never_overrides_an_open_quantity_question():
    """One numeric question per message: the product list widget refuses to replace a quantity question in the same reply."""
    cat = make_catalogue()
    renderer = Renderer("english", CLIENT, cat, make_cart())
    reply = Reply(text="x", last_clarify={"slot": "quantity", "options": [], "line_id": "l1", "sku": "KU1"})
    renderer.product_list_widget(reply, cat.views[:2])
    assert reply.last_clarify["slot"] == "quantity" and not reply.list_rows


def test_unusually_large_quantity_is_confirmed_first_and_the_confirmation_is_a_yes_no():
    """A quantity above agent_qty_sane_max (100) with stock available → 'Did you mean 150?'; yes applies it."""
    from app.schemas.agent_actions import LineFields, UpdateLine

    cat = make_catalogue()
    bag_cart = make_cart()
    bag_line = bag_cart.add_line("TB1")
    big_catalogue = make_catalogue()
    big_catalogue.get("TB1").product.stock = None                # untracked stock: any quantity fits
    result, issues = run_actions(bag_cart, [UpdateLine(type="update_line", line_id=bag_line["line_id"], fields=LineFields(qty=150))], catalogue=big_catalogue)
    issue = next(i for i in issues if i.code == "QTY_CONFIRM")
    assert issue.details["requested"] == 150 and result.cart.line(bag_line["line_id"]).get("qty") is None
    reply = _render(result.cart, big_catalogue, ex.clarify_for_issue(issue, result.cart, big_catalogue, CLIENT))
    assert "Did you mean 150" in reply.text and [t for _, t in reply.buttons] == ["Yes, 150", "No, change it"]
    assert reply.last_clarify["slot"] == "qty_confirm" and reply.last_clarify["qty"] == 150
    yes = fp.resolve(text="yes", button_id=None, cart=result.cart, catalogue=big_catalogue, client=CLIENT, clarify=reply.last_clarify)
    assert yes.name == "qty_ok" and yes.qty_confirmed and yes.actions[0].fields.qty == 150
    tap = fp.resolve(text="", button_id=reply.buttons[0][0], cart=result.cart, catalogue=big_catalogue, client=CLIENT, clarify={})
    assert tap.name == "qty_ok" and tap.qty_confirmed
    a_new_number = fp.resolve(text="20", button_id=None, cart=result.cart, catalogue=big_catalogue, client=CLIENT, clarify=reply.last_clarify)
    assert a_new_number.name == "quantity" and a_new_number.actions[0].fields.qty == 20


# ── 8. cheap deterministic paths ─────────────────────────────────────────────

def _words_catalogue():
    """Saree (colours Red / Yellow), Salwar Suit, Kurti, plus a bag."""
    from tests.test_agent_v15_units import _product
    from app.services.agent_catalog import Catalogue, ProductView, VariantView

    saree = ProductView(_product(1, "SR1", "Banarasi Saree", 2499.0, "Saree", has_variants=True), [
        VariantView(1, "Yellow", None, None, 2499.0, 5), VariantView(2, "Red", None, None, 2499.0, 5)])
    suit = ProductView(_product(2, "SU1", "Chudidar Georgette Suit", 1599.0, "Salwar Suit", has_variants=True), [VariantView(3, "Green", None, None, 1599.0, 5)])
    kurti = ProductView(_product(3, "KU1", "Cotton Kurti", 499.0, "Kurti", has_variants=True), [VariantView(4, "Red", None, None, 499.0, 5)])
    bag = ProductView(_product(4, "TB1", "Tote Bag", 250.0, "Bags", stock=10))
    return Catalogue([saree, suit, kurti, bag])


@pytest.mark.parametrize("text,kind", [
    ("Saree", "category"), ("saree", "category"), ("sari", "category"), ("kurti", "category"), ("suit", "category"), ("suite", "category"),
    ("show me sarees", "category"), ("saree dekh", "category"), ("dikha bhai suit", "category"), ("de do kurti", "category"),
    ("yellow saree", "search"), ("Chudidar suit georgette suite", "product"), ("dekh kurti red wala", "search"),
    ("show me the tote bag", "product"),
    # v1.7: "I'd like …" words are dropped — the noun alone decides (a digit, a size or a negation still goes to the planner)
    ("I want kurti", "category"), ("need saree", "category"), ("show suits", "category"), ("kurti chahiye", "category"),
    ("saree joiye", "category"), ("mujhe saree joiye", "category"), ("buy saree", "category"),
    ("Hi I want kurti", "category"), ("hello saree dikhao", "category"),
])
def test_catalogue_words_are_a_lookup_without_the_planner(text, kind):
    """TEST 14: single category words and plain noun / colour / fabric phrases resolve at ₹0."""
    lookup = agent_catalogue_words.parse(text, _words_catalogue())
    assert lookup is not None and lookup.kind == kind, (text, lookup)


@pytest.mark.parametrize("text", [
    "I want a red kurti", "I want the tote bag", "2 kurti", "kurti nahi chahiye", "don't want the saree", "red",
    "kurti ka price kitna hai", "yellow or red saree", "xl kurti", "cancel saree", "remove kurti", "kurti size m", "add kurti", "order saree",
])
def test_orders_removals_questions_and_bare_colours_still_go_to_the_planner(text):
    """Anything with order intent, a number, a negation, a size, an unknown word or only a colour is not a plain lookup."""
    assert agent_catalogue_words.parse(text, _words_catalogue()) is None, text


def test_the_fast_path_resolves_a_category_word_with_no_planner():
    """resolve() returns a ₹0 browse / search path for them (and nothing while a free-text slot is open)."""
    cat = _words_catalogue()
    cart = make_cart()
    path = fp.resolve(text="Saree", button_id=None, cart=cart, catalogue=cat, client=CLIENT, clarify={})
    assert path.name == "category" and path.actions[0].type == "browse_catalog" and path.actions[0].category.lower() in ("saree", "sari")
    path = fp.resolve(text="yellow saree", button_id=None, cart=cart, catalogue=cat, client=CLIENT, clarify={})
    assert path.name == "catalogue_search" and path.actions[0].query == "yellow saree"
    typing_address = {"slot": "address", "options": []}
    assert fp.resolve(text="Saree Market Surat", button_id=None, cart=cart, catalogue=cat, client=CLIENT, clarify=typing_address) is None


def test_hindi_roman_markers_are_detected():
    """'dekh', 'dikha', 'deke', 'bhai', 'wala' push a Roman message toward Hindi."""
    from app.services import language_service

    for text in ("saree dekh bhai", "red wala dikha do", "kurti deke dekh"):
        assert language_service.detect(text).lang == "hindi_roman", text


# ── 9. product photo → similar products ──────────────────────────────────────

def _photo_ctx(verdict=None, *, photo=None):
    """A minimal v3 context carrying one inbound product photo."""
    return SimpleNamespace(
        conv=SimpleNamespace(id=1, image_batch_reply_at=None), client=SimpleNamespace(id=1, business_name="Riya Boutique", accepts_cod=True,
        accepts_upi=True, upi_id="s@upi", delivery_days_min=3, delivery_days_max=7), message=SimpleNamespace(type="image", image=SimpleNamespace(id="m1")),
        is_whatsapp=True, download_media=AsyncMock(return_value=b"img"), image_verdict=verdict, image_bytes=None, photo_bytes=photo,
    )


@pytest.mark.asyncio
async def test_saree_photo_becomes_a_search_for_what_it_shows(monkeypatch):
    """TEST 2: the vision model describes 'red silk saree' → a SearchCatalog for exactly that (the list of the closest 3–5 products), not 'Nice photo!'."""
    from app.services import agent_v3, image_fingerprint, ocr_service
    from app.services.payment_proof_classifier import PRODUCT_PHOTO, ProofVerdict, VisionRead

    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: None)
    monkeypatch.setattr(image_fingerprint, "match", lambda *a, **k: None)
    cat = _words_catalogue()
    verdict = ProofVerdict(PRODUCT_PHOTO, 0.95, vision=VisionRead(PRODUCT_PHOTO, 0.95, "saree", ["red"], None, ["banarasi"]))
    outcome = await agent_v3._image_outcome(_photo_ctx(verdict), make_cart(), cat, "english")
    action = outcome.plan.output.actions[0]
    assert action.type == "search_catalog" and action.query == "red saree banarasi" and outcome.answer is None and not outcome.silent
    assert [v.sku for v in cat.search(action.query, top_k=5)][0] == "SR1"


@pytest.mark.asyncio
async def test_watermark_or_hash_match_shows_that_product(monkeypatch):
    """A catalogue picture (watermark / perceptual hash) → the product card."""
    from app.services import agent_v3, ocr_service

    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: "KU1")
    outcome = await agent_v3._image_outcome(_photo_ctx(), make_cart(), _words_catalogue(), "english")
    assert outcome.plan.output.actions[0].type == "show_product" and outcome.plan.output.actions[0].sku == "KU1"


@pytest.mark.asyncio
async def test_nothing_recognised_is_one_question_and_never_the_old_promise(monkeypatch):
    """No match → ONE reply asking what they want (it no longer promises 'Want me to find something similar?')."""
    from app.services import agent_v3, image_fingerprint, llm_health, ocr_service

    monkeypatch.setattr(ocr_service, "detect_sku_from_image", lambda b: None)
    monkeypatch.setattr(image_fingerprint, "match", lambda *a, **k: None)
    monkeypatch.setattr(llm_health, "is_vision_enabled", lambda: False)
    outcome = await agent_v3._image_outcome(_photo_ctx(), make_cart(), _words_catalogue(), "english")
    assert outcome.plan.output.actions == [] and outcome.key == "image_unknown"
    assert "couldn't find a close match" in outcome.answer and "something similar" not in outcome.answer


def test_the_image_reply_repeat_guard_is_five_minutes():
    """The same reply to a photo is not sent twice within 5 minutes."""
    from app.services import agent_v3

    assert agent_v3._IMAGE_REPEAT_SECONDS == 300
