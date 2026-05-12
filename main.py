import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any, Dict, List, Optional

from config import CHAT_ID, load_accounts, is_peak_time, now_london
from storage import (
    load_subscribers,
    save_subscribers,
    load_known_jobs,
    save_known_job,
    load_job_history,
    save_job_history,
)
from amazon_scraper import fetch_jobs, fetch_job_details_batch
from job_parser import is_fresh_job, score_job
from application_preparer import run_auto_prepare
from telegram_bot import (
    tg_send,
    tg_alert,
    send_all_shifts,
    handle_updates,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger(__name__)

bot_paused: bool = False
shutdown_event: Optional[asyncio.Event] = None


def get_shutdown_event() -> asyncio.Event:
    if shutdown_event is None:
        raise RuntimeError("shutdown_event has not been initialized")
    return shutdown_event


def _task_done_callback(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception("[BACKGROUND_TASK_FAILED] %s", e)


def create_background_task(coro: Any, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_task_done_callback)
    return task


async def safe_check_jobs() -> int:
    try:
        return await check_jobs()
    except Exception as e:
        log.exception("[CHECK_JOBS_ERROR] %s", e)
        return 0


async def check_jobs() -> int:
    global bot_paused

    if bot_paused:
        log.info("[SCAN] Bot paused, skipping job check")
        return 0

    known_jobs = load_known_jobs()
    job_history = load_job_history()
    accounts = load_accounts()

    jobs = await fetch_jobs()
    new_jobs: List[Dict[str, Any]] = []

    for job in jobs:
        job_id = job.get("id")
        if not job_id:
            continue

        if job_id in known_jobs:
            continue

        new_jobs.append(job)

    if not new_jobs:
        log.info("[SCAN] No new jobs")
        return 0

    log.info("[SCAN] New jobs found: %s", len(new_jobs))

    detailed_jobs = await fetch_job_details_batch(new_jobs)
    processed = 0

    for job in detailed_jobs:
        job_id = job.get("id")
        if not job_id:
            continue

        job_score, should_skip = score_job(job)

        if should_skip:
            log.info("[SKIPPED] job=%s reason=score_filter", job_id)
            continue

        known_jobs[job_id] = job
        job_history.append(job)

        save_known_job(job)
        save_job_history(job_history)

        processed += 1

        await send_all_shifts(
            job,
            status="new",
            chat_id=CHAT_ID,
            score=job_score if job_score > 0 else None,
        )

        if is_fresh_job(job):
            await tg_alert(job, "fresh_alert", chat_id=CHAT_ID)
            continue

        if accounts:
            create_background_task(
                run_auto_prepare(
                    job=job,
                    account=accounts[0],
                    alert_fn=tg_alert,
                    chat_id=CHAT_ID,
                ),
                name=f"auto_prepare_{job_id}",
            )
        else:
            log.warning("[NO_ACCOUNTS] Cannot auto-prepare job=%s", job_id)

    return processed


async def send_daily_summary() -> None:
    last_sent_date: Optional[str] = None
    event = get_shutdown_event()

    while not event.is_set():
        now = now_london()
        today_str = now.strftime("%Y-%m-%d")

        if now.hour == 7 and now.minute == 0 and last_sent_date != today_str:
            job_history = load_job_history()

            today_jobs = [
                job
                for job in job_history
                if job.get("found_at", "").startswith(today_str)
            ]

            if today_jobs:
                best = max(today_jobs, key=lambda x: x.get("pay", 0))
                avg_pay = sum(j.get("pay", 0) for j in today_jobs) / len(today_jobs)

                await tg_send(
                    f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {today_str}
🆕 Jobs: {len(today_jobs)}
💰 Avg: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location', '?')} £{best.get('pay', '?')}/hr
━━━━━━━━━━━━━━━━━"""
                )

            last_sent_date = today_str

        try:
            await asyncio.wait_for(event.wait(), timeout=30)
        except asyncio.TimeoutError:
            continue


async def scan_loop() -> None:
    event = get_shutdown_event()

    while not event.is_set():
        delay = 3 if is_peak_time() else 10

        try:
            await asyncio.wait_for(event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            await safe_check_jobs()


def ensure_owner_subscriber() -> Dict[str, Any]:
    subscribers = load_subscribers()

    if CHAT_ID not in subscribers:
        subscribers[CHAT_ID] = {
            "name": "Owner",
            "locations": ["Birmingham"],
            "radius": 50,
            "job_type": "both",
            "setup_complete": True,
            "auto_apply": True,
            "tier": "owner",
            "joined": now_london().isoformat(),
        }
        save_subscribers(subscribers)

    return subscribers


def setup_signal_handlers(event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, event.set)


async def main() -> None:
    global shutdown_event

    shutdown_event = asyncio.Event()

    log.info("[STARTUP] Amazon bot starting")

    setup_signal_handlers(shutdown_event)

    subscribers = ensure_owner_subscriber()
    accounts = load_accounts()

    await tg_send(
        f"""👑 <b>Amazon Bot Online</b>
━━━━━━━━━━━━━━━━━
✅ Safe prepare-only mode enabled
✅ GraphQL scraper enabled
✅ Detail batch fetch enabled
✅ Application preparer enabled
👥 Subscribers: {len(subscribers)}
🤖 Accounts: {len(accounts)}
━━━━━━━━━━━━━━━━━"""
    )

    tasks = [
        create_background_task(handle_updates(), "telegram_updates"),
        create_background_task(send_daily_summary(), "daily_summary"),
        create_background_task(scan_loop(), "scan_loop"),
    ]

    await safe_check_jobs()

    await shutdown_event.wait()

    log.info("[SHUTDOWN] Cancelling background tasks")

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    log.info("[SHUTDOWN] Complete")


if __name__ == "__main__":
    asyncio.run(main())
