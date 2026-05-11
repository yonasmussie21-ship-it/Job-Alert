"""
scheduler.py — OWNER MODE v1
Job check loop with scan lock + submit semaphore
"""
import asyncio
import logging
from datetime import datetime
from config import CHAT_ID, is_peak_time, now_london
from storage import (
    is_known_job, mark_job_known, save_job_history, log_error
)
from amazon_scraper import fetch_jobs, fetch_job_details
from job_parser import score_job, is_fresh_job, job_distance_miles, is_night_shift
from application_preparer import run_auto_prepare
from telegram_bot import tg_send, tg_alert, send_all_shifts

log = logging.getLogger(__name__)

# ─── CONCURRENCY CONTROLS ─────────────────────────────────────────────────────
_scan_lock       = asyncio.Lock()        # Only one scan at a time
_submit_semaphore = asyncio.Semaphore(2) # Max 2 concurrent prepares

# ─── CHECK JOBS ───────────────────────────────────────────────────────────────
async def check_jobs(state: dict) -> int:
    """
    Main job check. Owner-only mode.
    state = {known_jobs, job_history, bot_paused, accounts}
    """
    if state.get("bot_paused"):
        return 0

    # Only one scan at a time
    if _scan_lock.locked():
        log.info("[SCAN_SKIPPED] Previous scan still running")
        return 0

    async with _scan_lock:
        return await _do_check(state)


async def _do_check(state: dict) -> int:
    known_jobs  = state["known_jobs"]
    job_history = state["job_history"]
    accounts    = state["accounts"]
    new_count   = 0

    try:
        jobs = await fetch_jobs()
    except Exception as e:
        log.error(f"[FETCH_FAILED] {e}")
        log_error("FETCH_FAILED", str(e))
        await tg_send("⚠️ <b>Scan failed</b> — proxy or Amazon issue. Retrying next cycle.")
        return 0

    for job in jobs:
        jid = job["id"]

        # Check both memory and SQLite — survive restarts
        if jid in known_jobs or is_known_job(jid):
            continue

        new_count += 1
        log.info(f"[JOB_FOUND] {jid} — {job.get('location')} £{job.get('pay')}/hr")

        # Fetch full details
        try:
            job = await fetch_job_details(job)
        except Exception as e:
            log.warning(f"[JOB_DETAILS_FAILED] {jid}: {e}")
            log_error("JOB_DETAILS_FAILED", f"{jid}: {e}")

        # Score it
        job_score, skip = score_job(job)
        if skip:
            mark_job_known(job)
            known_jobs[jid] = job
            continue

        # Get distance from Birmingham
        best_distance = None
        try:
            postcode = job.get("postcode","")
            if postcode:
                d = await job_distance_miles(postcode, "Birmingham")
                if d is not None:
                    best_distance = d
        except Exception as e:
            log.warning(f"[DISTANCE_FAILED] {e}")

        # Save to memory + SQLite
        known_jobs[jid] = job
        job_history.append(job)
        mark_job_known(job)
        save_job_history(job)

        # Alert owner
        await send_all_shifts(
            job, "new",
            chat_id=CHAT_ID,
            distance=best_distance,
            score=job_score if job_score > 0 else None
        )

        # Auto prepare (non-blocking, rate limited)
        if accounts and not is_fresh_job(job):
            asyncio.create_task(
                _safe_prepare(job, accounts[0])
            )

    if new_count == 0:
        log.info(f"[NO_NEW_JOBS] {len(known_jobs)} tracked")

    return new_count


async def _safe_prepare(job: dict, account: dict):
    """Run prepare with semaphore to limit concurrency."""
    async with _submit_semaphore:
        try:
            await run_auto_prepare(job, account, tg_alert, chat_id=CHAT_ID)
        except Exception as e:
            log.error(f"[PREPARE_ERROR] {job.get('id')}: {e}")
            log_error("PREPARE_ERROR", f"{job.get('id')}: {e}")


# ─── DAILY SUMMARY ────────────────────────────────────────────────────────────
async def send_daily_summary(state: dict):
    while True:
        try:
            now = now_london()
            if now.hour == 8 and now.minute == 0:
                job_history = state.get("job_history", [])
                today = [
                    j for j in job_history
                    if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")
                ]
                if today:
                    best    = max(today, key=lambda x: x.get("pay",0))
                    avg_pay = sum(j.get("pay",0) for j in today) / len(today)
                    nights  = sum(1 for j in today if is_night_shift(j.get("schedule","")))
                    await tg_send(f"""📊 <b>Daily Summary — {now.strftime('%d %b %Y')}</b>
━━━━━━━━━━━━━━━━━
🆕 New jobs today: {len(today)}
🌙 Night shifts: {nights}
💰 Avg pay: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location','?')} £{best.get('pay','?')}/hr
📦 Total tracked: {len(state.get('known_jobs',{}))}
━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪""")
                else:
                    await tg_send(f"📊 <b>Daily Summary</b>\nNo new jobs found today.\nTotal tracked: {len(state.get('known_jobs',{}))}")
                await asyncio.sleep(61)
        except Exception as e:
            log.error(f"[SUMMARY_ERROR] {e}")
        await asyncio.sleep(30)


# ─── COOKIE HEALTH CHECK ──────────────────────────────────────────────────────
async def cookie_health_check(state: dict):
    """Alert owner if cookies are getting stale."""
    while True:
        try:
            from storage import get_cookie_age_hours
            age = get_cookie_age_hours(1)
            if age is not None and age > 12:
                await tg_send(
                    f"⚠️ <b>Cookie Warning</b>\n"
                    f"Account 1 cookies are {age:.1f} hours old.\n"
                    f"Consider refreshing AMAZON_COOKIES in Fly.io secrets."
                )
            await asyncio.sleep(3600)  # Check every hour
        except Exception as e:
            log.error(f"[COOKIE_CHECK_ERROR] {e}")
            await asyncio.sleep(3600)
