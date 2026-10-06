"""Tests for LLM model config, failure fallbacks/health, robust parsing, vision switch, and the UPI QR."""

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.services import conversation_flow, gemini_service, llm_client, llm_health, tool_router, vision_service
from app.services.language_templates import TEMPLATES, get_template


@pytest.fixture(autouse=True)
def _fresh_llm_state(mock_settings):
    """Isolate breaker/counters per test and make sure settings are mocked."""
    llm_health.reset_state()
    yield
    llm_health.reset_state()


def _resp(content, usage=True, **message_extras):
    """Fake chat.completions response (extra kwargs become message attrs, e.g. reasoning)."""
    msg = SimpleNamespace(content=content, **message_extras)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2) if usage else None,
    )


def _fail_chat(monkeypatch, exc=None):
    """Make llm_client.chat raise (default: a 404 model_not_found style error)."""
    async def _boom(*a, **k):
        raise exc or RuntimeError("Error code: 404 - model_not_found")

    monkeypatch.setattr(llm_client, "chat", _boom)


# ── config ───────────────────────────────────────────────────────────────────

def test_config_model_defaults():
    """Defaults: reply=gpt-oss-20b, classifier set, vision/stt set, no fallbacks."""
    s = Settings(_env_file=None, database_url="postgresql://u:p@h/d", meta_app_secret="x",
                 meta_verify_token="x", whatsapp_access_token="x", whatsapp_phone_number_id="1")
    assert s.llm_model_reply == "openai/gpt-oss-20b"
    assert s.llm_model_classifier and s.llm_model_vision and s.llm_model_stt
    assert s.llm_model_reply_fallbacks == ""
    assert s.groq_base_url == "https://api.groq.com/openai/v1"


def test_config_model_env_overrides_and_ignores_legacy(monkeypatch):
    """LLM_MODEL_* env vars override defaults; legacy CLASSIFY_MODEL/REPLY_MODEL are ignored."""
    monkeypatch.setenv("LLM_MODEL_REPLY", "vendor/reply-x")
    monkeypatch.setenv("LLM_MODEL_CLASSIFIER", "vendor/cls-y")
    monkeypatch.setenv("LLM_MODEL_VISION", "")
    monkeypatch.setenv("REPLY_MODEL", "stale-legacy-model")
    monkeypatch.setenv("CLASSIFY_MODEL", "stale-legacy-model")
    s = Settings(_env_file=None, database_url="postgresql://u:p@h/d", meta_app_secret="x",
                 meta_verify_token="x", whatsapp_access_token="x", whatsapp_phone_number_id="1")
    assert (s.llm_model_reply, s.llm_model_classifier, s.llm_model_vision) == ("vendor/reply-x", "vendor/cls-y", "")


