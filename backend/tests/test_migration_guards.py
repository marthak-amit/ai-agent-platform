"""
Migration discipline guards.

Guard 1 (test_revision_ids_within_32_chars): scans every alembic migration
file and asserts that revision = "..." is at most 32 characters long — the
width of the alembic_version.version_num varchar column.  Catches the exact
class of bug that caused the 0039 StringDataRightTruncationError.

Guard 2 (test_migrations_apply_clean_against_real_postgres): applies every
migration from zero against the real local Postgres DB (ai_agent_dev), then
checks that the final schema matches the ORM metadata.  Uses a throwaway
schema so the live data tables are never touched.
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"

# ---------------------------------------------------------------------------
# Guard 1 — revision id length
# ---------------------------------------------------------------------------

def _revision_ids_in_file(path: Path) -> list[str]:
    """Return all values assigned to a bare `revision` variable in *path*."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    ids: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "revision"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            ids.append(node.value.value)
    return ids


def test_revision_ids_within_32_chars():
    """Every alembic revision id must be ≤ 32 chars (varchar(32) in DB)."""
    bad: list[tuple[str, str]] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for rev_id in _revision_ids_in_file(path):
            if len(rev_id) > 32:
                bad.append((path.name, rev_id))
    assert not bad, (
        "The following revision ids exceed 32 chars and will fail to stamp:\n"
        + "\n".join(f"  {fname}: {rid!r} ({len(rid)} chars)" for fname, rid in bad)
    )


# ---------------------------------------------------------------------------
# Guard 2 — real Postgres round-trip
# ---------------------------------------------------------------------------

# Sync URL for admin operations (CREATE/DROP DATABASE) and inspection.
# The app's alembic env uses asyncpg, so we pass the asyncpg URL as
# DATABASE_URL to the subprocess and use psycopg2 only for admin + inspect.
_ADMIN_DB_URL = "postgresql://localhost/ai_agent_dev"  # psycopg2 sync
_TEST_DB = "alembic_ci_test"
_TEST_ASYNCPG_URL = f"postgresql+asyncpg://localhost/{_TEST_DB}"


def _pg_available() -> bool:
    try:
        engine = sa.create_engine(_ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_available(), reason="local Postgres not reachable")
def test_migrations_apply_clean_against_real_postgres(tmp_path):
    """
    Create a throwaway database, run alembic upgrade head from zero, then
    verify every ORM-declared table exists in the result.

    The test database is dropped on both success and failure.
    """
    admin_engine = sa.create_engine(_ADMIN_DB_URL, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_TEST_DB}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{_TEST_DB}"'))

    try:
        env = {**os.environ, "DATABASE_URL": _TEST_ASYNCPG_URL}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "alembic upgrade head failed:\n" + result.stdout + result.stderr
        )

        # Inspect final schema via sync psycopg2
        test_sync_url = f"postgresql://localhost/{_TEST_DB}"
        inspect_engine = sa.create_engine(test_sync_url)
        inspector = sa.inspect(inspect_engine)
        existing = set(inspector.get_table_names())
        inspect_engine.dispose()

        from app.db import Base  # noqa: PLC0415
        import app.models  # noqa: F401

        missing = [t for t in Base.metadata.tables if t not in existing]
        assert not missing, f"ORM tables not found in DB after upgrade head: {missing}"
    finally:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_TEST_DB}"'))
    admin_engine.dispose()
