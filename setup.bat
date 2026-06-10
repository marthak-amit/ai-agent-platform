@echo off
setlocal EnableDelayedExpansion
:: ─────────────────────────────────────────────────────────────────────────────
:: AI Agent Platform — Local Development Setup (Windows)
::
:: Usage: Double-click setup.bat, or run from a terminal:
::   setup.bat
::
:: Requirements:
::   - Python 3.10+ from https://www.python.org/downloads/
::     (check "Add Python to PATH" during install)
::   - PostgreSQL from https://www.postgresql.org/download/windows/
::     (note the superuser password you set during install)
::
:: Mac / Linux users: run setup.sh instead.
:: ─────────────────────────────────────────────────────────────────────────────

echo.
echo ==================================================
echo   AI Agent Platform -- Local Dev Setup (Windows)
echo ==================================================
echo.

:: ── Paths ─────────────────────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
set BACKEND=%SCRIPT_DIR%backend
set VENV=%BACKEND%\venv
set PYTHON=%VENV%\Scripts\python.exe
set PIP=%VENV%\Scripts\pip.exe
set ALEMBIC=%VENV%\Scripts\alembic.exe
set DB_NAME=ai_agent_dev

:: ── Step 1: Python check ──────────────────────────────────────────────────────
echo [Step 1/7] Checking Python...

python --version >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo   Install Python 3.10 or higher from:
    echo   https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [ok] Found Python %PY_VER%

:: ── Step 2: Virtual environment ───────────────────────────────────────────────
echo.
echo [Step 2/7] Setting up virtual environment...

if exist "%VENV%" (
    echo [warn] venv already exists at backend\venv\ -- skipping creation
) else (
    python -m venv "%VENV%"
    echo [ok] Created virtual environment at backend\venv\
)

:: ── Step 3: Install dependencies ──────────────────────────────────────────────
echo.
echo [Step 3/7] Installing Python dependencies (this may take a minute)...

"%PIP%" install --upgrade pip --quiet
"%PIP%" install -r "%BACKEND%\requirements.txt" --quiet
echo [ok] Dependencies installed

:: ── Step 4: Environment file ──────────────────────────────────────────────────
echo.
echo [Step 4/7] Configuring environment...

set ENV_FILE=%BACKEND%\.env
set ENV_LOCAL=%BACKEND%\.env.local

if exist "%ENV_FILE%" (
    echo [warn] .env already exists -- skipping copy to avoid overwriting your config
) else (
    if not exist "%ENV_LOCAL%" (
        echo [ERROR] backend\.env.local not found -- cannot create .env
        pause
        exit /b 1
    )
    copy "%ENV_LOCAL%" "%ENV_FILE%" >NUL
    echo [ok] Copied .env.local to .env
    echo [warn] Open backend\.env and set GEMINI_API_KEY to your real key.
)

:: ── Step 5: PostgreSQL ────────────────────────────────────────────────────────
echo.
echo [Step 5/7] Setting up PostgreSQL database...

where psql >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] psql not found in PATH.
    echo.
    echo   Install PostgreSQL from:
    echo   https://www.postgresql.org/download/windows/
    echo   Then add its bin\ folder to your PATH, e.g.:
    echo   C:\Program Files\PostgreSQL\16\bin
    pause
    exit /b 1
)

:: Try to create the database. Fail silently if it already exists.
psql -U postgres -c "CREATE DATABASE %DB_NAME%;" 2>NUL
if errorlevel 1 (
    echo [warn] Database '%DB_NAME%' may already exist -- skipping creation
) else (
    echo [ok] Created database '%DB_NAME%'
)

:: ── Step 6: Migrations ────────────────────────────────────────────────────────
echo.
echo [Step 6/7] Running database migrations...

cd "%BACKEND%"
"%ALEMBIC%" upgrade head
echo [ok] All migrations applied

:: ── Step 7: Seed account ──────────────────────────────────────────────────────
echo.
echo [Step 7/7] Creating seed developer account...

"%PYTHON%" scripts\seed.py

:: ── Done ──────────────────────────────────────────────────────────────────────
echo.
echo ==================================================
echo   Setup complete!
echo ==================================================
echo.
echo Next steps:
echo.
echo   1. Edit backend\.env and set your GEMINI_API_KEY:
echo      https://aistudio.google.com
echo.
echo   2. Start the backend (in a new terminal):
echo      cd backend
echo      venv\Scripts\activate
echo      uvicorn app.main:app --reload --port 8000
echo.
echo   3. Start the frontend (in another terminal):
echo      cd frontend
echo      npm install
echo      npm run dev
echo.
echo   4. Open http://localhost:5173 in your browser.
echo.
echo   5. API docs: http://localhost:8000/docs
echo.
echo Note: WhatsApp webhooks need a public URL.
echo   Use ngrok: ngrok http 8000
echo   Then set the webhook in your Meta Developer Console.
echo.
pause