def test_no_hardcoded_model_names_outside_config():
    """CI guard: model ids live only in config.py (and the cost_log pricing table)."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(r"""["'](?:[\w.-]+/)?(?:llama|gpt-oss|qwen|whisper|mixtral|gemma)[\w./-]*["']""", re.I)
    allowed = {"config.py", "cost_log.py"}
    offenders = [
        f"{p.name}:{i}" for p in app_dir.rglob("*.py") if p.name not in allowed
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []


# ── llm_client ───────────────────────────────────────────────────────────────

def test_build_request_adds_reasoning_headroom_only_for_reasoning_models():
    """gpt-oss gets reasoning_effort + extra max_tokens; other models are untouched."""
    r = llm_client.build_request("openai/gpt-oss-20b", [], max_tokens=5, temperature=0)
    assert r["max_tokens"] == 5 + 512 and r["extra_body"] == {"reasoning_effort": "low"}
    plain = llm_client.build_request("some/other-model", [], max_tokens=5, response_format={"type": "json_object"})
    assert plain["max_tokens"] == 5 and "extra_body" not in plain and plain["response_format"]


def test_is_reasoning_model_uses_config_prefixes():
    """Prefix match comes from LLM_REASONING_MODEL_PREFIXES."""
    assert llm_client.is_reasoning_model("openai/gpt-oss-120b") is True
    assert llm_client.is_reasoning_model("meta/llama") is False


def test_final_text_reads_only_content():
    """reasoning fields are ignored and <think> blocks stripped; missing content → ''."""
    assert llm_client.final_text(_resp("YES", reasoning="long hidden thoughts", reasoning_content="more")) == "YES"
    assert llm_client.final_text(_resp("<think>hmm</think>NO")) == "NO"
    assert llm_client.final_text(_resp(None, reasoning="only reasoning")) == ""
    assert llm_client.final_text(SimpleNamespace(choices=[])) == ""


def test_strip_reasoning_removes_think_blocks():
    """Inline <think> blocks (multi-line, any case) are removed."""
    assert llm_client.strip_reasoning("<THINK>a\nb</THINK> hi ") == "hi"


def test_parse_json_object_is_robust():
    """Fences, surrounding prose and think blocks are tolerated; non-objects rejected."""
    assert llm_client.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_client.parse_json_object('Sure! Here you go: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}
    assert llm_client.parse_json_object('<think>{"x":0}</think>{"a": 1}') == {"a": 1}
    assert llm_client.parse_json_object("[1, 2]") is None
    assert llm_client.parse_json_object("not json") is None
    assert llm_client.parse_json_object(None) is None


async def test_chat_calls_client_and_closes_breaker():
    """chat() builds the request, returns the response and records success."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_resp("ok"))
    llm_health.trip("old")
    llm_health._last_probe_at = -1e9  # probe due
    resp = await llm_client.chat("m", [{"role": "user", "content": "x"}], max_tokens=5, client=client)
    assert llm_client.final_text(resp) == "ok"
    assert llm_health.is_llm_available() is True


async def test_chat_short_circuits_when_breaker_open():
    """While the breaker is open (no probe due) no network call is made."""
    llm_health.trip("down")
    llm_health.should_attempt_llm()  # consume the probe slot
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    with pytest.raises(llm_health.LLMUnavailableError):
        await llm_client.chat("m", [], max_tokens=5, client=client)
    client.chat.completions.create.assert_not_called()


async def test_chat_json_retries_once_on_invalid_json(monkeypatch):
    """First response is junk → one repair retry → valid dict returned."""
    seq = iter([_resp("sorry, no json"), _resp('```json\n{"items": [1]}\n```')])
    monkeypatch.setattr(llm_client, "chat", AsyncMock(side_effect=lambda *a, **k: next(seq)))
    seen = []
    out = await llm_client.chat_json("m", [{"role": "user", "content": "x"}], max_tokens=10,
                                     validate=lambda d: "items" in d, on_response=seen.append)
    assert out == {"items": [1]} and len(seen) == 2


async def test_chat_json_returns_none_after_retry_budget(monkeypatch):
    """Two invalid responses → None (never raises)."""
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp("nope")))
    assert await llm_client.chat_json("m", [], max_tokens=10) is None


# ── llm_health ───────────────────────────────────────────────────────────────

def test_record_failure_counts_and_logs_error(caplog):
    """Each failure is counted and logged at ERROR with the running count."""
    with caplog.at_level(logging.ERROR):
        llm_health.record_failure("classify_buy_intent", RuntimeError("404 model_not_found"))
        llm_health.record_failure("is_off_topic_message", "bad output")
    assert llm_health.failures_last_hour() == 2
    assert "LLM_FAILURE kind=classify_buy_intent failures_last_hour=1" in caplog.text


def test_record_failure_ignores_breaker_skips():
    """LLMUnavailableError (breaker skip) doesn't inflate the failure count."""
    llm_health.record_failure("x", llm_health.LLMUnavailableError("down"))
    assert llm_health.failures_last_hour() == 0


def test_failures_last_hour_expires_old_entries(monkeypatch):
    """Entries older than an hour drop out of the count."""
    llm_health.record_failure("x", "e")
    llm_health._failure_times[0] -= 3601
    assert llm_health.failures_last_hour() == 0


def test_breaker_trips_after_threshold_but_not_on_429():
    """3 consecutive hard failures open the breaker; 429s and successes don't/reset."""
    for _ in range(5):
        llm_health.note_call_result(False, RuntimeError("Error code: 429 rate limit"))
    assert llm_health.is_llm_available() is True
    llm_health.note_call_result(False, RuntimeError("404"))
    llm_health.note_call_result(True)
    llm_health.note_call_result(False, RuntimeError("404"))
    llm_health.note_call_result(False, RuntimeError("404"))
    assert llm_health.is_llm_available() is True
    llm_health.note_call_result(False, RuntimeError("404"))
    assert llm_health.is_llm_available() is False
    assert "consecutive" in llm_health.unavailable_reason()


def test_is_rate_limited_detects_429():
    """429 by status_code attr or message."""
    assert llm_health.is_rate_limited(SimpleNamespace(status_code=429)) is True
    assert llm_health.is_rate_limited(RuntimeError("Error code: 429")) is True
    assert llm_health.is_rate_limited(RuntimeError("404")) is False


def test_is_llm_error_classifies_exceptions():
    """Breaker skips and openai errors are LLM errors; others aren't."""
    import openai

    assert llm_health.is_llm_error(llm_health.LLMUnavailableError("x")) is True
    assert llm_health.is_llm_error(openai.OpenAIError("x")) is True
    assert llm_health.is_llm_error(ValueError("x")) is False


def test_trip_is_llm_available_and_unavailable_reason():
    """trip() opens the breaker and records the reason."""
    assert llm_health.is_llm_available() is True and llm_health.unavailable_reason() is None
    llm_health.trip("model missing")
    assert llm_health.is_llm_available() is False and llm_health.unavailable_reason() == "model missing"


def test_should_attempt_llm_probes_periodically():
    """When open, only one probe per interval is allowed."""
    llm_health.trip("down")
    llm_health._last_probe_at = -1e9
    assert llm_health.should_attempt_llm() is True
    assert llm_health.should_attempt_llm() is False


def test_reset_state_clears_everything():
    """reset_state wipes counters and flags."""
    llm_health.record_failure("x", "e")
    llm_health.trip("down")
    llm_health.reset_state()
    assert llm_health.failures_last_hour() == 0 and llm_health.is_llm_available()


def test_parse_csv_and_configured_models(mock_settings):
    """CSV parsing trims/skips blanks; configured_models lists roles incl. fallbacks, omits empty vision."""
    assert llm_health.parse_csv(" a, ,b ,") == ["a", "b"]
    mock_settings.llm_model_reply_fallbacks = "fb1,fb2"
    models = llm_health.configured_models(mock_settings)
    assert models["reply_fallback_2"] == "fb2" and "vision" in models
    mock_settings.llm_model_vision = ""
    assert "vision" not in llm_health.configured_models(mock_settings)


def test_find_missing_models():
    """Returns only the roles whose model id isn't in the available list."""
    assert llm_health.find_missing_models({"reply": "a", "classifier": "b"}, ["a"]) == {"classifier": "b"}


async def test_fetch_available_models_parses_ids(monkeypatch):
    """GET /models → sorted ids, Bearer auth, trailing slash tolerated."""
    calls = {}

    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            calls["url"], calls["auth"] = url, headers["Authorization"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": [{"id": "b"}, {"id": "a"}]})

    monkeypatch.setattr(llm_health.httpx, "AsyncClient", _C)
    assert await llm_health.fetch_available_models("KEY", "https://x/v1/") == ["a", "b"]
    assert calls == {"url": "https://x/v1/models", "auth": "Bearer KEY"}


async def test_verify_models_missing_reply_trips_and_logs_critical(monkeypatch, mock_settings, caplog):
    """Missing reply model → CRITICAL, breaker open; vision missing → vision disabled. Never raises."""
    mock_settings.llm_model_reply = "absent/reply-model"
    monkeypatch.setattr(llm_health, "fetch_available_models",
                        AsyncMock(return_value=[mock_settings.llm_model_classifier, mock_settings.llm_model_stt]))
    with caplog.at_level(logging.CRITICAL):
        await llm_health.verify_models(mock_settings)
    assert llm_health.is_llm_available() is False
    assert llm_health.is_vision_enabled() is False
    assert "NOT FOUND" in caplog.text


async def test_verify_models_all_present_and_unreachable(monkeypatch, mock_settings):
    """All present → healthy; /models unreachable → inconclusive, nothing flagged."""
    allm = list(llm_health.configured_models(mock_settings).values())
    monkeypatch.setattr(llm_health, "fetch_available_models", AsyncMock(return_value=allm))
    await llm_health.verify_models(mock_settings)
    assert llm_health.is_llm_available() and llm_health.is_vision_enabled()
    monkeypatch.setattr(llm_health, "fetch_available_models", AsyncMock(side_effect=RuntimeError("net")))
    await llm_health.verify_models(mock_settings)
    assert llm_health.is_llm_available()


async def test_verify_models_without_api_key_trips(mock_settings):
    """No GROQ_API_KEY → breaker open, no network call."""
    mock_settings.groq_api_key = ""
    await llm_health.verify_models(mock_settings)
    assert llm_health.is_llm_available() is False


def test_vision_flags(mock_settings):
    """Vision is off when the model env is empty; warn_vision_disabled_once logs once."""
    assert llm_health.is_vision_enabled() is True
    mock_settings.llm_model_vision = ""
    assert llm_health.is_vision_enabled() is False
    assert llm_health.vision_disabled_reason() == "LLM_MODEL_VISION is empty"
    llm_health.warn_vision_disabled_once()
    assert llm_health._vision_warned is True


def test_health_summary_and_health_endpoint(client):
    """/health exposes llm_failures_last_hour and 'LLM unavailable' when the breaker is open."""
    body = client.get("/health").json()
    assert body["llm_failures_last_hour"] == 0 and "llm" not in body["checks"]
    llm_health.record_failure("x", "e")
    llm_health.trip("configured model missing")
    body = client.get("/health").json()
    assert body["checks"]["llm"].startswith("LLM unavailable")
    assert body["llm_failures_last_hour"] == 1 and body["status"] == "degraded"
    assert llm_health.health_summary()["available"] is False


# ── classifier fallbacks (safe default + ERROR + counter) ────────────────────

async def test_classify_buy_intent_failure_defaults_false_and_counts(monkeypatch, caplog):
    """LLM 404 → False, ERROR logged, counter incremented."""
    _fail_chat(monkeypatch)
    with caplog.at_level(logging.ERROR):
        assert await conversation_flow.classify_buy_intent("lena hai bhai", "Saree") is False
    assert llm_health.failures_last_hour() == 1
    assert "kind=classify_buy_intent" in caplog.text


async def test_classify_buy_intent_empty_output_is_a_failure(monkeypatch):
    """A reasoning model returning empty content is counted, not silently treated as NO."""
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp("", reasoning="…")))
    assert await conversation_flow.classify_buy_intent("lena hai bhai", "Saree") is False
    assert llm_health.failures_last_hour() == 1


async def test_classify_buy_intent_success_path(monkeypatch):
    """Normal YES → True with no failure recorded."""
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp("YES")))
    assert await conversation_flow.classify_buy_intent("lena hai bhai", "Saree") is True
    assert llm_health.failures_last_hour() == 0


