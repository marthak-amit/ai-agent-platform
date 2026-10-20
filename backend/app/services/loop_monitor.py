"""
Event-loop health: how late does the loop run things, and WHAT held it when it was late.

Every customer turn is a chain of awaits on ONE event loop. A single synchronous call inside an ``async def`` (a boto3 request, a Pillow
decode, Tesseract through ``subprocess.run``, ``time.sleep``) stops every other coroutine — database replies, other customers, the
APScheduler tick ("Run time of job … was missed by 0:00:03") — for as long as it runs. From the outside that looks like "the database
is slow" because the turn trace times a statement from send to reply, and the reply sits unread while the loop is busy.

Two tools:

* ``measure()`` — the delay of a zero-length hop through the loop (≈ 0 on a healthy loop). The TURN block prints it at the start and
  the end of a turn (``loop_lag=start/end/max``).
* ``Watchdog`` — a heartbeat on the loop plus a plain thread that notices when the heartbeat stops. While the loop is blocked the
  thread logs ``LOOP_BLOCKED`` with the **stack of the loop thread at that moment** (the exact line that is holding it); when the loop
  comes back it logs ``LOOP_STALL total=…ms``. Cheap enough to leave on (a 50 ms timer); ``LOOP_WATCHDOG=0`` turns it off, and in
  development asyncio's own debug mode (slow-callback log) is switched on as well (``ASYNCIO_DEBUG``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque

logger = logging.getLogger(__name__)

#: A stall shorter than this is not reported.
STALL_THRESHOLD_SECONDS = 0.100
_HEARTBEAT_SECONDS = 0.025
_MAX_SAMPLES = 400

# (monotonic timestamp, lag seconds) of the last heartbeats / measures — read by ``max_since``.
_samples: "deque[tuple[float, float]]" = deque(maxlen=_MAX_SAMPLES)
_watchdog: "Watchdog | None" = None


def _record(lag: float) -> None:
    """Remember one lag observation."""
    _samples.append((time.perf_counter(), lag))


async def measure() -> float:
    """Seconds a zero-length hop through the event loop took: how long everything already queued ahead of us held the loop (≈ 0 when healthy)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    started = time.perf_counter()
    loop.call_soon(future.set_result, None)
    await future
    lag = time.perf_counter() - started
    _record(lag)
    return lag


def max_since(timestamp: float) -> float:
    """The worst lag (seconds) observed since the ``time.perf_counter()`` value `timestamp` (0 when nothing was recorded)."""
    worst = 0.0
    for at, lag in reversed(_samples):
        if at < timestamp:
            break
        worst = max(worst, lag)
    return worst


class Watchdog:
    """A loop heartbeat plus a watcher thread that dumps the loop thread's stack while the heartbeat is stopped."""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, threshold: float = STALL_THRESHOLD_SECONDS) -> None:
        """Bind to `loop` (the thread that runs it is taken when ``start`` is called from inside it)."""
        self.loop = loop
        self.threshold = threshold
        self._last_beat = time.perf_counter()
        self._loop_thread_id: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._reported_for: float | None = None

    def start(self) -> None:
        """Begin beating (call from the loop's own thread) and start the watcher thread."""
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.perf_counter()
        self._beat()
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat and the watcher thread."""
        self._stop.set()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _beat(self) -> None:
        """Heartbeat (runs on the loop): record how late this tick is, then schedule the next one."""
        now = time.perf_counter()
        lag = max(0.0, now - self._last_beat - _HEARTBEAT_SECONDS)
        if lag >= self.threshold:
            logger.warning("LOOP_STALL total=%dms (the event loop was not running tasks for that long)", int((lag + _HEARTBEAT_SECONDS) * 1000))
        if lag > 0.005:
            _record(lag)
        self._last_beat = now
        self._reported_for = None
        if not self._stop.is_set():
            self._handle = self.loop.call_later(_HEARTBEAT_SECONDS, self._beat)

    def _watch(self) -> None:
        """Watcher thread: when the heartbeat is overdue, log the stack of the thread running the loop (once per stall)."""
        while not self._stop.wait(0.05):
            beat = self._last_beat
            blocked_for = time.perf_counter() - beat
            if blocked_for > self.threshold and self._reported_for != beat:
                self._reported_for = beat
                frame = sys._current_frames().get(self._loop_thread_id or 0)
                stack = "".join(traceback.format_stack(frame)[-12:]) if frame is not None else "(no frame)"
                logger.warning("LOOP_BLOCKED for %dms+ — the loop thread is currently executing:\n%s", int(blocked_for * 1000), stack)


def start_watchdog() -> "Watchdog | None":
    """Start the watchdog on the running loop (idempotent); None when ``LOOP_WATCHDOG=0``. Also turns on asyncio debug in development."""
    global _watchdog
    if os.environ.get("LOOP_WATCHDOG", "1") == "0":
        return None
    if _watchdog is not None:
        return _watchdog
    loop = asyncio.get_running_loop()
    from app.config import get_settings

    debug = os.environ.get("ASYNCIO_DEBUG")
    if debug == "1" or (debug is None and get_settings().environment == "development"):
        loop.set_debug(True)                       # asyncio logs "Executing <Task …> took 0.35 seconds" for callbacks over the threshold
        logging.getLogger("asyncio").setLevel(logging.WARNING)
    loop.slow_callback_duration = STALL_THRESHOLD_SECONDS
    _watchdog = Watchdog(loop)
    _watchdog.start()
    return _watchdog


def stop_watchdog() -> None:
    """Stop the watchdog (shutdown, tests)."""
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None
