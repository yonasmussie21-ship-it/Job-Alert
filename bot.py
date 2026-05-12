import asyncio
import logging

from config import CHAT_ID, load_accounts, is_peak_time, now_london
from storage import (
    load_subscribers,
    save_subscribers,
    load_known_jobs,
    save_known_job,
    load_job_history,
    save_job_history,
)
from job_scraper import fetch_jobs, fetch_job_details_batch
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

bot_paused = False


async def check_jobs() -> int:
    global bot_paused

    if bot_paused:
        return 0

    subscribers = load_subscribers()
    known_jobs = load_known_jobs()
    job_history = load_job_history()
    accounts = load_accounts()

    jobs = await fetch_jobs()
    new_jobs = []

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
            asyncio.create_task(
                run_auto_prepare(
                    job=job,
                    account=accounts[0],
                    alert_fn=tg_alert,
                    chat_id=CHAT_ID,
                )
            )

    return len(detailed_jobs)


async def send_daily_summary():
    while True:
        now = now_london()

        if now.hour == 7 and now.minute == 0:
            job_history = load_job_history()
            today_str = now.strftime("%Y-%m-%d")

            today_jobs = [
                job for job in job_history
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

            await asyncio.sleep(60)

        await asyncio.sleep(30)


async def main():
    log.info("[STARTUP] Amazon bot starting")

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

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())

    await check_jobs()

    while True:
        await asyncio.sleep(3 if is_peak_time() else 10)
        await check_jobs()


if __name__ == "__main__":
    asyncio.run(main())
