"""TaskPilot entry point — run bot + agent runner + web dashboard concurrently."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.config.settings import settings
from app.core.broker import recover_interrupted_chains, recover_interrupted_tasks
from app.core.db import init_db
from app.core.runner import AgentRunner
from app.telegram.bot import build_app, make_chain_notify_callback, make_notify_callback, make_progress_callback, make_typing_callback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("taskpilot")


async def _run() -> None:
    """Initialise DB, recover orphans, then run bot + runner."""
    await init_db()
    recovered = await recover_interrupted_tasks()
    if recovered:
        logger.info("Recovered %d interrupted tasks", recovered)
    recovered_chains = await recover_interrupted_chains()
    if recovered_chains:
        logger.info("Recovered %d interrupted chains", recovered_chains)

    from app.core.broker import purge_old_tasks
    purged = await purge_old_tasks(days=7)
    if purged:
        logger.info("Purged %d old tasks", purged)

    runner = AgentRunner()
    from app.core.broker import set_runner_wake
    set_runner_wake(runner.notify_new_task)

    app = build_app(runner=runner)
    notify = await make_notify_callback(app)
    runner._on_complete = notify
    chain_notify = await make_chain_notify_callback(app)
    runner._chain_notify = chain_notify
    progress_notify = await make_progress_callback(app)
    runner._progress_notify = progress_notify
    typing_notify = await make_typing_callback(app)
    runner._typing_notify = typing_notify

    # Initialize the application (connects to Telegram)
    await app.initialize()
    await app.start()

    # Register commands in Telegram's "/" menu
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("status", "Current task status"),
        BotCommand("queue", "Pending tasks"),
        BotCommand("history", "Recent tasks"),
        BotCommand("cancel", "Cancel a task"),
        BotCommand("retry", "Re-queue a failed task"),
        BotCommand("project", "Set project directory"),
        BotCommand("agent", "Set agent"),
        BotCommand("model", "Set model override"),
        BotCommand("output", "Get full task output"),
        BotCommand("repeat", "Repeat a task N times"),
        BotCommand("chains", "List saved chains"),
        BotCommand("recipes", "List saved recipes"),
        BotCommand("addrecipe", "Create a recipe"),
        BotCommand("delrecipe", "Delete a recipe"),
        BotCommand("recipe", "Show recipe details"),
        BotCommand("help", "Show all commands"),
    ])

    updater = app.updater
    await updater.start_polling(drop_pending_updates=True)

    logger.info("TaskPilot is running — press Ctrl+C to stop")

    runner_task = asyncio.create_task(runner.start())

    # Start web dashboard if enabled
    web_server = None
    web_task = None
    if settings.dashboard_enabled:
        try:
            import uvicorn

            from app.web.dashboard import create_dashboard_app

            dashboard = create_dashboard_app()
            config = uvicorn.Config(
                dashboard,
                host=settings.dashboard_host,
                port=settings.dashboard_port,
                log_level="warning",
                access_log=False,
            )
            web_server = uvicorn.Server(config)
            web_task = asyncio.create_task(web_server.serve())
            logger.info(
                "Dashboard running at http://%s:%d",
                settings.dashboard_host,
                settings.dashboard_port,
            )
        except ImportError:
            logger.warning("FastAPI/uvicorn not installed — dashboard disabled (pip install taskpilot[web])")

    shutdown_event = asyncio.Event()

    def _on_signal() -> None:
        runner.stop()
        if web_server is not None:
            web_server.should_exit = True
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await shutdown_event.wait()

    # Give runner a grace period to finish the current task
    grace = 10
    try:
        await asyncio.wait_for(runner_task, timeout=grace)
    except asyncio.TimeoutError:
        logger.warning("Runner did not stop within %ds, cancelling", grace)
        runner_task.cancel()
        try:
            await runner_task
        except asyncio.CancelledError:
            pass

    await runner.cancel_current()

    # Stop web dashboard
    if web_task is not None and web_server is not None:
        web_server.should_exit = True
        try:
            await asyncio.wait_for(web_task, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    await updater.stop()
    await app.stop()
    await app.shutdown()


def main() -> None:
    """Sync entry point for console_scripts."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
