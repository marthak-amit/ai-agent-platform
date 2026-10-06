"""
Per-client Instagram token health: startup verification and long-lived-token refresh.

Each client connects their own Instagram account (Instagram Login) and we store its long-lived
token on `clients.instagram_access_token`. Those tokens last 60 days and can only be refreshed
(while still valid, and at least 24h old) via `graph.instagram.com/refresh_access_token`.

- `verify_all_clients()` — startup: call `graph.instagram.com/me` per client, log VALID/INVALID
  per client_id and record the verdict in `channel_status` (surfaced by /health).
- `refresh_all_clients()` — scheduled weekly: exchange every stored token for a fresh 60-day one.

Log hygiene: tokens travel as query params, so request URLs must never be logged. Failures log
only the client_id, the HTTP status and Meta's error code/message — never the token or the URL.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.services import channel_status

logger = logging.getLogger(__name__)

IG_GRAPH_BASE_URL = "https://graph.instagram.com"
INVALID_TOKEN_ERROR_CODE = 190  # OAuthException: token invalid/expired/revoked
_TIMEOUT = 10.0
_PLACEHOLDER_TOKENS = {"test_token", "test-ig", "test-ig-token"}


def _error_info(resp: httpx.Response) -> tuple[int | None, str]:
    """Meta error (code, message) from a response body; (None, '') when the body is not a Graph error."""
    try:
        err = resp.json().get("error") or {}
    except Exception:
        return None, ""
    return err.get("code"), str(err.get("message") or "")[:200]


async def check_token(token: str) -> str:
    """
    Ask Instagram whether `token` is usable: GET graph.instagram.com/me.

    Returns "valid" (HTTP 200), "invalid" (Graph error 190 / HTTP 401 — expired, revoked or wrong
    kind of token) or "unknown" (network error or any other response; inconclusive, never treated
    as invalid). Never raises and never logs the token or URL.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get(f"{IG_GRAPH_BASE_URL}/me", params={"fields": "user_id", "access_token": token})
    except Exception as exc:
        logger.warning("Instagram /me check: request failed (%s)", type(exc).__name__)
        return "unknown"
    if resp.status_code == 200:
        return "valid"
    code, _ = _error_info(resp)
    if code == INVALID_TOKEN_ERROR_CODE or resp.status_code == 401:
        return "invalid"
    return "unknown"


async def _clients_with_tokens(db: AsyncSession) -> list[Client]:
    """All clients that have a (non-placeholder) Instagram token stored."""
    rows = (await db.execute(select(Client).where(Client.instagram_access_token.is_not(None)))).scalars().all()
    return [c for c in rows if c.instagram_access_token and c.instagram_access_token not in _PLACEHOLDER_TOKENS]


async def verify_all_clients(db: AsyncSession) -> dict[int, str]:
    """
    Check every client's stored Instagram token and log VALID/INVALID per client_id.

    Records each verdict in channel_status (read by /health). Returns {client_id: verdict}.
    An INVALID token is logged CRITICAL: that client must reconnect Instagram in the dashboard.
    """
    results: dict[int, str] = {}
    clients = await _clients_with_tokens(db)
    if not clients:
        logger.info("Instagram token check: no clients have an Instagram token stored")
    for client in clients:
        verdict = await check_token(client.instagram_access_token)
        results[client.id] = verdict
        channel_status.set_instagram_client_status(client.id, verdict)
        if verdict == "valid":
            logger.info("Instagram token client_id=%s: VALID", client.id)
        elif verdict == "invalid":
            logger.critical(
                "Instagram token client_id=%s: INVALID — expired, revoked or not an Instagram Login token. "
                "The client must reconnect Instagram (dashboard → Channels).",
                client.id,
            )
        else:
            logger.warning("Instagram token client_id=%s: inconclusive (network or Meta error) — not treated as invalid", client.id)
    return results


async def refresh_token(token: str) -> tuple[str, str | None]:
    """
    Exchange a long-lived Instagram token for a fresh 60-day one.

    Returns (outcome, new_token): ("refreshed", token) on success; ("invalid", None) when Meta says
    the token is dead (cannot be refreshed — the client must reconnect); ("error", None) for anything
    transient or inconclusive (too new to refresh, network, 5xx). Never raises or logs the token/URL.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get(
                f"{IG_GRAPH_BASE_URL}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            )
    except Exception as exc:
        logger.warning("Instagram token refresh: request failed (%s)", type(exc).__name__)
        return "error", None
    if resp.status_code == 200:
        new_token = (resp.json() or {}).get("access_token")
        return ("refreshed", new_token) if new_token else ("error", None)
    code, message = _error_info(resp)
    logger.warning("Instagram token refresh: HTTP %s code=%s %s", resp.status_code, code, message)
    return ("invalid", None) if code == INVALID_TOKEN_ERROR_CODE or resp.status_code == 401 else ("error", None)


async def refresh_all_clients(db: AsyncSession) -> dict[str, int]:
    """
    Refresh every client's stored Instagram token and persist the new one.

    Run weekly so no token ever nears its 60-day expiry. A token Meta reports as dead cannot be
    recovered by refreshing: it is logged CRITICAL and flagged in channel_status so the client
    gets prompted to reconnect. Returns counts {"refreshed", "invalid", "error"}.
    """
    counts = {"refreshed": 0, "invalid": 0, "error": 0}
    for client in await _clients_with_tokens(db):
        outcome, new_token = await refresh_token(client.instagram_access_token)
        counts[outcome] += 1
        if outcome == "refreshed":
            client.instagram_access_token = new_token
            await db.commit()
            channel_status.set_instagram_client_status(client.id, "valid")
            logger.info("Instagram token client_id=%s: refreshed (valid for another ~60 days)", client.id)
        elif outcome == "invalid":
            channel_status.set_instagram_client_status(client.id, "invalid")
            logger.critical(
                "Instagram token client_id=%s: cannot be refreshed (expired/revoked) — client must reconnect Instagram.",
                client.id,
            )
        else:
            logger.warning("Instagram token client_id=%s: refresh failed this run; will retry next run", client.id)
    logger.info("Instagram token refresh run complete: %s", counts)
    return counts
