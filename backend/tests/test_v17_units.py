"""
Unit tests (no database, no network) for the v1.7 round (conv=60 ORD-2026-0079):

  P0 latency        the event loop is never held by R2 / Pillow work; the loop watchdog names the blocking line; the hash index is built off the loop
  P0 no_reply_sent  every inbound message is answered unless its silence has a recognised reason
  P1 amounts        a black frame is "unreadable", OCR noise yields no amounts, real receipts yield 1758, the vision amount is a second source
  P1 picker         the newest unpaid order is attached by recency (never when another order is recent)
  P1 vision JSON    "unknown" with confidence 1.0 is retried once, then clamped
  P2 fast paths     "Hello buddy" / "I want kurti" never reach the planner

The Postgres end-to-end flows (tap → reply, order edit, notification) are in ``tests/replay/test_v17_flows.py``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import agent_catalogue_words, agent_multivariant
from app.services import agent_fast_paths as fp
from app.services import command_words as cw
from app.services import loop_monitor
from app.services import payment_proof_classifier as clf
from app.services.payment_amounts import extract_amounts, looks_like_text, read_amounts
from app.services.payment_proof_classifier import vision_read as real_vision_read      # tests/conftest.py replaces clf.vision_read with a stub
from tests.test_agent_v3_units import CLIENT, make_cart
from tests.test_v16_units import _words_catalogue

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"


# ── P0: the event loop ───────────────────────────────────────────────────────

async def test_measure_is_near_zero_on_a_free_loop_and_large_when_it_is_blocked():
    """``loop_monitor.measure`` is the metric printed as ``loop_lag=`` in every TURN block."""
    assert await loop_monitor.measure() < 0.02

    async def blocker():
        """Hold the loop for 120 ms the way a synchronous R2 / Pillow call does."""
        time.sleep(0.12)

    task = asyncio.ensure_future(blocker())
    lag = await loop_monitor.measure()
    await task
    assert lag >= 0.1, "a synchronous call ahead of us in the loop must show up as lag"


async def test_watchdog_logs_the_stack_of_the_line_that_holds_the_loop(caplog):
    """LOOP_BLOCKED carries the loop thread's stack at the moment of the stall — the line to fix — and LOOP_STALL its total."""
    caplog.set_level(logging.WARNING, logger="app.services.loop_monitor")
    dog = loop_monitor.Watchdog(asyncio.get_running_loop(), threshold=0.05)
    dog.start()

    async def a_slow_synchronous_call():
        """The kind of function the audit is looking for."""
        time.sleep(0.25)

    await asyncio.sleep(0.05)
    await a_slow_synchronous_call()
    await asyncio.sleep(0.1)
    dog.stop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "LOOP_BLOCKED" in text and "a_slow_synchronous_call" in text, text
    assert "LOOP_STALL" in text
    assert loop_monitor.max_since(time.perf_counter() - 5) >= 0.2


