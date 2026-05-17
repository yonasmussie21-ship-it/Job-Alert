"""
scheduler.py — OWNER MODE v4
Production scheduler:
- multi-account scan rotation
- bounded history
- concurrent job detail enrichment
- cancellation-safe tasks
- scan metrics
- shutdown-aware loops
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from config import CHAT_ID, now_london, is_peak_time
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

MAX_HISTORY = 1000
DETAIL_CONCURRENCY = 3
PREPARE_CONCURRENCY = 2

_scan_lock = asyncio.Lock()
_detail_semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
_submit_semaphore = asyncio.Semaphore(PREPARE_CONCURRENCY)

shutdown_event: Optional[asyncio.Event] = None


def set_shutdown_event(ev: asyncio.Event) -> None:
    global shutdown_event
    shutdown_event = ev


def shutting_down() -> bool:
    return shutdown_event is not None and shutdown_event.is_set()


async def _sleep_or_shutdown(seconds: int) -> None:
    if shutdown_event is None:
        await asyncio.sleep(seconds)
        return

    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


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
    state.setdefault("account_index", 0)


def _eligible_accounts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    accounts = state.get("accounts", []) or []

    return [
        account
        for account in accounts
        if account.get("id") and (account.get("cookies") or account.get("email"))
    ]


def _next_account(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    accounts = _eligible_accounts(state)

    if not accounts:
        return None

    _init_account_index(state)

    start = state["account_index"]
    total = len(accounts)

    account = accounts[start % total]
    state["account_index"] = (start + 1) % total

    return account


def _owner_location(state: Dict[str, Any]) -> str:
    subscribers = state.get("subscribers", {})
    owner = subscribers.get(str(CHAT_ID)) or subscribers.get(CHAT_ID) or {}

    locations = owner.get("locations") or ["Birmingham"]

    if isinstance(locations, list) and locations:
        return str(locations[0])

    return "Birmingham"


def _trim_history(state: Dict[str, Any]) -> None:
    history = state.get("job_history", [])

    if isinstance(history, list) and len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]


async def _fetch_details_safe(job: Dict[str, Any], account_id: Optional[int]) -> Dict[str, Any]:
    async with _detail_semaphore:
        try:
            if shutting_down():
                return job

            if account_id:
                return await fetch_job_details(job, account_id=account_id)

            return await fetch_job_details(job)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            job_id = job.get("id", "?")
            log.warning("[JOB_DETAILS_FAILED] %s: %s", job_id, e)
            log_error("JOB_DETAILS_FAILED", f"{job_id}: {e}")
            return job


async def _safe_prepare(job: Dict[str, Any], account: Dict[str, Any]) -> None:
    async with _submit_semaphore:
        try:
            if shutting_down():
                return

            await run_auto_prepare(
                job,
                account,
                tg_alert,
                chat_id=CHAT_ID,
            )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log.exception("[PREPARE_ERROR] %s: %s", job.get("id"), e)
            log_error("PREPARE_ERROR", f"{job.get('id')}: {e}")


async def check_jobs(state: Dict[str, Any]) -> int:
    if state.get("bot_paused"):
        log.info("[SCAN_PAUSED]")
        return 0

    if _scan_lock.locked():
        log.info("[SCAN_SKIPPED] previous scan still running")
        return 0

    async with _scan_lock:
        return await _do_check(state)


async def _do_check(state: Dict[str, Any]) -> int:
    scan_start = time.monotonic()

    known_jobs = state.setdefault("known_jobs", {})
    job_history = state.setdefault("job_history", [])

    found_count = 0
    processed_count = 0
    skipped_count = 0

    scan_account = _next_account(state)

    if not scan_account:
        log.warning("[NO_SCAN_ACCOUNT] no eligible accounts available")
        return 0

    account_id = scan_account.get("id")

    try:
        jobs = await fetch_jobs(account_id=account_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("[FETCH_FAILED] %s", e)
        log_error("FETCH_FAILED", str(e))

        try:
            await tg_send("⚠️ <b>Scan failed</b> — proxy or Amazon issue. Retrying next cycle.")
        except Exception:
            log.exception("[SCAN_FAILED_NOTIFY_ERROR]")

        return 0

    new_jobs: List[Dict[str, Any]] = []

    for job in jobs:
        if shutting_down():
            log.info("[SCAN_STOPPED] shutdown requested")
            break

        job_id = str(job.get("id") or "")

        if not job_id:
            continue

        if job_id in known_jobs or is_known_job(job_id):
            continue

        found_count += 1
        new_jobs.append(job)

    if not new_jobs:
        duration = round(time.monotonic() - scan_start, 2)
        log.info(
            "[NO_NEW_JOBS] tracked=%s scanned=%s duration=%ss account=%s",
            len(known_jobs),
            len(jobs),
            duration,
            account_id,
        )
        return 0

    detail_tasks = [
        _fetch_details_safe(job, account_id=account_id)
        for job in new_jobs
    ]

    detailed_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

    owner_location = _owner_location(state)

    for result in detailed_results:
        if shutting_down():
            break

        if isinstance(result, Exception):
            if isinstance(result, asyncio.CancelledError):
                raise result
            log.warning("[DETAIL_RESULT_ERROR] %s", result)
            continue

        job = result
        job_id = str(job.get("id") or "")

        if not job_id:
            continue

        job_score, should_skip = score_job(job)

        if should_skip:
            mark_job_known(job)
            known_jobs[job_id] = job
            skipped_count += 1
            log.info("[JOB_SKIPPED] %s reason=score_filter", job_id)
            continue

        best_distance = None

        postcode = job.get("postcode", "")

        if postcode:
            try:
                best_distance = await job_distance_miles(postcode, owner_location)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[DISTANCE_FAILED] job=%s error=%s", job_id, e)

        known_jobs[job_id] = job
        job_history.append(job)
        mark_job_known(job)
        processed_count += 1

        try:
            await send_all_shifts(
                job,
                "new",
                chat_id=CHAT_ID,
                distance=best_distance,
                score=job_score if job_score > 0 else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[ALERT_FAILED] job=%s error=%s", job_id, e)
            log_error("ALERT_FAILED", f"{job_id}: {e}")

        if not is_fresh_job(job):
            prepare_account = _next_account(state)

            if prepare_account:
                create_task(
                    _safe_prepare(job, prepare_account),
                    name=f"prepare_{job_id}",
                )
            else:
                log.warning("[NO_VALID_ACCOUNTS] Cannot auto-prepare job=%s", job_id)

    _trim_history(state)
    save_job_history(job_history)

    duration = round(time.monotonic() - scan_start, 2)

    log.info(
        "[SCAN_DONE] scanned=%s found=%s processed=%s skipped=%s tracked=%s duration=%ss account=%s",
        len(jobs),
        found_count,
        processed_count,
        skipped_count,
        len(known_jobs),
        duration,
        account_id,
    )

    return processed_count


async def scan_loop(state: Dict[str, Any]) -> None:
    log.info("[SCAN_LOOP] started")

    while not shutting_down():
        delay = 3 if is_peak_time() else 10

        try:
            await check_jobs(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[SCAN_LOOP_ERROR] %s", e)
            log_error("SCAN_LOOP_ERROR", str(e))

        await _sleep_or_shutdown(delay)

    log.info("[SCAN_LOOP] stopped")


async def send_daily_summary(state: Dict[str, Any]) -> None:
    last_sent: Optional[str] = None

    while not shutting_down():
        try:
            now = now_london()
            today = now.strftime("%Y-%m-%d")

            if now.hour == 8 and now.minute == 0 and last_sent != today:
                job_history = state.get("job_history", [])

                today_jobs = [
                    job for job in job_history
                    if str(job.get("found_at", ""))[:10] == today
                ]

                if today_jobs:
                    best = max(today_jobs, key=lambda x: x.get("pay", 0) or 0)
                    avg_pay = sum((j.get("pay", 0) or 0) for j in today_jobs) / len(today_jobs)
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

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[SUMMARY_ERROR] %s", e)
            log_error("SUMMARY_ERROR", str(e))

        await _sleep_or_shutdown(30)


async def cookie_health_check(state: Dict[str, Any]) -> None:
    last_alert: Dict[int, str] = {}

    while not shutting_down():
        try:
            from storage import get_cookie_age_hours

            accounts = state.get("accounts", [])
            today = now_london().strftime("%Y-%m-%d")

            for account in accounts:
                if shutting_down():
                    break

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

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[COOKIE_CHECK_ERROR] %s", e)
            log_error("COOKIE_CHECK_ERROR", str(e))

        await _sleep_or_shutdown(3600)
