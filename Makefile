.PHONY: setup run run-bg stop logs test lint clean help

PYTHON ?= python3
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Full setup (venv + deps + .env)
	bash setup.sh

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip -q

install: $(VENV)/bin/activate ## Install all dependencies
	$(PIP) install -e ".[web,dev]" -q

run: ## Run OpenTask (foreground)
	$(PY) -m app

run-bg: ## Run OpenTask in background (logs → data/bot.log)
	@mkdir -p data
	nohup $(PY) -m app > data/bot.log 2>&1 & echo "PID: $$!"

stop: ## Stop background OpenTask
	@pkill -f "python.*-m app" 2>/dev/null && echo "Stopped" || echo "Not running"

restart: stop ## Restart background OpenTask
	@sleep 1
	@$(MAKE) run-bg

logs: ## Tail the bot log
	tail -f data/bot.log

test: ## Run all tests
	$(PY) -m pytest tests/ -v

test-quick: ## Run tests (quiet, stop on first failure)
	$(PY) -m pytest tests/ -q -x --tb=short

clean: ## Remove caches and compiled files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf *.egg-info dist build htmlcov .coverage
