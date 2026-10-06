"""
FastAPI application factory and entry point.

Registers all routers and applies global middleware.
Run with: uvicorn app.main:app --reload --port 8000
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.log_redaction import configure_log_hygiene
from app.routers import admin, analytics, auth, briefing, campaigns, catalogue, catalogue_public, channels, conversations, customers, followup, instagram, integrations, knowledge, leads, onboarding, orders, payment, payment_settings, payment_verification, photo_enhancement, plans, realtime, sandbox, team, usage, webhook, whatsapp_signup, widget
from app.scheduler import start_scheduler, stop_scheduler
from app.services import channel_status, llm_health

_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(_UPLOADS_DIR, exist_ok=True)
_INVOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "invoices")
os.makedirs(_INVOICES_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
configure_log_hygiene()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background jobs on startup; shut them down cleanly on exit."""
    _startup_checks()
    await _schema_drift_check()
    await _check_whatsapp_token()
    await _check_instagram_token()
    await _check_llm_models()
    start_scheduler()
    yield
    stop_scheduler()


async def _schema_drift_check() -> None:
    """
    Compare ORM-declared columns for critical tables against the live DB.

    If any column present in the ORM model is absent from the live table,
    log CRITICAL for each missing column and exit(1) so the process never
    starts serving 500s caused by an un-applied migration.

    Tables checked: orders, conversations.
    """
    from app.config import get_settings as _gs

    db_url = _gs().database_url
    # sqlalchemy inspect requires a sync engine; create a minimal one
    sync_url = db_url.replace("+asyncpg", "").replace("+aiosqlite", "")

    try:
        engine = create_engine(sync_url)
        insp = inspect(engine)
        existing: dict[str, set[str]] = {}
        for table_name in ("orders", "conversations"):
            cols = insp.get_columns(table_name)
            existing[table_name] = {c["name"] for c in cols}
        engine.dispose()
    except Exception as exc:
        logger.warning("Schema drift check skipped (DB not reachable): %s", exc)
        return

    # import models so metadata is populated
    from app.db import Base  # noqa: F401
    import app.models  # noqa: F401

    missing_all: list[str] = []
    for table_name in ("orders", "conversations"):
        orm_table = Base.metadata.tables.get(table_name)
        if orm_table is None:
            continue
        for col in orm_table.columns:
            if col.name not in existing.get(table_name, set()):
                msg = f"Schema drift: column '{col.name}' missing from table '{table_name}'"
                logger.critical(msg)
                missing_all.append(msg)

    if missing_all:
        logger.critical(
            "Aborting startup — %d column(s) missing from live DB. Run `alembic upgrade head`.",
            len(missing_all),
        )
        sys.exit(1)


_DEBUG_TOKEN_URL = "https://graph.facebook.com/debug_token"
_INVALID_TOKEN_ERROR_CODE = 190  # Graph OAuthException: token invalid/expired/revoked