async def test_is_off_topic_message_failure_defaults_false_and_counts(monkeypatch):
    """LLM error → False + counter; YES → True."""
    _fail_chat(monkeypatch)
    assert await conversation_flow.is_off_topic_message("what is flutter framework", "greeting") is False
    assert llm_health.failures_last_hour() == 1
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp("YES")))
    assert await conversation_flow.is_off_topic_message("what is flutter framework", "greeting") is True


async def test_classify_user_intent_failure_defaults_answer_and_counts(monkeypatch):
    """LLM error → ANSWER + counter; valid label → that label."""
    _fail_chat(monkeypatch)
    out = await conversation_flow.classify_user_intent("hmm maybe later", next_slot="color", pinned_product_name="Saree")
    assert out == {"intent": "ANSWER", "entities": {}} and llm_health.failures_last_hour() == 1
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp("OFF_TOPIC")))
    out = await conversation_flow.classify_user_intent("who won the match yesterday", next_slot="color", pinned_product_name="Saree")
    assert out["intent"] == "OFF_TOPIC"


async def test_extract_cart_breakdown_failure_and_success(monkeypatch):
    """Failure → None + counter; fenced JSON with valid items → parsed."""
    vi = {"available_colors": ["red"], "available_sizes": ["M"]}
    _fail_chat(monkeypatch)
    assert await conversation_flow.extract_cart_breakdown("40 red M", vi, "Saree", 40) is None
    assert llm_health.failures_last_hour() == 1
    good = '```json\n{"items": [{"qty": 40, "color": "red", "size": "M"}], "inferred_split": false}\n```'
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp(good)))
    out = await conversation_flow.extract_cart_breakdown("40 red M", vi, "Saree", 40)
    assert out["items"][0]["qty"] == 40