async def test_watermarking_and_r2_calls_do_not_hold_the_event_loop(monkeypatch):
    """The R2 ``head_object`` / ``put_object`` and the Pillow watermark were synchronous inside an ``async def`` (one per product image sent)."""
    from app.services import storage_service as svc

    class SlowR2:
        """An R2 client whose every network call takes 300 ms (a normal round trip from India)."""

        def head_object(self, **kw):
            """Not generated yet: the first send of a product has to create the copy."""
            time.sleep(0.3)
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

        def put_object(self, **kw):
            """Upload."""
            time.sleep(0.3)

    class Settings:
        """Minimal settings."""

        r2_bucket_name, r2_public_base_url, r2_endpoint = "b", "https://cdn.test", "https://r2.test"

    svc._watermark_url_cache.clear()
    monkeypatch.setattr(svc, "is_r2_configured", lambda: True)
    monkeypatch.setattr(svc, "_r2_client", lambda: SlowR2())
    monkeypatch.setattr(svc, "get_settings", lambda: Settings())
    monkeypatch.setattr(svc, "watermark_sku", lambda b, sku: (time.sleep(0.2), b"wm")[1])

    class Resp:
        """A fetched source image."""

        content = b"img"

        def raise_for_status(self):
            """OK."""

    class FakeHttp:
        """httpx.AsyncClient stand-in."""

        def __init__(self, *a, **k):
            """Ignore the arguments."""

        async def __aenter__(self):
            """Enter."""
            return self

        async def __aexit__(self, *a):
            """Exit."""

        async def get(self, url):
            """The source image."""
            return Resp()

    monkeypatch.setattr(svc.httpx, "AsyncClient", FakeHttp)
    lags: list[float] = []

    async def probe():
        """How late does a 10 ms timer fire?"""
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    watcher = asyncio.create_task(probe())
    url = await svc.get_watermarked_image_url(1, "KU1", "https://cdn.test/src.jpg")
    watcher.cancel()
    assert url and url.startswith("https://cdn.test/1/watermarked/")
    assert max(lags) < 0.05, f"the loop was held for {max(lags):.3f}s while R2 / Pillow worked (~0.8 s of blocking calls)"
    # and the second send of the same product costs no R2 round trip at all
    monkeypatch.setattr(svc, "_r2_client", lambda: (_ for _ in ()).throw(AssertionError("R2 was called again")))
    assert await svc.get_watermarked_image_url(1, "KU1", "https://cdn.test/src.jpg") == url


async def test_hashing_the_catalogue_runs_off_the_loop_and_match_never_builds():
    """200 catalogue photos = ~17 s of Pillow decoding; on the loop that froze every customer. It now runs in the hash pool."""
    from PIL import Image

    from app.services import image_fingerprint as fpr

    fpr.clear()
    buf = io.BytesIO()
    Image.effect_noise((1600, 1200), 80).convert("RGB").save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    lags: list[float] = []

    async def probe():
        """How late does a 10 ms timer fire?"""
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    async def fetch(url):
        """A catalogue image download."""
        await asyncio.sleep(0.001)
        return data

    watcher = asyncio.create_task(probe())
    index = await fpr.build_index([(f"SKU{i}", "u") for i in range(40)], fetch=fetch)
    watcher.cancel()
    assert len(index) == 40
    assert max(lags) < 0.08, f"hashing held the loop for {max(lags):.3f}s"

    # match() only reads the cache: with no index it returns None and starts nothing
    assert fpr.match(5, data, [("SKU1", "u")]) is None and not fpr._building and not fpr._tasks
    # ensure_index schedules ONE background build per catalogue state (no TTL rebuilds), and match_async finds the picture afterwards
    fpr.set_index(5, index)
    assert await fpr.match_async(5, data) in index
    fpr.clear()


def test_ensure_index_is_a_noop_for_an_unchanged_catalogue_and_without_a_loop():
    """No running loop (sync caller) → nothing scheduled; the signature of the photos decides whether a rebuild is needed."""
    from app.services import image_fingerprint as fpr

    fpr.clear()
    assert fpr.ensure_index(9, [("A", "u1")]) is False          # no loop here: the startup warm-up / next snapshot load schedules it
    assert fpr.ensure_index(9, [("A", None), ("", "u")]) is False   # nothing indexable


async def test_ensure_index_builds_once_per_catalogue_state(monkeypatch):
    """Same (sku, url) set → no second build; a changed photo → one rebuild."""
    from app.services import image_fingerprint as fpr

    fpr.clear()
    builds = []

    async def fake_build(products, fetch=None):
        """Count builds."""
        builds.append(list(products))
        return {s: 1 for s, _ in products}

    monkeypatch.setattr(fpr, "build_index", fake_build)
    assert fpr.ensure_index(3, [("A", "u1")]) is True
    await asyncio.gather(*list(fpr._tasks))
    assert fpr.ensure_index(3, [("A", "u1")]) is False            # unchanged
    assert fpr.ensure_index(3, [("A", "u2")]) is True             # the photo changed
    await asyncio.gather(*list(fpr._tasks))
    assert len(builds) == 2
    fpr.clear()


