"""
Seed script — creates a default development client account.

Run from the backend/ directory with the venv active:
    python scripts/seed.py

The created account is safe to delete; it is only for local development.
"""

import asyncio
import os
import sys

# Allow import of app.* from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.client import Client
from app.services.auth_service import hash_password

SEED_EMAIL = "dev@example.com"
SEED_PASSWORD = "devpassword123"
SEED_BUSINESS = "Dev Business"


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        result = await session.execute(select(Client).where(Client.email == SEED_EMAIL))
        existing = result.scalar_one_or_none()

        if existing:
            print(f"[seed] Client already exists — email: {SEED_EMAIL}  id: {existing.id}")
        else:
            client = Client(
                email=SEED_EMAIL,
                hashed_password=hash_password(SEED_PASSWORD),
                business_name=SEED_BUSINESS,
                plan_slug="starter",
                daily_message_limit=100,
            )
            session.add(client)
            await session.commit()
            await session.refresh(client)
            print(f"[seed] Created client  id: {client.id}")

    await engine.dispose()

    print()
    print("  Login credentials")
    print(f"  Email   : {SEED_EMAIL}")
    print(f"  Password: {SEED_PASSWORD}")
    print()
    print("  Admin key (for /admin/* endpoints)")
    print(f"  X-Admin-Key: {settings.admin_secret_key}")


if __name__ == "__main__":
    asyncio.run(main())
