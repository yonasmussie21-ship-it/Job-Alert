"""
bot.py

Safe Python restore for the Amazon Jobs bot entrypoint.

Use this to replace the accidental Bash deployment script that was saved as bot.py.
This file is intentionally defensive: it first tries to run main.py, and if your
main module does not expose a runnable entrypoint, it falls back to starting the
Telegram update loop and scheduler directly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _get_callable(module: Any, names: tuple[str, ...]) -> Optional[Callable[..., Any]]:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


async def run_main_module() -> bool:
    """Prefer your existing main.py if it has a runnable entrypoint."""
    try:
        import main as main_module
    except Exception as exc:
        log.warning("[BOT_RESTORE] Could not import main.py: %s", exc)
        return False

    entrypoint = _get_callable(
        main_module,
        (
            "main",
            "run",
            "start",
            "serve",
        ),
    )

    if not entrypoint:
        log.warning("[BOT_RESTORE] main.py imported but no main/run/start/serve function found")
        return False

    log.info("[BOT_RESTORE] Starting main.py entrypoint: %s", entrypoint.__name__)
    await _maybe_await(entrypoint())
    return True


async def load_state() -> dict[str, Any]:
    """Load bot state using storage.py when available."""
    state: dict[str, Any] = {
        "subscribers": {},
        "known_jobs": {},
        "job_history": [],
        "accounts": [],
        "bot_paused": False,
    }

    try:
        import storage

        loaders = {
            "subscribers": ("load_subscribers", "get_subscribers"),
            "known_jobs": ("load_known_jobs", "get_known_jobs"),
            "job_history": ("load_job_history", "get_job_history"),
            "accounts": ("load_accounts", "get_accounts"),
        }

        for key, names in loaders.items():
            fn = _get_callable(storage, names)
            if fn:
                try:
                    value = await _maybe_await(fn())
                    if value is not None:
                        state[key] = value
                except Exception as exc:
                    log.warning("[BOT_RESTORE] Failed loading %s: %s", key, exc)

    except Exception as exc:
        log.warning("[BOT_RESTORE] Could not import storage.py: %s", exc)

    return state


async def fallback_scheduler_loop(state: dict[str, Any], stop_event: asyncio.Event) -> None:
    """Run scheduler.check_jobs repeatedly if no main.py entrypoint exists."""
    try:
        import scheduler
    except Exception as exc:
        log.error("[BOT_RESTORE] Could not import scheduler.py: %s", exc)
        return

    check_jobs = _get_callable(scheduler, ("check_jobs", "scan_jobs", "run_once"))
    if not check_jobs:
        log.error("[BOT_RESTORE] scheduler.py has no check_jobs/scan_jobs/run_once function")
        return

    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "10"))
    log.info("[BOT_RESTORE] Fallback scheduler loop started interval=%ss", interval)

    while not stop_event.is_set():
        try:
            await _maybe_await(check_jobs(state))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[BOT_RESTORE] Scheduler loop error: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def fallback_telegram_loop(state: dict[str, Any]) -> None:
    """Run telegram_bot.handle_updates(state) if available."""
    try:
        import telegram_bot
    except Exception as exc:
        log.error("[BOT_RESTORE] Could not import telegram_bot.py: %s", exc)
        return

    handle_updates = _get_callable(telegram_bot, ("handle_updates", "start_bot", "run_bot"))
    if not handle_updates:
        log.error("[BOT_RESTORE] telegram_bot.py has no handle_updates/start_bot/run_bot function")
        return

    log.info("[BOT_RESTORE] Fallback Telegram loop started: %s", handle_updates.__name__)

    try:
        await _maybe_await(handle_updates(state))
    except TypeError:
        # Some bot starters may not accept state.
        await _maybe_await(handle_updates())


async def run_fallback() -> None:
    """Fallback runner when main.py cannot be used."""
    state = await load_state()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    scheduler_task = asyncio.create_task(
        fallback_scheduler_loop(state, stop_event),
        name="scheduler-loop",
    )
    telegram_task = asyncio.create_task(
        fallback_telegram_loop(state),
        name="telegram-loop",
    )

    done, pending = await asyncio.wait(
        {scheduler_task, telegram_task},
        return_when=asyncio.FIRST_EXCEPTION,
    )

    stop_event.set()

    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc:
            raise exc


async def app() -> None:
    setup_logging()
    log.info("[BOT_RESTORE] bot.py started")

    used_main = await run_main_module()
    if used_main:
        return

    log.warning("[BOT_RESTORE] Falling back to direct telegram/scheduler runner")
    await run_fallback()


if __name__ == "__main__":
    try:
        asyncio.run(app())
    except KeyboardInterrupt:
        pass
