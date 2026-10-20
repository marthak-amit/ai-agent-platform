"""
Round-3 unit tests (no database, no network): central LLM cost tracking, the COST / TIME trace lines, the planner's cache-friendly
prompt layout, the typed-union plan schema, catalogue attribute parsing.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import llm_client, llm_usage_service, turn_trace

BASE = dict(
    database_url="postgresql://u:p@h/d", meta_app_secret="x", meta_verify_token="x",
    whatsapp_access_token="x", whatsapp_phone_number_id="1", _env_file=None,
)


@pytest.fixture
def prices(monkeypatch):
    """Model 'm' = $1 in / $0.5 cached in / $2 out per 1M; USD_INR 80; whisper $0.36/hour."""
    s = Settings(usd_inr=80.0, llm_price_usd_per_1m={"m": (1.0, 0.5, 2.0), "old": (1.0, 2.0)},
                 llm_price_default_usd_per_1m=(10.0, 10.0), llm_stt_usd_per_hour=0.36, **BASE)
    monkeypatch.setattr(llm_usage_service, "get_settings", lambda: s)
    monkeypatch.setattr(llm_client, "get_settings", lambda: s)
    llm_usage_service._warned_models.clear()
    return s


@pytest.fixture
def rows(monkeypatch):
    """Capture the rows that would be persisted; start a fresh per-turn accumulator."""
    captured: list[dict] = []

    async def _capture(row):
        """Record instead of writing to a database."""
        captured.append(dict(row))

    monkeypatch.setattr(llm_usage_service, "_persist_row", _capture)
    llm_usage_service.set_context(None, None)
    yield captured
    llm_usage_service.set_context(None, None)


def _resp(prompt=1000, completion=200, cached=0, reasoning=0, content="ok"):
    """A provider response with Groq-shaped usage details."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached) if cached else None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning) if reasoning else None,
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


def _client(resp=None, exc=None):
    """Fake AsyncOpenAI."""
    c = MagicMock()
    c.chat.completions.create = AsyncMock(return_value=resp, side_effect=exc)
    return c


# ── A. cost tracking ─────────────────────────────────────────────────────────

def test_usd_inr_is_required_and_has_no_default_in_code(monkeypatch):
    """The rate comes from the environment; without it the settings do not load (no silently hard-coded 83)."""
    monkeypatch.delenv("USD_INR", raising=False)
    with pytest.raises(ValidationError):
        Settings(**BASE)
    assert Settings(usd_inr=90.0, **BASE).usd_inr == 90.0


def test_price_table_has_cached_input_and_falls_back_for_two_part_entries(prices):
    """(in, cached_in, out) per 1M; an old (in, out) entry bills cached tokens at the input rate."""
    assert llm_usage_service.compute_cost_usd("m", 1000, 400, 100) == pytest.approx((600 * 1.0 + 400 * 0.5 + 100 * 2.0) / 1e6)
    assert llm_usage_service.compute_cost_usd("old", 1000, 400, 100) == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)
    assert llm_usage_service.compute_cost_inr("m", 1000, 100, cached_tokens=400) == pytest.approx(
        llm_usage_service.compute_cost_usd("m", 1000, 400, 100) * 80)


def test_default_planner_model_is_priced_with_the_cached_rate():
    """gpt-oss-120b: $0.15 in / $0.075 cached / $0.60 out per 1M."""
    s = Settings(usd_inr=83.0, **BASE)
    assert s.llm_price_usd_per_1m[s.llm_model_planner] == (0.15, 0.075, 0.60)


