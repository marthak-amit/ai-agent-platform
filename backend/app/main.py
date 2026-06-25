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
from app.routers import admin, analytics, auth, briefing, campaigns, catalogue, catalogue_public, channels, conversations, customers, followup, instagram, integrations, knowledge, leads, onboarding, orders, payment, plans, sandbox, usage, webhook, widget
from app.scheduler import start_scheduler, stop_scheduler

_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(_UPLOADS_DIR, exist_ok=True)
_INVOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "invoices")
os.makedirs(_INVOICES_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background jobs on startup; shut them down cleanly on exit."""
    _startup_checks()
    await _schema_drift_check()
    await _check_whatsapp_token()
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


async def _check_whatsapp_token() -> None:
    """
    Call the Meta token-debug endpoint at startup and log token validity + expiry.

    Logs CRITICAL if the token is invalid or expires within 7 days so the
    problem is visible in Railway boot logs — not as silent send failures later.
    Skips the check when the token is the placeholder test value.
    """
    import httpx
    from app.config import get_settings as _gs

    s = _gs()
    token = s.whatsapp_access_token
    if not token or token in ("test_token", "test-wa", "test-wa-token"):
        logger.info("WhatsApp token check: skipped (test/placeholder token)")
        return

    url = "https://graph.facebook.com/debug_token"
    params = {"input_token": token, "access_token": token}
    sep = "=" * 50
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        data = resp.json().get("data", {})
    except Exception as exc:
        logger.warning("WhatsApp token check: HTTP error — %s", exc)
        return

    is_valid = data.get("is_valid", False)
    expires_at = data.get("expires_at", 0)  # 0 = never expires (System User token)
    token_type = data.get("type", "unknown")
    app_name = data.get("application", "unknown")
    scopes = data.get("scopes", [])

    logger.info(sep)
    if not is_valid:
        error = data.get("error", {})
        logger.critical(
            "WhatsApp token INVALID at startup — sends will fail immediately. "
            "Error: %s (code %s). Regenerate via Meta Business Settings → System Users.",
            error.get("message", "unknown"),
            error.get("code", "?"),
        )
    elif expires_at == 0:
        logger.info(
            "WhatsApp token: VALID ✓ | type=%s | app=%s | expiry=NEVER (System User) | scopes=%s",
            token_type, app_name, scopes,
        )
    else:
        import time
        secs_left = expires_at - int(time.time())
        days_left = secs_left // 86400
        expires_iso = datetime.utcfromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M UTC")
        if days_left <= 7:
            logger.critical(
                "WhatsApp token expires in %d day(s) on %s — rotate NOW via Meta Business Settings → System Users.",
                days_left, expires_iso,
            )
        else:
            logger.warning(
                "WhatsApp token: VALID but EXPIRES in %d days on %s. "
                "Switch to a System User token (never expires).",
                days_left, expires_iso,
            )
    logger.info(sep)


def _startup_checks() -> None:
    """Log a structured startup banner so Railway logs show config state immediately."""
    from app.config import get_settings as _gs
    s = _gs()
    sep = "=" * 50
    logger.info(sep)
    logger.info("AI Agent Platform Starting")
    logger.info("Environment : %s", s.environment)
    logger.info("Groq API    : %s", "configured" if s.groq_api_key else "MISSING ⚠️")
    logger.info(
        "WhatsApp    : %s",
        "test mode" if s.whatsapp_access_token == "test_token" else "configured",
    )
    logger.info(
        "Instagram   : %s",
        "configured" if s.instagram_access_token else "not set",
    )
    logger.info(
        "Razorpay    : %s",
        "configured" if s.razorpay_key_id else "not set",
    )
    logger.info(sep)


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
app.include_router(payment.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(catalogue.router)
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

    all_ok = all(v in ("ok", "valid (never expires)") or v.startswith("valid") or v.startswith("skipped") for v in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
    }