async def test_tool_router_failure_returns_empty_and_counts(monkeypatch):
    """Router LLM error → [] + counter; fenced JSON → validated proposals."""
    _fail_chat(monkeypatch)
    assert await tool_router.call_tool_router("red", "order_collection", "color", {}, [], "Saree") == []
    assert llm_health.failures_last_hour() == 1
    out = '```json\n{"calls": [{"tool": "set_slot", "args": {"field": "color", "value": "red"}}]}\n```'
    monkeypatch.setattr(llm_client, "chat", AsyncMock(return_value=_resp(out)))
    got = await tool_router.call_tool_router("red", "order_collection", "color", {}, [], "Saree")
    assert got == [{"tool": "set_slot", "args": {"field": "color", "value": "red"}}]


# ── reply path ───────────────────────────────────────────────────────────────

def test_groq_models_dedupes_and_defaults_fallback_to_classifier(mock_settings):
    """Reply chain = reply model + configured fallbacks (default: classifier), deduped."""
    mock_settings.llm_model_reply, mock_settings.llm_model_classifier = "r", "c"
    assert gemini_service._groq_models() == ["r", "c"]
    mock_settings.llm_model_reply_fallbacks = "x, r, y"
    assert gemini_service._groq_models() == ["r", "x", "y"]


async def test_generate_reply_all_models_404_raises_llm_unavailable(monkeypatch):
    """Every model 404s → LLMUnavailableError (pipeline then sends the rephrase template)."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("Error code: 404 - model_not_found"))
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    with pytest.raises(llm_health.LLMUnavailableError):
        await gemini_service.generate_reply("hi")
    assert llm_health.failures_last_hour() >= 1


async def test_generate_reply_falls_through_404_to_next_model(monkeypatch, mock_settings):
    """First model 404, fallback model answers; reasoning field is not returned."""
    mock_settings.llm_model_reply, mock_settings.llm_model_classifier = "missing/m", "good/m"
    calls = []

    async def _create(**kw):
        calls.append(kw["model"])
        if kw["model"] == "missing/m":
            raise RuntimeError("Error code: 404 - model_not_found")
        return _resp("Namaste!", reasoning="secret chain of thought")

    client = MagicMock()
    client.chat.completions.create = _create
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    assert await gemini_service.generate_reply("hi") == "Namaste!"
    assert calls == ["missing/m", "good/m"]


async def test_generate_reply_all_429_returns_busy_fallback(monkeypatch):
    """Pure rate limiting keeps the old behaviour: friendly busy reply, no exception."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("Error code: 429"))
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    assert await gemini_service.generate_reply("hi") == gemini_service._BUSY_FALLBACK_REPLY


