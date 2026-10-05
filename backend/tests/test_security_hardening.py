"""Tests for the security-hardening changes: log redaction, SECRET_KEY guard, IG kill-switch, shop URL."""

import logging

import pytest

from app import log_redaction, main
from app.config import DEFAULT_SECRET_KEY, Settings, ensure_secret_key_is_safe
from app.services import channel_status, outbound


@pytest.fixture(autouse=True)
def _reset_ig_flag():
    """Make sure no test leaks the process-wide Instagram flag."""
    channel_status.enable_instagram()
    yield
    channel_status.enable_instagram()


def _settings(**kw) -> Settings:
    """Build a Settings with the minimum required fields plus overrides."""
    base = dict(
        database_url="postgresql://u:p@localhost/db", meta_app_secret="x", meta_verify_token="x",
        whatsapp_access_token="x", whatsapp_phone_number_id="1",
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


# ── log_redaction ────────────────────────────────────────────────────────────

def test_redact_query_secrets_masks_token_values():
    """token=/access_token= values are masked; other params and paths are untouched."""
    out = log_redaction.redact_query_secrets("GET /events/stream?token=eyJabc.def&x=1&access_token=AAA HTTP/1.1")
    assert "eyJabc" not in out and "AAA" not in out
    assert "/events/stream?token=[REDACTED]&x=1&access_token=[REDACTED]" in out
    assert log_redaction.redact_query_secrets("/events/poll?since=2026-01-01") == "/events/poll?since=2026-01-01"


def test_access_log_filter_redacts_uvicorn_args():
    """The uvicorn access record's request-line arg is redacted in place and the record is kept."""
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/events/stream?token=SECRETJWT", "1.1", 200), None,
    )
    assert log_redaction.AccessLogRedactionFilter().filter(rec) is True
    assert "SECRETJWT" not in rec.getMessage()
    assert "token=[REDACTED]" in rec.getMessage()


def test_configure_log_hygiene_sets_levels_and_is_idempotent():
    """httpx/httpcore go to WARNING and the filter is attached exactly once."""
    log_redaction.configure_log_hygiene()
    log_redaction.configure_log_hygiene()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, log_redaction.AccessLogRedactionFilter)]
    assert len(filters) == 1


# ── config ───────────────────────────────────────────────────────────────────

def test_ensure_secret_key_is_safe_refuses_default_in_production():
    """Default key + non-development environment raises; dev or a custom key passes."""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        ensure_secret_key_is_safe(_settings(environment="production", secret_key=DEFAULT_SECRET_KEY))
    with pytest.raises(RuntimeError):
        ensure_secret_key_is_safe(_settings(environment="staging", secret_key=DEFAULT_SECRET_KEY))
    ensure_secret_key_is_safe(_settings(environment="development", secret_key=DEFAULT_SECRET_KEY))
    ensure_secret_key_is_safe(_settings(environment="production", secret_key="a-real-random-secret"))


def test_public_shop_base_url_env_and_legacy_alias(monkeypatch):
    """PUBLIC_SHOP_BASE_URL wins; legacy CATALOGUE_BASE_URL still works."""
    monkeypatch.setenv("CATALOGUE_BASE_URL", "https://legacy.example/shop")
    assert _settings().public_shop_base_url == "https://legacy.example/shop"
    monkeypatch.setenv("PUBLIC_SHOP_BASE_URL", "https://new.example/shop")
    assert _settings().public_shop_base_url == "https://new.example/shop"


# ── channel_status ───────────────────────────────────────────────────────────

def test_disable_instagram_sets_flag_and_reason():
    """disable_instagram flips the flag and records why."""
    channel_status.disable_instagram("bad token")
    assert channel_status.is_instagram_disabled() is True


def test_enable_instagram_clears_flag():
    """enable_instagram resets the flag and reason."""
    channel_status.disable_instagram("bad token")
    channel_status.enable_instagram()
    assert channel_status.is_instagram_disabled() is False
    assert channel_status.instagram_disabled_reason() is None


def test_is_instagram_disabled_default_false():
    """Instagram is enabled by default."""
    assert channel_status.is_instagram_disabled() is False


def test_instagram_disabled_reason_returns_reason():
    """The stored reason is returned verbatim."""
    channel_status.disable_instagram("token invalid at startup")
    assert channel_status.instagram_disabled_reason() == "token invalid at startup"


def test_warn_instagram_send_skipped_is_rate_limited(caplog):
    """Many skipped sends in a row produce a single warning."""
    channel_status.disable_instagram("bad token")
    with caplog.at_level(logging.WARNING, logger=channel_status.logger.name):
        for _ in range(5):
            channel_status.warn_instagram_send_skipped()
    assert len([r for r in caplog.records if "Instagram send skipped" in r.getMessage()]) == 1


# ── outbound IG guard ────────────────────────────────────────────────────────

def test_ig_channel_disabled_helper():
    """Helper mirrors the flag."""
    assert outbound._ig_channel_disabled() is False
    channel_status.disable_instagram("bad token")
    assert outbound._ig_channel_disabled() is True


