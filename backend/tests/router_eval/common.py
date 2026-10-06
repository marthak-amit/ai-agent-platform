"""Shared helpers for the offline router-eval harness (no live-pipeline imports beyond config/language)."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from app.schemas.router import ROUTER_ACTIONS
from app.services import intent_router

HERE = Path(__file__).parent
DATASET_PATH = HERE / "dataset.jsonl"
CSV_PATH = HERE / "dataset.csv"
CATALOGUE_PATH = HERE / "catalogue_snapshot.json"

GOLDEN_PATH = HERE / "golden_router.jsonl"

# The production action list (app/schemas/router.py) — the eval never keeps its own copy.
ACTIONS = list(ROUTER_ACTIONS)
# The one arg per action that is compared when an expected value is provided.
PRIMARY_ARG = {
    "show_product": "sku", "start_order": "sku", "answer_slot": "value", "change_slot": "value",
    "search_catalog": "query", "faq": "query", "order_status": "focus",
}
# Free-text args: shown in the report but never scored.
UNSCORED_ARGS = {"query", "question"}

# dataset language names → the router's four codes
LANGUAGE_CODE = {
    "english": "en", "hindi_roman": "hi", "hindi_devanagari": "hi", "gujarati_roman": "gu",
    "gujarati_script": "gu", "hinglish": "hinglish",
}


# ── production prompt context for a dataset row ──────────────────────────────

def _snapshot_product(p: dict) -> SimpleNamespace:
    """Catalogue-snapshot dict → an object shaped like a Product row (variants carry the in-stock colours/sizes)."""
    variants = [SimpleNamespace(color=c, size=None, material=None, stock=1, is_active=True) for c in p.get("colors") or []]
    variants += [SimpleNamespace(color=None, size=s, material=None, stock=1, is_active=True) for s in p.get("sizes") or []]
    return SimpleNamespace(
        id=p["sku"], sku=p["sku"], name=p["name"], price=p["price"], category=p.get("category"),
        description=p.get("description"), is_active=True, stock=1 if p.get("in_stock", True) else 0,
        has_variants=bool(variants), variants=variants,
    )


def eval_context(row: dict, catalogue: list[dict]) -> "intent_router.RouterContext":
    """
    The router context for a dataset row, built with the SAME production functions the live pipeline uses
    (fuzzy candidates, state block, history redaction), so the prompt text is identical to production.

    Rows may carry optional state keys: next_slot, slot_options, orders (list of prompt lines).
    """
    products = [_snapshot_product(p) for p in catalogue]
    state = row.get("state") or {}
    pinned = next((p for p in products if p.sku == state.get("pending_product_sku")), None)
    conv = SimpleNamespace(
        selected_color=state.get("selected_color"), selected_size=state.get("selected_size"),
        selected_material=None, pending_order_quantity=state.get("quantity"), payment_method=None,
        customer_name="x" if state.get("has_customer_name") else None,
        delivery_address="x" if state.get("has_address") else None, mobile_number=None,
        summary_shown=bool(state.get("summary_shown")), pending_choice_skus=state.get("pending_choice_skus"),
        last_customer_language=row.get("language"),
    )
    next_slot = state.get("next_slot")
    vi = {"available_" + k: v for k, v in (state.get("slot_options") or {}).items()}
    candidates = intent_router.pick_candidates(products, row["message"], pinned)
    history = [SimpleNamespace(role="user" if m["role"] == "customer" else "assistant", content=m["text"])
               for m in row.get("context", [])]
    client = SimpleNamespace(business_name="Test Store", delivery_days_min=3, delivery_days_max=7,
                             accepts_cod=True, accepts_upi=True, upi_id="x@upi")
    return intent_router.RouterContext(
        shop=intent_router._shop_block(client, conv, None),
        state=intent_router._state_block(conv, state.get("stage", "greeting"), pinned, vi, next_slot),
        orders="\n".join(state.get("orders") or []) or "(none)",
        candidates="\n".join(intent_router.format_candidate(p) for p in candidates)
        or "(no close catalogue matches for this message)",
        history=intent_router._history_block(history),
        stage=state.get("stage", "greeting"), next_slot=next_slot, pinned_sku=getattr(pinned, "sku", None),
        candidate_products=candidates,
        allowed_skus={p.sku.upper() for p in candidates} | ({pinned.sku.upper()} if pinned else set()),
    )


# ── anonymisation ────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,13}\d(?!\d)")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_UPI_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")
_PIN_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# Bot messages that echo the saved address: "📍 <address>" lines and "deliver to: <address>?".
_ADDR_LINE_RE = re.compile(r"^(\s*[📍🏠🏡]\s*).+$", re.MULTILINE)
_DELIVER_TO_RE = re.compile(r"(deliver to:\s*).+?(\?)", re.IGNORECASE)


def build_name_pattern(names: set[str]) -> re.Pattern | None:
    """Compile a case-insensitive pattern matching any name token of 3+ letters."""
    tokens = {t for n in names for t in re.findall(r"\w+", n or "") if len(t) >= 3}
    if not tokens:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def anonymize(text: str, name_re: re.Pattern | None, address: str | None = None) -> str:
    """Mask phones, URLs, emails/UPI ids, pincodes, the customer's name and saved address."""
    if not text:
        return text
    if address and len(address) > 8:
        text = text.replace(address, "<ADDRESS>")
    text = _ADDR_LINE_RE.sub(r"\1<ADDRESS>", text)
    text = _DELIVER_TO_RE.sub(r"\1<ADDRESS>\2", text)
    text = _URL_RE.sub("<URL>", text)
    text = _EMAIL_UPI_RE.sub("<EMAIL_OR_UPI>", text)
    text = _PHONE_RE.sub("<PHONE>", text)
    text = _PIN_RE.sub("<PIN>", text)
    if name_re:
        text = name_re.sub("<NAME>", text)
    return text


