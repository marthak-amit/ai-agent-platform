# AI Agent Platform

WhatsApp + Instagram AI chat agent SaaS for Indian businesses. Clients self-serve their own agent — product catalogue, lead tagging, UPI payments via Razorpay, AI replies via Google Gemini.

---

## Quick start — Docker (3 commands)

```bash
git clone <your-repo-url>
cd ai-agent-platform

# 1. Add your Gemini API key (the only required change)
echo "GEMINI_API_KEY=your_key_here" >> backend/.env.local

# 2. Start everything
docker compose up

# 3. Load realistic test data (run in a second terminal)
docker compose exec backend python seed.py
```

Docker starts three services: PostgreSQL 15 → FastAPI backend → Vite frontend.  
Alembic migrations run automatically before the backend starts.

> **Get a free Gemini key:** [aistudio.google.com](https://aistudio.google.com) → Get API Key → free tier is sufficient for local dev.

---

## Quick start — without Docker (Mac / Linux)

```bash
# One-time setup (creates venv, DB, runs migrations, seeds dev account)
chmod +x setup.sh && ./setup.sh

# Backend (in one terminal)
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Frontend (in another terminal)
cd frontend
npm install
npm run dev
```

**Windows:** run `setup.bat` instead of `setup.sh`.

---

## Access points

| Service | URL |
|---|---|
| Backend API | http://localhost:8000 |
| Interactive API docs | http://localhost:8000/docs |
| Frontend dashboard | http://localhost:3000 |
| Health check | http://localhost:8000/health |
| Admin endpoints | `GET /admin/clients` with header `X-Admin-Key: admin123` |

---

## Test the system

### Interactive WhatsApp simulator

Simulates a real customer sending WhatsApp messages — signs the Meta webhook payload and prints the AI reply.

```bash
cd backend
source venv/bin/activate        # skip if using Docker: docker compose exec backend bash

# Interactive chat (Hindi/Hinglish works)
python tests/whatsapp_simulator.py

# Run 5 canned saree-shop scenarios, then drop to interactive mode
python tests/whatsapp_simulator.py --demo

# Start a brand-new conversation (fresh phone number)
python tests/whatsapp_simulator.py --fresh
```

Commands inside the simulator: `/demo` `/reset` `/quit` `/help`

---

### Full end-to-end flow test

Proves the entire purchase journey — price enquiry → order → QR payment → confirmed — in one script.

```bash
python tests/full_flow_test.py
```

Walks through 8 steps and prints DB state, AI reply, and lead status after each one.  
Final summary shows total messages, lead status, order placed, and time taken.

---

### Instagram simulator

```bash
python tests/instagram_simulator.py           # interactive DM
python tests/instagram_simulator.py --demo    # 4 canned scenarios
python tests/instagram_simulator.py --fresh   # new IGSID

# Send a comment reply
You: comment: POST_ID_HERE this saree looks beautiful!

# Send a story reaction
You: story: loved the Banarasi collection!
```

---

### Payment simulator

Tests the full Razorpay QR → webhook → paid lifecycle without real credentials.

```bash
python tests/payment_simulator.py
```

Runs 7 steps: create payment → capture webhook → verify DB → wrong signature → duplicate idempotency.

---

### Unit test suite (132 tests)

```bash
cd backend
pytest                    # all 132 tests
pytest -v                 # verbose — see each test name
pytest tests/test_webhook.py -v          # single module
pytest tests/test_webhook.py::test_receive_message_success -v   # single test
```

---

## Seed data

`seed.py` creates a realistic Indian textile shop dataset. Safe to re-run — skips existing rows.

**Test client**
- Email: `riya@riyasarees.com`
- Password: `riya1234`
- Business: Riya Sarees Surat (Growth plan)

**Products loaded**

| Name | Price |
|---|---|
| Banarasi Silk Saree | ₹2,450 |
| Kanjivaram Silk Saree | ₹4,200 |
| Georgette Saree | ₹1,100 |
| Cotton Saree | ₹850 |
| Bridal Lehenga | ₹6,500 |

**Also creates:**
- 10 sample conversations (hot / warm / cold mix) with Hindi/Hinglish messages
- 7 days of usage log history
- Admin account: `admin@test.com` / `admin123`

---

## Environment variables

All variables live in `backend/.env` (local dev) or set as Railway env vars (production).  
Copy `backend/.env.example` → `backend/.env` and fill in values.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | `postgresql://user:pass@host/db` — asyncpg added automatically |
| `GEMINI_API_KEY` | Yes | Get from aistudio.google.com |
| `META_APP_SECRET` | Yes | Meta Developer Console → App → WhatsApp → App Secret |
| `META_VERIFY_TOKEN` | Yes | Any string — set same value in Meta Console |
| `WHATSAPP_ACCESS_TOKEN` | Yes | Temporary or permanent token from Meta |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | From Meta WhatsApp configuration |
| `INSTAGRAM_ACCESS_TOKEN` | Growth/Pro | Only needed for Instagram DM feature |
| `RAZORPAY_KEY_ID` | Payment | Razorpay Dashboard → Settings → API Keys |
| `RAZORPAY_KEY_SECRET` | Payment | Same — use test keys locally |
| `SECRET_KEY` | Yes | JWT signing secret — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_SECRET_KEY` | Yes | Sent as `X-Admin-Key` header to `/admin/*` routes |

`backend/.env.local` has safe dummy values for all non-Gemini variables — `docker compose up` uses it automatically.

---

## Common errors and fixes

### Port already in use

```
Error: address already in use :::8000
```

Find and kill the process using the port:

```bash
# Mac / Linux
lsof -ti:8000 | xargs kill -9
lsof -ti:3000 | xargs kill -9
lsof -ti:5432 | xargs kill -9

# Or run on different ports
uvicorn app.main:app --reload --port 8001
python tests/whatsapp_simulator.py --url http://localhost:8001
```

---

### DB connection failed

```
sqlalchemy.exc.OperationalError: could not connect to server
```

**With Docker:** wait a few seconds after `docker compose up` — the healthcheck retries until Postgres is ready, then the backend starts.

```bash
docker compose ps          # check all services are "running" / "healthy"
docker compose logs postgres
```

**Without Docker:** make sure PostgreSQL is running and the database exists.

```bash
# Mac
brew services start postgresql@15
createdb ai_agent_dev

# Linux
sudo service postgresql start
sudo -u postgres createdb ai_agent_dev

# Verify DATABASE_URL in backend/.env matches
```

If the URL uses `postgres://` instead of `postgresql://` — that is fine, `config.py` rewrites it automatically.

---

### Gemini API key invalid

```
google.api_core.exceptions.InvalidArgument: API key not valid
```

1. Open `backend/.env` (or `backend/.env.local`).
2. Replace `your_real_key_here` with your actual key from [aistudio.google.com](https://aistudio.google.com).
3. Restart the backend — `get_settings()` is cached at startup.

```bash
# Quick test without running the full server
cd backend && source venv/bin/activate
python -c "
from app.config import get_settings
s = get_settings()
print('Key starts with:', s.gemini_api_key[:8])
"
```

---

### Meta webhook signature errors in test mode

```
HTTP 401 — Invalid signature
```

The WhatsApp and payment simulators sign payloads with `META_APP_SECRET` and `RAZORPAY_KEY_SECRET` from your `.env`.  
The backend verifies with the same values. They must match.

```bash
# Check what the backend loaded
cd backend && source venv/bin/activate
python -c "
from app.config import get_settings
s = get_settings()
print('META_APP_SECRET:', s.meta_app_secret)
print('RAZORPAY_KEY_SECRET:', s.razorpay_key_secret)
"
```

When running simulators from outside Docker (against a Docker backend), the `.env.local` file is the source of truth for both the host script and the container — they should already match.

---

### WhatsApp send errors (expected in local dev)

```
httpx.HTTPStatusError: 401 — OAuthException
```

The backend logs `WhatsApp send error` but still returns HTTP 200 — Meta requires 200 even on partial failure. The AI reply is saved to the database regardless. The simulator retrieves the reply from the DB, so this error does not affect local testing.

To suppress it: set `WHATSAPP_ACCESS_TOKEN=test_token` (already done in `.env.local`).

---

### Alembic migration errors on first boot

```
alembic.util.exc.CommandError: Can't locate revision
```

The database has a stale migration history. Reset and re-migrate:

```bash
# Without Docker
cd backend && source venv/bin/activate
alembic downgrade base
alembic upgrade head

# With Docker
docker compose down -v          # wipes the postgres_data volume
docker compose up               # migrations re-run automatically
```

---

## How to add a new test client

**Option A — via the React dashboard (recommended)**

1. Open http://localhost:3000
2. Click "Register" → fill in email / password / business name
3. Complete the 4-step onboarding wizard
4. Use the generated API key shown at the end

**Option B — via the API directly**

```bash
# Register
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"shop@example.com","password":"pass1234","business_name":"My Shop"}'

# Login → get JWT token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"shop@example.com","password":"pass1234"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Complete onboarding
curl -X POST http://localhost:8000/onboarding/setup-agent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "business_type": "textile",
    "business_description": "Premium sarees from Surat",
    "whatsapp_number": "919900000002",
    "products": [{"name":"Cotton Saree","price":800,"description":"Soft cotton"}]
  }'
```

**Option C — add to seed.py**

Open `backend/seed.py` and copy the `_seed_client()` block with new values. Re-run `python seed.py` — it skips existing rows.

---

## How to reset the database

**Wipe everything and start fresh (Docker)**

```bash
docker compose down -v          # removes postgres_data volume
docker compose up               # fresh DB + auto-migrations
docker compose exec backend python seed.py   # reload test data
```

**Wipe everything (without Docker)**

```bash
cd backend && source venv/bin/activate
alembic downgrade base          # drops all tables
alembic upgrade head            # recreates schema
python seed.py                  # reloads test data
```

**Wipe only conversations and leads (keep client + products)**

```bash
# Connect to psql
psql postgresql://localhost/ai_agent_dev

# Truncate in dependency order
TRUNCATE messages, conversations, leads, payments, usage_logs RESTART IDENTITY CASCADE;
\q
```

---

## How to view all logs

**Docker — tail all services together**

```bash
docker compose logs -f
```

**Docker — single service**

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f postgres
```

**Without Docker — uvicorn writes to stdout**

The backend logs every webhook call, Gemini call, lead tag, and error at INFO level.  
To increase verbosity:

```bash
uvicorn app.main:app --reload --port 8000 --log-level debug
```

**Filter for specific events**

```bash
# All webhook hits
docker compose logs -f backend | grep "Message from"

# Gemini errors only
docker compose logs -f backend | grep "Gemini"

# Lead tagging
docker compose logs -f backend | grep "Lead classification"

# Payment events
docker compose logs -f backend | grep -E "payment|Payment"
```

**Open a psql shell to query the DB directly**

```bash
# Docker
docker compose exec postgres psql -U postgres -d ai_agent_dev

# Without Docker
psql postgresql://localhost/ai_agent_dev
```

Useful queries:

```sql
-- All conversations with message counts
SELECT c.phone_number, c.channel, COUNT(m.id) AS messages
FROM conversations c
LEFT JOIN messages m ON m.conversation_id = c.id
GROUP BY c.id ORDER BY messages DESC;

-- Lead status summary
SELECT status, COUNT(*) FROM leads GROUP BY status;

-- Recent payments
SELECT phone_number, amount/100 AS inr, status, created_at
FROM payments ORDER BY created_at DESC LIMIT 10;

-- Today's usage per client
SELECT client_id, message_count FROM usage_logs WHERE log_date = CURRENT_DATE;
```

---

## Project structure

```
ai-agent-platform/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory + router registration
│   │   ├── config.py            # Settings (pydantic-settings, reads .env)
│   │   ├── db.py                # Async SQLAlchemy engine + session factory
│   │   ├── models/              # SQLAlchemy ORM models
│   │   ├── routers/             # One file per feature (webhook, payment, auth …)
│   │   ├── services/            # Business logic (gemini, whatsapp, lead, …)
│   │   └── schemas/             # Pydantic request/response models
│   ├── alembic/                 # DB migration scripts
│   ├── tests/
│   │   ├── conftest.py          # Shared fixtures (mock_settings, mock_db, client)
│   │   ├── test_*.py            # 132 unit tests
│   │   ├── whatsapp_simulator.py
│   │   ├── instagram_simulator.py
│   │   ├── payment_simulator.py
│   │   └── full_flow_test.py    # End-to-end saree purchase scenario
│   ├── seed.py                  # Realistic test data (Riya Sarees Surat)
│   ├── .env.example             # Template — copy to .env and fill in values
│   ├── .env.local               # Safe dummy values for Docker local dev
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── pages/               # Login, Onboarding, Dashboard, Catalogue, Settings
│       ├── components/          # Layout, nav
│       └── api/client.ts        # Axios wrapper for all backend calls
├── docker-compose.yml
├── setup.sh                     # One-command local setup (Mac / Linux)
├── setup.bat                    # One-command local setup (Windows)
└── README.md
```

---

## Tech stack

| Layer | Technology |
|---|---|
| AI | Google Gemini 2.0 Flash |
| Backend | FastAPI + SQLAlchemy async + asyncpg |
| Database | PostgreSQL 15 |
| Auth | JWT (python-jose HS256) + bcrypt |
| Messaging | Meta Cloud API (WhatsApp + Instagram) |
| Payments | Razorpay UPI QR codes |
| Frontend | React + Vite + Tailwind CSS |
| Hosting | Railway (backend + frontend + Postgres add-on) |

---

## Deployment (Railway)

1. Push to GitHub.
2. Create a new Railway project → "Deploy from GitHub repo".
3. Add two services: `backend/` and `frontend/`.
4. Add a PostgreSQL add-on → Railway injects `DATABASE_URL` automatically.
5. Set all env vars from `.env.example` in Railway's Variables tab.
6. Set `VITE_BACKEND_URL` on the frontend service to your backend Railway URL.
7. On first deploy, migrations run automatically via `docker-entrypoint.sh`.
