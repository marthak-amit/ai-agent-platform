# Fix Prompt — conv=52 run (0-quantity charge bug + card cost leak)

Paste to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.conversation_flow`, `app.services.order_service`,
`app.services.gemini_service`). Model: `llama-3.3-70b-versatile`.

**Rules:** Show me the diff + file/function before applying each fix. Don't change
`mark_order_paid` or price math. Work P0 → P2. After each P0, replay the captured
conv=52 banarasi transcript and confirm the check passes.

---

## P0-1 — Quantity validation: reject 0 / invalid, never advance, never fall back

**Symptom (L752-755, L764, L787):**
```
USER: 5   → "only 1 available, how many (1–1)?"   (good)
USER: 0   → "Amit, deliver to: 702 Somerest...?"  (BUG: advanced)
final order: banarasi saree × 1 = ₹999
```
Typing `0` advanced the flow to address instead of re-prompting, and the order was
placed with `qty=1` — a stale value left over from an earlier "Yes 1 pic". Net result:
the customer entered 0 and was charged for 1. A quantity the customer never agreed to.

**Fix — one strict validator for the quantity slot:**
1. Parse an integer from the message. Accept only `1 ≤ qty ≤ available_stock`.
2. Reject and RE-PROMPT, staying on the quantity slot (no stage change, no address jump):
   - `0` → "Quantity must be at least 1. How many would you like? (1–{stock})" — and treat
     repeated `0` / "none" / "cancel" as a cancel intent: offer to cancel the order
     instead of looping.
   - negative, non-numeric, or empty → same re-prompt.
   - `> stock` → existing "only {stock} available, how many (1–{stock})?" message (keep it).
3. **Never fall back to a previous/stale quantity.** If the current message fails
   validation, the quantity slot stays UNFILLED. Do not carry forward an earlier value.
   The flow may not leave the quantity slot until a valid qty is filled this turn.
4. Add an order-creation guard: refuse to create/confirm an order unless
   `1 ≤ qty ≤ stock`. Make it a hard assert in `order_service` so an invalid qty can
   never reach a placed order, regardless of upstream bugs.

**Check:** banarasi (stock 1): `5` → re-prompt; `0` → re-prompt (then cancel offer on
repeat); `1` → proceeds. No order can be placed with qty 0 or qty>stock. The "charged for
1 after typing 0" path is impossible.

---

## P0-2 — CI invariant for quantity (lock it permanently)

Add a test to CI that asserts: for every placed order, `1 ≤ qty ≤ stock_at_time`. Feed
the captured "0" transcript as a fixture; it must FAIL on today's code and PASS after
P0-1. This is the same guard style as the pinned-SKU invariant — cheap, permanent.

**Check:** CI fails on the "0" fixture before P0-1, passes after.

---

## P1-3 — All product cards deterministic (kill the LLM card cost leak)

**Symptom (L849, L852):** "Banarasi saree Is this available?" → LLM card reply (5,442
tok, ₹0.27); "Yes 1 pic" → LLM card AGAIN (₹0.27). Those two calls are the ENTIRE ₹0.54
of order 2. Meanwhile "choli" produced the same kind of card for FREE via the
deterministic name-match path (TEMPLATE+CLASSIFY, ₹0). Same situation, two paths — one
free, one paid.

Root cause: A001 (banarasi) isn't resolved by the deterministic name-match/SKU index, so
it falls through to the LLM card path. PR17761 (choli) is indexed, so it's free.

**Fix:**
1. Index EVERY product (all SKU formats: A001, PR17761, KU23444…) into the name-match /
   SKU resolver. No product may rely on the LLM to produce its card.
2. Route every product-card render — single match or multi match — through the ONE
   deterministic card renderer (the one that made choli free). Delete the LLM card path.
3. The card renderer reads name/price/variants from the DB by SKU at render time.

**Check:** "Banarasi saree Is this available?" → deterministic card, ₹0, byte-format
identical to the choli card. Order 2 total drops from ₹0.54 toward ₹0.

---

## P1-4 — Affirmative + quantity in one message ("Yes 1 pic")

**Symptom (L747-749):** "Yes 1 pic" at the offer stage → bot just re-showed the product
card and captured nothing; needed a second "Yes", and "1 pic" (qty=1) was lost (later
resurfaced as the stale qty in P0-1).

**Fix:**
1. At the offer/product_inquiry stage, treat an affirmative as "start order" deterministically
   (pin the shown SKU → `order_collection`), same as a bare "yes".
2. In the SAME pass, run the multi-slot extractor over the message: "1 pic" → quantity=1,
   plus any colour/size present. Validate each against DB (quantity via the P0-1 validator).
   Fill what's valid; ask only for what's still empty.
3. Do not re-show the card on an affirmative — advance the flow.

**Check:** "Yes 1 pic" → order starts, qty=1 captured (validated ≤ stock), bot asks only
for remaining slots. No duplicate card, no LLM call.

---

## P2-5 — "Pay" / "Done" at confirmation should advance, not re-show summary

**Symptom (L797, L823):** at `awaiting_final_confirmation`, "Pay" and "Done" re-showed the
order summary instead of moving to payment. Only the "confirm" button advanced.

**Fix:** Map payment-intent words (pay/done/paid/confirm/ok/go ahead) at the confirmation
stage to the confirm action, via the same deterministic keyword set used elsewhere.

**Check:** "Pay" at confirmation → payment instructions, not a repeated summary.

---

## P2-6 — Verify per-product delivery window

**Symptom:** choli summary said "20 business days" (L77), banarasi said "3–7 business
days" (L889). Confirm this is intended per-product config, not a default/parse bug.

**Fix:** Trace the delivery-window source. If per-product and correct, leave it. If it's
a stray default or parse error, source it from one config field per product and make the
aside-answer, summary, and success message all read that one field.

**Check:** Each product's delivery window is identical across its aside-answer, summary,
and success message, and matches client config.

---

### Suggested order
P0-1 (stop the wrong-quantity charge) → P0-2 (lock it in CI) → P1-3 (kill card cost leak)
→ P1-4 (affirmative+qty) → P2-5 → P2-6.

### Note
P0-1 is a missing validation — add the guard. P1-3 and P1-4 are the recurring disease: a
second LLM path doing what a deterministic path already does. Fix = delete the detour, not
add logic beside it.
