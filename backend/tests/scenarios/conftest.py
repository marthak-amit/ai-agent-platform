"""Scenario suite fixtures: the replay harness' real-Postgres fixtures plus a zero reply delay."""

from __future__ import annotations

import pytest

from tests.replay.conftest import (  # noqa: F401  (fixtures are re-exported for this directory)
    _PG_AVAILABLE, _clean_replay_db, replay_db_url, replay_http, replay_session,
)

import app.main  # noqa: F401,E402  (see replay_test_running: import the app first so settings patches bind consistently)


def pytest_collection_modifyitems(config, items):
    """Skip the whole directory when local Postgres is not reachable (same rule as the replay suite)."""
    if not _PG_AVAILABLE:
        skip = pytest.mark.skip(reason="local Postgres not reachable")
        for item in items:
            if "scenarios" in str(item.fspath):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _scenario_env(monkeypatch):
    """No human-like reply delay, no live Groq (the localizer / any leaked call must fail fast, not spend tokens)."""
    monkeypatch.setattr("app.services.order_pipeline.random.uniform", lambda *_a, **_k: 0)

    async def _no_rate_limit(phone):
        """The per-phone limiter (5 msgs / 10 s) must not drop scripted conversation turns."""
        return False

    monkeypatch.setattr("app.routers.webhook._is_rate_limited", _no_rate_limit)
    monkeypatch.setenv("GROQ_API_KEY", "invalid-key-for-test")


@pytest.fixture(autouse=True)
def _reset_llm_health():
    """Scenarios that fail the planner on purpose must not leave a failure count / open breaker behind."""
    from app.services import llm_health

    llm_health.reset_state()
    yield
    llm_health.reset_state()


@pytest.fixture(autouse=True)
def _fast_planner_retry_and_fake_watermark(monkeypatch):
    """No real sleeping on a planner 429, and product photos are not downloaded for watermarking."""
    monkeypatch.setattr("app.services.agent_planner.RETRY_BACKOFF_SECONDS", 0.0, raising=False)

    async def _wm(client_id, sku, url):
        """Stand-in for storage_service.get_watermarked_image_url."""
        return f"{url}?wm={sku}"

    monkeypatch.setattr("app.services.storage_service.get_watermarked_image_url", _wm)


@pytest.fixture(autouse=True)
def _persist_llm_usage_to_the_replay_db(replay_db_url, monkeypatch):  # noqa: F811 (pytest fixture argument)
    """The COST line reads the llm_usage ledger, so scenario turns must write real rows — into the replay DB, never the dev DB."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.llm_usage import LLMUsage
    from app.services import llm_usage_service

    factory = async_sessionmaker(create_async_engine(replay_db_url), class_=AsyncSession, expire_on_commit=False)

    async def _persist(row):
        """Insert one ledger row (resolving client_id from the conversation, like the real writer)."""
        async with factory() as db:
            db.add(LLMUsage(**row))
            await db.commit()

    monkeypatch.setattr(llm_usage_service, "_persist_row", _persist)


@pytest.fixture(autouse=True)
def _stub_inbound_media(monkeypatch):
    """Inbound photos: no Meta download, no storage upload, no OCR (the photo never names a product)."""
    from tests.replay.helpers import TINY_PNG

    async def _download(media_id, *a, **k):
        """A 1×1 PNG."""
        return TINY_PNG

    async def _store(client_id, data, content_type, folder="chat"):
        """A fake CDN URL."""
        return f"https://cdn.test/{folder}/1.png"

    monkeypatch.setattr("app.services.vision_service.download_whatsapp_media", _download)
    monkeypatch.setattr("app.services.media_service.store_media", _store)
    monkeypatch.setattr("app.services.ocr_service.detect_sku_from_image", lambda *_: None)
