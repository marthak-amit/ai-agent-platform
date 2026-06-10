"""
Analytics router.

Endpoints:
- GET /analytics/overview      : key metrics and time-series data
- GET /analytics/leads-funnel  : funnel breakdown from inquiry to order
- GET /analytics/top-questions : most common topics in customer messages
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["analytics"])

# ── keyword topics for simple topic extraction ────────────────────────────────

_TOPIC_PATTERNS: list[tuple[str, list[str]]] = [
    ("price inquiry", ["price", "cost", "rate", "how much", "kitna", "charge"]),
    ("stock availability", ["stock", "available", "in stock", "availability", "hai kya"]),
    ("delivery time", ["delivery", "shipping", "dispatch", "deliver", "kab aayega"]),
    ("order placement", ["order", "buy", "purchase", "book", "khareed"]),
    ("product details", ["size", "colour", "color", "weight", "specification", "detail"]),
    ("return & refund", ["return", "refund", "replace", "exchange", "wapas"]),
    ("discount & offers", ["discount", "offer", "coupon", "deal", "sale", "promo"]),
    ("payment", ["payment", "upi", "gpay", "paytm", "cash", "cod"]),
    ("location & pickup", ["location", "address", "pickup", "shop", "store", "kahan"]),
    ("warranty & quality", ["warranty", "guarantee", "quality", "original", "genuine"]),
]


def _classify_message(text: str) -> str | None:
    """Return the first matching topic for a message, or None."""
    lower = text.lower()
    for topic, keywords in _TOPIC_PATTERNS:
        if any(kw in lower for kw in keywords):
            return topic
    return None


# ── helpers ───────────────────────────────────────────────────────────────────


def _today_utc() -> datetime:
    """Return midnight UTC for the current day."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/overview")