async def _debug_token(token: str, label: str) -> tuple[str, dict]:
    """
    Ask Meta's debug_token endpoint about `token`.

    Returns (verdict, data) where verdict is "valid", "invalid" or "unknown"
    (network/Meta error — inconclusive, never treated as invalid). Never logs
    the request URL (it carries the token as a query param) or any token
    value; failures log only the exception class.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _DEBUG_TOKEN_URL, params={"input_token": token, "access_token": token}
            )
        body = resp.json()
    except Exception as exc:
        logger.warning("%s token check: request failed (%s)", label, type(exc).__name__)
        return "unknown", {}

    data = body.get("data") or {}
    if data.get("is_valid") is True:
        return "valid", data
    if data.get("is_valid") is False or (body.get("error") or {}).get("code") == _INVALID_TOKEN_ERROR_CODE:
        return "invalid", data
    return "unknown", data


def _log_token_status(label: str, verdict: str, data: dict) -> None:
    """Log valid/invalid, type, expiry and scopes for a checked token — nothing else."""
    import time

    if verdict == "unknown":
        logger.warning("%s token check: inconclusive (Meta returned no validity data)", label)
        return
    if verdict == "invalid":
        logger.critical(
            "%s token INVALID at startup — sends will fail immediately. "
            "Regenerate via Meta Business Settings → System Users.",
            label,
        )
        return

    token_type = data.get("type", "unknown")
    scopes = data.get("scopes", [])
    expires_at = data.get("expires_at", 0)  # 0 = never expires (System User token)
    if expires_at == 0:
        logger.info(
            "%s token: VALID | type=%s | expiry=NEVER | scopes=%s", label, token_type, scopes
        )
        return

    days_left = (expires_at - int(time.time())) // 86400
    expires_iso = datetime.utcfromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M UTC")
    if days_left <= 7:
        logger.critical(
            "%s token: VALID | type=%s | expiry=%s (%d day(s) left) | scopes=%s — rotate NOW "
            "via Meta Business Settings → System Users.",
            label, token_type, expires_iso, days_left, scopes,
        )
    else:
        logger.warning(
            "%s token: VALID | type=%s | expiry=%s (%d days left) | scopes=%s — switch to a "
            "System User token (never expires).",
            label, token_type, expires_iso, days_left, scopes,
        )


async def _check_whatsapp_token() -> None:
    """
    Call the Meta token-debug endpoint at startup and log token validity + expiry.

    Logs CRITICAL if the token is invalid or expires within 7 days so the
    problem is visible in Railway boot logs — not as silent send failures later.
    Skips the check when the token is the placeholder test value.
    """
    from app.config import get_settings as _gs

    token = _gs().whatsapp_access_token
    if not token or token in ("test_token", "test-wa", "test-wa-token"):
        logger.info("WhatsApp token check: skipped (test/placeholder token)")
        return

    verdict, data = await _debug_token(token, "WhatsApp")
    _log_token_status("WhatsApp", verdict, data)


async def _check_instagram_token() -> None:
    """
    Verify every client's own Instagram token at startup (graph.instagram.com/me).

    Instagram Login tokens live per client on `clients.instagram_access_token`; there is no
    global/System User token to check. Logs VALID/INVALID per client_id (never token values) and
    records verdicts for /health. Informational only — it never disables sends or raises, so a
    DB or Meta outage cannot block startup.
    """
    from app.db import _get_session_factory
    from app.services import instagram_token_service

    try:
        async with _get_session_factory()() as db:
            await instagram_token_service.verify_all_clients(db)
    except Exception as exc:
        logger.warning("Instagram token check skipped (%s)", type(exc).__name__)


async def _check_llm_models() -> None:
    """Verify configured Groq models exist (CRITICAL log + 'LLM unavailable' in /health if not). Never raises."""
    from app.config import get_settings as _gs

    await llm_health.verify_models(_gs())


def _startup_checks() -> None:
    """Log a structured startup banner so Railway logs show config state immediately."""
    from app.config import get_settings as _gs
    from app.config import DEFAULT_SECRET_KEY, ENV_FILE, ensure_secret_key_is_safe
    from app.services import ocr_service
    s = _gs()
    ensure_secret_key_is_safe(s)
    sep = "=" * 50
    logger.info(sep)
    logger.info("AI Agent Platform Starting")
    logger.info("Environment : %s", s.environment)
    logger.info("SECRET_KEY default: %s (env file: %s)", s.secret_key == DEFAULT_SECRET_KEY, "found" if ENV_FILE.is_file() else "NOT FOUND")
    logger.info("Groq API    : %s", "configured" if s.groq_api_key else "MISSING ⚠️")
    logger.info(
        "WhatsApp    : %s",
        "test mode" if s.whatsapp_access_token == "test_token" else "configured",
    )
    logger.info(
        "Instagram   : per-client tokens (verified below); global INSTAGRAM_ACCESS_TOKEN fallback %s",
        "set" if s.instagram_access_token else "not set",
    )
    logger.info(
        "Razorpay    : %s",
        "configured" if s.razorpay_key_id else "not set",
    )
    logger.info(sep)
    ocr_service.check_tesseract_installed()


app = FastAPI(
    title="AI Agent Platform",
    description="WhatsApp + Instagram AI agent SaaS backend.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(briefing.router)
app.include_router(campaigns.router)
app.include_router(admin.router)
app.include_router(analytics.router)
app.include_router(channels.router)
app.include_router(followup.router)
app.include_router(webhook.router)
app.include_router(instagram.router)
app.include_router(integrations.router)
app.include_router(whatsapp_signup.router)
app.include_router(payment.router)
app.include_router(payment_settings.router)
app.include_router(payment_verification.router)
app.include_router(realtime.router)
app.include_router(auth.router)
app.include_router(team.router)
app.include_router(onboarding.router)
app.include_router(catalogue.router)
app.include_router(photo_enhancement.router)
app.include_router(plans.router)
app.include_router(usage.router)
app.include_router(conversations.router)
app.include_router(leads.router)
app.include_router(leads.public_router)
app.include_router(customers.router)
app.include_router(knowledge.router)
app.include_router(orders.router)
app.include_router(sandbox.router)
app.include_router(widget.router)
app.include_router(catalogue_public.router, prefix="")

app.mount("/uploads", StaticFiles(directory=_UPLOADS_DIR), name="uploads")
app.mount("/invoices", StaticFiles(directory=_INVOICES_DIR), name="invoices")


@app.get("/health", tags=["health"])
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Health check endpoint for Railway deployment monitoring.

    Verifies database connectivity. Returns HTTP 200 in all cases so
    Railway's health check doesn't restart a running (but DB-degraded) pod.

    Returns:
        JSON with status ("healthy" | "degraded"), per-check results, and timestamp.
    """
    import httpx
    from app.config import get_settings as _gs

    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    # WhatsApp token validity check
    s = _gs()
    token = s.whatsapp_access_token
    if not token or token in ("test_token", "test-wa", "test-wa-token"):
        checks["whatsapp_token"] = "skipped (test token)"
    else:
        try:
            async with httpx.AsyncClient(timeout=5) as _c:
                _r = await _c.get(
                    "https://graph.facebook.com/debug_token",
                    params={"input_token": token, "access_token": token},
                )
            _d = _r.json().get("data", {})
            if not _d.get("is_valid"):
                checks["whatsapp_token"] = "INVALID"
            elif _d.get("expires_at", 0) == 0:
                checks["whatsapp_token"] = "valid (never expires)"
            else:
                import time as _t
                _days = (_d["expires_at"] - int(_t.time())) // 86400
                checks["whatsapp_token"] = f"valid (expires in {_days}d)"
        except Exception as exc:
            checks["whatsapp_token"] = f"check_failed: {exc}"

    if channel_status.is_instagram_disabled():
        checks["instagram"] = "Instagram disconnected"
    elif channel_status.instagram_invalid_clients():
        checks["instagram"] = f"Instagram disconnected for client(s) {channel_status.instagram_invalid_clients()}"

    llm = llm_health.health_summary()
    if not llm["available"]:
        checks["llm"] = f"LLM unavailable: {llm['reason']}"
    if not llm["vision_enabled"]:
        checks["vision"] = f"skipped (disabled: {llm['vision_disabled_reason']})"

    all_ok = all(v in ("ok", "valid (never expires)") or v.startswith("valid") or v.startswith("skipped") for v in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "llm_failures_last_hour": llm["failures_last_hour"],
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
    }
