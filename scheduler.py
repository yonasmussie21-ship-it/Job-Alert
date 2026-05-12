"""
scheduler.py — OWNER MODE v3
Multi-account rotation, safer prepare, shutdown-aware loops.
"""

import asyncio
import logging
from typing import Any, Dict, List

from config import CHAT_ID, now_london
from storage import (
    is_known_job,
    mark_job_known,
    save_job_history,
    log_error,
)
from amazon_scraper import fetch_jobs, fetch_job_details
from job_parser import score_job, is_fresh_job, job_distance_miles, is_night_shift
from application_preparer import run_auto_prepare
from telegram_bot import tg_send, tg_alert, send_all_shifts

log = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()
_submit_semaphore = asyncio.Semaphore(2)

shutdown_event: asyncio.Event | None = None


def set_shutdown_event(ev: asyncio.Event) -> None:
    global shutdown_event
    shutdown_event = ev


def shutting_down() -> bool:
    return shutdown_event is not None and shutdown_event.is_set()


def create_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)

    def done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("[TASK_FAILED] %s: %s", name, e)
            log_error("TASK_FAILED", f"{name}: {e}")

    task.add_done_callback(done)
    return task


def _init_account_index(state: Dict[str, Any]) -> None:
    if "account_index" not in state:
        state["account_index"] = 0


def _next_account(state: Dict[str, Any]) -> Dict[str, Any] | None:
    accounts: List[Dict[str, Any]] = state.get("accounts", []) or []

    if not accounts:
        return None

    _init_account_index(state)

    start = state["account_index"]
    total = len(accounts)

    for i in range(total):
        idx = (start + i) % total
        account = accounts[idx]

        # Current prepare flow needs cookies.
        if account.get("cookies"):
            state["account_index"] = (idx + 1) % total
            return account

    return None


async def check_jobs(state: dict) -> int:
    """
    Main job check.
    state = {
        known_jobs,
        job_history,
        bot_paused,
        accounts,
        account_index?
    }
    """
    if state.get("bot_paused"):
        log.info("[SCAN_PAUSED]")
        return 0

    if _scan_lock.locked():
        log.info("[SCAN_SKIPPED] Previous scan still running")
        return 0

    async with _scan_lock:
        return await _do_check(state)


async def _do_check(state: dict) -> int:
    known_jobs = state["known_jobs"]
    job_history = state["job_history"]

    found_count = 0
    processed_count = 0

    try:
        jobs = await fetch_jobs()
    except Exception as e:
        log.exception("[FETCH_FAILED] %s", e)
        log_error("FETCH_FAILED", str(e))
        await tg_send("⚠️ <b>Scan failed</b> — proxy or Amazon issue. Retrying next cycle.")
        return 0

    for job in jobs:
        if shutting_down():
            log.info("[SCAN_STOPPED] Shutdown requested")
            break

        job_id = job.get("id")
        if not job_id:
            continue

        if job_id in known_jobs or is_known_job(job_id):
            continue

        found_count += 1
        log.info(
            "[JOB_FOUND] %s — %s £%s/hr",
            job_id,
            job.get("location"),
            job.get("pay"),
        )

        try:
            detailed = await fetch_job_details(job)
            job = detailed or job
        except Exception as e:
            log.warning("[JOB_DETAILS_FAILED] %s: %s", job_id, e)
            log_error("JOB_DETAILS_FAILED", f"{job_id}: {e}")

        job_score, should_skip = score_job(job)

        if should_skip:
            mark_job_known(job)
            known_jobs[job_id] = job
            log.info("[JOB_SKIPPED] %s reason=score_filter", job_id)
            continue

        best_distance = None

        try:
            postcode = job.get("postcode", "")
            if postcode:
                best_distance = await job_distance_miles(postcode, "Birmingham")
        except Exception as e:
            log.warning("[DISTANCE_FAILED] %s", e)

        known_jobs[job_id] = job
        job_history.append(job)
        mark_job_known(job)
        processed_count += 1

        await send_all_shifts(
            job,
            "new",
            chat_id=CHAT_ID,
            distance=best_distance,
            score=job_score if job_score > 0 else None,
        )

        if not is_fresh_job(job):
            account = _next_account(state)

            if account:
                create_task(
                    _safe_prepare(job, account),
                    name=f"prepare_{job_id}",
                )
            else:
                log.warning("[NO_VALID_ACCOUNTS] Cannot auto-prepare job=%s", job_id)

    save_job_history(job_history)

    if found_count == 0:
        log.info("[NO_NEW_JOBS] %s tracked", len(known_jobs))
    else:
        log.info("[SCAN_DONE] found=%s processed=%s", found_count, processed_count)

    return processed_count


