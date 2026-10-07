"""Startup migration check — real Postgres (the replay DB is migrated to head by the fixture)."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from app.services import migration_check

pytestmark = pytest.mark.asyncio


async def test_expected_heads_is_a_nonempty_set_of_revision_ids():
    """expected_heads reads the shipped migration scripts."""
    heads = migration_check.expected_heads()
    assert heads and all(isinstance(h, str) and h for h in heads)


async def test_current_revisions_matches_head_after_upgrade(replay_session):
    """current_revisions returns what alembic upgrade head wrote."""
    assert await migration_check.current_revisions(replay_session) == migration_check.expected_heads()


async def test_check_passes_silently_when_at_head(replay_session, caplog):
    """At head: True, no ERROR."""
    with caplog.at_level(logging.INFO):
        assert await migration_check.check_migrations_at_head(replay_session) is True
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_check_logs_error_naming_the_revisions_when_behind(replay_session, caplog):
    """A DB stamped with an old revision -> False and an ERROR naming both sides."""
    heads = migration_check.expected_heads()
    await replay_session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef0000'"))
    await replay_session.commit()
    try:
        with caplog.at_level(logging.ERROR):
            assert await migration_check.check_migrations_at_head(replay_session) is False
        msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert "MIGRATION MISMATCH" in msg and "deadbeef0000" in msg and sorted(heads)[0] in msg
    finally:
        await replay_session.execute(text("DELETE FROM alembic_version"))
        for h in heads:
            await replay_session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:h)"), {"h": h})
        await replay_session.commit()


async def test_check_never_raises_when_the_table_is_missing(replay_session, caplog):
    """No alembic_version table (fresh DB) -> False + ERROR, no exception."""
    await replay_session.execute(text("ALTER TABLE alembic_version RENAME TO alembic_version_x"))
    await replay_session.commit()
    try:
        with caplog.at_level(logging.ERROR):
            assert await migration_check.check_migrations_at_head(replay_session) is False
        assert any("MIGRATION CHECK FAILED" in r.getMessage() for r in caplog.records)
    finally:
        await replay_session.rollback()
        await replay_session.execute(text("ALTER TABLE alembic_version_x RENAME TO alembic_version"))
        await replay_session.commit()
