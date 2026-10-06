"""
Build tests/router_eval/dataset.jsonl + dataset.csv + catalogue_snapshot.json from the DB.

READ-ONLY: the connection is opened with default_transaction_read_only=on, so any
accidental write fails. Run from backend/:

    python tests/router_eval/build_dataset.py --client-id 1 --n 50

Re-running overwrites dataset.jsonl/csv, so it refuses to if any row already has an
expected_action unless --force is given.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.services.language_service import detect_language  # noqa: E402
from tests.router_eval.common import (  # noqa: E402
    CATALOGUE_PATH, CSV_PATH, DATASET_PATH, anonymize, build_name_pattern, load_dataset, save_dataset,
)

CONTEXT_N = 6
_TAG_PATTERNS = {
    "greeting": r"^\s*(hi+|hello+|hey+|namaste|namaskar|kem cho|jai shree krishna|good (morning|evening)|hlo)\b",
    "price": r"\b(price|rate|kitna|kitne|kimat|bhav|cost|ketla|ketlu|₹|rs\.?|rupee)\b",
    "size": r"\b(size|xl|xxl|medium|large|small|fitting|chest|length)\b",
    "order_status": r"\b(order|delivery|deliver|dispatch|shipped|tracking|track|kab|kyare|aaya|pahoch|status|parcel)\b",
    "complaint": r"\b(wrong|damage|damaged|refund|return|exchange|complain|bad|fake|cheat|late|not received|nahi aaya|kharab|paisa)\b",
    "vague_product": r"\b(kuch|something|show|dikhao|batao|batav|dekhao|available|hai kya|che\?|saree|kurti|dress|shirt|top|lehenga|design|catalog|catalogue)\b",
}
# A bot turn asking for personal details -> the customer's reply to it is PII (name/address/phone).
_PII_ASK = re.compile(r"address|pincode|pin code|full name|your name|naam|nam kya|mobile|phone|contact number|સરનામ|નામ|पता|नाम|मोबाइल", re.I)
_ADDRESS_HINT = re.compile(r"<PIN>|\b(flat|house|society|apartment|road|near|opp\.?|nagar|colony|pincode|pin code)\b", re.I)


def _tag(msg: str) -> list[str]:
    """Heuristic category tags used only to stratify the sample (not labels)."""
    tags = [name for name, pat in _TAG_PATTERNS.items() if re.search(pat, msg, re.I)]
    if "?" in msg:
        tags.append("question")
    return tags or ["other"]


async def _fetch(client_id: int):
    """Return (conversations, messages_by_conv, customers_by_phone, products) with a read-only connection."""
    engine = create_async_engine(
        get_settings().database_url, connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    from sqlalchemy.ext.asyncio import AsyncSession
    async with AsyncSession(engine, expire_on_commit=False) as db:
        convs = (await db.execute(
            select(Conversation).where(Conversation.client_id == client_id, Conversation.is_sandbox.is_(False))
        )).scalars().all()
        msgs = (await db.execute(
            select(Message).where(Message.conversation_id.in_([c.id for c in convs])).order_by(Message.created_at, Message.id)
        )).scalars().all()
        customers = (await db.execute(select(Customer).where(Customer.client_id == client_id))).scalars().all()
        products = (await db.execute(
            select(Product).where(Product.client_id == client_id).options(selectinload(Product.variants))
        )).scalars().all()
    await engine.dispose()
    by_conv: dict[int, list] = defaultdict(list)
    for m in msgs:
        by_conv[m.conversation_id].append(m)
    return convs, by_conv, {c.phone: c for c in customers}, products


def _snapshot(products) -> list[dict]:
    """Catalogue snapshot for the prompt: active products, in-stock colours/sizes, no stock counts."""
    out = []
    for p in products:
        if p.is_active is False:
            continue
        variants = [v for v in (p.variants or []) if getattr(v, "is_active", True) and (v.stock or 0) > 0]
        out.append({
            "sku": p.sku, "name": p.name, "price": p.price, "category": p.category,
            "description": (p.description or "")[:200],
            "colors": sorted({v.color for v in variants if v.color}),
            "sizes": sorted({v.size for v in variants if v.size}),
            "in_stock": bool(variants) if p.has_variants else (p.stock or 0) > 0,
        })
    return out


def _state(conv: Conversation) -> dict:
    """Snapshot of the conversation's state fields (current, NOT as-of the message — see README)."""
    return {
        "stage": conv.current_stage, "pending_product_sku": conv.pending_product_sku,
        "last_shown_sku": conv.last_shown_sku, "selected_color": conv.selected_color,
        "selected_size": conv.selected_size, "summary_shown": conv.summary_shown,
        "has_customer_name": bool(conv.customer_name), "has_address": bool(conv.delivery_address),
    }


