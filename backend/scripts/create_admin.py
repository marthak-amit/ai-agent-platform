"""
Create (or reset) an admin-panel operator from the command line — the bootstrap path for the FIRST superadmin.

Run from backend/ with DATABASE_URL set (env or backend/.env):

    python scripts/create_admin.py --email you@company.com --name "Your Name" --role superadmin

The password is read with a hidden prompt (never pass it on the command line, it would land in shell history).
Use --reset to set a new password for an existing operator (also clears lockout and revokes their sessions).
The new account must change its password at first sign-in.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db import _get_session_factory  # noqa: E402
from app.models.admin_user import ADMIN_ROLES, AdminUser  # noqa: E402
from app.services import admin_auth_service, auth_service  # noqa: E402


async def _run(args: argparse.Namespace, password: str) -> int:
    """Create or reset the operator. Returns a process exit code."""
    async with _get_session_factory()() as db:
        email = admin_auth_service.normalize_email(args.email)
        existing = (await db.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one_or_none()
        if args.reset:
            if existing is None:
                print(f"No operator with email {email}.", file=sys.stderr)
                return 1
            admin_auth_service.validate_password_strength(password, email)
            existing.hashed_password = auth_service.hash_password(password)
            existing.must_change_password = True
            existing.failed_login_count = 0
            existing.locked_until = None
            existing.is_active = True
            admin_auth_service.revoke_sessions(existing)
            await db.commit()
            print(f"Password reset for {email}; they must change it at next sign-in.")
            return 0
        admin = await admin_auth_service.create_admin(
            db, email=email, password=password, name=args.name, role=args.role,
            created_by="cli", must_change_password=True,
        )
        await db.commit()
        print(f"Created {admin.role} {admin.email} (id={admin.id}). Sign in at /admin/login and change the password.")
        return 0


def main() -> int:
    """Parse arguments, prompt for the password, and run."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--role", default="superadmin", choices=ADMIN_ROLES)
    parser.add_argument("--reset", action="store_true", help="reset the password of an existing operator")
    args = parser.parse_args()

    password = getpass.getpass("Password (min 12 chars, letters + digits): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run(args, password))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
