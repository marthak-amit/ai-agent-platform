# Conversation simulator

Plays thousands of synthetic customers through the **real** app (signed WhatsApp webhook → pipeline → Postgres) and
reports every defect it can detect without a human reading the chats.

```bash
cd backend
python -m tests.sim.run_sim --n 5000 --concurrency 10 --seed 1            # clean run
python -m tests.sim.run_sim --n 5000 --concurrency 10 --seed 1 --chaos    # + 429s, empty/invalid JSON, wrong-language LLM answers
python -m tests.sim.run_sim --n 1500 --lang gujarati_script --raw          # translator down: see the engine's own text
python -m tests.sim.run_sim --n 20 --only sc_order_variant_upi --shop legacy --verbose   # debug one flow
python -m tests.sim.show "<text in a finding>" 3                           # print the transcripts of matching findings
```

Output: `tests/sim/out/report.json` (counts by code / flow / language + grouped examples) and `transcripts.json`.
Exit code 1 when anything was found.

## What is real, what is faked

| Real | Faked |
|---|---|
| FastAPI app, webhook signature check, pipeline, slot machine, router front door, localizer, payment flow, Postgres (own DB `sim_5k`, created by alembic) | Groq (`tests/sim/fake_llm.py`: rule-based multilingual NLU + chaos knobs), Meta send APIs (captured per recipient), media download/storage |

The fake LLM tests how the **engine** handles model output. It says nothing about the real model's quality — run
`RUN_LIVE_LLM=1` tests and the router eval against Groq for that.

## Customers

15 languages (`lexicon.py`): English, Hinglish, Hindi (Devanagari), Gujarati (Roman + script), Marathi (Devanagari + Roman),
Bengali, Tamil, Telugu, Kannada, Malayalam, Punjabi, Odia, Urdu. 21 behaviours (`scenarios.py`): browse, simple/variant
orders with COD or UPI (screenshot → approve/reject), cancel mid-order / at payment, change slot, switch product, order
status, hand-off, FAQ, sold-out, over-quantity, gibberish at every option question, returning customer (saved address:
yes / typed / change), edge inputs (emoji, 1500 chars, SQL/HTML/format-string injection), language switching, buttons.
Both shop modes run: router ON (LLM front door) and router OFF (keyword pipeline).

## Invariants checked every turn (`checks.py`)

reply exists · HTTP 200 · no leaked `{placeholder}` / `None` / traceback · WhatsApp limits (4096 chars, ≤3 buttons, 20-char
titles) · every ₹ amount is a real price × quantity · reply is in the customer's **script** · native-script replies contain
no stray English words (catalogue names, brands, ids are allowed) · button labels are translated · no server ERROR logs ·
plus per-scenario goals (order created with the right product/qty/total/payment, stock never negative, cancel really
cancels, gibberish never restarts the flow).
