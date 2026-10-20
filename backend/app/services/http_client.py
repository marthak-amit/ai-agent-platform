"""
One shared, keep-alive HTTP client for Meta's WhatsApp Cloud API sends.

Opening a client per message costs a DNS lookup + TCP + TLS handshake to Meta's servers on EVERY send (hundreds of ms, the
``wa_send`` span of the turn trace). A single ``httpx.AsyncClient`` keeps connections open and re-uses them. HTTP/2 is switched on
when the ``h2`` package is installed (``pip install "httpx[http2]"``), otherwise HTTP/1.1 keep-alive is used.

Each request adds two spans to the running turn: ``wa_connect`` (time spent opening a connection — DNS/TCP/TLS; ~0 once the pool is
warm) and ``wa_request`` (the rest: sending, Meta's processing, the response), so the TURN block shows where the time goes.

The client belongs to the event loop that created it; if another loop asks (test runs, a restarted worker) a fresh one is made.
``aclose()`` is called on application shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx

from app.services import turn_trace

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0)
_CONNECT_EVENTS = ("connection.connect_tcp", "connection.start_tls", "connection.connect_unix_socket")

_client: httpx.AsyncClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _http2_available() -> bool:
    """True when the optional ``h2`` package is installed."""
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


def meta() -> httpx.AsyncClient:
    """The shared client for the running event loop (created on first use)."""
    global _client, _loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _loop is not loop:
        _client = httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS, http2=_http2_available())
        _loop = loop
    return _client


async def aclose() -> None:
    """Close the shared client (application shutdown). Safe to call twice or when it was never opened."""
    global _client, _loop
    client, _client, _loop = _client, None, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("shared HTTP client close failed: %s", exc)


class _Timed:
    """The shared client with a ``post`` that books connect vs request time into the turn trace."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Wrap `client`."""
        self._client = client

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        """POST JSON; adds ``wa_connect`` and ``wa_request`` spans to the running turn."""
        started: dict[str, float] = {}
        connect = 0.0

        async def trace(event: str, info: dict) -> None:
            """httpx trace hook: sum the time of the connection-opening phases."""
            nonlocal connect
            for name in _CONNECT_EVENTS:
                if event == f"{name}.started":
                    started[name] = time.perf_counter()
                elif event in (f"{name}.complete", f"{name}.failed") and name in started:
                    connect += time.perf_counter() - started.pop(name)

        t0 = time.perf_counter()
        try:
            return await self._client.post(url, headers=headers, json=json, extensions={"trace": trace})
        finally:
            total = time.perf_counter() - t0
            turn_trace.add_span("wa_connect", connect)
            turn_trace.add_span("wa_request", max(0.0, total - connect))


@asynccontextmanager
async def meta_session():
    """``async with http_client.meta_session() as client: await client.post(url, headers=…, json=…)`` — the shared, timed client."""
    yield _Timed(meta())
