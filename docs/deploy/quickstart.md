# Quick Start

## Prerequisites

- **Python 3.9+** — `python3 --version`
- **An AI coding agent** — at least one of:
  - [OpenCode](https://github.com/opencode-ai/opencode) — `opencode run` (default)
  - [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) — `claude -p`
- **Telegram account** — create a bot via [@BotFather](https://t.me/BotFather) and get your user ID from [@userinfobot](https://t.me/userinfobot)

## One-Command Setup

```bash
git clone git@github.com:learn-by-exploration/opentask.git
cd opentask
bash setup.sh
```

This creates a virtualenv, installs dependencies, runs tests, and creates `.env` from the template.

Edit `.env` with your Telegram bot token and user ID, then:

```bash
source .venv/bin/activate
python -m app
```

## Manual Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # edit with your config
python -m app
```

## Getting a Telegram Bot Token

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts
3. Copy the token to `TELEGRAM_BOT_TOKEN` in `.env`

## Finding Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with your numeric user ID
3. Add it to `ALLOWED_USER_IDS` in `.env`

## Running in Background

```bash
make run-bg          # start in background (logs → data/bot.log)
make logs            # tail the log
make stop            # stop the bot
make restart         # stop + start
```

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make setup` | Full setup via setup.sh |
| `make install` | Install all dependencies into .venv |
| `make run` | Run in foreground |
| `make run-bg` | Run in background |
| `make stop` | Stop background process |
| `make restart` | Stop + start |
| `make logs` | Tail the bot log |
| `make test` | Run all tests (verbose) |
| `make test-quick` | Quick test (stop on first failure) |
| `make clean` | Remove caches and build artifacts |

## Running Tests

```bash
source .venv/bin/activate
make test              # verbose
make test-quick        # stop on first failure
pytest tests/ -v       # directly
```
