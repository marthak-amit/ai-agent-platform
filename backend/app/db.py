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

        _engine = create_async_engine(get_settings().database_url, echo=False)
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