async def test_llm_call_records_cached_tokens_reasoning_and_usd(prices, rows):
    """Every call reports prompt / cached / completion (reasoning included) tokens, $ and ₹ to the ledger."""
    await llm_client.llm_call(
        "planner", "m", [{"role": "user", "content": "x"}], 7, 55, max_tokens=10,
        client=_client(_resp(prompt=2000, completion=300, cached=1500, reasoning=120)),
    )
    [row] = rows
    assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"]) == (2000, 1500, 300)
    usd = (500 * 1.0 + 1500 * 0.5 + 300 * 2.0) / 1e6
    assert row["cost_usd"] == pytest.approx(usd) and row["cost_inr"] == pytest.approx(usd * 80) and row["audio_seconds"] == 0


async def test_failed_calls_are_recorded_with_zero_cost(prices, rows):
    """A provider error is a ledger row (success False) and a zero-token entry in the turn."""
    boom = type("Boom", (Exception,), {"status_code": 429})("slow")
    with pytest.raises(Exception):
        await llm_client.llm_call("planner", "m", [], max_tokens=5, client=_client(exc=boom))
    assert rows[0]["success"] is False and rows[0]["cost_usd"] == 0


async def test_whisper_cost_uses_the_audio_seconds_the_provider_reports(prices, rows):
    """STT: billed by the transcribed duration ($ per hour); the size estimate is only the fallback."""
    stt = MagicMock()
    stt.audio.transcriptions.create = MagicMock(return_value=SimpleNamespace(text="hello there", duration=42.5))
    text = await llm_client.llm_transcribe("stt", "whisper-large-v3-turbo", b"\x00" * 3000, "a.ogg", "audio/ogg", 7, 55, client=stt)
    assert text == "hello there"
    [row] = rows
    assert row["audio_seconds"] == pytest.approx(42.5) and row["cost_usd"] == pytest.approx(42.5 / 3600 * 0.36)
    assert row["cost_inr"] == pytest.approx(row["cost_usd"] * 80)
    stt.audio.transcriptions.create = MagicMock(return_value="plain text result")           # old-style string response
    assert await llm_client.llm_transcribe("stt", "whisper-large-v3-turbo", b"\x00" * 3000, "a.ogg", "audio/ogg", client=stt) == "plain text result"
    assert rows[-1]["audio_seconds"] == pytest.approx(10.0)                                  # size estimate, 10 s minimum


def test_cost_line_format():
    """COST turn=₹x session=₹y lifetime=₹z — three ₹ figures, all read from the llm_usage ledger."""
    assert turn_trace.format_cost_line(turn=0.1411, session=0.2234, lifetime=4.7858) == "COST turn=₹0.14 session=₹0.22 lifetime=₹4.79"
    assert turn_trace.format_cost_line(turn=0, session=0, lifetime=0) == "COST turn=₹0.00 session=₹0.00 lifetime=₹0.00"


def test_the_in_memory_cost_accumulators_are_gone():
    """One source of truth: the ledger. No per-turn accumulator, no per-process running totals, no cart usage_total."""
    for name in ("begin_turn", "note_turn_call", "turn_calls", "turn_summary", "conversation_total", "_conv_running"):
        assert not hasattr(llm_usage_service, name), name
    from app.services import agent_cart

    assert "usage_total" not in agent_cart.CARRIED_STATE_KEYS


def test_time_line_format_and_spans():
    """TIME total=…ms wait=… db=… search=… planner=… render=… localizer=… wa_send=… wa_connect=… wa_request=… image_send=…; spans accumulate per turn."""
    turn_trace.start()
    turn_trace.add_span("planner", 0.5)
    with turn_trace.span("planner"):
        pass
    turn_trace.add_span("wait", 1.25)
    spans = turn_trace.current_spans()
    assert spans["planner"] >= 0.5 and spans["wait"] == pytest.approx(1.25)
    line = turn_trace.format_time_line(spans, total_ms=2500)
    assert line.startswith("TIME total=2500ms wait=1250ms db=0ms search=0ms planner=") and "wa_send=0ms wa_connect=0ms wa_request=0ms image_send=0ms" in line


# ── C10. cache-friendly planner prompt ───────────────────────────────────────

