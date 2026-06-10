# How to Run AI Agent Platform Locally

A complete step-by-step guide. No prior developer experience needed.

---

## Prerequisites

You need four things installed on your Mac before you start.

### 1. Python 3.11 or newer

**Check if installed:**
```
python3 --version
```
You should see something like `Python 3.13.2`. Any version 3.11 or higher is fine.

**Not installed?**
```
brew install python@3.13
```
> Don't have Homebrew? Install it first: paste this in Terminal → `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`

---

### 2. PostgreSQL (the database)

**Check if installed:**
```
psql --version
```
You should see `psql (PostgreSQL) 15.x` or similar.

**Not installed?**
```
brew install postgresql@15
brew services start postgresql@15
```

---

### 3. Node.js and npm (for the React frontend)

**Check if installed:**
```
node --version
npm --version
```
You should see `v20.x.x` and `8.x.x` (or higher for both).

**Not installed?**
```
brew install node
```

---

### 4. A Gemini API key (free)

The AI replies are powered by Google Gemini. You need a free API key.

1. Go to **https://aistudio.google.com**
2. Sign in with your Google account
3. Click **"Get API Key"** → **"Create API key"**
4. Copy the key — it looks like `AIzaSyXXXXXXXXXXXXXXXXX`

Keep this key ready. You will paste it in the setup step below.

---

### Quick check — run all at once

```bash
chmod +x check_requirements.sh && ./check_requirements.sh
```

This script checks all four prerequisites and prints ✅ or ❌ with the exact install command for anything missing.

---

## First Time Setup (run once)

Do these steps in order, exactly once when you first set up the project.

### Step 1 — Open the project folder in Terminal

```bash
cd /path/to/ai-agent-platform
```

> If you downloaded a zip: unzip it, then drag the folder into Terminal after typing `cd ` (with a space).

---

### Step 2 — Create the database

```bash
createdb ai_agent_dev
```

This creates a local empty database called `ai_agent_dev`. You only do this once.

If you see `database "ai_agent_dev" already exists` — that's fine, skip to Step 3.

If you see `command not found: createdb`:
```bash
# The Postgres tools aren't on your PATH yet. Add them:
echo 'export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
createdb ai_agent_dev
```

---

### Step 3 — Create a Python virtual environment

```bash
cd backend
python3 -m venv venv
```

This creates a folder called `venv/` that holds all Python packages for this project. It keeps them separate from your system Python.

---

### Step 4 — Activate the virtual environment

```bash
source venv/bin/activate
```

Your Terminal prompt changes to show `(venv)` at the start. **You must do this every time you open a new Terminal window.**

---

### Step 5 — Install Python packages

```bash
pip install -r requirements.txt
```

This downloads and installs all the Python libraries the backend needs (FastAPI, SQLAlchemy, Google Gemini SDK, etc.). Takes 1–3 minutes on first run.

---

### Step 6 — Set up your environment variables (.env file)

Environment variables are how the app gets secret keys and configuration. They live in a `.env` file that is **never committed to Git**.

```bash
cp .env.local .env
```

Now open `.env` in any text editor and **replace the Gemini key** on line 13:

```
# BEFORE (placeholder):
GEMINI_API_KEY=your_real_key_here

# AFTER (your actual key):
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXX
```

Everything else in `.env.local` is already filled with working dummy values for local development. You don't need to change anything else to get the system running.

---

### Step 7 — Run database migrations

```bash
alembic upgrade head
```

This creates all the database tables (clients, conversations, messages, leads, payments, etc.). You'll see output like:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, create all tables
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, add phone to clients
...
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, add plan slug
```

---

### Step 8 — Seed test data

```bash
python seed.py
```

This loads a realistic Indian textile shop dataset so you can test immediately:

- **Test login:** `riya@riyasarees.com` / `riya1234`
- 5 products (Banarasi Silk ₹2,450, Kanjivaram ₹4,200, Georgette ₹1,100, Cotton ₹850, Lehenga ₹6,500)
- 10 sample conversations with Hindi/Hinglish messages
- Admin account: `admin@test.com` / `admin123`

You'll see:

```
✓ client: Riya Sarees Surat (riya@riyasarees.com)
✓ products: 5 created
✓ conversations: 10 created
✓ admin: admin@test.com
```

---

## Run the Project (every time)

After the one-time setup above, this is all you need each time.

### Terminal 1 — Start the backend

```bash
cd ai-agent-platform/backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

Leave this Terminal open. Press `Ctrl+C` to stop.

---

### Terminal 2 — Start the frontend

