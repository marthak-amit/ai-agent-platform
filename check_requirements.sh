#!/usr/bin/env bash
# check_requirements.sh — verify local dev prerequisites on macOS

PASS="✅"
FAIL="❌"

check_python() {
    local py
    # prefer python3, fall back to python
    if command -v python3 &>/dev/null; then
        py="python3"
    elif command -v python &>/dev/null; then
        py="python"
    else
        echo "$FAIL  Python 3.11+     not found"
        echo "       install:  brew install python@3.13"
        return
    fi

    local ver
    ver=$("$py" -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))")
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)

    if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
        echo "$PASS  Python $ver        $("$py" --version 2>&1)"
    else
        echo "$FAIL  Python $ver        need 3.11+"
        echo "       install:  brew install python@3.13"
    fi
}

check_pip() {
    if command -v pip3 &>/dev/null; then
        echo "$PASS  pip              $(pip3 --version)"
    elif command -v pip &>/dev/null; then
        echo "$PASS  pip              $(pip --version)"
    else
        echo "$FAIL  pip              not found"
        echo "       install:  python3 -m ensurepip --upgrade"
    fi
}

check_postgres() {
    if command -v psql &>/dev/null; then
        local ver
        ver=$(psql --version | awk '{print $3}')
        echo "$PASS  PostgreSQL       psql $ver"
    else
        echo "$FAIL  PostgreSQL       not found"
        echo "       install:  brew install postgresql@15 && brew services start postgresql@15"
    fi
}

check_node() {
    if command -v node &>/dev/null; then
        echo "$PASS  Node.js          $(node --version)"
    else
        echo "$FAIL  Node.js          not found"
        echo "       install:  brew install node"
    fi
}

check_npm() {
    if command -v npm &>/dev/null; then
        echo "$PASS  npm              $(npm --version)"
    else
        echo "$FAIL  npm              not found"
        echo "       install:  brew install node   (npm is bundled with node)"
    fi
}

echo ""
echo "  Checking local dev requirements…"
echo "  ─────────────────────────────────"
check_python
check_pip
check_postgres
check_node
check_npm
echo "  ─────────────────────────────────"
echo ""