# ── P0: every inbound message is answered ────────────────────────────────────

class _Db:
    """A DB stand-in answering the one lookup ``_reply_sent_recently`` makes."""

    def __init__(self, created_at):
        """Remember when the bot last spoke."""
        self.created_at = created_at

    async def execute(self, _stmt):
        """The newest assistant message's timestamp."""
        value = self.created_at

        class R:
            """Result."""

            def scalar_one_or_none(self):
                """The timestamp."""
                return value

        return R()


def _ctx(db=None):
    """A minimal inbound context."""
    return SimpleNamespace(db=db, client=None, conv=SimpleNamespace(id=1, current_stage="greeting"), message=SimpleNamespace(type="image"))


async def test_silence_with_a_recognised_reason_is_respected(caplog):
    """Paused bot / blocked / not live / muted / render error: no reply, no violation."""
    from app.services import order_pipeline as op

    caplog.set_level(logging.ERROR)
    for reason in sorted(op.SILENCE_REASONS):
        result = op.PipelineResult(text=None, skip_send=True, skip_reason=reason)
        assert await op.ensure_reply(result, _ctx()) is result and result.skip_send
    assert not [r for r in caplog.records if "no_reply_sent" in r.getMessage()]


async def test_a_deduped_image_needs_a_reply_sent_within_ten_seconds():
    """A burst's extra image may stay silent only while the bot's reply is ≤ 10 s old; later it must be answered."""
    from app.services import order_pipeline as op

    fresh = _Db(datetime.now(timezone.utc) - timedelta(seconds=4))
    result = op.PipelineResult(text=None, skip_send=True, skip_reason="image_burst")
    assert await op.ensure_reply(result, _ctx(fresh)) is result and result.skip_send

    async def fallback(db, conv, client):
        """The deterministic stage-aware text."""
        return "Got your picture 👍"

    stale = _Db(datetime.now(timezone.utc) - timedelta(seconds=45))
    ctx = _ctx(stale)
    ctx.db.refresh = _noop
    ctx.db.add = lambda *_: None
    old = op.stage_aware_fallback_text
    op.stage_aware_fallback_text = fallback
    try:
        late = op.PipelineResult(text=None, skip_send=True, skip_reason="image_burst")
        out = await op.ensure_reply(late, ctx)
    finally:
        op.stage_aware_fallback_text = old
    assert out.skip_send is False and out.text == "Got your picture 👍"


async def _noop(*_a, **_k):
    """An awaitable that does nothing."""


async def test_silence_without_a_reason_is_a_logged_violation_and_gets_an_answer(caplog, monkeypatch):
    """``no_reply_sent``: skip_send with no reason (or an unknown one) is a bug — the customer gets the deterministic fallback."""
    from app.services import order_pipeline as op

    async def fallback(db, conv, client):
        """The deterministic stage-aware text."""
        return "Sorry, say that again?"

    async def save(*a, **k):
        """Skip the transcript write."""

    monkeypatch.setattr(op, "stage_aware_fallback_text", fallback)
    monkeypatch.setattr(op.conversation_service, "save_message", save)
    ctx = _ctx(SimpleNamespace(refresh=_noop))
    caplog.set_level(logging.ERROR)
    result = op.PipelineResult(text=None, skip_send=True)
    out = await op.ensure_reply(result, ctx)
    assert out.skip_send is False and out.text == "Sorry, say that again?"
    assert any("no_reply_sent" in r.getMessage() for r in caplog.records)


# ── P1: the screenshot that was a black frame ────────────────────────────────

def test_the_black_frame_of_conv60_is_unreadable_and_its_ocr_noise_has_no_amounts():
    """
    The stored "₹1,758 screenshot" (messages.id 4825) is a frame whose brightest pixel is 6 of 255. Tesseract returned 150 characters of noise
    (``tests/fixtures/ocr/black_frame_receipt.ocr.txt``) and the old reader turned its digits into ``[7.0, 2.0]``.
    """
    noise = (FIXTURES / "black_frame_receipt.ocr.txt").read_text()
    assert len(noise) >= 100 and not looks_like_text(noise)
    assert extract_amounts(noise) == [], "single digits in OCR noise are not rupee amounts"
    assert clf.is_blank_image((FIXTURES / "black_frame_receipt.jpg").read_bytes())