Open a **second Terminal window**, then:

```bash
cd ai-agent-platform/frontend
npm install          # first time only — downloads React packages
npm run dev
```

You should see:
```
  VITE v5.x.x  ready in 300 ms
  ➜  Local:   http://localhost:3000/
```

Leave this Terminal open too.

---

### Verify everything is working

Open these URLs in your browser:

| URL | What you should see |
|---|---|
| http://localhost:8000/health | `{"status":"healthy"}` |
| http://localhost:8000/docs | Interactive API docs (Swagger UI) |
| http://localhost:3000 | React dashboard login page |

If all three open — you're up and running.

---

## Test the System

### Health check (quickest test)

```bash
curl http://localhost:8000/health
```

Expected: `{"status":"healthy"}`

---

### API docs

Open **http://localhost:8000/docs** in your browser.

Every API endpoint is listed here. You can click any endpoint, fill in values, and hit "Execute" to test it directly — no code needed.

---

### WhatsApp simulator — chat with the AI

This lets you test the AI agent exactly as a customer would, without a real WhatsApp number.

```bash
cd backend
source venv/bin/activate
python tests/whatsapp_simulator.py
```

Type a message and press Enter. The AI replies. Hindi and Hinglish work.

```
  You: Banarasi saree ka price kya hai?
  Agent: Namaste! Hamara Banarasi Silk Saree ₹2,450 mein available hai...
```

**Useful flags:**

```bash
# Run 5 canned test scenarios automatically, then go interactive
python tests/whatsapp_simulator.py --demo

# Start a brand new conversation (fresh phone number)
python tests/whatsapp_simulator.py --fresh

# Point at a different port
python tests/whatsapp_simulator.py --url http://localhost:8001
```

**Commands inside the simulator:** type `/demo`, `/reset`, `/quit`, or `/help`

---

### Full flow test — complete purchase journey

Tests the entire purchase: price enquiry → order → QR payment → confirmed.

```bash
python tests/full_flow_test.py
```

Runs 8 steps automatically and prints what happened at each one — DB changes, AI replies, lead status, payment status. Final summary shows total messages, lead status (should be "hot"), and time taken.

---

### Unit tests (132 automated tests)

```bash
cd backend
source venv/bin/activate
pytest
```

All 132 tests should pass. Add `-v` to see each test name:

```bash
pytest -v
```

Run a single test file:

```bash
pytest tests/test_webhook.py -v
```

---

## Common Errors and Fixes

### "main.py not found" or "No module named app"

**Cause:** You ran `uvicorn main:app` instead of `uvicorn app.main:app`.

The backend code lives inside the `app/` package. The correct command is always:

```bash
uvicorn app.main:app --reload --port 8000
```

Also check that you are in the `backend/` folder, not the project root:

```bash
pwd          # should show .../ai-agent-platform/backend
```

---

### Database connection failed

```
sqlalchemy.exc.OperationalError: could not connect to server
```

**Fix 1** — Is PostgreSQL running?

```bash
brew services list | grep postgresql   # should show "started"
brew services start postgresql@15      # start it if not running
```

**Fix 2** — Does the database exist?

```bash
psql -l | grep ai_agent_dev   # should appear in the list
createdb ai_agent_dev          # create it if missing
```

**Fix 3** — Check your DATABASE_URL in `backend/.env`:

```
DATABASE_URL=postgresql://localhost/ai_agent_dev
```

No username/password needed for a local Homebrew Postgres installation.

---

### Gemini API key errors

```
google.api_core.exceptions.InvalidArgument: API key not valid
```

or

```
[warn] No AI reply saved — check server logs for Gemini error
```

1. Open `backend/.env`
2. Find the `GEMINI_API_KEY=` line
3. Replace the placeholder with your real key from https://aistudio.google.com
4. Stop and restart the backend (`Ctrl+C`, then `uvicorn app.main:app --reload --port 8000`)

> The settings are cached at startup — you must restart after changing `.env`.

Quick test without restarting the server:

```bash
cd backend && source venv/bin/activate
python -c "
from app.config import get_settings
s = get_settings()
print('Key loaded:', s.gemini_api_key[:10] + '...')
"
```

---

### Port already in use

```
ERROR: [Errno 48] Address already in use
```

Another process is already listening on port 8000 (or 3000). Kill it:

```bash
# Find and kill whatever is on port 8000
lsof -ti:8000 | xargs kill -9

# Or use a different port:
uvicorn app.main:app --reload --port 8001
python tests/whatsapp_simulator.py --url http://localhost:8001
```

For the frontend:

