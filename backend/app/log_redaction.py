"""
Log hygiene — keep credentials out of log output.

- `httpx`/`httpcore` log every request URL at INFO, and our Meta Graph calls
  carry `access_token=` / `input_token=` in the query string, so both loggers
  are raised to WARNING.
- The dashboard's SSE stream authenticates with `GET /events/stream?token=<jwt>`
  (EventSource cannot send an Authorization header), so uvicorn's access log
  would record the JWT. `AccessLogRedactionFilter` masks any `*token=` query
  value before the record is emitted.
"""

from __future__ import annotations

import logging
import re

_REDACTED = "[REDACTED]"

# `?token=…`, `&access_token=…`, `&input_token=…` — any query key ending in "token".
_TOKEN_QUERY_RE = re.compile(r"([?&][\w.-]*token=)[^&\s\"']*", re.IGNORECASE)


def redact_query_secrets(text: str) -> str:
    """Replace the value of every `*token=` query parameter in `text` with [REDACTED]."""
    return _TOKEN_QUERY_RE.sub(rf"\1{_REDACTED}", text)


class AccessLogRedactionFilter(logging.Filter):
    """Logging filter that masks token query parameters in uvicorn access-log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact token query values in the record's message/args in place; never drops a record."""
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_secrets(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def configure_log_hygiene() -> None:
    """Quiet URL-logging HTTP client loggers and attach the access-log redaction filter (idempotent)."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactionFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogRedactionFilter())
