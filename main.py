import asyncio
import logging
import signal
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

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
from job_parser import is_fresh_job, score_job, close_session
from application_preparer import run_auto_prepare
from telegram_bot import tg_send, tg_alert, send_all_shifts, handle_updates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

shutdown_event: asyncio.Event = asyncio.Event()
background_tasks: List[asyncio.Task] = []

health_state = {
    "ready": False,
    "critical_tasks_alive": True,
}

# ─── HEALTH SERVER ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            ok = health_state["ready"] and health_state["critical_tasks_alive"]
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(b"OK" if ok else b"UNHEALTHY")
        elif self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Amazon Jobs Bot Running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", 3000), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health server started on 0.0.0.0:3000")
    return server

# ─── TASK MANAGEMENT ─────────────────────────────────────────────────────────
def _task_done_callback(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception("[BACKGROUND_TASK_FAILED] %s", e)
        health_state["critical_tasks_alive"] = False
        shutdown_event.set()

def create_background_task(coro: Any, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_task_done_callback)
    background_tasks.append(task)
    return task

# ─── JOB SCANNING ────────────────────────────────────────────────────────────
async def check_jobs(state: dict) -> int:
    if state.get("bot_paused"):
        return 0

    known_jobs = state["known_jobs"]
    job_history = state["job_history"]
    accounts = state["accounts"]

    jobs = await fetch_jobs()
    new_jobs = [job for job in jobs if job.get("id") and job["id"] not in known_jobs]

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
            continue

        known_jobs[job_id] = job
        job_history.append(job)
        save_known_job(job)
        processed += 1

        await send_all_shifts(job, status="new", chat_id=CHAT_ID,
                              score=job_score if job_score > 0 else None)

        if is_fresh_job(job):
            await tg_alert(job, "fresh_alert", chat_id=CHAT_ID)
            continue

        account = next((a for a in accounts if a.get("cookies")), None)
        if account:
            create_background_task(
                run_auto_prepare(job=job, account=account,
                                 alert_fn=tg_alert, chat_id=CHAT_ID),
                name=f"auto_prepare_{job_id}",
            )

    save_job_history(job_history)
    return processed

# ─── SCAN LOOP ────────────────────────────────────────────────────────────────
async def scan_loop(state: dict) -> None:
    while not shutdown_event.is_set():
        delay = 3 if is_peak_time() else 10
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            await check_jobs(state)

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary(state: dict) -> None:
    last_sent_date = None
    while not shutdown_event.is_set():
        now = now_london()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == 7 and now.minute == 0 and last_sent_date != today_str:
            job_history = state.get("job_history", [])
            today_jobs = [j for j in job_history if j.get("found_at", "").startswith(today_str)]
            if today_jobs:
                best = max(today_jobs, key=lambda x: x.get("pay", 0))
                avg_pay = sum(j.get("pay", 0) for j in today_jobs) / len(today_jobs)
                await tg_send(
                    f"📊 <b>Daily Summary</b>\n━━━━━━━━━━━━━━━━━\n"
                    f"📅 {today_str}\n🆕 Jobs: {len(today_jobs)}\n"
                    f"💰 Avg: £{avg_pay:.2f}/hr\n"
                    f"⭐ Best: {best.get('location','?')} £{best.get('pay','?')}/hr\n"
                    f"━━━━━━━━━━━━━━━━━"
                )
            last_sent_date = today_str
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass

# ─── OWNER SETUP ─────────────────────────────────────────────────────────────
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

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main() -> None:
    log.info("[STARTUP] Amazon bot starting")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown_event.set)

    subscribers = ensure_owner_subscriber()
    accounts = load_accounts()

    state = {
        "subscribers": subscribers,
        "known_jobs":  load_known_jobs(),
        "job_history": load_job_history(),
        "bot_paused":  False,
        "accounts":    accounts,
    }

    await tg_send(
        f"👑 <b>Amazon Bot Online</b>\n━━━━━━━━━━━━━━━━━\n"
        f"👥 Subscribers: {len(subscribers)}\n"
        f"🤖 Accounts: {len(accounts)}\n━━━━━━━━━━━━━━━━━"
    )

    create_background_task(handle_updates(state), "telegram_updates")
    create_background_task(send_daily_summary(state), "daily_summary")
    create_background_task(scan_loop(state), "scan_loop")

    health_state["ready"] = True

    await shutdown_event.wait()

    log.info("[SHUTDOWN] Cancelling tasks")
    health_state["ready"] = False

    for task in background_tasks:
        task.cancel()

    await asyncio.gather(*background_tasks, return_exceptions=True)
    await close_session()
    log.info("[SHUTDOWN] Complete")


if __name__ == "__main__":
    server = run_health_server()
    asyncio.run(main())