async def main() -> None:
    """Select a stratified, anonymised sample of inbound messages and write the dataset files."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", type=int, default=1)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--force", action="store_true", help="overwrite even if rows are already labelled")
    args = ap.parse_args()

    if DATASET_PATH.exists() and not args.force and any(r.get("expected_action") for r in load_dataset()):
        sys.exit("dataset.jsonl already has labelled rows — refusing to overwrite (use --force).")

    convs, by_conv, customers, products = await _fetch(args.client_id)
    rng = random.Random(args.seed)
    candidates, seen = [], set()
    for conv in convs:
        cust = customers.get(conv.phone_number)
        names = {n for n in (conv.customer_name, getattr(cust, "name", None)) if n}
        name_re = build_name_pattern(names)
        msgs = by_conv.get(conv.id, [])
        for i, m in enumerate(msgs):
            if m.role != "user" or m.original_type == "image":
                continue
            if i > 0 and msgs[i - 1].role != "user" and _PII_ASK.search(msgs[i - 1].content or ""):
                continue  # reply to a name/address/phone question — never sampled
            clean = anonymize(m.content.strip(), name_re, conv.delivery_address)
            norm = re.sub(r"\W+", " ", clean.lower()).strip()
            if len(norm) < 2 or norm in seen or len(clean) > 300 or _ADDRESS_HINT.search(clean):
                continue
            seen.add(norm)
            lang = detect_language(clean)
            tags = _tag(clean)
            # messiness: non-English, questions, mid-length text score higher
            score = (2 if lang != "english" else 0) + (1 if "?" in clean else 0) + (1 if 4 <= len(clean.split()) <= 25 else 0) + rng.random()
            window = msgs[max(0, i - CONTEXT_N):i]
            context = []
            for j, x in enumerate(window):
                prev = window[j - 1] if j > 0 else (msgs[i - CONTEXT_N - 1] if i - CONTEXT_N - 1 >= 0 else None)
                if x.role == "user" and prev is not None and prev.role != "user" and _PII_ASK.search(prev.content or ""):
                    txt = "<PII_REPLY>"  # customer's answer to a name/address/phone question
                else:
                    txt = anonymize(x.content[:400], name_re, conv.delivery_address)
                context.append({"role": "customer" if x.role == "user" else "bot", "text": txt})
            candidates.append({
                "tags": tags, "score": score, "language": lang, "message": clean, "context": context,
                "state": _state(conv), "was_voice_note": m.original_type == "audio",
                "conv_ref": hashlib.sha1(f"{args.seed}:{conv.id}".encode()).hexdigest()[:8],
            })

    # round-robin across tags (best-scored first) so categories are all represented
    pools: dict[str, list] = defaultdict(list)
    for c in candidates:
        pools[c["tags"][0]].append(c)
    for pool in pools.values():
        pool.sort(key=lambda c: -c["score"])
    chosen, order = [], sorted(pools)
    while len(chosen) < args.n and any(pools.values()):
        for t in order:
            if pools[t] and len(chosen) < args.n:
                chosen.append(pools[t].pop(0))
    rng.shuffle(chosen)

    rows = [{
        "id": f"r{idx:03d}", "message": c["message"], "language": c["language"], "context": c["context"],
        "state": c["state"], "heuristic_tags": c["tags"], "was_voice_note": c["was_voice_note"],
        "conv_ref": c["conv_ref"], "expected_action": "", "expected_args": None, "notes": "",
    } for idx, c in enumerate(chosen, 1)]

    save_dataset(rows)
    CATALOGUE_PATH.write_text(__import__("json").dumps(_snapshot(products), ensure_ascii=False, indent=2), encoding="utf-8")
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel reads Gujarati/Hindi
        w = csv.writer(f)
        w.writerow(["id", "message", "language", "stage", "state", "context", "heuristic_tags", "expected_action", "expected_args", "notes"])
        for r in rows:
            ctx = "\n".join(f"{c['role']}: {c['text']}" for c in r["context"])
            w.writerow([r["id"], r["message"], r["language"], r["state"]["stage"],
                        "; ".join(f"{k}={v}" for k, v in r["state"].items() if k != "stage" and v not in (None, False)),
                        ctx, ",".join(r["heuristic_tags"]), "", "", ""])
    langs = defaultdict(int)
    for r in rows:
        langs[r["language"]] += 1
    print(f"{len(candidates)} eligible messages -> sampled {len(rows)} | languages: {dict(langs)}")
    print(f"wrote {DATASET_PATH.name}, {CSV_PATH.name}, {CATALOGUE_PATH.name} ({len(_snapshot(products))} products)")


if __name__ == "__main__":
    asyncio.run(main())
