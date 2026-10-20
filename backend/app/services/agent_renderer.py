"""
Agent v3 — the renderer: every word the customer reads comes from here, built from templates and database rows.

Nothing the planner wrote reaches a customer except a ``sanitize_question``-approved clarify sentence. Prices, totals,
stock, delivery estimates and order numbers are formatted from real data, and every amount shown is recorded in
``Reply.amounts`` so the invariants can prove no other number slipped in.

Languages: English (``_EN`` below) plus Hinglish / Hindi / Gujarati (Roman and native script) from the pre-translated tables in
``app/services/agent_templates/*.json`` (every key, validated at startup) — those replies are complete as rendered and skip the LLM
localizer (``Reply.localized``). For any other language the English text goes out and ``reply_localizer`` translates it — digits,
prices, SKUs and URLs survive that step. Dynamic free text (the planner's clarify question, a knowledge-base answer) is the only
thing in the table languages that still needs the localizer.
Example strings ("e.g. 2 Red, 1 Green") are generated from the product's real variants, never hardcoded.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.services import agent_executor as ex
from app.services import agent_fast_paths, agent_templates, indic_vocab, language_service, wa_labels
from app.services.agent_cart import CartData
from app.services.agent_catalog import Catalogue, ProductView
from app.services.delivery_service import order_delivery_days
from app.services.language_templates import format_price

logger = logging.getLogger("app.services.agent_renderer")

# ── templates ────────────────────────────────────────────────────────────────

_TABLES: dict[str, dict[str, str]] = dict(agent_templates.load_all())      # english / hindi_roman / hindi_devanagari / gujarati_roman / gujarati_script
_EN: dict[str, str] = _TABLES["english"]
TABLE_LANGUAGES = frozenset(_TABLES)
_NUMERIC_QUESTIONS = frozenset({"quantity", "qty_confirm"})        # questions a bare number answers        # languages whose replies are complete without the LLM localizer


def text_for(lang: str, key: str, **kw: Any) -> str:
    """A static template in `lang` (English when the language has no table) — for callers outside the renderer."""
    text = _TABLES.get(lang, _EN).get(key) or _EN[key]
    return text.format(**kw) if kw else text


# ── reply ────────────────────────────────────────────────────────────────────

@dataclass
class Reply:
    """A rendered turn: text + optional buttons / list / images, plus what the invariants and the state need to know."""

    text: str
    buttons: list[tuple[str, str]] | None = None            # (id, title)
    list_rows: list[tuple[str, str, str | None]] | None = None   # (id, title, description)
    list_header: str | None = None
    list_button_text: str | None = None
    images: list[tuple[str, str]] | None = None
    image_fallback_text: str | None = None
    terminal: bool = False                                   # order placed / handed over: need not end with a question
    amounts: set[int] = field(default_factory=set)           # every ₹ amount this reply states
    lists_options: bool = False                              # real options / alternatives are shown
    last_clarify: dict[str, Any] | None = None               # persisted so a bare number / option name can be resolved
    offered_upsell: str | None = None
    mentions_unavailable: bool = False
    unavailable_sku: str | None = None                       # the product that is not available — alternatives are chosen from ITS category
    summary_shown: bool = False                              # the order summary is on screen after this reply
    resume_greeting: bool = False                            # a welcome-back: cart stage / open question stay as they were
    localized: bool = False                                  # complete in the customer's language already: the LLM localizer is skipped

    def append(self, extra: str) -> None:
        """Add a paragraph."""
        self.text = f"{self.text}\n\n{extra}".strip()


class Renderer:
    """Builds the customer-facing text for one turn. Stateless apart from the amounts it records."""

    def __init__(
        self, lang: str, client, catalogue: Catalogue, cart: CartData, *, is_whatsapp: bool = True, idle_hours: float | None = None,
    ) -> None:
        """Bind the language, shop and catalogue; `cart` is the cart AFTER this turn's actions, `idle_hours` how long it sat untouched."""
        self.idle_hours = idle_hours
        self.violations: list[str] = []              # e.g. "lang_mismatch" (planner text in the wrong language, replaced by a template)
        self.lang = lang if lang in _TABLES else "english"
        self.table_language = lang in _TABLES        # False: English text goes out and the localizer translates it
        self.dynamic = False                         # free text that is not in a table (planner question, KB answer) went into the reply
        self.client, self.catalogue, self.cart, self.is_whatsapp = client, catalogue, cart, is_whatsapp
        self.amounts: set[int] = set()

    # ── helpers ──────────────────────────────────────────────────────────────
    def t(self, key: str, **kw: Any) -> str:
        """Template `key` in the customer's language (English when that language has no hand-written text)."""
        text = _TABLES[self.lang].get(key) or _EN[key]
        return text.format(**kw) if kw else text

    def money(self, amount: float) -> str:
        """Format a price and record it as a legitimately stated amount."""
        self.amounts.add(int(round(amount)))
        return format_price(amount)

    @property
    def shop(self) -> str:
        """The shop's display name."""
        return getattr(self.client, "business_name", None) or "our shop"

    def eta(self, views: list[ProductView]) -> str:
        """Delivery estimate from the single source (client settings, product override for the slowest line)."""
        lo, hi = order_delivery_days(self.client, [getattr(v.product, "delivery_days", None) for v in views] or None)
        if lo != hi:
            return self.t("eta_range", lo=lo, hi=hi)
        return self.t("eta_day" if lo == 1 else "eta_days", n=lo)

    @staticmethod
    def variant_label(line: dict[str, Any]) -> str:
        """'Red / M' (empty when no variant chosen yet)."""
        return " / ".join(str(line[k]) for k in ("color", "size", "material") if line.get(k))

    def line_name(self, line: dict[str, Any], *, with_qty: bool = False) -> str:
        """'Cotton Kurti (Red / M)' (+ '2× ' prefix)."""
        view = self.catalogue.get(line["sku"])
        name = view.name if view else line["sku"]
        label = self.variant_label(line)
        out = f"{name} ({label})" if label else name
        return f"{line['qty']}× {out}" if with_qty and line.get("qty") else out

    @staticmethod
    def join_options(options: list[str]) -> str:
        """'Red, Green, Pink'."""
        return ", ".join(options)

    def for_what(self, gap_sku: str | None, line: dict[str, Any] | None = None) -> str:
        """' for Cotton Kurti (Red)' — the product a question is about (empty when unknown / single obvious)."""
        view = self.catalogue.get(gap_sku) if gap_sku else None
        if view is None:
            return ""
        label = self.variant_label(line) if line else ""
        return self.t("for_what_variant", name=view.name, label=label) if label else self.t("for_what", name=view.name)

    # ── option widgets ───────────────────────────────────────────────────────
    def options_widget(
        self, reply: Reply, options: list[str], *, ids: list[str] | None = None, slot: str | None = None, line_id: str | None = None,
        sku: str | None = None, combos: list[dict[str, str]] | None = None,
    ) -> None:
        """
        Attach the options as buttons (≤3) or a list (≤10); the text already names them, so every channel works.

        Ids are structured (see ``agent_fast_paths``): the id alone says which line, slot and value was tapped, so a tap resolves
        without the "which list was on screen" state and never from the (shortened) title. Titles are fitted to WhatsApp's limits in
        ``render``.
        """
        if not options or not self.is_whatsapp:
            if options:
                reply.lists_options = True
            return
        ids = ids or self._option_ids(options, slot, line_id, sku, combos)
        reply.lists_options = True
        if len(options) <= 3:
            reply.buttons = [(i, o) for i, o in zip(ids, options)]
        elif len(options) <= 10:
            reply.list_rows = [(i, o, None) for i, o in zip(ids, options)]
            reply.list_header = self.t("list_choose")
            reply.list_button_text = self.t("list_view_options")

    @staticmethod
    def _option_ids(options: list[str], slot: str | None, line_id: str | None, sku: str | None, combos: list[dict[str, str]] | None) -> list[str]:
        """Structured id per option where the question has an owner (line / product), else the positional ``v3_opt_N`` form."""
        out: list[str] = []
        for n, opt in enumerate(options, start=1):
            sid = None
            if slot in ("color", "size", "material") and line_id:
                sid = agent_fast_paths.option_id(line_id, slot, opt)
            elif slot == "variant" and combos and n <= len(combos) and sku:
                sid = agent_fast_paths.variant_id(line_id, sku, combos[n - 1])
            out.append(sid or f"v3_opt_{n}")
        return out

    # ── acknowledgements ─────────────────────────────────────────────────────
    def ack_lines(self, applied: list[ex.Applied]) -> list[str]:
        """One short factual line per change that really happened (max 5)."""
        lines: list[str] = []
        for ap in applied:
            if ap.kind == "added" and ap.line:
                lines.append(self.t("ack_added", what=self.line_name(ap.line, with_qty=True)))
            elif ap.kind == "updated" and ap.line and ap.before:
                changes = []
                for f in ap.fields:
                    if f in ("line_id", "sku"):
                        continue
                    old, new = ap.before.get(f), ap.line.get(f)
                    label = self.t(f"word_{f}") if f in ("color", "size", "material") else self.t("word_qty")
                    changes.append(f"{label} {old} → {new}" if old else f"{label} {new}")
                if changes:
                    lines.append(self.t("ack_updated", what=self.line_name({**ap.line, "color": None, "size": None, "material": None, "qty": None}), changes=", ".join(changes)))
            elif ap.kind == "removed" and ap.line:
                same = [x for x in applied if x.kind == "removed" and x.line and x.line["sku"] == ap.line["sku"]]
                if len(same) == 1:
                    lines.append(self.t("ack_removed", what=self.line_name(ap.line)))
                elif same[0] is ap:             # several lines of one product are ONE "removed <product>"
                    view = self.catalogue.get(ap.line["sku"])
                    lines.append(self.t("ack_removed", what=view.name if view else ap.line["sku"]))
            elif ap.kind == "split" and ap.before:
                parts = ", ".join(f"{p['qty']}× {self.variant_label(p) or '—'}" for p in ap.parts)
                lines.append(self.t("ack_split", what=self.line_name({**ap.before, "color": None, "size": None, "material": None, "qty": None}), parts=parts))
            elif ap.kind == "customer":
                lines.append(self.t("ack_customer", fields=", ".join(self.t(f"field_{f}") for f in ap.fields)))
            elif ap.kind == "payment" and self.cart.payment_method:
                lines.append(self.t("ack_payment", method=self.payment_label(self.cart.payment_method)))
            elif ap.kind == "autofilled" and ap.line:
                for f in ap.fields:
                    lines.append(self.t("ack_autofill", value=ap.line[f], for_what=self.for_what(ap.line["sku"], None)))
            elif ap.kind == "dropped" and ap.line:
                lines.append(self.t("ack_dropped", what=ap.line["sku"]))
        if len(lines) > 5:
            lines = lines[:5] + [self.t("ack_more", n=len(lines) - 5)]
        return lines

    def payment_label(self, method: str | None) -> str:
        """'Cash on Delivery' / 'UPI'."""
        return self.t("pay_cod") if method == "COD" else self.t("pay_upi") if method == "UPI" else str(method or "")

    # ── products ─────────────────────────────────────────────────────────────
    def product_card(self, view: ProductView, reply: Reply) -> str:
        """Name, price, real in-stock options and delivery estimate of one product (or the sold-out explanation)."""
        if not view.in_stock():
            return self.t("sold_out", name=view.name)
        prices = {self.catalogue_variant_price(view, v) for v in view.variants} or {view.price}
        lines = [view.name]
        lines.append(self.t("card_price_from" if len(prices) > 1 else "card_price", price=self.money(min(prices))))
        desc = " ".join((getattr(view.product, "description", None) or "").split())
        if desc:
            lines.append(desc[:140] + ("…" if len(desc) > 140 else ""))
        for attr in view.needed_attrs:
            values = view.values(attr)
            if values:
                lines.append(self.t("card_options", label=self.t(f"attr_{attr}"), values=self.join_options(values)))
        lines.append(self.t("card_delivery", eta=self.eta([view])))
        lines.append(self.t("card_code", sku=view.sku))
        return "\n".join(lines)

    @staticmethod
    def catalogue_variant_price(view: ProductView, variant) -> float:
        """A variant's price (its own, else the product's)."""
        return float(variant.price) if variant.price else view.price

    def product_lines(self, views: list[ProductView]) -> list[str]:
        """'1. Name — ₹x (code SKU)' lines."""
        return [
            self.t("list_line", i=i, name=v.name, price=self.money(v.price), sku=v.sku)
            for i, v in enumerate(views, start=1)
        ]

    def product_list_widget(self, reply: Reply, views: list[ProductView]) -> None:
        """Rows to tap (WhatsApp) + the numbered-choice memory for "2" / a SKU typed back."""
        if (reply.last_clarify or {}).get("slot") in _NUMERIC_QUESTIONS:
            # A quantity (or quantity-confirmation) question is open in THIS reply: a numbered product list next to it would make a bare "1"
            # ambiguous (conv=60: "1" meant 1 piece and opened product 1). One numeric question per message.
            logger.info("INVARIANT one_numeric_question — product list not attached to an open %s question", reply.last_clarify["slot"])
            return
        reply.last_clarify = {"slot": "product", "options": [v.sku for v in views], "line_id": None, "sku": None}
        reply.lists_options = True
        if self.is_whatsapp and 2 <= len(views) <= 10:
            reply.list_rows = [(f"v3_sku_{v.sku.lower()}", v.name, format_price(v.price)) for v in views]
            reply.list_header = self.shop
            reply.list_button_text = self.t("list_view_products")
            self.amounts.update(int(round(v.price)) for v in views)

    def alternatives(self, exclude: set[str], like: ProductView | None = None, limit: int = 4, *, same_category_only: bool = False) -> list[ProductView]:
        """
        In-stock products to suggest instead: same category as `like` first. ``same_category_only`` keeps ONLY that category (nothing when
        `like` has none) — a kurti that is out of stock is never answered with "here is the first of the catalogue".
        """
        pool = [v for v in self.catalogue.views if v.in_stock() and v.sku not in exclude]
        category = ((like.product.category or "").lower() if like is not None else "")
        if same_category_only:
            return [v for v in pool if category and (v.product.category or "").lower() == category][:limit]
        if like is not None and like.product.category:
            pool.sort(key=lambda v: (v.product.category or "").lower() != category)
        return pool[:limit]

    # ── clarify ──────────────────────────────────────────────────────────────
    def clarify(self, c: ex.Clarify, reply: Reply, parts: list[str]) -> None:
        """The question (and, when something was unavailable, WHY plus what is available) — options always real."""
        d = c.details
        view = self.catalogue.get(c.sku or d.get("sku"))
        line = self.cart.line(c.line_id) if c.line_id else None
        what = self.for_what(c.sku or d.get("sku"), line)
        product = d.get("product") or (view.name if view else "")
        lead: str | None = None
        reason = c.reason
        if reason == "INVALID_VARIANT":
            lead = self.t("r_INVALID_VARIANT", product=product, value=d.get("value"))
        elif reason == "OUT_OF_STOCK_VARIANT":
            lead = self.t("r_OUT_OF_STOCK_VARIANT", product=product, value=d.get("value"))
            reply.mentions_unavailable = True
            reply.unavailable_sku = view.sku if view else None
        elif reason == "AMBIGUOUS_VARIANT":
            lead = self.t("r_AMBIGUOUS_VARIANT", value=d.get("value"), attrs=self.t(f"words_{d.get('attr', 'color')}"))
        elif reason == "QTY_EXCEEDS_STOCK":
            avail = int(d.get("available", 0))
            variant = {k[len("variant_"):]: v for k, v in d.items() if k.startswith("variant_")}
            lead = self.t("r_QTY_EXCEEDS_STOCK_one" if avail == 1 else "r_QTY_EXCEEDS_STOCK_many", available=avail, what=self.line_name({"sku": c.sku or d.get("sku") or "", **variant}))
            reply.mentions_unavailable = True
        elif reason == "QTY_MIN":
            lead = self.t("r_QTY_MIN")
        elif reason == "QTY_MISMATCH":
            lead = self.t("r_QTY_MISMATCH", got=d.get("got"), expected=d.get("expected"))
        elif reason == "UNKNOWN_SKU":
            lead = self.t("r_UNKNOWN_SKU")
        elif reason == "INVALID_NAME":
            lead = self.t("r_INVALID_NAME")
        elif reason == "INVALID_ADDRESS":
            lead = self.t(f"r_INVALID_ADDRESS_{d.get('reason') or 'too_short'}")
        elif reason == "INVALID_MOBILE":
            lead = self.t("r_INVALID_MOBILE")
        elif reason == "PAYMENT_NOT_ACCEPTED":
            lead = self.t("r_PAYMENT_NOT_ACCEPTED")
        if lead:
            parts.append(lead)

        slot, options = c.slot, list(c.options)
        reply.last_clarify = {"slot": slot, "options": options, "line_id": c.line_id, "sku": c.sku}
        if slot in ("color", "size", "material"):
            if options:
                parts.append(self.t(f"q_{slot}", for_what=what, options=self.join_options(options)))
                self.options_widget(reply, options, slot=slot, line_id=c.line_id, sku=c.sku)
            else:                                    # every value is gone: never leave "not available" without a way forward
                self._alternatives_block(parts, reply, view, exclude={view.sku} if view else set())
        elif slot == "quantity":
            if reason == "QTY_EXCEEDS_STOCK" and d.get("available"):
                avail = int(d["available"])
                reply.lists_options = True                  # the stock answer is complete: never followed by "here is what else we have"
                if self.is_whatsapp and c.line_id:
                    parts.append(self.t("q_qty_stock_choice", available=avail))
                    reply.buttons = [
                        (f"v3_qtytake:{c.line_id}:{avail}", self.t("btn_qty_take", n=avail)),
                        (agent_fast_paths.remove_id(c.line_id), self.t("btn_qty_remove")),
                        (f"v3_similar:{c.sku or d.get('sku') or ''}", self.t("btn_qty_similar")),
                    ]
                else:
                    parts.append(self.t("q_quantity_max", for_what=what, available=avail))
            else:
                parts.append(self.t("q_quantity", for_what=what))
        elif slot == "qty_confirm":
            n = int(d.get("requested", 0))
            reply.last_clarify = {"slot": "qty_confirm", "options": [], "line_id": c.line_id, "sku": c.sku, "qty": n}
            parts.append(self.t("q_qty_confirm", n=n, what=what.strip() or product))
            reply.lists_options = True
            if self.is_whatsapp and c.line_id:
                reply.buttons = [(f"v3_qtyok:{c.line_id}:{n}", self.t("btn_qty_yes", n=n)), ("v3_qtyno:" + c.line_id, self.t("btn_qty_no"))]
        elif slot == "variant":
            parts.append(self.t("q_variant", for_what=what, options=self.join_options(options)))
            reply.last_clarify["combos"] = d.get("combos")
            self.options_widget(reply, options, slot="variant", line_id=c.line_id, sku=c.sku or d.get("sku"), combos=d.get("combos"))
        elif slot == "split":
            named = d.get("named") or []          # only options the customer wrote themselves are ever quoted back
            parts.append(self.t("q_split_named", options=self.join_options(named)) if named else self.t("q_split_plain"))
        elif slot == "payment":
            methods = options or ex.payment_options(self.client)
            reply.last_clarify["options"] = methods
            if methods:
                parts.append(self.t("q_payment", options=self.join_options([self.payment_label(m) for m in methods])))
                if self.is_whatsapp:
                    reply.buttons = [("v3_pay_cod" if m == "COD" else "v3_pay_upi", self.t("btn_pay_cod" if m == "COD" else "btn_pay_upi")) for m in methods]
                reply.lists_options = True
            else:
                parts.append(self.t("r_UPI_NOT_CONFIGURED"))
        elif slot == "product":
            candidates = [v for v in (self.catalogue.get(sku) for sku in c.options) if v is not None]
            if reason in ("OUT_OF_STOCK", "UNKNOWN_SKU") or view is not None:
                # A SPECIFIC product is not available: say so and offer its own category (never the first of the catalogue).
                self._alternatives_block(parts, reply, view, exclude={view.sku} if view else set(), sold_out=reason in ("OUT_OF_STOCK",), product=product,
                                         same_category_only=True)
            elif candidates:
                # The planner is asking "which of these?" with products it found for the customer's words — only those.
                parts.append(self.t("alts_header") + "\n" + "\n".join(self.product_lines(candidates[:8])))
                parts.append(self.t("alts_cta"))
                self.product_list_widget(reply, candidates[:8])
            else:
                # A bare "which product?" with nothing to anchor it: ask, don't dump the catalogue (conv=60: "These are available: Chanderi, Cotton, Crepe…"
                # after a quantity message). A catalogue list belongs to browse / search intents only.
                logger.info("INVARIANT clarify_no_catalogue_dump — product clarify without a product or candidates: plain question")
                parts.append(self.t("cta_browse"))
        elif slot == "cancel":
            number = d.get("number")
            parts.append(self.t("q_cancel_order", number=number) if number else self.t("q_cancel"))
            if self.is_whatsapp:
                reply.buttons = [("v3_cancel_yes", self.t("btn_yes_cancel")), ("v3_cancel_no", self.t("btn_no_keep"))]
        elif slot in ("name", "address", "mobile"):
            parts.append(self.t(f"q_{slot}"))
        elif slot == "change":
            parts.append(self.t("q_change"))
        elif slot == "remove":
            self._remove_question(c, reply, parts)
        else:
            question = c.question if self._question_in_language(c.question) else None
            if question:
                self.dynamic = True         # the planner's own sentence (sanitised): only the localizer can put it in the customer's language
            parts.append(question or self.t("q_other"))
            if options:
                parts.append(self.t("options_line", options=self.join_options(options)))
                self.options_widget(reply, options)

    def _category_empty_block(self, parts: list[str], reply: Reply, category: str) -> str:
        """"Sorry, <category> is sold out right now." + what IS in stock; returns the closing question that goes with it."""
        parts.append(self.t("category_empty", cat=category))
        reply.mentions_unavailable = True
        alts = self.alternatives(set())
        if not alts:
            return self.t("browse_cta")
        parts.append(self.t("alts_header") + "\n" + "\n".join(self.product_lines(alts)))
        self.product_list_widget(reply, alts)
        return self.t("alts_cta")

    def _question_in_language(self, question: str | None) -> bool:
        """
        Is the planner's free-text question written in the customer's language? (script for native-script languages, the keyword
        detector for Roman ones.) A mismatch is logged as ``lang_mismatch`` and the caller uses the template instead.
        Languages without a table are translated by the localizer later, so they are not judged here. No text = nothing to judge.
        """
        if not question or not self.table_language:
            return bool(question)
        ok = language_service.reply_matches_language(question, self.lang)
        if ok and language_service.expected_script(self.lang) is None:
            found = language_service.detect(question)
            ok = not (found.confident and found.lang != self.lang and found.lang in ("english", "hindi_roman", "gujarati_roman", "marathi_roman"))
        if not ok:
            self.violations.append("lang_mismatch")
            logger.warning("INVARIANT lang_mismatch planner question %r is not %s — template used instead", question[:80], self.lang)
        return ok

    def _remove_question(self, c: ex.Clarify, reply: Reply, parts: list[str]) -> None:
        """
        "Which one should I remove?" — the candidate lines numbered in the text and (WhatsApp) as LIST rows that carry the line id.

        Always a list, never buttons: several lines of one product share a name, and button titles (≤ 20 characters) cannot tell them
        apart. A row's title is the product + variant (made unique by ``fit_labels``), its description the full name, variant and quantity.
        """
        lines = [ln for ln in (self.cart.line(lid) for lid in c.options) if ln]
        reply.last_clarify = {"slot": "remove", "options": [ln["line_id"] for ln in lines], "line_id": None, "sku": None}
        names = [self.line_name(ln) for ln in lines]
        parts.append(self.t("q_remove") + "\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names, start=1)))
        reply.lists_options = True
        if self.is_whatsapp and lines:
            rows = []
            for ln in lines:
                view = self.catalogue.get(ln["sku"])
                label = self.variant_label(ln)
                title = f"{view.name if view else ln['sku']} – {label}" if label else (view.name if view else ln["sku"])
                rows.append((agent_fast_paths.remove_id(ln["line_id"]), title, self.line_name(ln, with_qty=True)))
            reply.list_rows = rows
            reply.list_header = self.t("list_choose")
            reply.list_button_text = self.t("list_view_options")

    def cart_block(self) -> str:
        """"🛒 In your cart now:" + one line per item (no prices) — shown after something was removed and the order goes on."""
        rows = []
        for i, ln in enumerate(self.cart.lines, start=1):
            view = self.catalogue.get(ln["sku"])
            variant = f" ({self.variant_label(ln)})" if self.variant_label(ln) else ""
            key = "cart_line" if ln.get("qty") else "cart_line_open"
            rows.append(self.t(key, i=i, name=view.name if view else ln["sku"], variant=variant, qty=ln.get("qty")))
        return self.t("cart_now") + "\n" + "\n".join(rows)

    def _alternatives_block(
        self, parts: list[str], reply: Reply, like: ProductView | None, *, exclude: set[str], sold_out: bool = True, product: str = "",
        same_category_only: bool = False,
    ) -> None:
        """'Sorry X is sold out' + in-stock alternatives (numbered, tappable)."""
        if like is not None:
            reply.unavailable_sku = like.sku
        if sold_out and like is not None:
            parts.append(self.t("sold_out", name=like.name))
            reply.mentions_unavailable = True
        elif sold_out and product:
            parts.append(self.t("sold_out", name=product))
            reply.mentions_unavailable = True
        alts = self.alternatives(exclude, like, same_category_only=same_category_only and like is not None)
        if alts:
            parts.append(self.t("alts_header") + "\n" + "\n".join(self.product_lines(alts)))
            parts.append(self.t("alts_cta"))
            self.product_list_widget(reply, alts)
        else:
            parts.append(self.t("cta_browse"))

    # ── summary ──────────────────────────────────────────────────────────────
    def pick_upsell(self, subtotal: float) -> ProductView | None:
        """
        One in-stock product worth at most `agent_upsell_max_cart_share` of the cart.

        Deterministic per cart: the pick made for this cart (``state.upsell``) is shown again every time its summary is shown,
        and a new pick is drawn from the best three candidates with a generator seeded by the cart id. Never a product the
        customer already viewed and left, removed, or was offered for an EARLIER cart (not taken = declined).
        """
        cap = subtotal * get_settings().agent_upsell_max_cart_share
        in_cart = set(self.cart.skus)
        current = self.cart.state.get("upsell") or {}
        if current.get("cart_id") == self.cart.id:
            kept = self.catalogue.get(current.get("sku"))
            if kept is not None and kept.sku not in in_cart and kept.in_stock() and 0 < kept.price <= cap:
                return kept
        excluded = in_cart | set(self.cart.memory("viewed_skus")) | set(self.cart.memory("removed_skus")) | set(self.cart.memory("offered_upsell"))
        categories = {(v.product.category or "").lower() for v in (self.catalogue.get(s) for s in in_cart) if v}
        pool = [v for v in self.catalogue.views if v.sku not in excluded and v.in_stock() and v.price <= cap and v.price > 0]
        pool.sort(key=lambda v: ((v.product.category or "").lower() not in categories, -v.price, v.sku))
        top = pool[:3]
        return random.Random(f"upsell:{self.cart.id}").choice(top) if top else None

    def upsell_allowed(self, subtotal: float) -> bool:
        """
        May this summary carry an upsell at all? At most ONE offer per order cycle (a cart): once it was shown — taken, ignored or
        removed again — later summaries of the same cart stay clean. Never on a big cart (``agent_upsell_max_cart_total``, or the
        client's own ``upsell_max_cart_total`` when it has one): somebody spending that much is not nudged for ₹250 more.
        """
        if self.cart.state.get("upsell_done"):
            return False
        cap = getattr(self.client, "upsell_max_cart_total", None)
        cap = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else get_settings().agent_upsell_max_cart_total
        return subtotal <= cap

    def upsell_label(self, view: ProductView) -> str:
        """
        "+ Chanderi ₹3,199": a short nickname + the price, ≤ 20 characters (a WhatsApp reply button).

        The nickname is the first words of the product name that are not just its category ("Chanderi Silk Saree" → "Chanderi"),
        so two upsells never both read "Add Cotton…".
        """
        price = self.money(view.price)
        budget = wa_labels.BUTTON_MAX - len("+ ") - 1 - len(price)
        generic = {w.lower().rstrip("s") for w in indic_vocab.tokenize(view.product.category or "")} | _GENERIC_NAME_WORDS
        words = view.name.split()
        distinct = [w for w in words if w.lower().rstrip("s") not in generic] or words
        nick = ""
        for w in distinct:
            candidate = f"{nick} {w}".strip()
            if len(candidate) > budget:
                break
            nick = candidate
        nick = nick or wa_labels.fit(distinct[0] if distinct else view.name, budget)
        return f"+ {nick} {price}"

    def summary(self, reply: Reply, parts: list[str]) -> None:
        """Per-line order summary, delivery estimate, payment and customer details, then the confirmation question."""
        lines = [self.t("sum_header")]
        total = 0.0
        views: list[ProductView] = []
        for i, ln in enumerate(self.cart.lines, start=1):
            view = self.catalogue.get(ln["sku"])
            if view is None:
                continue
            views.append(view)
            unit = view.unit_price(ex.line_fixed(view, ln))
            subtotal = unit * int(ln["qty"])
            total += subtotal
            variant = f" ({self.variant_label(ln)})" if self.variant_label(ln) else ""
            lines.append(self.t("sum_line", i=i, name=view.name, variant=variant, qty=ln["qty"], subtotal=self.money(subtotal)))
        lines.append(self.t("sum_total", total=self.money(total)))
        lines.append(self.t("sum_delivery", eta=self.eta(views)))
        method = ex.effective_payment(self.cart, self.client)
        if method:
            lines.append(self.t("sum_payment", method=self.payment_label(method)))
        c = self.cart.customer
        for key in ("name", "address", "mobile"):
            if c.get(key):
                lines.append(self.t(f"sum_{key}", value=c[key]))
        upsell = self.pick_upsell(total) if self.upsell_allowed(total) else None
        if upsell is not None:
            lines.append("")
            lines.append(self.t("sum_upsell", name=upsell.name, price=self.money(upsell.price)))
            reply.offered_upsell = upsell.sku
        lines.append("")
        lines.append(self.t("sum_cta"))
        parts.append("\n".join(lines))
        reply.summary_shown = True
        if self.is_whatsapp:
            middle = (f"v3_upsell_{upsell.sku.lower()}", self.upsell_label(upsell)) if upsell else ("v3_change", self.t("btn_change"))
            reply.buttons = [("v3_confirm", self.t("btn_confirm")), middle, ("v3_cancel", self.t("btn_cancel"))]
        reply.last_clarify = None

    # ── terminal states ──────────────────────────────────────────────────────
    def order_placed(self, order: ex.OrderResult) -> Reply:
        """COD: confirmation. UPI: the pay-here instruction (+ QR image). Both are terminal."""
        from app.services import payment_verification_service as pvs

        o = order.order
        self.amounts.add(int(round(o.total_amount)))
        for li in getattr(o, "line_items", None) or []:
            self.amounts.add(int(round(li.subtotal)))
            self.amounts.add(int(round(li.unit_price)))
        if order.method == "UPI" and order.instruction is not None:
            reply = Reply(text=order.instruction.text, terminal=True, amounts=self.amounts)
            if order.instruction.qr_url:
                reply.images = [(order.instruction.qr_url, order.instruction.closing)]
                reply.image_fallback_text = order.instruction.closing
            return reply
        views = [v for v in (self.catalogue.get(ln["sku"]) for ln in self.cart.lines) if v]
        text = self.t(
            "order_cod", number=o.order_number, items=pvs.render_items(o), amount=format_price(o.total_amount),
            eta=self.eta(views), shop=self.shop,
        )
        return Reply(text=text, terminal=True, amounts=self.amounts)

    def handoff(self) -> Reply:
        """Passed to a human (terminal)."""
        return Reply(text=self.t("handoff"), terminal=True, amounts=self.amounts)

    def cancelled(self, kind: str) -> Reply:
        """The cancel outcome (cart dropped / order cancelled / cannot cancel / nothing to cancel)."""
        what, _, number = kind.partition(":")
        key = {"cart": "cancel_cart", "order": "cancel_order", "blocked": "cancel_blocked", "nothing": "cancel_nothing"}[what]
        reply = Reply(text=self.t(key, number=number), amounts=self.amounts)
        if what in ("cart", "order", "nothing") and self.is_whatsapp:
            reply.buttons = [("v3_browse", self.t("btn_browse"))]
        return reply

    def cancel_ack(self, kind: str) -> str:
        """One-line acknowledgement of a cancel that comes with other content ("cancelled — and here is what we have")."""
        what, _, number = kind.partition(":")
        return self.t({"cart": "ack_cancel_cart", "order": "ack_cancel_order", "blocked": "ack_cancel_blocked"}.get(what, "ack_cancel_cart"), number=number)

    @staticmethod
    def _filter_phrase(search: dict) -> str:
        """"red saree" / "XL lehenga" / "blue M lehenga": the customer's filters and product words, for "No … right now"."""
        f = search.get("filters") or {}
        words = [f[k] for k in ("color", "material") if f.get(k)] + ([f["size"]] if f.get("size") else []) + ([search["text"]] if search.get("text") else [])
        return " ".join(words) or search.get("query", "")

    @staticmethod
    def item_count(cart: CartData) -> int:
        """Pieces in the cart (a line without a quantity yet counts as one)."""
        return sum(int(ln.get("qty") or 1) for ln in cart.lines)

    @property
    def cart_is_stale(self) -> bool:
        """True when the cart has sat untouched for ``agent_greeting_stale_hours`` or more (a greeting then recaps it, never resumes it)."""
        return self.idle_hours is not None and self.idle_hours >= get_settings().agent_greeting_stale_hours

    def welcome_back_stale(self) -> Reply:
        """A hello on an OLD cart: greeting + what is in it + [Continue order] [New order] [Browse products]; no question about an old line."""
        rows = []
        for i, ln in enumerate(self.cart.lines, start=1):
            view = self.catalogue.get(ln["sku"])
            variant = f" ({self.variant_label(ln)})" if self.variant_label(ln) else ""
            rows.append(self.t("cart_line" if ln.get("qty") else "cart_line_open", i=i, name=view.name if view else ln["sku"], variant=variant, qty=ln.get("qty")))
        text = self.t("welcome_back_stale", shop=self.shop) + "\n" + "\n".join(rows) + "\n\n" + self.t("stale_cta")
        reply = Reply(text=text, amounts=self.amounts, resume_greeting=True, lists_options=True)
        if self.is_whatsapp:
            reply.buttons = [("v3_continue", self.t("btn_continue")), ("v3_fresh", self.t("btn_new_order")), ("v3_browse", self.t("btn_browse"))]
        return reply

    def welcome_back(self) -> Reply:
        """A hello with items in the cart: one short line + [Continue order] [Start fresh] — no summary, no upsell."""
        n = self.item_count(self.cart)
        reply = Reply(
            text=self.t("welcome_back_short", n=n, item_word=self.t("word_item" if n == 1 else "word_items")),
            amounts=self.amounts, resume_greeting=True,
        )
        if self.is_whatsapp:
            reply.buttons = [("v3_continue", self.t("btn_continue")), ("v3_fresh", self.t("btn_fresh"))]
        return reply

    # ── the turn ─────────────────────────────────────────────────────────────
    def reshow_open_question(self, resume: dict | None, reply: Reply, parts: list[str]) -> str | None:
        """
        Put the question the customer was last asked back on screen (list rows / order buttons) without changing anything.

        Returns the closing line that belongs with it, or None when the open question is not a list or an offer (a missing colour,
        the summary … are re-asked by the normal closing logic from the cart itself).
        """
        slot = (resume or {}).get("slot")
        if slot == "product":
            views = [v for v in (self.catalogue.get(s) for s in resume.get("options") or []) if v]
            if views:
                self.product_list_widget(reply, views)
                if not reply.list_rows:             # no tappable rows (Instagram / website / a single item): the numbered lines carry the choice
                    parts.append("\n".join(self.product_lines(views)))
                return self.t("list_cta")
        elif slot == "order_offer" and resume.get("sku"):
            view = self.catalogue.get(resume["sku"])
            if view is not None and view.in_stock():
                reply.last_clarify = {"slot": "order_offer", "options": [], "line_id": None, "sku": view.sku}
                if self.is_whatsapp:
                    reply.buttons = [(f"v3_order_{view.sku.lower()}", self.t("btn_order_this")), ("v3_browse", self.t("btn_see_more"))]
                return self.t("card_cta")
        return None

    def render(
        self, result: ex.ExecResult, before: CartData, *, intent: str = "none", images: list[tuple[str, str]] | None = None,
        resume: dict | None = None,
    ) -> Reply:
        """Compose the reply for one executed turn (see `_render`) and mark whether it is complete in the customer's language."""
        reply = self._render(result, before, intent=intent, images=images, resume=resume)
        reply.localized = self.table_language and not self.dynamic and not result.effects.dynamic
        self.fit_labels(reply)
        return reply

    @staticmethod
    def fit_labels(reply: Reply) -> None:
        """Every interactive label within WhatsApp's limits (button ≤20, row title ≤24, row description ≤72, list button ≤20, header ≤60) and unique."""
        if reply.buttons:
            titles = wa_labels.unique_buttons([t for _, t in reply.buttons])
            reply.buttons = [(i, title) for (i, _), title in zip(reply.buttons, titles)]
        if reply.list_rows:
            titles = wa_labels.unique_row_titles([t for _, t, _ in reply.list_rows])
            reply.list_rows = [(i, title, wa_labels.row_description(d) if d else d) for (i, _, d), title in zip(reply.list_rows, titles)]
        if reply.list_header:
            reply.list_header = wa_labels.list_header(reply.list_header)
        if reply.list_button_text:
            reply.list_button_text = wa_labels.list_button(reply.list_button_text)

    def _render(
        self, result: ex.ExecResult, before: CartData, *, intent: str = "none", images: list[tuple[str, str]] | None = None,
        resume: dict | None = None,
    ) -> Reply:
        """Compose the reply for one executed turn: acknowledgements → content → exactly one closing question (`resume` = the open question)."""
        eff = result.effects
        if eff.handoff:
            return self.handoff()
        if eff.order is not None:
            return self.order_placed(eff.order)
        has_content = bool(eff.search or eff.browse or eff.product or eff.images_for or eff.answers)
        cancel_part: str | None = None
        if eff.cancel and eff.cancel != "asked":
            if not has_content:
                return self.cancelled(eff.cancel)
            cancel_part = self.cancel_ack(eff.cancel)
        returning = intent == "greeting" and bool(self.cart.lines) and not (has_content or result.applied or eff.cancel or eff.clarify or eff.show_summary)
        if returning:           # never the open question of an old line out of the blue: a recap (old cart) or a short hello + buttons (recent)
            return self.welcome_back_stale() if self.cart_is_stale else self.welcome_back()

        reply = Reply(text="", amounts=self.amounts)
        parts: list[str] = []
        if cancel_part:
            parts.append(cancel_part)
        if result.planner_failed and not eff.search:
            parts.append(self.t("not_understood"))
        acks = self.ack_lines(result.applied)
        if acks:
            parts.append("\n".join(acks))
        for miss in eff.unavailable_options:
            view = self.catalogue.get(miss.get("sku"))
            if view is not None:
                parts.append(self.t("r_option_unavailable", value=miss["value"], product=view.name, options=self.join_options(view.values(miss["attr"]))))
                reply.lists_options = True          # the line names what IS available
        if any(ap.kind == "removed" for ap in result.applied) and not eff.show_summary and eff.order is None:
            parts.append(self.cart_block() if self.cart.lines else self.t("cart_empty_now"))
        parts.extend(eff.answers)
        content_closing_absent: str | None = None
        if eff.not_in_cart and self.cart.lines:
            parts.append(self.t("rm_not_in_cart", what=eff.not_in_cart))
            parts.append(self.cart_block())
            content_closing_absent = self.t("rm_cta")
            reply.last_clarify = None
            if self.is_whatsapp:
                reply.buttons = [("v3_remove_menu", self.t("btn_remove_item")), ("v3_continue", self.t("btn_continue"))]
                reply.lists_options = True

        content_closing: str | None = content_closing_absent          # CTA that goes with listed content
        card_view: ProductView | None = None        # a product card / its photos were shown: "Would you like to order this?"
        if intent == "greeting" and not has_content:
            parts.insert(0, self.t("greet", shop=self.shop))
        elif intent == "thanks" and not (eff.product or eff.search or eff.browse):
            parts.insert(0, self.t("thanks"))
        elif intent == "goodbye" and not self.cart.lines:
            return Reply(text=self.t("goodbye", shop=self.shop), terminal=True, amounts=self.amounts)
        elif intent == "decline" and not has_content and not acks:
            if not self.cart.lines:
                return Reply(text=self.t("decline_ack"), terminal=True, amounts=self.amounts)
            parts.insert(0, self.t("decline_ack"))          # something is in the cart: acknowledge, then the step that is still open
        elif intent == "reaction" and not has_content and not acks:
            parts.insert(0, self.t("reaction_ack"))
            content_closing = self.reshow_open_question(resume, reply, parts)
        elif intent == "smalltalk" and not (has_content or acks):
            parts.insert(0, self.t("smalltalk"))

        if eff.search is not None:
            views = [v for v in (self.catalogue.get(s) for s in eff.search["skus"]) if v]
            query = eff.search["query"][:40]
            closest = eff.search.get("closest")
            if eff.search.get("category_empty"):
                content_closing = self._category_empty_block(parts, reply, eff.search["category_empty"])
            elif views:
                labels = eff.search.get("labels") or {}
                lines = [
                    self.t("search_with_labels", i=i, name=v.name, price=self.money(v.price), labels=", ".join(labels[v.sku]), sku=v.sku)
                    if labels.get(v.sku) else self.t("list_line", i=i, name=v.name, price=self.money(v.price), sku=v.sku)
                    for i, v in enumerate(views, start=1)
                ]
                parts.append(self.t("search_header", query=query) + "\n" + "\n".join(lines))
                self.product_list_widget(reply, views)
                content_closing = self.t("list_cta")
            elif closest and closest.get("skus"):
                alts = [v for v in (self.catalogue.get(s) for s in closest["skus"]) if v]
                what = self._filter_phrase(eff.search)
                parts.append(self.t("search_filter_none", what=what, values=self.join_options(closest["values"])) + "\n" + "\n".join(self.product_lines(alts)))
                self.product_list_widget(reply, alts)
                reply.mentions_unavailable = True
                content_closing = self.t("list_cta")
            else:
                cats = self.catalogue.categories()[:8]
                if cats:
                    parts.append(self.t("search_none_cats", query=query, cats=self.join_options(cats)))
                else:
                    alts = self.alternatives(set())
                    parts.append(self.t("search_none", query=query) + ("\n" + "\n".join(self.product_lines(alts)) if alts else ""))
                    if alts:
                        self.product_list_widget(reply, alts)
                content_closing = self.t("browse_cta")
                if self.is_whatsapp and not reply.list_rows:
                    reply.buttons = [("v3_browse", self.t("btn_browse"))]
        if eff.browse is not None:
            views = [v for v in (self.catalogue.get(s) for s in eff.browse["skus"]) if v]
            if eff.browse.get("category_empty"):
                content_closing = self._category_empty_block(parts, reply, eff.browse["category_empty"])
            elif eff.browse.get("unknown_category"):
                cats = self.catalogue.categories()[:8]
                parts.append(self.t("category_not_found", q=eff.browse["unknown_category"][:40], cats=self.join_options(cats)) if cats
                             else self.t("search_none", query=eff.browse["unknown_category"][:40]))
                content_closing = self.t("browse_cta")
                if self.is_whatsapp:
                    reply.buttons = [("v3_browse", self.t("btn_browse"))]
            elif views:
                block = [self.t("browse_bestsellers_header" if eff.browse.get("sort") == "bestseller" else "browse_header")]
                if eff.browse.get("categories") and not eff.browse.get("category"):
                    block.append(self.t("browse_cats", cats=self.join_options(eff.browse["categories"][:8])))
                block.extend(self.product_lines(views))
                block.append(self.t("catalogue_link", url=_catalogue_url(self.client)))
                parts.append("\n".join(block))
                self.product_list_widget(reply, views)
                content_closing = self.t("list_cta")
            else:
                parts.append(self.t("browse_empty"))
        if eff.product:
            view = self.catalogue.get(eff.product)
            if view is not None:
                parts.append(self.product_card(view, reply))
                if not view.in_stock():
                    self._alternatives_block(parts, reply, view, exclude={view.sku}, sold_out=False)
                    content_closing = ""            # the alternatives block already ends with its question
                else:
                    card_view = view
        if eff.images_for:
            view = self.catalogue.get(eff.images_for)
            if view is not None:
                if images:
                    parts.append(self.t("images_sent", name=view.name))
                    reply.images = list(images)
                else:
                    parts.append(self.t("images_none", name=view.name))
                card_view = view

        # ── exactly one closing question ────────────────────────────────────
        open_gaps = ex.gaps(self.cart, self.catalogue, self.client, is_whatsapp=self.is_whatsapp)
        if card_view is not None and not any(g.sku == card_view.sku for g in open_gaps):
            content_closing = self.t("card_cta")        # an open line of THIS product is asked about instead
            reply.last_clarify = {"slot": "order_offer", "options": [], "line_id": None, "sku": card_view.sku}
            if self.is_whatsapp:
                reply.buttons = [(f"v3_order_{card_view.sku.lower()}", self.t("btn_order_this")), ("v3_browse", self.t("btn_see_more"))]
        if eff.show_summary:
            self.summary(reply, parts)
            reply.list_rows = None
        elif eff.clarify is not None:
            self.clarify(eff.clarify, reply, parts)
        else:
            if content_closing is not None:
                if content_closing:
                    parts.append(content_closing)
            elif open_gaps:
                g = open_gaps[0]
                self.clarify(ex.Clarify(slot=g.slot, options=g.options, line_id=g.line_id, sku=g.sku), reply, parts)
            elif self.cart.lines:
                self.summary(reply, parts)          # a complete cart + a side question / greeting: resume at the summary
            else:
                browse_prompt = not parts or intent in ("greeting", "smalltalk", "reaction")
                if not parts:
                    parts.append(self.t("not_understood"))
                parts.append(self.t("cta_browse") if browse_prompt else self.t("cta_more"))
                if browse_prompt and self.is_whatsapp:
                    reply.buttons = [("v3_browse", self.t("btn_browse"))]
        reply.text = "\n\n".join(p for p in parts if p).strip()
        reply.amounts = self.amounts
        if intent in ("greeting", "thanks", "smalltalk") and not (has_content or result.applied) and reply.last_clarify is None:
            reply.resume_greeting = True        # pure social reply: a list the customer may still answer by number stays valid
        return reply


_GENERIC_NAME_WORDS = frozenset("for and with the women womens men mens ladies kids new latest pure premium designer".split())


def _catalogue_url(client) -> str:
    """Public catalogue link of this shop."""
    settings = get_settings()
    slug = getattr(client, "catalogue_slug", None)
    return f"{settings.public_shop_base_url}/{slug}" if slug else settings.public_shop_base_url
