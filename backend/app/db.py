"""
Async SQLAlchemy engine and session factory.

Engine is created lazily on first use so module import never fails if
DATABASE_URL is not yet set (e.g. during test collection).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

_engine = None
_session_factory = None

    
class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory, creating the engine on first call."""
    global _engine, _session_factory
    if _session_factory is None:
        from app.config import get_settings

        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url, echo=False,
            pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds, pool_recycle=settings.db_pool_recycle_seconds, pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.

    Yields:
        AsyncSession: An active SQLAlchemy async session.
    """
    async with _get_session_factory()() as session:
        yield session


async def release_connection(db: AsyncSession) -> None:
    """
    Hand the session's pooled connection back before a slow call that does not need the database (an LLM, vision, OCR or WhatsApp request).

    A session that has run any statement holds its connection until the transaction ends, so a 3-second LLM call made mid-turn kept one of
    the pool's connections "idle in transaction" for 3 seconds — with a handful of customers talking at once the pool ran dry and every
    other turn queued on ``pool_timeout``. ``expire_on_commit`` is False for these sessions, so loaded rows stay readable; nothing is
    lost: it only commits what the turn has already written. Never raises (a failed commit is rolled back by the caller's own handling).
    """
    try:
        if db.in_transaction():
            await db.commit()
    except Exception:  # noqa: BLE001 - a failed commit surfaces again at the caller's next real write
        pass