```bash
lsof -ti:3000 | xargs kill -9
```

---

### "Module not found" errors

```
ModuleNotFoundError: No module named 'fastapi'
```

The virtual environment is not active, or packages weren't installed.

```bash
# Make sure you're in backend/ and venv is active
cd backend
source venv/bin/activate
# Your prompt should show (venv)

# Reinstall packages
pip install -r requirements.txt
```

If you get errors during `pip install` mentioning `gcc` or `libpq`:

```bash
brew install gcc postgresql@15
pip install -r requirements.txt
```

---

### WhatsApp simulator: "signature rejected"

```
[error] Signature rejected (HTTP 401)
```

The simulator signs webhook payloads using `META_APP_SECRET` from `.env`. The backend verifies with the same value. They must match.

Default value in `.env.local` is `test_app_secret`. As long as you haven't changed it in `.env`, this should work automatically. If you changed it:

```bash
grep META_APP_SECRET backend/.env
```

Both the running server and the simulator must use the same value. Restart the server after any `.env` change.

---

### Alembic "Can't locate revision" on startup

```
alembic.util.exc.CommandError: Can't locate revision
```

The database has a broken migration history. Reset it:

```bash
cd backend && source venv/bin/activate
alembic downgrade base   # removes all tables
alembic upgrade head     # recreates from scratch
python seed.py           # reloads test data
```

---

## Project Structure

Here is what every folder and file does, explained plainly.

```
ai-agent-platform/
│
├── backend/                     ← Python / FastAPI backend
│   │
│   ├── app/                     ← The actual application code (Python package)
│   │   ├── main.py              ← App entry point. Registers all routes, starts the server.
│   │   ├── config.py            ← Reads environment variables from .env into typed settings.
│   │   ├── db.py                ← Database connection. Creates the async engine and session.
│   │   │
│   │   ├── models/              ← Database table definitions (SQLAlchemy ORM)
│   │   │   ├── client.py        ← Business owner accounts (email, password, plan, API key)
│   │   │   ├── conversation.py  ← One row per customer (WhatsApp number or Instagram ID)
│   │   │   ├── message.py       ← Individual messages inside a conversation (user + AI)
│   │   │   ├── lead.py          ← Lead classification: hot / warm / cold per customer
│   │   │   ├── payment.py       ← Razorpay QR code payments (created → paid)
│   │   │   ├── product.py       ← Product catalogue items for a client
│   │   │   └── usage_log.py     ← Daily message count per client (for plan limits)
│   │   │
│   │   ├── routers/             ← HTTP endpoints — one file per feature area
│   │   │   ├── webhook.py       ← POST /webhook — receives WhatsApp messages from Meta
│   │   │   ├── instagram.py     ← POST /instagram/webhook — receives Instagram DMs
│   │   │   ├── payment.py       ← POST /payments/qr and POST /payments/webhook (Razorpay)
│   │   │   ├── auth.py          ← POST /auth/register and POST /auth/login (JWT)
│   │   │   ├── onboarding.py    ← POST /onboarding/setup-agent (first-time wizard)
│   │   │   ├── catalogue.py     ← CRUD for /catalogue/products
│   │   │   ├── conversations.py ← GET /conversations (dashboard data)
│   │   │   ├── leads.py         ← GET /leads (dashboard data)
│   │   │   ├── plans.py         ← GET /plans and POST /plans/upgrade
│   │   │   ├── usage.py         ← GET /usage/stats (dashboard usage bar)
│   │   │   └── admin.py         ← GET /admin/clients (admin panel — needs X-Admin-Key)
│   │   │
│   │   ├── services/            ← Business logic — no HTTP here, just functions
│   │   │   ├── gemini_service.py      ← Calls Google Gemini API to generate AI replies
│   │   │   ├── whatsapp_service.py    ← Sends text messages via Meta Cloud API
│   │   │   ├── instagram_service.py   ← Sends replies via Instagram Messaging API
│   │   │   ├── conversation_service.py ← Saves messages to DB, loads history
│   │   │   ├── lead_service.py        ← Classifies conversations as hot/warm/cold
│   │   │   ├── catalogue_service.py   ← Product search and context formatting
│   │   │   ├── auth_service.py        ← Password hashing, JWT creation/verification
│   │   │   ├── onboarding_service.py  ← Generates system prompts and API keys
│   │   │   ├── razorpay_service.py    ← Creates QR codes, verifies payment webhooks
│   │   │   ├── usage_service.py       ← Records daily message counts
│   │   │   ├── plan_service.py        ← Plan definitions and upgrade logic
│   │   │   └── admin_service.py       ← Admin queries (client list, revenue, stats)
│   │   │
│   │   └── schemas/             ← Data shapes for incoming webhooks (Pydantic models)
│   │       ├── webhook.py       ← WhatsApp message payload structure
│   │       └── instagram.py     ← Instagram webhook payload structure
│   │
│   ├── alembic/                 ← Database migration scripts (version history for tables)
│   │   └── versions/            ← One .py file per schema change
│   │
│   ├── tests/                   ← Automated tests + interactive simulators
│   │   ├── conftest.py          ← Shared test fixtures (mock DB, mock settings)
│   │   ├── test_*.py            ← 132 unit tests (one file per service/router)
│   │   ├── whatsapp_simulator.py ← Interactive chat: simulates a real WhatsApp customer
│   │   ├── instagram_simulator.py ← Same for Instagram DMs, comments, story replies
│   │   ├── payment_simulator.py  ← Simulates Razorpay QR → payment webhook → confirmed
│   │   └── full_flow_test.py    ← End-to-end: customer buys a saree, 8 steps
│   │
│   ├── seed.py                  ← Loads test data (Riya Sarees shop + products + conversations)
│   ├── requirements.txt         ← Python package list (pip install -r requirements.txt)
│   ├── alembic.ini              ← Alembic configuration (points at the alembic/ folder)
│   ├── pytest.ini               ← Pytest configuration (asyncio_mode=auto)
│   ├── .env.example             ← Template showing all required env variables
│   ├── .env.local               ← Safe dummy values for local dev (copy → .env)
│   └── .env                     ← Your actual secrets (never committed to Git)
│
├── frontend/                    ← React + TypeScript dashboard
│   └── src/
│       ├── App.tsx              ← Routes: /login, /onboarding, /dashboard, /catalogue, /settings
│       ├── main.tsx             ← React entry point
│       ├── api/client.ts        ← All API calls to the backend (axios wrapper)
│       ├── context/AuthContext.tsx ← Stores the JWT token and current user in memory
│       ├── components/          ← Reusable UI pieces (Layout, ProtectedRoute, StatCard)
│       └── pages/               ← Full page components
│           ├── Login.tsx        ← Register + login form
│           ├── Onboarding.tsx   ← 4-step wizard (business details, products, WhatsApp number)
│           ├── Dashboard.tsx    ← Usage bar, conversation list, lead summary
│           ├── Catalogue.tsx    ← Add / edit / delete products
│           ├── Conversations.tsx ← Conversation history viewer
│           ├── Leads.tsx        ← Lead list with hot/warm/cold badges
│           └── Settings.tsx     ← Edit system prompt, WhatsApp number, API key display
│
├── docker-compose.yml           ← Runs everything in Docker (postgres + backend + frontend)
├── setup.sh                     ← One-command local setup for Mac / Linux
├── setup.bat                    ← One-command local setup for Windows
├── check_requirements.sh        ← Checks Python / Postgres / Node are installed
├── README.md                    ← Technical overview and quick-start
└── HOW_TO_RUN.md                ← This file — step-by-step guide for anyone
```

