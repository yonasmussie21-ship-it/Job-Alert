sudo tee /opt/amazon-bot/current/main.py << 'MAINEOF'
import asyncio
import logging
import signal
import sys
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

from config import CHAT_ID, load_accounts, now_london, validate_env
from storage import load_subscribers, save_subscribers, load_known_jobs, load_job_history
from job_parser import close_session
from telegram_bot import tg_send, handle_updates
from scheduler import (
    scan_loop,
    send_daily_summary,
    cookie_health_check,
    set_shutdown_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

HOST = "0.0.0.0"
PORT = 3000

health_state = {
    "ready": False,
    "critical_tasks_alive": True,
    "shutting_down": False,
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            ok = (
                health_state["ready"]
                and health_state["critical_tasks_alive"]
                and not health_state["shutting_down"]
            )
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(b"OK" if ok else b"UNHEALTHY")
            return
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Amazon Jobs Bot Running")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server() -> HTTPServer:
    server = HTTPServer((HOST, PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    log.info("[HEALTH] server started on %s:%s", HOST, PORT)
    return server


async def safe_tg_send(message: str) -> None:
    try:
        await tg_send(message)
    except Exception:
        log.exception("[TELEGRAM_SEND_FAILED]")


def ensure_owner_subscriber() -> Dict[str, Any]:
    subscribers = load_subscribers()
    if CHAT_ID and str(CHAT_ID) not in subscribers:
        subscribers[str(CHAT_ID)] = {
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
        log.info("[OWNER_CREATED] chat_id=%s", CHAT_ID)
    return subscribers


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    def shutdown(sig_name: str) -> None:
        if shutdown_event.is_set():
            return
        log.info("[SHUTDOWN_SIGNAL] %s received", sig_name)
        health_state["ready"] = False
        health_state["shutting_down"] = True
        shutdown_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown, sig.name)


def create_task(tasks, shutdown_event, coro, name):
    task = asyncio.create_task(coro, name=name)
    def done(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.exception("[TASK_FAILED] %s", name, exc_info=exc)
            health_state["critical_tasks_alive"] = False
            health_state["ready"] = False
            shutdown_event.set()
    task.add_done_callback(done)
    tasks.append(task)
    return task


async def shutdown_tasks(tasks) -> None:
    if not tasks:
        return
    log.info("[SHUTDOWN] cancelling %s tasks", len(tasks))
    for task in tasks:
        task.cancel()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=20,
        )


async def main() -> int:
    shutdown_event = asyncio.Event()
    set_shutdown_event(shutdown_event)
    install_signal_handlers(shutdown_event)

    validate_env()

    subscribers = ensure_owner_subscriber()
    accounts = load_accounts()

    state = {
        "subscribers": subscribers,
        "known_jobs": load_known_jobs(),
        "job_history": load_job_history(),
        "bot_paused": False,
        "accounts": accounts,
    }

    log.info(
        "[STARTUP] accounts=%s subscribers=%s known_jobs=%s",
        len(accounts), len(subscribers), len(state["known_jobs"]),
    )

    await safe_tg_send(
        f"👑 <b>Amazon Bot Online</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"👥 Subscribers: {len(subscribers)}\n"
        f"🤖 Accounts: {len(accounts)}\n"
        f"📦 Known jobs: {len(state['known_jobs'])}\n"
        f"━━━━━━━━━━━━━━━━━"
    )

    tasks: List[asyncio.Task] = []
    create_task(tasks, shutdown_event, handle_updates(state), "telegram_updates")
    create_task(tasks, shutdown_event, scan_loop(state), "scan_loop")
    create_task(tasks, shutdown_event, send_daily_summary(state), "daily_summary")
    create_task(tasks, shutdown_event, cookie_health_check(state), "cookie_health")

    health_state["ready"] = True
    health_state["critical_tasks_alive"] = True
    health_state["shutting_down"] = False

    log.info("[READY] service healthy")

    await shutdown_event.wait()

    health_state["ready"] = False
    health_state["shutting_down"] = True

    await shutdown_tasks(tasks)

    with suppress(Exception):
        await close_session()

    await safe_tg_send("🛑 Bot shutting down")
    log.info("[SHUTDOWN] complete")
    return 0 if health_state["critical_tasks_alive"] else 1


if __name__ == "__main__":
    server = run_health_server()
    exit_code = 1
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 0
    except Exception:
        log.exception("[FATAL]")
        exit_code = 1
    finally:
        health_state["ready"] = False
        health_state["shutting_down"] = True
        with suppress(Exception):
            server.shutdown()
        with suppress(Exception):
            server.server_close()
    sys.exit(exit_code)
MAINEOF