@pytest.mark.parametrize("fn,args,expected", [
    (outbound.ig_send_dm, ("ig", "u", "hi"), None),
    (outbound.ig_send_quick_replies, ("ig", "u", "hi", []), False),
    (outbound.ig_send_image, ("ig", "u", "https://x/y.jpg"), None),
    (outbound.ig_send_generic_template, ("ig", "u", []), False),
    (outbound.ig_send_private_reply, ("ig", "c1", "hi"), None),
    (outbound.ig_reply_to_comment, ("ig", "c1", "hi"), {}),
])
async def test_ig_sends_skipped_when_disabled(monkeypatch, fn, args, expected):
    """With the flag set, every IG send returns its 'suppressed' value and never hits the transport."""
    from app.services import instagram_service

    def _boom(*a, **k):
        raise AssertionError("transport must not be called")

    for name in dir(instagram_service):
        if name.startswith("_raw_"):
            monkeypatch.setattr(instagram_service, name, _boom)
    kwargs = {} if fn is outbound.ig_reply_to_comment else (
        {"kind": outbound.MessageKind.PIPELINE_REPLY} if fn is not outbound.ig_send_private_reply else {}
    )
    channel_status.disable_instagram("bad token")
    assert await fn(*args, **kwargs) == expected


# ── main: token checks ───────────────────────────────────────────────────────

class _FakeResp:
    """Minimal httpx response stand-in."""

    def __init__(self, body):
        self._body = body

    def json(self):
        """Return the canned body."""
        return self._body


def _patch_httpx(monkeypatch, body=None, exc=None):
    """Replace httpx.AsyncClient with a fake returning `body` or raising `exc`."""
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if exc:
                raise exc
            return _FakeResp(body)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_debug_token_verdicts(monkeypatch):
    """valid / invalid (is_valid false or error 190) / unknown (other error or network failure)."""
    _patch_httpx(monkeypatch, {"data": {"is_valid": True, "type": "SYSTEM_USER"}})
    assert (await main._debug_token("tok", "X"))[0] == "valid"
    _patch_httpx(monkeypatch, {"data": {"is_valid": False}})
    assert (await main._debug_token("tok", "X"))[0] == "invalid"
    _patch_httpx(monkeypatch, {"error": {"code": 190, "message": "Invalid OAuth access token"}})
    assert (await main._debug_token("tok", "X"))[0] == "invalid"
    _patch_httpx(monkeypatch, {"error": {"code": 4, "message": "rate limited"}})
    assert (await main._debug_token("tok", "X"))[0] == "unknown"
    _patch_httpx(monkeypatch, exc=RuntimeError("boom https://graph.facebook.com/debug_token?access_token=SECRETTOKEN"))
    assert (await main._debug_token("SECRETTOKEN", "X"))[0] == "unknown"


async def test_debug_token_failure_log_has_no_token_or_url(monkeypatch, caplog):
    """A transport error logs only the exception class — never the message/URL/token."""
    _patch_httpx(monkeypatch, exc=RuntimeError("https://graph.facebook.com/debug_token?access_token=SECRETTOKEN"))
    with caplog.at_level(logging.DEBUG):
        await main._debug_token("SECRETTOKEN", "Instagram")
    assert "SECRETTOKEN" not in caplog.text and "graph.facebook.com" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_log_token_status_logs_only_allowed_fields(caplog):
    """Valid-token log carries type/expiry/scopes and no app name, error text or token."""
    data = {"type": "SYSTEM_USER", "expires_at": 0, "scopes": ["instagram_basic"],
            "application": "MyApp", "access_token": "SECRETTOKEN"}
    with caplog.at_level(logging.INFO):
        main._log_token_status("Instagram", "valid", data)
    assert "type=SYSTEM_USER" in caplog.text and "instagram_basic" in caplog.text and "expiry=NEVER" in caplog.text
    assert "MyApp" not in caplog.text and "SECRETTOKEN" not in caplog.text


async def test_check_instagram_token_invalid_disables_channel(monkeypatch):
    """An invalid IG token at startup disables the channel without raising."""
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(instagram_access_token="real-looking-token"))
    _patch_httpx(monkeypatch, {"error": {"code": 190}})
    await main._check_instagram_token()
    assert channel_status.is_instagram_disabled() is True


async def test_check_instagram_token_inconclusive_keeps_channel(monkeypatch):
    """A network failure or placeholder token must not disable Instagram."""
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(instagram_access_token="real-looking-token"))
    _patch_httpx(monkeypatch, exc=RuntimeError("down"))
    await main._check_instagram_token()
    assert channel_status.is_instagram_disabled() is False


async def test_check_whatsapp_token_skips_placeholder(monkeypatch):
    """Placeholder WhatsApp tokens skip the Meta call entirely."""
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(whatsapp_access_token="test_token"))
    _patch_httpx(monkeypatch, exc=AssertionError("must not be called"))
    await main._check_whatsapp_token()


def test_health_reports_instagram_disconnected(client):
    """/health shows 'Instagram disconnected' (and degraded) only when the flag is set."""
    assert "instagram" not in client.get("/health").json()["checks"]
    channel_status.disable_instagram("bad token")
    body = client.get("/health").json()
    assert body["checks"]["instagram"] == "Instagram disconnected"
    assert body["status"] == "degraded"