---

## What is the difference between `backend/` files and `backend/app/`?

> Short answer: you never need to edit anything outside `backend/app/` for normal development.

The `backend/` folder is the project root for the Python backend. It contains:
- `app/` — the actual application code (this is what you edit)
- `alembic/` — database migration history
- `tests/` — tests and simulators
- `seed.py` — test data loader
- config files (`requirements.txt`, `pytest.ini`, `alembic.ini`)

The reason the code lives inside `app/` (not directly in `backend/`) is so Python can treat it as a proper package. This is the standard FastAPI convention and allows all the imports like `from app.services.gemini_service import generate_reply` to work correctly.

The uvicorn start command reflects this: `uvicorn app.main:app` means "find the `app` package, open `main.py` inside it, and use the `app` variable defined there."

---

## Cheat Sheet

```bash
# Activate virtual environment (do this every time you open Terminal)
cd ai-agent-platform/backend && source venv/bin/activate

# Start backend
uvicorn app.main:app --reload --port 8000

# Start frontend (separate Terminal)
cd ai-agent-platform/frontend && npm run dev

# Chat with the AI locally
python tests/whatsapp_simulator.py

# Run all tests
pytest

# Load / reload test data
python seed.py

# Check database tables
psql ai_agent_dev -c "SELECT phone_number, channel FROM conversations;"

# Apply new database migrations
alembic upgrade head

# Stop everything
Ctrl+C  (in each Terminal window)
```