@pytest.mark.parametrize("name", [
    "phonepe_dark_1758", "gpay_spaced_1758", "paytm_rs_1758", "bank_sms_1758", "bhim_plain_1758",
])
def test_real_receipt_ocr_texts_yield_1758(name):
    """Five receipt layouts / OCR habits (``%`` or ``Z`` for ₹, spaced thousands, Rs.1758.00, bank SMS) → exactly ₹1,758."""
    text = (FIXTURES / f"{name}.ocr.txt").read_text()
    assert extract_amounts(text) == [1758.0], (name, read_amounts(text))


def test_a_rendered_dark_mode_receipt_is_read_end_to_end_with_the_real_ocr():
    """A synthetic dark-mode UPI success screen (light text on near-black) → inversion → Tesseract → ₹1,758 (skipped without the binary)."""
    from app.services import ocr_service

    if not ocr_service.check_tesseract_installed():
        pytest.skip("tesseract is not installed")
    data = (FIXTURES / "dark_mode_receipt_1758.png").read_bytes()
    assert not clf.is_blank_image(data)
    text = clf._ocr_sync(data)
    assert 1758.0 in extract_amounts(text), text


def test_amounts_below_the_minimum_and_noise_words_are_not_amounts():
    """A list index, a count, a digit of noise is never a payment; a ₹-marked amount is still taken."""
    assert extract_amounts("Items 3 qty 2 Order total shown above") == []
    assert extract_amounts("Payment successful ₹ 1,758 paid to riya") == [1758.0]
    assert extract_amounts("₹ 5") == []


async def test_a_blank_image_is_classified_unreadable_without_ocr_or_vision(monkeypatch):
    """No OCR task, no model call: the verdict is ``image_blank`` and the customer is asked to resend."""
    called = []

    async def boom(*a, **k):
        """Fail when the expensive evidence is gathered."""
        called.append(1)
        raise AssertionError("OCR / vision must not run on a black frame")

    monkeypatch.setattr(clf, "ocr_text", boom)
    monkeypatch.setattr(clf, "vision_read", boom)
    verdict = await clf.classify((FIXTURES / "black_frame_receipt.jpg").read_bytes(), order_totals=[1758.0, 17245.0])
    assert verdict.unreadable and verdict.amounts == [] and not called


async def test_vision_amount_is_a_second_source_that_must_agree_with_an_unpaid_order(monkeypatch):
    """OCR sure it is a receipt but its amount fits no order → the model's amount is adopted when it equals one unpaid total."""
    async def ocr(_b):
        """A receipt whose amount OCR garbled (matches neither order)."""
        return "PhonePe\nPayment successful\nPaid to Riya Sarees\n₹ 4\nUTR 412345678901\nUPI transaction ID 412345678901"

    async def vision(b, **k):
        """The model reads the real amount."""
        return clf.VisionRead(clf.PAYMENT_PROOF, 0.95, amount=1758.0)

    monkeypatch.setattr(clf, "ocr_text", ocr)
    monkeypatch.setattr(clf, "vision_read", vision)
    monkeypatch.setattr(clf, "is_blank_image", lambda b: False)
    verdict = await clf.classify(b"img", order_totals=[1758.0, 17245.0])
    assert verdict.kind == clf.PAYMENT_PROOF and 1758.0 in verdict.amounts and "vision_amount_matches_order" in verdict.signals
    # a stray amount that fits no order is ignored
    async def stray(b, **k):
        """An amount nobody owes."""
        return clf.VisionRead(clf.PAYMENT_PROOF, 0.95, amount=999.0)

    monkeypatch.setattr(clf, "vision_read", stray)
    verdict = await clf.classify(b"img", order_totals=[1758.0, 17245.0])
    assert 999.0 not in verdict.amounts


