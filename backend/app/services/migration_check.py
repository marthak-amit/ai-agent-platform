"""
Startup check: is the database at the Alembic head?

Deploys are meant to run `alembic upgrade head` BEFORE the server starts (railway.json startCommand,
docker-entrypoint.sh and the Procfile all chain it with `&&`, so a failed migration stops the start). This
module is the belt-and-braces: if the process nevertheless comes up on a database that is behind (or ahead of)
the code — e.g. someone started uvicorn by hand — it logs an ERROR naming the revisions, so the mismatch shows up
in Railway logs instead of as 500s on the first request that touches a new column. It never raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


def expected_heads() -> set[str]:
    """The head revision id(s) of the migration scripts shipped with this code."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


async def current_revisions(db: AsyncSession) -> set[str]:
    """The revision id(s) recorded in the database's alembic_version table (empty if unmigrated)."""
    rows = await db.execute(text("SELECT version_num FROM alembic_version"))
    return {r[0] for r in rows}


async def check_migrations_at_head(db: AsyncSession) -> bool:
    """
    Compare the DB's revision(s) with the code's head(s). Logs ERROR on any mismatch and returns False;
    returns True when they match. Never raises (a failure to check is itself logged as an ERROR and returns False).
    """
    try:
        heads = expected_heads()
        current = await current_revisions(db)
    except Exception:
        logger.exception("MIGRATION CHECK FAILED: could not compare alembic current with head")
        return False
    if current == heads:
        logger.info("Migrations: database is at head (%s)", ", ".join(sorted(heads)))
        return True
    logger.error(
        "MIGRATION MISMATCH: database is at %s but the code expects head %s — run `alembic upgrade head` "
        "before serving traffic (missing: %s)",
        sorted(current) or "<unmigrated>", sorted(heads), sorted(heads - current) or "none",
    )
    return False
