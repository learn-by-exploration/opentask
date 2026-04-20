# Kickoff Discovery Log — TaskPilot (Full Dry Run)

> If we were starting TaskPilot from scratch today, this is what the discovery gate would produce.
> Dry run performed 2026-04-19 against live GitHub search and PyPI.

## 1. Repository Search

### Searches Executed (10 queries)

```bash
# Core concept
gh search repos "telegram bot task queue python sqlite" --sort=stars --limit=5
gh search repos "telegram ai agent subprocess python" --sort=stars --limit=5
gh search repos "telegram coding assistant bot" --sort=stars --limit=5
gh search repos "telegram remote command runner python" --sort=stars --limit=5

# Broader
gh search repos "python telegram bot" --sort=stars --limit=8
gh search repos "telegram llm bot python" --sort=stars --limit=5
gh search repos "task chain orchestrator python asyncio sqlite" --sort=stars --limit=5
gh search repos "python subprocess job queue asyncio" --sort=stars --limit=5

# Code-level
gh search code "async def enqueue_task" --limit=5
gh search code "add_signal_handler SIGTERM runner" --limit=5
```

### Top Candidates

| # | Repo | Stars | Stack | Relevance | Reuse? |
|---|------|-------|-------|-----------|--------|
| 1 | python-telegram-bot/python-telegram-bot | 29k | Python | Framework we build on | YES — already using as dependency |
| 2 | ohld/django-telegram-bot | 724 | Django+Celery+Redis+Postgres | Telegram + task queue template | NO — requires Django, Redis, Postgres (violates minimal-infra constraint) |
| 3 | poco-ai/poco-claw | 1288 | FastAPI+SQLAlchemy+Docker | Claude Code agent with task queue, sessions, IM support | NO — full web platform (FastAPI+React), 50+ services, massive deps |
| 4 | gavraq/telegram-claude-agent | 0 | Python, FastAPI | Telegram + Claude Agent SDK gateway | NO — uses Claude SDK (not subprocess), no task queue, no tests |
| 5 | Timosan61/telegram-claude-agent | 1 | Python, Streamlit | Telegram + Claude + web UI | NO — web-first (Streamlit), empty tests dir |
| 6 | rabilrbl/gemini-pro-bot | 167 | Python | Telegram + Gemini LLM | NO — direct API wrapper, no queue/subprocess |

### Patterns Inspected (Code)

| Source | File | Pattern | Adopted? | Reason |
|--------|------|---------|----------|--------|
| poco-ai/poco-claw | backend/app/api/v1/tasks.py | enqueue_task() service-repo pattern | Studied, not adopted | Over-engineered for single-user; our broker.py is simpler |
| poco-ai/poco-claw | backend/app/services/task_service.py | Model validation + session mgmt | Studied, not adopted | 50+ service files; suits multi-user SaaS, not single-user daemon |
| gavraq/telegram-claude-agent | gateway/src/telegram_gateway/bot.py | split_long_message() | Studied, not adopted | Simple char-split; we use smart summarization with tail preservation |
| ohld/django-telegram-bot | tgbot/dispatcher.py | Handler registration pattern | Partial influence | Confirms python-telegram-bot handler pattern is standard |

## 2. Package Registry Check

| Package | Latest | What It Does | Fits TaskPilot? | Decision |
|---------|--------|-------------|-----------------|----------|
| python-telegram-bot | 22.5 | Telegram Bot API wrapper | YES | Already using |
| SQLAlchemy + aiosqlite | 2.0.49 | Async ORM for SQLite | YES | Already using |
| arq | 0.28.0 | Async Redis job queue | NO — requires Redis | Skip |
| celery | 5.4+ | Distributed task queue | NO — requires Redis/RabbitMQ | Skip — overkill |
| dramatiq | 1.17+ | Task queue with broker | NO — requires broker infra | Skip |
| huey | 3.0.0 | Lightweight queue (SQLite available) | CLOSEST FIT | Skip — see evaluation below |
| rq | 1.16+ | Simple Redis queue | NO — requires Redis | Skip |

### huey Deep Evaluation (closest competitor)

huey's SQLiteHuey was the single closest external option. Rejected because:
1. No chain orchestration — we need multi-step task chains with advance/fail semantics
2. No recovery — we recover orphaned running tasks on restart; huey does not
3. No Telegram integration — we would still build bot.py, runner.py, and the glue
4. Extra dependency for ~50 lines of broker CRUD we already own
5. Testing — our broker is 100% covered; huey would be a black box

## 3. Build vs Reuse Decision

- [x] BUILD — no suitable match because:
  - Our exact combination (Telegram long-poll + SQLite broker + subprocess agent + chain orchestration + crash recovery + zero-infra) does not exist
  - Closest repo (poco-claw, 1288 stars) is a full web platform — extracting just the task queue would be harder than building from scratch
  - Closest package (huey) covers ~30% of our broker needs but misses chains, recovery, and would add a dependency for minimal gain

**Rationale**: TaskPilot's value is the specific integration of Telegram, SQLite task queue, subprocess agent execution, chain orchestration, and crash recovery — all in a single asyncio process with zero external services. No discovered repo or package covers more than 30% of this combination. Clean build is the efficient path.

## 4. Future Reuse Opportunities

| When Scope Expands To | Reuse From |
|----------------------|------------|
| Claude Agent SDK mode (not subprocess) | gavraq/telegram-claude-agent runner.py |
| Web dashboard | poco-claw frontend patterns |
| Multi-user / multi-machine | poco-claw session_queue_service.py |
| Heavy job queue with retries | huey (SQLiteHuey backend) |
| Voice/photo input | zack-lau/telegram-claude-agent |

## Sign-off

- Searched by: AI agent (dry run)
- Date: 2026-04-19
- Queries executed: 10 (8 repo, 2 code)
- Repos inspected: 6
- Packages evaluated: 7
- Decision: BUILD — validated with evidence
- Approved to proceed: [x] Yes
