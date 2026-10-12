"""
Concurrency regressions found by the conversation simulator (tests/sim):

* two customers confirming an order at the same instant computed the same `ORD-YYYY-NNNN` and the second INSERT
  died on the global unique index — the confirmed order was lost;
* the first two messages of a day for one client both INSERTed today's usage row — one webhook request crashed.

Runs against the replay Postgres (skipped when it is unreachable).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.replay.conftest import _PG_AVAILABLE, seed_client_and_product

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="local Postgres not reachable")

N = 12


async def test_concurrent_order_creation_issues_unique_sequential_numbers(replay_db_url, replay_session):
    """12 simultaneous order confirmations → 12 distinct, gap-free order numbers, none lost."""
    from app.models.order import Order
    from app.services import order_service

    client, product = await seed_client_and_product(replay_session, phone="919800000001", wa_phone_number_id="880001", product_sku="CC0001")
    engine = create_async_engine(replay_db_url, pool_size=N)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def place(i: int):
        async with factory() as db:
            return await order_service.create_order(
                db=db, client_id=client.id, customer_name=f"Cust {i}", customer_phone=f"91990000{i:04d}",
                delivery_address="1 Test Road", product_name=product.name, quantity=1, unit_price=product.price,
                payment_method="COD", product_id=product.id, product_sku=product.sku, initial_status="new",
            )

    orders = await asyncio.gather(*(place(i) for i in range(N)))
    await engine.dispose()

    numbers = sorted(o.order_number for o in orders)
    assert len(set(numbers)) == N, numbers
    suffixes = sorted(int(n.rsplit("-", 1)[1]) for n in numbers)
    assert suffixes == list(range(suffixes[0], suffixes[0] + N))
    stored = (await replay_session.execute(select(func.count()).select_from(Order))).scalar_one()
    assert stored == N


async def test_concurrent_first_messages_of_the_day_do_not_crash(replay_db_url, replay_session):
    """12 simultaneous record_message calls for one client → no UniqueViolation, count == 12."""
    from app.models.usage_log import UsageLog
    from app.services import usage_service

    client, _ = await seed_client_and_product(replay_session, phone="919800000002", wa_phone_number_id="880002", product_sku="CC0002")
    engine = create_async_engine(replay_db_url, pool_size=N)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def hit():
        async with factory() as db:
            return await usage_service.record_message(db, client)

    await asyncio.gather(*(hit() for _ in range(N)))
    await engine.dispose()

    total = (await replay_session.execute(select(func.sum(UsageLog.message_count)).where(UsageLog.client_id == client.id))).scalar_one()
    assert total == N


async def test_concurrent_first_contact_creates_one_conversation_customer_and_lead(replay_db_url, replay_session):
    """A customer's first two messages processed in parallel → one conversation / customer / lead, no crash."""
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.lead import Lead
    from app.services import conversation_service, customer_service, lead_service

    client, _ = await seed_client_and_product(replay_session, phone="919800000003", wa_phone_number_id="880003", product_sku="CC0003")
    engine = create_async_engine(replay_db_url, pool_size=N)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    phone = "919811111111"

    async def first_contact():
        async with factory() as db:
            conv = await conversation_service.get_or_create_conversation(db, phone, client_id=client.id)
            await customer_service.upsert_customer(db, client.id, phone)
            await db.commit()
            await lead_service.tag_lead(db, phone, conv.id, [{"role": "user", "content": "order karna hai"}], client_id=client.id)
            return conv.id

    ids = await asyncio.gather(*(first_contact() for _ in range(6)))
    await engine.dispose()

    assert len(set(ids)) == 1
    for model in (Conversation, Customer, Lead):
        count = (await replay_session.execute(select(func.count()).select_from(model).where(model.client_id == client.id))).scalar_one()
        assert count == 1, model
