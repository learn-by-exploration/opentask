#!/usr/bin/env bash
# OpenTask — one-command setup
# Usage: bash setup.sh
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }
header(){ echo -e "\n${BOLD}── $* ──${NC}"; }

header "OpenTask Setup"

# ── 1. Python check ─────────────────────────────────────────────
header "Checking Python"
if command -v python3 &>/dev/null; then
    PY=$(python3 --version)
    info "Found $PY"
    # Check >= 3.9
    python3 -c "import sys; exit(0 if sys.version_info >= (3,9) else 1)" \
        || fail "Python 3.9+ required (found $PY)"
else
    fail "python3 not found — install Python 3.9+"
fi

# ── 2. Virtual environment ──────────────────────────────────────
header "Setting up virtual environment"
if [[ -d ".venv" ]]; then
    info "Virtual environment already exists (.venv/)"
else
    python3 -m venv .venv
    info "Created .venv/"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
info "Activated .venv ($(python3 --version))"

# ── 3. Install dependencies ─────────────────────────────────────
header "Installing dependencies"
pip install --upgrade pip -q
pip install -e ".[web,dev]" -q
info "Installed app + web dashboard + dev/test deps"

# ── 4. Environment file ─────────────────────────────────────────
header "Configuring environment"
if [[ -f ".env" ]]; then
    info ".env already exists — keeping your config"
else
    cp .env.example .env
    warn "Created .env from template — edit it now:"
    echo "     TELEGRAM_BOT_TOKEN=<your token from @BotFather>"
    echo "     ALLOWED_USER_IDS=<your Telegram user ID>"
    echo "     DEFAULT_PROJECT_DIR=<path to your project>"
    echo "     ALLOWED_PROJECT_DIRS=<comma-separated paths>"
fi

# ── 5. Data directory ───────────────────────────────────────────
mkdir -p data
info "data/ directory ready"

# ── 6. Verify agent CLIs ────────────────────────────────────────
header "Checking agent CLIs"
if command -v opencode &>/dev/null; then
    info "opencode found at $(command -v opencode)"
else
    warn "opencode not found — install it or update AGENT_COMMANDS in .env"
fi
if command -v claude &>/dev/null; then
    info "claude found at $(command -v claude)"
else
    warn "claude CLI not found — install it or update AGENT_COMMANDS in .env"
fi

# ── 7. Run tests ────────────────────────────────────────────────
header "Running tests"
if python3 -m pytest tests/ -q --tb=line 2>/dev/null; then
    info "All tests passed"
else
    warn "Some tests failed — run 'pytest tests/ -v' for details"
fi

# ── 8. Summary ──────────────────────────────────────────────────
header "Ready!"
echo ""
echo "  To run OpenTask:"
echo ""
echo "    source .venv/bin/activate"
echo "    python -m app"
echo ""
echo "  Or use the Makefile:"
echo ""
echo "    make run          # start the bot"
echo "    make test         # run tests"
echo "    make logs         # tail the log"
echo ""
echo "  Dashboard: http://127.0.0.1:8095 (when running)"
echo ""
