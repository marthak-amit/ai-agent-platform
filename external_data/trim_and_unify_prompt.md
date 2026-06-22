# Prompt — Trim LLM Cost WITHOUT Losing Customer-Facing Accuracy

Paste to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.gemini_service`, `app.services.knowledge_service`,
`app.services.learning_service`, `app.services.order_service`, `app.services.cost_log`).
Model: `llama-3.3-70b-versatile` (Groq), fallback `llama-3.1-8b-instant`.

## NON-NEGOTIABLE GUARANTEE (read first)

Every fact the **end customer sees** — product name, SKU, price, colour, size,
quantity, line totals, grand total, address, delivery window, UPI id — MUST be
rendered from DB truth, byte-identical to today's correct cases. No trim, no
summary, no model swap may change a single customer-facing number or name.
If the accuracy gate (Section 5) cannot prove this, the change does NOT ship.

**Work order:** Section 1 (renderer split) → Section 2 (multi-slot) → Section 3
(post-order flow) → Section 4 (trim) → Section 5 (gate, runs throughout).
Show me the diff + the file/function before applying each section.

---

## Section 1 — LLM stops writing customer text (fixes voice drift + locks accuracy)

**Observation being fixed:** template replies and LLM replies look different. Root
cause: the LLM writes free prose for customer-facing answers, so tone/format/facts
drift from the deterministic templates.

**Rule:** The LLM is an *understander*, never a *writer*. All customer-facing text is
produced by ONE deterministic renderer reading DB + slot state.

1. Change every customer-facing LLM call so the model returns **structured JSON only**,
   never prose. Example contract:
   ```json
   {"intent": "ANSWER|ASK_PRODUCT|ASK_PRICE|CHANGE_ADDRESS|SIDE_QUESTION|CHITCHAT",
    "sku": "PR17761 | null",
    "slots": {"color": "Red|null", "size": "XL|null", "quantity": 2|null},
    "question_topic": "delivery_time|payment|quality|null"}
   ```
   Force JSON with a system instruction + `response_format` if available; reject and
   retry once on parse failure; on second failure fall back to a safe deterministic
   re-prompt (never echo raw model prose to the customer).
2. Build/extend a single `render_reply(state)` function. It is the ONLY place that emits
   customer text. It reads product/price/variants from DB by SKU, reads slots, and picks
   the matching template. Templates already exist for greeting, product card, slot
   prompts, summary, payment, success — reuse them verbatim.
3. For side-questions (quality, delivery time, payment): the LLM returns
   `question_topic`; the renderer answers from a **known-fact table / KB**, then re-emits
   the current slot prompt so the order resumes. The LLM supplies the topic, not the
   answer text.

**Check:** Grep the codebase — no path sends model-generated prose to WhatsApp. Same
situation produces the same bytes whether understanding came from template logic or LLM.

---

## Section 2 — Smart multi-slot capture ("I want to buy red color with XL size")

**Observation being fixed:** when a user gives several values at once, the system fills
one and re-asks for values already given.

1. On every `order_collection` message, run a deterministic multi-slot extractor FIRST
   (no LLM): scan the whole message for colour ∈ `available_colors`, size ∈
   `available_sizes` (with alias map: "xl"→"XL", "large"→"L"…), and integer quantity.
2. **Validate each extracted value against DB variants for the pinned SKU** before
   filling. Valid → fill. Invalid (e.g. colour not offered, XL not stocked) → tell the
   customer exactly what's unavailable and list the real options, via the renderer.
3. After filling, **ask only for still-empty required slots.** Never re-prompt a slot
   that is now filled. If colour+size both arrived, jump straight to quantity (or address
   if qty also present).
4. Only fall to the LLM extractor when the deterministic pass is ambiguous (free-text
   mixed with a question). Even then, the LLM returns slots as JSON (Section 1); the fill
   + validation stays deterministic.

**Check:** "red color with XL size" on a SKU that stocks both → both filled in one turn,
bot asks only "Quantity?". On a SKU without XL → bot says XL unavailable + lists real
sizes, does not silently downgrade.

---

## Section 3 — Post-order flow break (the wrong-product bug)

**Observation being fixed:** after an order is placed, the next customer message breaks
the flow and the LLM "takes over." In the captured log this caused the bot to confirm
choli (PR17761, ₹3,999) but place **Georgette (SR31045, ₹2,200)** — wrong product, wrong
price.

Root causes from the log:
- `last_shown_sku` is updated only by the deterministic product card; products surfaced
  by the LLM never update it, so it stays stale (order 1's SKU).
- The post-order repin trusted stale `last_shown_sku` blindly.
- Continued-shopping messages routed to the LLM for prose, bypassing deterministic
  product resolution.

**Fix:**
1. **Single source for the pin.** Every time ANY product is surfaced to the customer —
   deterministic card OR a product named in an LLM-understood turn — update
   `last_shown_sku` in the same transaction. There is exactly one writer.
2. **Repin must agree with the named product.** Before pinning on an affirmative, resolve
   the product from the recent customer turns (name/SKU match). If the resolved product
   ≠ `last_shown_sku`, DO NOT pin the stale one — re-resolve by name and pin that, or ask
   which product if ambiguous. A stale pin may never override the product the customer
   just named.
3. **Post-order continuation is a fresh deterministic order intent.** After completion
   reset, treat "choli che?", "I want red choli XL", etc. through the same
   product-resolver → pin → `order_collection` path used for a first order. Affirmative +
   product + variants in one message is handled in code (resolve product, fill the
   variants via Section 2), not handed to the LLM to narrate.
4. **Render the order summary from the pinned SKU's DB row, not from any LLM text.** The
   summary's name/price/variants come from `get_product(sku)` at render time
   (DB-at-render). If pin and conversation disagree, halt and re-ask rather than place a
   mismatched order.

**Check:** Replay the captured second order — customer asks for choli, gives "red…XL" —
and the placed order is choli PR17761 at ₹3,999 with the variants the customer chose, or
the bot stops and asks. It must be impossible to confirm one product's name/price and
persist a different SKU.

---

## Section 4 — Trim the LLM request (the cost cut)

Only after Sections 1–3 are green. The LLM now returns small JSON, so input size is the
remaining cost.

1. **Type-B memory (summary in DB, not raw history).** After each turn, store a compact
   state record: `{sku, product_name, color, size, qty, stage, customer_name, address,
   last_2_user_msgs}`. Feed THIS to the LLM instead of `history_len:10` raw turns.
2. **Trim the system prompt** to essentials needed for JSON classification/extraction.
   Remove prose style guidance (the renderer owns style now), examples that don't change
   behaviour, and any persona text not needed for understanding.
3. **Drop redundant product context** from the prompt when the SKU is already pinned
   deterministically — the renderer reads DB, the LLM doesn't need the full catalogue.
4. **Do NOT inject `learning_service` "similar past conversations" into the prompt.** They
   add tokens and can pull a wrong product/price from an old chat — the exact failure
   class as Section 3. Keep learning for offline analytics, not live prompt context.
5. Measure median input tokens before/after via the existing cost report instrumentation.
   Target: well under the current ~6,600, ideally ~1,500–2,000.

**Check:** Median LLM `in_tok` drops sharply; the accuracy gate (Section 5) stays green;
no `learning_service` output appears in any live prompt.

---

## Section 5 — Accuracy gate (the "surety" — runs across all sections)

This is how "the requested thing reaches the customer correctly" is *proven*, not hoped.

1. **Golden set.** Assemble 25–40 real conversation transcripts covering: clean order,
   multi-slot ("red + XL"), side-question mid-order, change-address, OOS colour/size,
   second order after completion, the captured wrong-product case, mixed-language. Store
   as fixtures (input messages + the correct expected customer-facing replies).
2. **Baseline.** Run the golden set on current code; snapshot every outbound customer
   message. This is the reference.
3. **Per-change diff.** After each section's change, re-run the golden set. Extract the
   **customer-facing fact set** from each reply — product name, SKU, price, colour, size,
   quantity, line + grand totals, address, delivery window, UPI id — and assert it matches
   the baseline EXACTLY. Wording/format may only change where the change intends it
   (Section 1 unifies voice); facts may never change. Any fact mismatch = FAIL = do not
   ship that change.
4. **Pinned-SKU invariant test.** Assert: the SKU on the persisted order == the SKU named
   in the last product confirmation shown to the customer. This single test catches the
   wrong-product class permanently — add it to CI.
5. **Shadow run before live.** Deploy trimmed prompt in shadow: for real traffic, run both
   old and new understanding, render with the renderer, log when the chosen template/slots
   differ. Zero customer-facing-fact divergence over an agreed window → promote.
6. **Auto-rollback.** Feature-flag every change. If shadow shows any fact divergence, or
   the pinned-SKU invariant fails in CI, flip back automatically and alert.

**Definition of done:** golden set green, pinned-SKU invariant in CI, shadow run clean,
median input tokens down. Then promote.

---

### Summary of guarantees this delivers
- Customer never sees model-written prose → tone/format/facts can't drift (Section 1).
- Multi-value messages fill all given slots, DB-validated, no re-asking (Section 2).
- Confirmed product == placed product, always, enforced by a CI invariant (Section 3).
- Cost falls via summary-memory + trimmed prompt, with no accuracy loss (Section 4).
- Every change is proven against a golden set + shadow before customers see it (Section 5).