def _prompt_for(message, lines, history):
    """build_messages for a minimal shop / cart."""
    from app.services import agent_planner
    from app.services.agent_cart import CartData
    from app.services.agent_catalog import Catalogue, ProductView, VariantView

    kurti = ProductView(SimpleNamespace(id=1, sku="KU1", name="Kurti", price=499.0, category="K", stock=None, has_variants=True, description=None,
                                        is_active=True, delivery_days=None, image_url=None),
                        [VariantView(1, "Red", None, None, 499.0, 5)])
    cart = CartData(id=1, conversation_id=1, client_id=1, lines=lines)
    client = SimpleNamespace(id=1, business_name="Riya Boutique", accepts_cod=True, accepts_upi=True, upi_id="a@b",
                             delivery_days_min=3, delivery_days_max=7)
    hist = [SimpleNamespace(role="user", content=h) for h in history]
    return agent_planner.build_messages(client=client, catalogue=Catalogue([kurti]), cart=cart, message=message, history_rows=hist,
                                        pending_orders=[], last_clarify=None)


def test_planner_prompt_puts_every_static_byte_first_and_identical():
    """Rules + action reference + store policies form ONE system message that is byte-identical for every customer turn."""
    a = _prompt_for("show me kurti", [], [])
    b = _prompt_for("2 red please", [{"line_id": "x1", "sku": "KU1", "color": "Red", "size": None, "material": None, "qty": 2}], ["hi", "hello"])
    assert a[0]["content"] == b[0]["content"]
    system = a[0]["content"]
    assert "STORE POLICIES" in system and "Riya Boutique" in system and '"payment_methods":["COD","UPI"]' in system.replace(" ", "")
    for dynamic in ("show me kurti", "2 red please", '"line_id":"x1"', "CUSTOMER MESSAGE", "RECENT MESSAGES", "hello"):
        assert dynamic not in system
    user = a[1]["content"]
    assert "STORE POLICIES" not in user and user.index("RECENT MESSAGES") < user.index("STATE") < user.index("CATALOGUE") < user.index("CUSTOMER MESSAGE")


# ── C11. typed-union schema / raw logging ────────────────────────────────────

def test_union_schema_is_strict_and_each_action_requires_its_own_fields():
    """anyOf, one closed branch per action; split_line cannot be generated without line_id and parts."""
    from app.schemas.agent_actions import ACTION_TYPES, union_wire_schema

    schema = union_wire_schema()
    branches = schema["properties"]["actions"]["items"]["anyOf"]
    # actions with identical fields share a branch (smaller prompt): every action type is still allowed exactly once
    listed = [name for b in branches for name in b["properties"]["type"]["enum"]]
    assert sorted(listed) == sorted(ACTION_TYPES)
    for b in branches:
        assert b["additionalProperties"] is False and set(b["required"]) == set(b["properties"])
    split = next(b for b in branches if b["properties"]["type"]["enum"] == ["split_line"])
    assert set(split["required"]) == {"type", "line_id", "parts"} and split["properties"]["line_id"]["type"] == "string"
    assert split["properties"]["parts"]["minItems"] == 1


def test_a_bare_split_line_is_an_invalid_action():
    """The live INVALID_ACTION@0: every field null → line_id / parts missing."""
    from app.schemas.agent_actions import parse_planner_output

    out, issues = parse_planner_output({"actions": [{"type": "split_line", "line_id": None, "parts": None, "query": None}], "reply_intent": "none"})
    assert out.actions == [] and issues[0].code == "INVALID_ACTION" and "line_id" in issues[0].message


def test_empty_extra_objects_from_the_flat_schema_do_not_invalidate_an_action():
    """Flat-schema fallback: {"type":"search_catalog","query":"x","variant":{"color":null},"fields":{}} is still a search."""
    from app.schemas.agent_actions import parse_planner_output

    out, issues = parse_planner_output({"actions": [
        {"type": "search_catalog", "query": "x", "variant": {"color": None, "size": None}, "fields": {}, "parts": []},
    ], "reply_intent": "none"})
    assert [a.type for a in out.actions] == ["search_catalog"] and not issues


