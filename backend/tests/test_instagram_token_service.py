"""Per-client Instagram token verification + refresh (graph.instagram.com), startup check, scheduler job."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import scheduler
from app.services import channel_status, instagram_token_service as svc


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured once so repeated patches never stack


def _patch_http(monkeypatch, handler):
    """Route the service's httpx.AsyncClient through a MockTransport; handler(request) -> Response."""
    real = _REAL_ASYNC_CLIENT

    def _factory(*a, **kw):
        """AsyncClient bound to the mock transport."""
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(svc.httpx, "AsyncClient", _factory)


def _client(cid, token):
    """Stand-in Client row."""
    return SimpleNamespace(id=cid, instagram_access_token=token)


def _db_with(clients):
    """Fake AsyncSession whose select() returns `clients`."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: clients)))
    db.commit = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _reset_status():
    """Isolate the module-level per-client verdicts."""
    channel_status.clear_instagram_client_status()
    yield
    channel_status.clear_instagram_client_status()


async def test_check_token_valid_uses_graph_instagram_me(monkeypatch):
    """HTTP 200 from graph.instagram.com/me → 'valid'."""
    seen = {}

    def handler(req):
        """Capture the request, answer 200."""
        seen["host"], seen["path"] = req.url.host, req.url.path
        return httpx.Response(200, json={"user_id": "1"})

    _patch_http(monkeypatch, handler)
    assert await svc.check_token("tok") == "valid"
    assert seen == {"host": "graph.instagram.com", "path": "/me"}


async def test_check_token_invalid_on_oauth_190(monkeypatch):
    """Graph error code 190 → 'invalid'."""
    _patch_http(monkeypatch, lambda r: httpx.Response(400, json={"error": {"code": 190, "message": "expired"}}))
    assert await svc.check_token("tok") == "invalid"


async def test_check_token_inconclusive_is_unknown_not_invalid(monkeypatch):
    """A 5xx or a network error must not be read as an invalid token."""
    _patch_http(monkeypatch, lambda r: httpx.Response(500, json={}))
    assert await svc.check_token("tok") == "unknown"

    def boom(req):
        """Simulate a network failure."""
        raise httpx.ConnectError("down")

    _patch_http(monkeypatch, boom)
    assert await svc.check_token("tok") == "unknown"


async def test_verify_all_clients_logs_per_client_without_tokens(monkeypatch, caplog):
    """One log line per client_id with VALID/INVALID; token values never appear; /health state updated."""
    secret_ok, secret_bad = "SECRET_OK_TOKEN", "SECRET_BAD_TOKEN"

    def handler(req):
        """Valid for one client's token, 190 for the other."""
        return httpx.Response(200, json={}) if req.url.params["access_token"] == secret_ok else httpx.Response(
            400, json={"error": {"code": 190}})

    _patch_http(monkeypatch, handler)
    db = _db_with([_client(1, secret_ok), _client(2, secret_bad), _client(3, None), _client(4, "test_token")])
    with caplog.at_level(logging.INFO):
        out = await svc.verify_all_clients(db)
    assert out == {1: "valid", 2: "invalid"}  # no-token and placeholder clients skipped
    assert "client_id=1: VALID" in caplog.text and "client_id=2: INVALID" in caplog.text
    assert secret_ok not in caplog.text and secret_bad not in caplog.text
    assert "System User" not in caplog.text
    assert channel_status.instagram_invalid_clients() == [2]


async def test_refresh_token_outcomes(monkeypatch):
    """200 → refreshed with new token; 190 → invalid; anything else → error."""
    _patch_http(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000}))
    assert await svc.refresh_token("old") == ("refreshed", "NEW")
    _patch_http(monkeypatch, lambda r: httpx.Response(400, json={"error": {"code": 190}}))
    assert await svc.refresh_token("old") == ("invalid", None)
    _patch_http(monkeypatch, lambda r: httpx.Response(400, json={"error": {"code": 10, "message": "too new"}}))
    assert await svc.refresh_token("old") == ("error", None)


async def test_refresh_token_calls_ig_refresh_endpoint(monkeypatch):
    """Uses graph.instagram.com/refresh_access_token with grant_type=ig_refresh_token."""
    seen = {}

    def handler(req):
        """Capture the request."""
        seen.update(host=req.url.host, path=req.url.path, grant=req.url.params["grant_type"])
        return httpx.Response(200, json={"access_token": "NEW"})

    _patch_http(monkeypatch, handler)
    await svc.refresh_token("old")
    assert seen == {"host": "graph.instagram.com", "path": "/refresh_access_token", "grant": "ig_refresh_token"}


async def test_refresh_all_clients_persists_new_token_and_flags_dead_ones(monkeypatch, caplog):
    """Refreshed tokens are stored+committed; dead ones are left alone, flagged and logged CRITICAL."""
    def handler(req):
        """Refresh client A's token, report B's as dead."""
        if req.url.params["access_token"] == "A_OLD":
            return httpx.Response(200, json={"access_token": "A_NEW"})
        return httpx.Response(400, json={"error": {"code": 190}})

    _patch_http(monkeypatch, handler)
    a, b = _client(1, "A_OLD"), _client(2, "B_OLD")
    db = _db_with([a, b])
    with caplog.at_level(logging.INFO):
        counts = await svc.refresh_all_clients(db)
    assert counts == {"refreshed": 1, "invalid": 1, "error": 0}
    assert a.instagram_access_token == "A_NEW" and b.instagram_access_token == "B_OLD"
    db.commit.assert_awaited_once()
    assert channel_status.instagram_invalid_clients() == [2]
    assert "A_NEW" not in caplog.text and "B_OLD" not in caplog.text


def test_scheduler_registers_weekly_refresh_job(monkeypatch):
    """start_scheduler registers refresh_instagram_tokens as a weekly cron job."""
    added = {}
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda fn, trigger, id, **kw: added.setdefault(id, (fn, trigger)))
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)
    scheduler.start_scheduler()
    fn, trigger = added["refresh_instagram_tokens"]
    assert fn is scheduler._refresh_instagram_tokens_job
    assert "day_of_week='mon'" in str(trigger)


async def test_refresh_job_opens_session_and_runs_refresh(monkeypatch):
    """The scheduled job delegates to refresh_all_clients with a fresh DB session."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value="DB")
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.db._get_session_factory", lambda: (lambda: session))
    run = AsyncMock(return_value={})
    monkeypatch.setattr(svc, "refresh_all_clients", run)
    await scheduler._refresh_instagram_tokens_job()
    run.assert_awaited_once_with("DB")


async def test_startup_check_never_raises_and_does_not_disable_channel(monkeypatch):
    """main._check_instagram_token swallows DB errors and never flips the global kill switch."""
    from app import main

    monkeypatch.setattr("app.db._get_session_factory", MagicMock(side_effect=RuntimeError("db down")))
    await main._check_instagram_token()
    assert channel_status.is_instagram_disabled() is False


async def test_health_reports_instagram_disconnected_per_client(monkeypatch):
    """channel_status exposes invalid client ids for /health."""
    channel_status.set_instagram_client_status(7, "invalid")
    channel_status.set_instagram_client_status(8, "valid")
    assert channel_status.instagram_invalid_clients() == [7]
    assert channel_status.instagram_client_status_summary() == {"invalid": 1, "valid": 1}