async def _safe_prepare(job: dict, account: dict) -> None:
    async with _submit_semaphore:
        try:
            await run_auto_prepare(
                job,
                account,
                tg_alert,
                chat_id=CHAT_ID,
            )
        except Exception as e:
            log.exception("[PREPARE_ERROR] %s: %s", job.get("id"), e)
            log_error("PREPARE_ERROR", f"{job.get('id')}: {e}")


async def send_daily_summary(state: dict) -> None:
    last_sent = None

    while not shutting_down():
        try:
            now = now_london()
            today = now.strftime("%Y-%m-%d")

            if now.hour == 8 and now.minute == 0 and last_sent != today:
                job_history = state.get("job_history", [])

                today_jobs = [
                    job for job in job_history
                    if job.get("found_at", "")[:10] == today
                ]

                if today_jobs:
                    best = max(today_jobs, key=lambda x: x.get("pay", 0))
                    avg_pay = sum(j.get("pay", 0) for j in today_jobs) / len(today_jobs)
                    nights = sum(
                        1 for j in today_jobs
                        if is_night_shift(j.get("schedule", ""))
                    )

                    await tg_send(
                        f"""📊 <b>Daily Summary — {now.strftime('%d %b %Y')}</b>
━━━━━━━━━━━━━━━━━
🆕 New jobs today: {len(today_jobs)}
🌙 Night shifts: {nights}
💰 Avg pay: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location', '?')} £{best.get('pay', '?')}/hr
📦 Total tracked: {len(state.get('known_jobs', {}))}
━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪"""
                    )
                else:
                    await tg_send(
                        f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
No new jobs today.
📦 Total tracked: {len(state.get('known_jobs', {}))}
━━━━━━━━━━━━━━━━━"""
                    )

                last_sent = today

        except Exception as e:
            log.exception("[SUMMARY_ERROR] %s", e)
            log_error("SUMMARY_ERROR", str(e))

        await _sleep_or_shutdown(30)


async def cookie_health_check(state: dict) -> None:
    """
    Checks cookie age for all accounts.
    Alerts once per day per account if cookies are older than 12 hours.
    """
    last_alert: dict[int, str] = {}

    while not shutting_down():
        try:
            from storage import get_cookie_age_hours

            accounts = state.get("accounts", [])
            today = now_london().strftime("%Y-%m-%d")

            for account in accounts:
                account_id = account.get("id")

                if not account_id:
                    continue

                if not (account.get("cookies") or account.get("email")):
                    continue

                try:
                    age = get_cookie_age_hours(account_id)
                except Exception as e:
                    log.warning("[COOKIE_AGE_ERROR] account=%s %s", account_id, e)
                    continue

                if age is None:
                    continue

                if age > 12 and last_alert.get(account_id) != today:
                    cookie_name = (
                        "AMAZON_COOKIES"
                        if account_id == 1
                        else f"AMAZON_COOKIES_{account_id}"
                    )

                    await tg_send(
                        f"""⚠️ <b>Cookie Warning</b>
Account {account_id} cookies are {age:.1f} hours old.
Consider refreshing {cookie_name}."""
                    )

                    last_alert[account_id] = today

        except Exception as e:
            log.exception("[COOKIE_CHECK_ERROR] %s", e)
            log_error("COOKIE_CHECK_ERROR", str(e))

        await _sleep_or_shutdown(3600)


async def _sleep_or_shutdown(seconds: int) -> None:
    if shutdown_event is None:
        await asyncio.sleep(seconds)
        return

    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