# ── P1: the order picker ─────────────────────────────────────────────────────

def _order(number, minutes_ago, total, now):
    """An unpaid order created `minutes_ago` minutes before `now`."""
    return SimpleNamespace(id=int(number[-4:]), order_number=number, status="pending_payment", total_amount=total, created_at=now - timedelta(minutes=minutes_ago))


def test_the_newest_unpaid_order_is_attached_when_its_instructions_are_recent_and_the_rest_are_old():
    """conv=60: ORD-0079 (7 min ago) vs ORD-0077 (12 h ago) and no readable amount → ORD-0079."""
    from app.services import payment_inbound as pi

    now = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    new, old = _order("ORD-2026-0079", 7, 1758.0, now), _order("ORD-2026-0077", 12 * 60, 17245.0, now)
    assert pi.pick_order_by_recency([old, new], now=now) is new


@pytest.mark.parametrize("newest_age,other_age", [(45, 12 * 60), (7, 3 * 60), (7, 5 * 60 + 59)])
def test_the_picker_asks_when_the_recency_rule_does_not_clearly_decide(newest_age, other_age):
    """Newest sent > 30 min ago, or another order's instructions < 6 h old → ask the customer."""
    from app.services import payment_inbound as pi

    now = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    orders = [_order("ORD-2026-0079", newest_age, 1758.0, now), _order("ORD-2026-0077", other_age, 17245.0, now)]
    assert pi.pick_order_by_recency(orders, now=now) is None


def test_a_re_sent_instruction_counts_for_the_newest_order():
    """An edited order re-sends its payment instructions: the conversation's last instruction time keeps it 'recent'."""
    from app.services import payment_inbound as pi

    now = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    orders = [_order("ORD-2026-0079", 50, 1758.0, now), _order("ORD-2026-0077", 12 * 60, 17245.0, now)]
    assert pi.pick_order_by_recency(orders, now=now) is None
    assert pi.pick_order_by_recency(orders, last_instruction_at=now - timedelta(minutes=2), now=now) is orders[0]


def test_amount_decides_before_recency():
    """The amount on the receipt beats the recency rule."""
    from app.services import payment_inbound as pi

    now = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    new, old = _order("ORD-2026-0079", 7, 1758.0, now), _order("ORD-2026-0077", 12 * 60, 17245.0, now)
    ctx = SimpleNamespace(conv=SimpleNamespace(last_payment_instruction_at=None))
    order, ambiguous, auto = pi.resolve_target(ctx, [new, old], [17245.0])
    assert order is old and not ambiguous and not auto


# ── P1: vision JSON ──────────────────────────────────────────────────────────

def test_unknown_with_full_confidence_is_a_schema_problem_and_is_clamped():
    """``vision_label=unknown`` with ``vision_conf=1.0`` cannot be trusted."""
    assert clf.vision_problem({"kind": "unknown", "confidence": 1.0}) is not None
    assert clf.vision_problem({"kind": "unknown", "confidence": 0.4}) is None
    assert clf.vision_problem({"kind": "banana", "confidence": 0.9}) is not None
    assert clf.vision_problem({"kind": "payment_proof", "confidence": "high"}) is not None
    assert clf._vision_read_from({"kind": "unknown", "confidence": 1.0}).confidence == 0.5


async def test_a_contradictory_vision_reply_is_retried_once_with_a_strict_prompt(monkeypatch, caplog):
    """First reply contradicts itself → logged raw, retried with the strict suffix; a consistent second reply is used."""
    from app.services import llm_health

    replies = iter([
        '{"kind": "unknown", "confidence": 1.0, "category": null, "colours": [], "fabric": null, "keywords": []}',
        '{"kind": "product_photo", "confidence": 0.9, "category": "saree", "colours": ["red"], "fabric": null, "keywords": []}',
    ])
    prompts = []

    async def call(image_bytes, prompt, **k):
        """The model."""
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(clf, "_vision_call", call)
    monkeypatch.setattr(llm_health, "is_vision_enabled", lambda: True)
    caplog.set_level(logging.WARNING)
    read = await real_vision_read(b"img")
    assert read.kind == clf.PRODUCT_PHOTO and read.confidence == 0.9 and len(prompts) == 2
    assert "broke the schema" in prompts[1]
    assert any("VISION_SCHEMA_MISMATCH" in r.getMessage() and "confidence=1.0" in r.getMessage() for r in caplog.records)