@pytest.mark.parametrize("lang", sorted(TEMPLATES))
def test_rephrase_template_exists_in_every_language(lang):
    """The deterministic 'could you rephrase' reply exists for all template languages."""
    text = get_template(lang, "llm_unavailable_rephrase")
    assert text and "order status" in text


async def test_analyze_product_image_disabled_returns_none(mock_settings, monkeypatch):
    """Empty LLM_MODEL_VISION → None without making any LLM call."""
    mock_settings.llm_model_vision = ""
    monkeypatch.setattr(llm_client, "llm_call", AsyncMock(side_effect=AssertionError("must not be called")))
    assert await vision_service.analyze_product_image(b"img", "catalogue") is None


# ── check_llm_models script ──────────────────────────────────────────────────

def _script():
    """Import scripts/check_llm_models.py as a module."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "check_llm_models.py"
    spec = importlib.util.spec_from_file_location("check_llm_models", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_load_config_env_and_defaults():
    """Env wins; otherwise Settings defaults; no DB settings required."""
    mod = _script()
    key, cfg = mod.load_config({"GROQ_API_KEY": "k", "LLM_MODEL_REPLY": "z/z"})
    assert key == "k" and cfg.llm_model_reply == "z/z" and cfg.llm_model_classifier


async def test_script_run_exit_codes():
    """0 = all present, 1 = some missing, 2 = no key / unreachable."""
    mod = _script()
    _, cfg = mod.load_config({})
    every = list(llm_health.configured_models(cfg).values())
    lines = []
    assert await mod.run("k", cfg, fetch=AsyncMock(return_value=every), out=lines.append) == 0
    assert await mod.run("k", cfg, fetch=AsyncMock(return_value=[m for m in every if m != cfg.llm_model_stt]), out=lines.append) == 1
    assert any("MISSING" in ln for ln in lines)
    assert await mod.run("k", cfg, fetch=AsyncMock(side_effect=RuntimeError("x")), out=lines.append) == 2
    assert await mod.run("", cfg, out=lines.append) == 2


# ── UPI QR ───────────────────────────────────────────────────────────────────

def test_make_qr_png_generates_valid_png():
    """segno is installed and produces a real PNG for a UPI URI."""
    from app.services import payment_verification_service as pvs

    uri = pvs.build_upi_uri("shop@okaxis", "Riya Sarees", 2450, 7)
    png = pvs.make_qr_png(uri)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 200


def _order_and_client():
    """Minimal order/client stand-ins for build_payment_instruction."""
    order = SimpleNamespace(id=7, order_number="ORD7", total_amount=2450, line_items=[], product_name="Saree",
                            quantity=1, variant_color=None, variant_size=None)
    client = SimpleNamespace(id=1, upi_id="shop@okaxis", upi_display_name="Riya", payment_instructions=None, upi_qr_url=None)
    return order, client


async def test_build_payment_instruction_attaches_qr(monkeypatch):
    """A generated QR is stored and its URL returned alongside the text."""
    from app.services import payment_verification_service as pvs

    stored = {}

    async def _store(client_id, data, mime, folder=None):
        stored["png"] = data
        return "/uploads/qr.png"

    monkeypatch.setattr(pvs.media_service, "store_media", _store)
    monkeypatch.setattr(pvs.media_service, "absolute_url", lambda u: f"https://api.test{u}")
    order, client = _order_and_client()
    instr = await pvs.build_payment_instruction(None, order, client, None)
    assert instr.qr_url == "https://api.test/uploads/qr.png"
    assert stored["png"][:4] == b"\x89PNG" and "shop@okaxis" in instr.text


async def test_qr_failure_still_sends_text_and_warns_once(monkeypatch, caplog):
    """If QR generation breaks the text message is still produced; WARNING only the first time."""
    from app.services import payment_verification_service as pvs

    monkeypatch.setattr(pvs, "_qr_failure_warned", False)
    monkeypatch.setattr(pvs, "make_qr_png", MagicMock(side_effect=ModuleNotFoundError("No module named 'segno'")))
    order, client = _order_and_client()
    with caplog.at_level(logging.DEBUG, logger=pvs.logger.name):
        first = await pvs.build_payment_instruction(None, order, client, None)
        await pvs.build_payment_instruction(None, order, client, None)
    assert first.qr_url is None and "shop@okaxis" in first.text and first.closing in first.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "QR generation failed" in r.getMessage()]
    assert len(warnings) == 1


def test_warn_qr_failure_once_helper(monkeypatch, caplog):
    """The helper logs WARNING once, then DEBUG."""
    from app.services import payment_verification_service as pvs

    monkeypatch.setattr(pvs, "_qr_failure_warned", False)
    with caplog.at_level(logging.DEBUG, logger=pvs.logger.name):
        pvs._warn_qr_failure_once(1, RuntimeError("a"))
        pvs._warn_qr_failure_once(2, RuntimeError("b"))
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.DEBUG]