def test_call_model_tries_the_union_schema_then_the_flat_one_then_json_mode(monkeypatch):
    """Provider refuses a schema (400) → the next rung; the answer of the first accepted rung is returned."""
    from app.services import agent_planner

    seen: list[str] = []

    async def fake_call(purpose, model, messages, client_id, conversation_id, *, max_tokens, temperature, response_format):
        """Refuse both json_schema forms, accept JSON mode."""
        kind = response_format["type"] if response_format["type"] != "json_schema" else (
            "union" if "anyOf" in str(response_format["json_schema"]["schema"]["properties"]["actions"]) else "flat")
        seen.append(kind)
        if kind != "json_object":
            err = Exception("400: response_format json_schema is not supported for this schema")
            err.status_code = 400
            raise err
        msg = SimpleNamespace(content='{"actions": [], "reply_intent": "none"}', reasoning=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    monkeypatch.setattr(llm_client, "llm_call", fake_call)
    out = asyncio.run(agent_planner.call_model([{"role": "user", "content": "x"}], client_id=1, conversation_id=1))
    assert seen == ["union", "flat", "json_object"] and out == {"actions": [], "reply_intent": "none"}


def test_plan_logs_the_raw_output_when_actions_are_invalid(monkeypatch, caplog):
    """INVALID_ACTION → one WARNING with the raw planner JSON (that is how the cause of INVALID_ACTION@0 was found)."""
    from app.services import agent_planner

    async def fake_model(messages, *, client_id=None, conversation_id=None):
        """The bare split_line."""
        return {"actions": [{"type": "split_line", "line_id": None, "parts": None}], "reply_intent": "none"}

    async def fake_history(db, conv_id):
        """No history."""
        return []

    monkeypatch.setattr(agent_planner, "call_model", fake_model)
    monkeypatch.setattr(agent_planner.conversation_service, "get_history", fake_history)
    monkeypatch.setattr(agent_planner, "build_messages", lambda **k: [{"role": "user", "content": "x"}])
    with caplog.at_level(logging.WARNING):
        asyncio.run(agent_planner.plan(db=None, client=SimpleNamespace(id=1), conv=SimpleNamespace(id=5), catalogue=None,
                                       cart=None, message="2 xl and 3 M", pending_orders=[], last_clarify=None))
    assert "PLANNER invalid actions" in caplog.text and "split_line" in caplog.text


# ── C8. attribute parsing in search ──────────────────────────────────────────

def _catalogue():
    """Kurti (colours Red/Green/Blue), saree (Maroon/Gold), lehenga (colour × size M / XXL)."""
    from app.services.agent_catalog import Catalogue, ProductView, VariantView

    def p(pid, sku, name, price, cat, **kw):
        """A Product-shaped object."""
        return SimpleNamespace(id=pid, sku=sku, name=name, price=price, category=cat, stock=kw.get("stock"), has_variants=kw.get("has_variants", False),
                               description=None, is_active=True, delivery_days=None, image_url=None)
    kurti = ProductView(p(1, "KU1", "Cotton Kurti", 499.0, "Kurti", has_variants=True),
                        [VariantView(i, c, None, None, 499.0, 5) for i, c in enumerate(("Red", "Green", "Blue"), 1)])
    saree = ProductView(p(2, "SR1", "Silk Saree", 2499.0, "Saree", has_variants=True),
                        [VariantView(4, "Maroon", None, None, 2499.0, 3), VariantView(5, "Gold", None, None, 2499.0, 2)])
    lehenga = ProductView(p(3, "LH1", "Cotton Lehenga", 1299.0, "Lehenga", has_variants=True),
                          [VariantView(6, "Pink", "M", None, 1299.0, 5), VariantView(7, "Blue", "XXL", None, 1299.0, 3)])
    return Catalogue([kurti, saree, lehenga])


def test_parse_attributes_splits_the_query_into_text_and_filters():
    """"red kurti" → kurti + colour red; "xl lehenga" → size XL; "blue m lehenga" → colour + size; plain text stays."""
    from app.services.agent_catalog import parse_attributes

    cat = _catalogue()
    assert parse_attributes("red kurti", cat) == ("kurti", {"color": "red"})
    assert parse_attributes("xl lehenga", cat) == ("lehenga", {"size": "XL"})
    assert parse_attributes("blue m lehenga", cat) == ("lehenga", {"color": "blue", "size": "M"})
    assert parse_attributes("I am looking for a saree", cat)[1] == {}
    assert parse_attributes("laal kurti", cat) == ("kurti", {"color": "red"})


def test_filtered_search_matches_closest_colour_family_and_size():
    """Matches need an in-stock variant with ALL filters; otherwise the closest real option is named."""
    cat = _catalogue()
    hit = cat.search_filtered("red kurti")
    assert [v.sku for v in hit["views"]] == ["KU1"] and hit["labels"]["KU1"] == ["Red"] and hit["closest"] is None
    miss = cat.search_filtered("red saree")
    assert miss["views"] == [] and miss["closest"]["values"] == ["Maroon"] and [v.sku for v in miss["closest"]["views"]] == ["SR1"]
    size = cat.search_filtered("xl lehenga")
    assert size["closest"]["attr"] == "size" and size["closest"]["values"] == ["XXL"]
    nothing = cat.search_filtered("pink kurti")
    assert nothing["closest"]["values"] == ["Red", "Green", "Blue"]


# ── B7. latency: catalogue snapshot, exact product names, batching, reply delay ──

async def test_catalogue_snapshot_is_reused_but_stock_and_reservations_are_always_fresh(monkeypatch):
    """The product / variant rows (987 products, 6,301 variants in production) are cached for a minute; stock is re-read each turn."""
    from app.services import catalogue_snapshot
    from app.services.agent_catalog import Catalogue

    catalogue_snapshot.invalidate()
    queries: list[str] = []
    state = {"stock": 5}

    class FakeResult:
        """Result of a fake query."""

        def __init__(self, rows):
            """Rows to return."""
            self._rows = rows

        def all(self):
            """All rows."""
            return self._rows

        def scalar_one(self):
            """The single value of an aggregate query (the stock maps come back as one jsonb document)."""
            return self._rows

    class FakeDB:
        """Answers the four loader queries with canned rows and records which ran."""

        async def execute(self, stmt):
            """Return rows by the table the statement reads."""
            text = str(stmt)
            queries.append("variants_static" if "product_variants.color" in text else "variants_stock" if "product_variants.stock" in text
                           else "products_static" if "products.name" in text else "products_stock" if "products.stock" in text else "reservations")
            if queries[-1] == "products_static":
                return FakeResult([(1, "KU1", "Kurti", 499.0, "K", "d", None, None, True)])
            if queries[-1] == "variants_static":
                return FakeResult([(10, 1, "Red", None, None, 499.0, None, None, None, False)])
            if queries[-1] == "variants_stock":
                return FakeResult({"10": state["stock"]})            # {variant_id: stock} as one jsonb document
            if queries[-1] == "products_stock":
                return FakeResult({"1": None})
            return FakeResult([])

    first = await Catalogue.load(FakeDB(), 7)
    assert first.get("KU1").available({"color": "Red"}) == 5
    state["stock"] = 2
    second = await Catalogue.load(FakeDB(), 7)
    assert second.get("KU1").available({"color": "Red"}) == 2                          # stock re-read
    assert queries.count("products_static") == 1 and queries.count("variants_static") == 1       # rows cached
    assert queries.count("variants_stock") == 2
    catalogue_snapshot.invalidate(7)
    await Catalogue.load(FakeDB(), 7)
    assert queries.count("products_static") == 2                                         # a catalogue write invalidates
    catalogue_snapshot.invalidate()


async def _noop_write(client_id=None, product=None):
    """A stand-in catalogue write."""
    return None


def test_catalogue_writes_invalidate_the_snapshot():
    """create / update / delete product and variant syncs drop the tenant's cached rows."""
    from app.services import catalogue_service, catalogue_snapshot

    for fn in ("create_product", "update_product", "delete_product", "sync_product_variants", "create_product_variants"):
        assert getattr(getattr(catalogue_service, fn), "__invalidates_snapshot__", False), fn
    catalogue_snapshot.put(3, ())
    asyncio.run(catalogue_service._invalidates_snapshot(_noop_write)(client_id=3))
    assert catalogue_snapshot.get(3) is None


def test_exact_product_name_is_a_fast_path_and_is_not_held_by_message_batching():
    """"A-Line Cotton Kurti" → show_product for ₹0; the batcher does not wait for more fragments of a bare greeting / gate / product name."""
    from app.services import agent_fast_paths as fp
    from app.services import message_batcher

    cat = _catalogue()
    from app.services.agent_cart import CartData

    got = fp.resolve(text="cotton   KURTI", button_id=None, cart=CartData(id=1, conversation_id=1, client_id=1), catalogue=cat, client=None)
    assert got is None or got.name == "name"          # "cotton kurti" is the exact name of the kurti in this fixture catalogue? (it is "Cotton Kurti")
    exact = fp.resolve(text="Cotton Kurti", button_id=None, cart=CartData(id=1, conversation_id=1, client_id=1), catalogue=cat, client=None)
    assert exact.name == "name" and exact.actions[0].sku == "KU1"
    conv = SimpleNamespace(current_stage="product_inquiry", interrupted_sku=None, summary_shown=False, ai_enabled=True)
    assert message_batcher.eligible(conv, "hello")                               # legacy behaviour unchanged
    assert not message_batcher.eligible(conv, "hello", skip_social=True)         # agent v3: nothing to wait for
    assert not message_batcher.eligible(conv, "thanks", skip_social=True)
    assert not message_batcher.eligible(conv, "images please", skip_social=True)  # ₹0 fast paths are not held either:
    assert not message_batcher.eligible(conv, "photo dikhao", skip_social=True)   #   photos of the product in focus,
    assert not message_batcher.eligible(conv, "is COD available?", skip_social=True)   # side questions answered from the shop's facts
    assert message_batcher.eligible(conv, "i want a red", skip_social=True)      # a real fragment still batches


async def test_agent_reply_delay_is_what_is_left_of_the_target_after_the_work(monkeypatch):
    """The human-like pause is a target total (default 0.3–0.8 s): a turn that already took longer waits 0."""
    from app.services import agent_v3

    assert agent_v3.remaining_delay(target=0.6, elapsed=0.1, credit=0.0) == pytest.approx(0.5)
    assert agent_v3.remaining_delay(target=0.6, elapsed=2.0, credit=0.0) == 0.0
    assert agent_v3.remaining_delay(target=0.6, elapsed=0.1, credit=3.5) == 0.0      # already waited in the batcher


def test_agent_v3_uses_its_own_shorter_batching_windows():
    """Tenants on agent v3 wait 1.5 / 2.5 / 4.0 s (sentence / short fragment / unfinished) instead of 3.5 / 5 / 7 — tunable."""
    f = Settings.model_fields
    assert (f["agent_batch_window_seconds"].default, f["agent_batch_short_window_seconds"].default,
            f["agent_batch_extended_window_seconds"].default) == (1.5, 2.5, 4.0)
    assert (f["message_batch_window_seconds"].default, f["message_batch_short_window_seconds"].default,
            f["message_batch_extended_window_seconds"].default) == (3.5, 5.0, 7.0)