async def test_a_vision_reply_that_stays_contradictory_is_clamped_not_trusted(monkeypatch):
    """Two bad replies → the verdict is kept but 'unknown' never exceeds 0.5."""
    from app.services import llm_health

    async def call(image_bytes, prompt, **k):
        """Always the same contradiction."""
        return '{"kind": "unknown", "confidence": 1.0}'

    monkeypatch.setattr(clf, "_vision_call", call)
    monkeypatch.setattr(llm_health, "is_vision_enabled", lambda: True)
    read = await real_vision_read(b"img")
    assert read.kind == clf.UNKNOWN and read.confidence <= 0.5


# ── P2: deterministic paths ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Hello buddy", "hi bro", "hey there", "hello ji", "Good morning sir", "namaste didi", "hii dear", "hello hello", "Hey guys!",
])
def test_greeting_plus_filler_is_a_greeting_without_the_planner(text):
    """Prefix greeting + up to three filler words, in any case."""
    assert cw.is_greeting_message(text), text
    path = fp.resolve(text=text, button_id=None, cart=make_cart(), catalogue=_words_catalogue(), client=CLIENT, clarify={})
    assert path is not None and path.name == "greeting", (text, path)


@pytest.mark.parametrize("text", ["hi kurti", "hello what is the price", "hey I want to cancel", "hi 2", "hello order status"])
def test_a_greeting_followed_by_a_request_is_not_just_a_greeting(text):
    """Anything beyond filler is a request."""
    assert not cw.is_greeting_message(text), text


@pytest.mark.parametrize("text,kind,value", [
    ("I want kurti", "category", "kurti"), ("need saree", "category", "saree"), ("show suits", "category", "suit"),
    ("kurti chahiye", "category", "kurti"), ("saree joiye", "category", "saree"), ("hi I want kurti", "category", "kurti"),
])
def test_intent_words_around_a_category_are_a_category_lookup(text, kind, value):
    """want / need / show / chahiye / joiye are stripped; the noun is browsed — zero planner calls."""
    lookup = agent_catalogue_words.parse(text, _words_catalogue())
    assert lookup is not None and lookup.kind == kind and value in lookup.value.lower(), (text, lookup)
    path = fp.resolve(text=text, button_id=None, cart=make_cart(), catalogue=_words_catalogue(), client=CLIENT, clarify={})
    assert path is not None and path.name == "category"


@pytest.mark.parametrize("text", ["I want 2 kurti", "need saree in xl", "I want to cancel", "I want"])
def test_quantities_sizes_and_bare_intent_still_go_to_the_planner(text):
    """A digit, a size, or no noun at all is not a plain lookup."""
    assert agent_catalogue_words.parse(text, _words_catalogue()) is None


# ── P0: quantity glued to its option ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Ohh I want 1 black and 1gray", [["1", "black"], ["1", "gray"]]),
    ("1gray 2black", [["1", "gray"], ["2", "black"]]),
    ("black2 grey1", [["black", "2"], ["grey", "1"]]),
    ("1 x black and 2 x grey", [["1", "black"], ["2", "grey"]]),
    ("2xl and 1 black", [["2xl"], ["1", "black"]]),
])
def test_quantity_glued_to_its_option_is_split_and_sizes_stay_whole(text, expected):
    """"1gray" → 1 + gray; "2xl" stays a size; interjections / "x" are filler."""
    got = [[t for t in seg if t not in agent_multivariant._FILLER] for seg in agent_multivariant._segments(text)]
    assert got == expected, got
