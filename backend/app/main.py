"""
FastAPI application factory and entry point.

Registers all routers and applies global middleware.
Run with: uvicorn app.main:app --reload --port 8000
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers import admin, analytics, auth, briefing, campaigns, catalogue, catalogue_public, channels, conversations, customers, followup, instagram, knowledge, leads, onboarding, orders, payment, plans, sandbox, usage, webhook, widget
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
    start_scheduler()
    yield
    stop_scheduler()


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
app.include_router(payment.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(catalogue.router)
app.include_router(plans.router)
app.include_router(usage.router)
app.include_router(conversations.router)
app.include_router(leads.router)
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
    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
    }
