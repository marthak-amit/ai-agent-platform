"""
Replay-harness fixtures.

Uses a dedicated throwaway Postgres database (replay_ci_test) so the real
ai_agent_dev data is never touched.  Each test gets a fresh schema via
alembic upgrade head + seed data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

_ADMIN_SYNC_URL = "postgresql://bytes-amit@localhost/ai_agent_dev"
_REPLAY_DB = "replay_ci_test"
_REPLAY_ASYNCPG_URL = f"postgresql+asyncpg://bytes-amit@localhost/{_REPLAY_DB}"
_REPLAY_SYNC_URL = f"postgresql://bytes-amit@localhost/{_REPLAY_DB}"

META_APP_SECRET = "replay-test-secret"
META_VERIFY_TOKEN = "replay-verify"
WA_PHONE_NUMBER_ID = "111000111"


# ---------------------------------------------------------------------------
# DB lifecycle — module-scoped so migrations run ONCE per test run.
# ---------------------------------------------------------------------------

def _pg_available() -> bool:
    """Return True if a local Postgres connection is reachable."""
    try:
        engine = sa.create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT")
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:
        return False


_PG_AVAILABLE = _pg_available()


def pytest_collection_modifyitems(config, items):
    """Skip all replay tests when Postgres is not reachable."""
    if not _PG_AVAILABLE:
        skip = pytest.mark.skip(reason="local Postgres not reachable")
        for item in items:
            if "replay" in str(item.fspath):
                item.add_marker(skip)


@pytest.fixture(scope="module")
def replay_db_url():
    """
    Create the replay_ci_test database, run alembic upgrade head, yield the
    asyncpg URL, then drop the database.
    """
    if not _PG_AVAILABLE:
        pytest.skip("local Postgres not reachable")

    admin_engine = sa.create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        # WITH (FORCE) kills active asyncpg connections so DROP never blocks.
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_REPLAY_DB}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{_REPLAY_DB}"'))
    admin_engine.dispose()

    env = {**os.environ, "DATABASE_URL": _REPLAY_ASYNCPG_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "alembic upgrade head failed on replay DB:\n" + result.stdout + result.stderr
    )

    yield _REPLAY_ASYNCPG_URL

    admin_engine2 = sa.create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT")
    with admin_engine2.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_REPLAY_DB}"'))
    admin_engine2.dispose()


@pytest_asyncio.fixture(scope="function")
async def replay_session(replay_db_url):
    """Yield an AsyncSession connected to the replay DB (auto-rollback per test)."""
    engine = create_async_engine(replay_db_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers — inline so tests can call them directly.
# ---------------------------------------------------------------------------

async def seed_client_and_product(
    session: AsyncSession,
    *,
    phone: str = "919999999999",
    wa_phone_number_id: str = WA_PHONE_NUMBER_ID,
    product_sku: str = "SKU001",
    product_name: str = "Test Kurta",
    price: float = 500.0,
    stock: int = 10,
    has_variants: bool = False,
    payment_method: str = "COD",  # "COD", "UPI", or "BOTH"
    image_url: str | None = None,
) -> tuple:
    """
    Insert a minimal client + product row and return (client, product).

    Keeps the seed minimal — only fields the order flow reads are set.
    """
    from app.models.client import Client
    from app.models.product import Product

    client = Client(
        business_name="Replay Test Store",
        email=f"replay_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=wa_phone_number_id,
        accepts_cod=(payment_method in ("COD", "BOTH")),
        hashed_password="x",
        upi_id="test@upi" if payment_method in ("UPI", "BOTH") else None,
    )
    session.add(client)
    await session.flush()

    product = Product(
        client_id=client.id,
        name=product_name,
        sku=product_sku,
        price=price,
        stock=stock,
        is_active=True,
        has_variants=has_variants,
        image_url=image_url,
    )
    session.add(product)
    await session.commit()
    await session.refresh(client)
    await session.refresh(product)
    return client, product


async def seed_variant_and_simple(
    session: AsyncSession,
    *,
    phone: str,
    wa_phone_number_id: str,
) -> tuple:
    """
    Seed a UPI-only client with two products for extended-harness tests:

    Variant product — "Cotton Lehenga" (has_variants=True):
      Pink + M   → stock 5  (in-stock)
      Pink + XXL → stock 0  (OOS combo)
      Blue + M   → stock 5  (in-stock)
      Blue + XXL → stock 3  (in-stock)

    Simple product — "Silk Stole" (no variants, stock 3).

    Returns (client, variant_product, simple_product).
    """
    from app.models.client import Client
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

    client = Client(
        business_name="Extended Replay Store",
        email=f"ext_{phone}@test.com",
        phone=phone,
        whatsapp_phone_number_id=wa_phone_number_id,
        accepts_cod=False,
        hashed_password="x",
        upi_id="ext@upi",
    )
    session.add(client)
    await session.flush()

    var_prod = Product(
        client_id=client.id,
        name="Cotton Lehenga",
        sku=f"VR_{phone[-4:]}",
        price=500.0,
        stock=13,  # sum of all variants
        is_active=True,
        has_variants=True,
    )
    session.add(var_prod)
    await session.flush()

    for color, size, stock in [
        ("Pink", "M",   5),
        ("Pink", "XXL", 0),
        ("Blue", "M",   5),
        ("Blue", "XXL", 3),
    ]:
        session.add(ProductVariant(
            product_id=var_prod.id,
            client_id=client.id,
            color=color,
            size=size,
            stock=stock,
            is_active=True,
            price=500.0,
        ))

    sim_prod = Product(
        client_id=client.id,
        name="Silk Stole",
        sku=f"SM_{phone[-4:]}",
        price=300.0,
        stock=3,
        is_active=True,
        has_variants=False,
    )
    session.add(sim_prod)
    await session.commit()
    await session.refresh(client)
    await session.refresh(var_prod)
    await session.refresh(sim_prod)

    return client, var_prod, sim_prod


# ---------------------------------------------------------------------------
# ASGI test client wired to the replay DB.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def replay_http(replay_db_url, monkeypatch):
    """
    Return an AsyncClient wired to the real app using the replay DB.

    All external calls (Groq, WhatsApp API, owner notifications) are patched
    to no-ops so tests are deterministic.
    """
    # Patch settings so the app uses our test secrets.
    from app.config import Settings, get_settings

    test_settings = Settings(
        database_url=replay_db_url,
        gemini_api_key="test",
        openai_api_key="test",
        groq_api_key="test",
        meta_app_secret=META_APP_SECRET,
        meta_verify_token=META_VERIFY_TOKEN,
        whatsapp_access_token="test-wa",
        whatsapp_phone_number_id=WA_PHONE_NUMBER_ID,
        instagram_access_token="test-ig",
        razorpay_key_id="test",
        razorpay_key_secret="test",
        secret_key="test",
        admin_secret_key="test",
    )
    get_settings.cache_clear()
    monkeypatch.setattr("app.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.routers.webhook.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.whatsapp_service.get_settings", lambda: test_settings)

    # Replace the async engine used by get_db.
    from app import db as _db_module
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    replay_engine = create_async_engine(replay_db_url, echo=False)
    replay_factory = async_sessionmaker(
        replay_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _override_get_db():
        async with replay_factory() as s:
            yield s

    from app.db import get_db
    from app.main import app
    app.dependency_overrides[get_db] = _override_get_db

    # Silence all external HTTP calls and stub every LLM entry point so no
    # test hits the network (Groq, Gemini, etc.).
    import unittest.mock as mock
    monkeypatch.setattr(
        "app.services.whatsapp_service.send_text_message",
        mock.AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service.send_image_message",
        mock.AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service.send_button_message",
        mock.AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.gemini_service.generate_reply",
        mock.AsyncMock(return_value="__AI_REPLY__"),
    )
    # classify_buy_intent: return False so all tests rely on the deterministic
    # affirmation fast-path (FIX 1).  If the happy path only works when this
    # returns True it means FIX 1 is broken.
    monkeypatch.setattr(
        "app.services.conversation_flow.classify_buy_intent",
        mock.AsyncMock(return_value=False),
    )
    # classify_user_intent: return "ANSWER" (neutral — does not trigger any
    # special branch; lets keyword/stage logic drive behaviour).
    monkeypatch.setattr(
        "app.services.conversation_flow.classify_user_intent",
        mock.AsyncMock(return_value="ANSWER"),
    )
    # is_off_topic_message: return False so the off-topic guard never fires.
    monkeypatch.setattr(
        "app.services.conversation_flow.is_off_topic_message",
        mock.AsyncMock(return_value=False),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.pop(get_db, None)
    await replay_engine.dispose()
    get_settings.cache_clear()
