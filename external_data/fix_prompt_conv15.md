# Fix Prompt — conv=52 run (browse-to-LLM cost + deflect + stock question)

Paste to your coding agent. Codebase: FastAPI webhook (`app.routers.webhook`,
`app.services.conversation_flow`, `app.services.knowledge_service`,
`app.services.gemini_service`). Model: `llama-3.3-70b-versatile`.

**Rules:** Show me the diff + file/function before each fix. Don't change price math or
`mark_order_paid`. Work P0 → P2. Replay the captured conv=52 transcript after each P0.

**Headline:** Two orders in this run — a browse-heavy one cost **₹1.11**, the clean one
right after cost **₹0.00**. The entire difference is browse / discovery / deflect messages
routing to the LLM reply path instead of deterministic paths. Plus the address loop from
the previous run is still live (that prompt isn't shipped yet — keep it P0).

---

## P0-0 — Carryover: address loop + resume re-escalate (STILL UNFIXED)

The top of this log (L64-148) is the same failure as the prior run: valid Rajkot address
rejected → attempt cap → human escalation → resume re-trips the cap → dead-air loop. The
previous fix prompt (accept valid address, handle "use my old address", explain pincode,
reset attempt counter on resume, soften cap) is NOT yet applied. Ship it first — it loses
paying customers. Don't duplicate; apply the prior `fix_prompt_conv14` P0-1/2/3.

---

## P0-1 — Browse / discovery replies must be deterministic (the ₹1.11 driver)

**Symptom (cost report L526-537):** four LLM reply calls, ~5,500 input tokens each,
~₹0.28 apiece:
- "Are you only sale sarees?"      → LLM produced the catalogue option list (₹0.27)
- "Okay but I want kurti"          → LLM produced the kurti option list (₹0.28)
- "Behind my house … girl … number"→ LLM deflect (₹0.28)
- "Saras mane … different che?"    → LLM deflect (₹0.27)

The deterministic multi-option lister already exists (it rendered the same kind of list
elsewhere for free). Browse/category questions are falling to the LLM instead.

**Fix:**
1. Route category / "what do you sell" / "show me X" / "I want {category}" through the
   deterministic catalogue lister:
   - whole-catalogue or "only sarees?" → list categories or top products (template, ₹0).
   - "I want kurti" / "show kurtis" → deterministic category filter → option list (the
     same list the LLM produced), ₹0.
2. The lister reads products from the DB and renders the existing "Here are our options:
   • … Which one interests you?" template. No LLM.
3. Store the listed SKUs as `last_offered_skus` so the next pick resolves deterministically.

**Check:** "Are you only sale sarees?" and "I want kurti" → deterministic option lists,
₹0 each. The browse order's total drops from ₹1.11 toward ₹0.

---

## P0-2 — Deflections must be deterministic (cost + safety)

**Symptom:** "Behind my house one girl name savita I want his number" (a request for a
person's phone number) and the off-topic Gujarati line both went to the LLM for a deflect,
each ~₹0.28.

**Fix:**
1. Off-topic / non-shopping / personal-info requests → ONE deterministic deflect template,
   never an LLM call. Same template used everywhere for consistency.
2. Treat requests for personal information (someone's phone number, address, etc.) as a
   hard deterministic deflect — these must never reach the model. Reply with the standard
   "I can only help with shopping here" deflect.
3. This is the same off-topic short-circuit that catches other non-product questions —
   extend it to cover these cases in any language.

**Check:** The "girl's number" request and the off-topic Gujarati line → free deterministic
deflect, zero LLM calls.

---

## P1-3 — "How many are available?" at the quantity slot → answer, don't re-ask

**Symptom (L418-419):** at the quantity prompt, the customer asked "Avilable Ketla che"
(how many are available?). The bot ignored the question and just repeated "Ketla pieces
joiye?". The customer only learned the stock by guessing "10" and getting rejected.

**Fix:**
1. Recognise a stock/availability question at the quantity slot (any language) and answer
   it from DB: "We have {stock} available." then re-ask the quantity prompt — the same
   answer-then-resume pattern as the existing aside-question handler (FIX2).
2. Keep it deterministic — stock count comes from the DB, no LLM.

**Check:** "Avilable Ketla che" at the quantity slot → "9 available. How many would you
like? (1–9)" (in the conversation language), state unchanged, then proceeds.

---

## P1-4 — "What's different between products?" deserves a real answer

**Symptom (L315, L411):** "Saras mane em k bane ma su different che?" (what's the difference
between the products?) got the generic "which item?" deflect — a real product question
treated as off-topic, and paid via LLM.

**Fix:**
1. Recognise compare/difference questions and answer deterministically from the catalogue:
   list the relevant products with their key facts (name, price, one differentiator) from
   the DB — reuse the option-list renderer.
2. No LLM reply; if the comparison genuinely needs free text, that's the only case allowed
   to call the model, and it should still render via the deterministic lister where possible.

**Check:** A "what's the difference" question → deterministic product comparison/list, not a
generic deflect, and ₹0 where the lister can serve it.

---

### What's already working (don't regress)
- Gujarati slot flow ("Ha" → "Ketla pieces joiye?") and Gujarati stock-cap message.
- Quantity stock cap (`10` → "only 9 available (1–9)") and `1 ≤ qty ≤ stock`.
- Multi-option catalogue list rendering and the clean happy-path order at ₹0.00.

### Theme (same as every prior run)
The cost and the inconsistencies are all one root: a second LLM path doing what a
deterministic path already does. The clean order was ₹0.00; the browse order was ₹1.11
purely because browse/deflect fell to the LLM. Route every browse, list, deflect, and
stock question through the deterministic renderer. Delete the LLM detour — don't add logic
beside it.

### Suggested order
P0-0 (ship the address fix) → P0-1 (browse → deterministic, kills the ₹1.11) →
P0-2 (deflect → deterministic + privacy safety) → P1-3 (stock question) → P1-4 (compare).