async def get_overview(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Return aggregated platform metrics.

    Includes message volume, lead breakdown, channel split, peak hours,
    average response time, and conversation counts.
    """
    today = _today_utc()
    seven_days_ago = today - timedelta(days=6)
    month_start = today.replace(day=1)

    # ── messages last 7 days ──────────────────────────────────────────────────
    rows = await db.execute(
        select(
            func.date(Message.created_at).label("day"),
            func.count(Message.id).label("cnt"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.role == "user",
            Message.created_at >= seven_days_ago,
            Conversation.is_sandbox == False,  # noqa: E712
        )
        .group_by(func.date(Message.created_at))
        .order_by(func.date(Message.created_at))
    )
    day_counts: dict[str, int] = {str(r.day): r.cnt for r in rows}
    messages_last_7_days = []
    for i in range(7):
        d = (seven_days_ago + timedelta(days=i)).strftime("%Y-%m-%d")
        messages_last_7_days.append({"date": d, "count": day_counts.get(d, 0)})

    # ── leads breakdown ───────────────────────────────────────────────────────
    lead_rows = await db.execute(
        select(Lead.status, func.count(Lead.id).label("cnt")).group_by(Lead.status)
    )
    lead_map: dict[str, int] = {r.status: r.cnt for r in lead_rows}
    total_leads = sum(lead_map.values())
    leads_breakdown = {
        "hot": lead_map.get("hot", 0),
        "warm": lead_map.get("warm", 0),
        "cold": lead_map.get("cold", 0),
        "total": total_leads,
    }

    hot = leads_breakdown["hot"]
    conversion_rate = round((hot / total_leads * 100), 1) if total_leads else 0.0

    # ── top channels ──────────────────────────────────────────────────────────
    chan_rows = await db.execute(
        select(Conversation.channel, func.count(Message.id).label("cnt"))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.role == "user", Conversation.is_sandbox == False)  # noqa: E712
        .group_by(Conversation.channel)
    )
    top_channels: dict[str, int] = {}
    for r in chan_rows:
        top_channels[r.channel] = r.cnt
    top_channels.setdefault("whatsapp", 0)
    top_channels.setdefault("instagram", 0)
    top_channels.setdefault("website", 0)

    # ── peak hours (all 24) ───────────────────────────────────────────────────
    hour_rows = await db.execute(
        select(
            func.extract("hour", Message.created_at).label("hr"),
            func.count(Message.id).label("cnt"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.role == "user", Conversation.is_sandbox == False)  # noqa: E712
        .group_by(func.extract("hour", Message.created_at))
    )
    hour_map: dict[int, int] = {int(r.hr): r.cnt for r in hour_rows}
    peak_hours = [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]

    # ── avg response time (seconds between user msg and next model msg) ───────
    # Approximate: average gap between consecutive pairs in each conversation.
    # We pull all messages ordered by conv + time, then pair user→model.
    all_msgs = await db.execute(
        select(Message.conversation_id, Message.role, Message.created_at)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.is_sandbox == False)  # noqa: E712
        .order_by(Message.conversation_id, Message.created_at)
    )
    msg_list = list(all_msgs)
    gaps: list[float] = []
    prev: dict[int, datetime] = {}
    for m in msg_list:
        if m.role == "user":
            prev[m.conversation_id] = m.created_at
        elif m.role == "assistant" and m.conversation_id in prev:
            delta = (m.created_at - prev.pop(m.conversation_id)).total_seconds()
            if 0 < delta < 300:  # ignore outliers > 5 min
                gaps.append(delta)
    avg_response_time = round(sum(gaps) / len(gaps), 1) if gaps else 0.0

    # ── conversation counts ───────────────────────────────────────────────────
    total_conv_row = await db.execute(
        select(func.count(Conversation.id)).where(Conversation.is_sandbox == False)  # noqa: E712
    )
    total_conversations = total_conv_row.scalar_one()

    new_today_row = await db.execute(
        select(func.count(Conversation.id)).where(
            Conversation.created_at >= today,
            Conversation.is_sandbox == False,  # noqa: E712
        )
    )
    new_conversations_today = new_today_row.scalar_one()

    month_msgs_row = await db.execute(
        select(func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.role == "user",
            Message.created_at >= month_start,
            Conversation.is_sandbox == False,  # noqa: E712
        )
    )
    messages_this_month = month_msgs_row.scalar_one()

    return {
        "messages_last_7_days": messages_last_7_days,
        "leads_breakdown": leads_breakdown,
        "conversion_rate": conversion_rate,
        "top_channels": top_channels,
        "peak_hours": peak_hours,
        "avg_response_time_seconds": avg_response_time,
        "total_conversations": total_conversations,
        "new_conversations_today": new_conversations_today,
        "messages_this_month": messages_this_month,
    }


@router.get("/revenue-chart")
async def get_revenue_chart(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """
    Return per-day revenue totals for the last 7 days (all clients, UTC dates).

    Each entry: {"date": "YYYY-MM-DD", "revenue": float}.
    """
    today = _today_utc()
    seven_days_ago = today - timedelta(days=6)

    rows = await db.execute(
        select(
            func.date(Order.created_at).label("day"),
            func.sum(Order.total_amount).label("total"),
        )
        .where(
            Order.created_at >= seven_days_ago,
            Order.status.notin_(["cancelled"]),
        )
        .group_by(func.date(Order.created_at))
        .order_by(func.date(Order.created_at))
    )
    day_map: dict[str, float] = {str(r.day): float(r.total or 0) for r in rows}

    result = []
    for i in range(7):
        d = (seven_days_ago + timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": d, "revenue": day_map.get(d, 0.0)})
    return result


@router.get("/leads-funnel")
async def get_leads_funnel(db: AsyncSession = Depends(get_db)) -> dict:
    """
    Return a simplified sales funnel from total inquiries to placed orders.

    Definitions used:
    - total_inquiries    = total conversations
    - qualified_leads    = warm + hot leads
    - hot_leads          = hot leads only
    - orders_placed      = currently 0 (no orders table yet)
    - conversion_rate    = orders_placed / total_inquiries * 100
    """
    total_conv_row = await db.execute(
        select(func.count(Conversation.id)).where(Conversation.is_sandbox == False)  # noqa: E712
    )
    total_inquiries: int = total_conv_row.scalar_one()

    lead_rows = await db.execute(
        select(Lead.status, func.count(Lead.id).label("cnt")).group_by(Lead.status)
    )
    lead_map: dict[str, int] = {r.status: r.cnt for r in lead_rows}
    hot = lead_map.get("hot", 0)
    warm = lead_map.get("warm", 0)
    qualified_leads = hot + warm

    # Orders placed is not tracked yet; placeholder for future integration.
    orders_placed = 0
    conversion_rate = round((orders_placed / total_inquiries * 100), 1) if total_inquiries else 0.0

    return {
        "total_inquiries": total_inquiries,
        "qualified_leads": qualified_leads,
        "hot_leads": hot,
        "orders_placed": orders_placed,
        "conversion_rate": conversion_rate,
    }


@router.get("/top-questions")
async def get_top_questions(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """
    Analyse the last 100 customer messages and return the top 10 topic groups.

    Topics are identified by keyword matching against common commerce intents.
    Messages that match no known topic are counted under 'other inquiry'.
    """
    rows = await db.execute(
        select(Message.content)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.role == "user", Conversation.is_sandbox == False)  # noqa: E712
        .order_by(Message.created_at.desc())
        .limit(100)
    )
    topics: Counter[str] = Counter()
    for (content,) in rows:
        topic = _classify_message(content)
        if topic:
            topics[topic] += 1
        else:
            topics["other inquiry"] += 1

    return [
        {"topic": topic, "count": count}
        for topic, count in topics.most_common(10)
    ]