# ── fuzzy catalogue matching ─────────────────────────────────────────────────

def _tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens (Roman script only; other scripts never match)."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def top_catalogue_matches(message: str, catalogue: list[dict], k: int = 5) -> list[dict]:
    """
    Top-k products for `message` by fuzzy token match against name/category/sku/description.

    Each message token (3+ chars) scores its best difflib ratio (>=0.78) against the
    product's tokens; name hits weigh 2x, an exact SKU mention adds a large bonus.
    Returns [] when nothing scores, so the prompt can say "no close matches".
    """
    msg_tokens = [t for t in set(_tokens(message)) if len(t) >= 3]
    scored: list[tuple[float, dict]] = []
    for p in catalogue:
        name_t, cat_t = set(_tokens(p["name"])), set(_tokens(p.get("category") or ""))
        desc_t = set(_tokens(p.get("description") or ""))
        score = 0.0
        for t in msg_tokens:
            for pool, weight in ((name_t, 2.0), (cat_t, 1.5), (desc_t, 0.5)):
                best = max((difflib.SequenceMatcher(None, t, c).ratio() for c in pool), default=0.0)
                if best >= 0.78:
                    score += weight * best
        if p.get("sku") and p["sku"].lower() in (message or "").lower():
            score += 10
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:k]]


def format_catalogue_snippet(matches: list[dict]) -> str:
    """Render matches for the prompt: name, SKU, price, category, in-stock colours/sizes (no stock counts)."""
    if not matches:
        return "(no close catalogue matches for this message)"
    lines = []
    for p in matches:
        line = f"- {p['name']} | sku={p.get('sku') or 'n/a'} | ₹{p['price']:,.0f} | {p.get('category') or 'uncategorised'}"
        if p.get("colors"):
            line += f" | colors in stock: {', '.join(p['colors'])}"
        if p.get("sizes"):
            line += f" | sizes in stock: {', '.join(p['sizes'])}"
        if not p.get("in_stock", True):
            line += " | OUT OF STOCK"
        lines.append(line)
    return "\n".join(lines)


# ── dataset io ───────────────────────────────────────────────────────────────

def load_dataset(path: Path = DATASET_PATH) -> list[dict]:
    """Read dataset.jsonl into a list of row dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_dataset(rows: list[dict], path: Path = DATASET_PATH) -> None:
    """Write rows to dataset.jsonl (one JSON object per line, UTF-8, no ASCII escaping)."""
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def parse_expected_args(raw: str, action: str) -> dict | None:
    """
    Parse the spreadsheet `expected_args` cell.

    Accepts JSON (`{"sku": "KUR-001"}`), `key=value;key2=value2`, or a bare value
    that is assigned to the action's primary arg (e.g. `KUR-001` for show_product).
    Blank -> None (args are then not scored for that row).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    if "=" in raw:
        return {k.strip(): v.strip() for k, v in (part.split("=", 1) for part in raw.split(";") if "=" in part)}
    key = PRIMARY_ARG.get(action)
    return {key: raw} if key else None
