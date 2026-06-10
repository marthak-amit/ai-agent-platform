#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AI Agent Platform — Local Development Setup (Mac / Linux)
#
# Usage:
#   chmod +x setup.sh && ./setup.sh
#
# What this script does:
#   1. Checks system requirements (Python 3.10+, PostgreSQL)
#   2. Creates a Python virtual environment in backend/venv/
#   3. Installs all Python dependencies
#   4. Copies backend/.env.local → backend/.env (skips if .env already exists)
#   5. Creates the local PostgreSQL database "ai_agent_dev"
#   6. Runs all Alembic database migrations
#   7. Seeds a default developer account
#   8. Prints next-steps summary
#
# Windows users: run setup.bat instead.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Terminal colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[info]${NC}  $*"; }
success() { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
error()   { echo -e "${RED}[error]${NC} $*" >&2; }
step()    { echo -e "\n${BOLD}── $* ──${NC}"; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║   AI Agent Platform — Local Dev Setup   ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Detect OS ─────────────────────────────────────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLATFORM="mac"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    PLATFORM="linux"
else
    error "Unsupported platform: $OSTYPE"
    error "Please run setup.bat on Windows."
    exit 1
fi
info "Platform: $PLATFORM"

# ── Resolve paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"
VENV="$BACKEND/venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
ALEMBIC="$VENV/bin/alembic"
DB_NAME="ai_agent_dev"

# ── Step 1: Python version check ──────────────────────────────────────────────
step "Checking Python"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=${ver%%.*}
        minor=${ver##*.}
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    error "Python 3.10 or higher is required but was not found."
    if [[ "$PLATFORM" == "mac" ]]; then
        echo "  Install via Homebrew:  brew install python@3.12"
        echo "  Or download from:      https://www.python.org/downloads/"
    else
        echo "  Ubuntu/Debian:  sudo apt install python3.12 python3.12-venv"
        echo "  Fedora/RHEL:    sudo dnf install python3.12"
    fi
    exit 1
fi

success "Found $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# ── Step 2: Virtual environment ───────────────────────────────────────────────
step "Setting up virtual environment"

if [[ -d "$VENV" ]]; then
    warn "venv already exists at backend/venv/ — skipping creation"
else
    "$PYTHON_BIN" -m venv "$VENV"
    success "Created virtual environment at backend/venv/"
fi

# ── Step 3: Install requirements ──────────────────────────────────────────────
step "Installing Python dependencies"

"$PIP" install --upgrade pip --quiet
"$PIP" install -r "$BACKEND/requirements.txt" --quiet
success "Dependencies installed"

# ── Step 4: Environment file ──────────────────────────────────────────────────
step "Configuring environment"

ENV_FILE="$BACKEND/.env"
ENV_LOCAL="$BACKEND/.env.local"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists — skipping copy to avoid overwriting your config"
    warn "If you want a fresh config, delete backend/.env and re-run this script."
else
    if [[ ! -f "$ENV_LOCAL" ]]; then
        error "backend/.env.local not found — cannot create .env"
        exit 1
    fi
    cp "$ENV_LOCAL" "$ENV_FILE"
    success "Copied .env.local → .env"
    warn "Open backend/.env and set GEMINI_API_KEY to your real key before starting the server."
fi

# ── Step 5: PostgreSQL ────────────────────────────────────────────────────────
step "Setting up PostgreSQL database"

# Check psql is available
if ! command -v psql &>/dev/null; then
    error "PostgreSQL client (psql) not found."
    if [[ "$PLATFORM" == "mac" ]]; then
        echo "  Install via Homebrew:   brew install postgresql@16"
        echo "  Then start the service: brew services start postgresql@16"
    else
        echo "  Ubuntu/Debian:  sudo apt install postgresql postgresql-client"
        echo "  Fedora/RHEL:    sudo dnf install postgresql postgresql-server"
        echo "  Then start:     sudo systemctl start postgresql"
    fi
    exit 1
fi

# Check if the database already exists
DB_EXISTS=false
if [[ "$PLATFORM" == "mac" ]]; then
    # On Mac (Homebrew), psql runs as the current user
    if psql -lqt 2>/dev/null | cut -d '|' -f1 | grep -qw "$DB_NAME"; then
        DB_EXISTS=true
    fi
else
    # On Linux, the postgres superuser owns the cluster
    if sudo -u postgres psql -lqt 2>/dev/null | cut -d '|' -f1 | grep -qw "$DB_NAME"; then
        DB_EXISTS=true
    fi
fi

if $DB_EXISTS; then
    warn "Database '$DB_NAME' already exists — skipping creation"
else
    if [[ "$PLATFORM" == "mac" ]]; then
        createdb "$DB_NAME"
    else
        sudo -u postgres createdb "$DB_NAME"
        # Grant the current user access so migrations can run without sudo
        CURRENT_USER="$(whoami)"
        sudo -u postgres psql -c "
            DO \$\$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${CURRENT_USER}') THEN
                    CREATE ROLE \"${CURRENT_USER}\" LOGIN;
                END IF;
            END
            \$\$;
            GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO \"${CURRENT_USER}\";
        " 2>/dev/null || true
    fi
    success "Created database '$DB_NAME'"
fi

# ── Step 6: Migrations ────────────────────────────────────────────────────────
step "Running database migrations"

cd "$BACKEND"
"$ALEMBIC" upgrade head
success "All migrations applied"

# ── Step 7: Seed admin account ────────────────────────────────────────────────
step "Creating seed developer account"

"$PYTHON" scripts/seed.py

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Setup complete!${NC}"
echo ""
echo -e "${BOLD}Next steps:${NC}"
echo ""
echo "  1. Edit backend/.env and set your GEMINI_API_KEY:"
echo "     https://aistudio.google.com"
echo ""
echo "  2. Start the backend API server:"
echo "     cd backend"
echo "     source venv/bin/activate"
echo "     uvicorn app.main:app --reload --port 8000"
echo ""
echo "  3. In a second terminal, start the frontend dev server:"
echo "     cd frontend"
echo "     npm install && npm run dev"
echo ""
echo "  4. Open http://localhost:5173 in your browser."
echo ""
echo "  5. API docs:    http://localhost:8000/docs"
echo "  6. Health:      http://localhost:8000/health"
echo ""
echo -e "${YELLOW}Note:${NC} WhatsApp webhooks require a public URL."
echo "  Use ngrok for local testing: ngrok http 8000"
echo "  Then set the webhook URL in your Meta Developer Console."
echo ""
