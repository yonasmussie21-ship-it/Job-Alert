import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = os.environ.get("HEALTH_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "3000"))
STARTED_AT = time.time()

shutdown_event = threading.Event()
bot_ready = threading.Event()
last_heartbeat = time.time()


def mark_bot_ready() -> None:
    bot_ready.set()


def heartbeat() -> None:
    global last_heartbeat
    last_heartbeat = time.time()


def check_health() -> tuple[bool, dict[str, Any]]:
    required_env = ["BOT_TOKEN", "CHAT_ID", "DATA_DIR"]
    missing_env = [key for key in required_env if not os.environ.get(key)]

    data_dir = os.environ.get("DATA_DIR", "")
    data_dir_writable = bool(data_dir and os.path.isdir(data_dir) and os.access(data_dir, os.W_OK))

    heartbeat_age = time.time() - last_heartbeat

    checks = {
        "env": {
            "ok": not missing_env,
            "missing": missing_env,
        },
        "data_dir": {
            "ok": data_dir_writable,
            "path": data_dir,
        },
        "bot_ready": {
            "ok": bot_ready.is_set(),
        },
        "heartbeat": {
            "ok": heartbeat_age < 120,
            "age_seconds": round(heartbeat_age, 2),
        },
    }

    ok = all(check["ok"] for check in checks.values())

    return ok, {
        "status": "ok" if ok else "unhealthy",
        "service": "amazon-bot",
        "uptime_seconds": round(time.time() - STARTED_AT, 2),
        "checks": checks,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AmazonBotHealth/1.0"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            ok, payload = check_health()
            self._send_json(200 if ok else 503, payload)
            return

        if self.path == "/ready":
            self._send_json(200 if bot_ready.is_set() else 503, {
                "ready": bot_ready.is_set()
            })
            return

        self._send_json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:
        if self.path == "/health":
            ok, _ = check_health()
            self.send_response(200 if ok else 503)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Health server running on {HOST}:{PORT}", flush=True)
    return server


def stop_health_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def handle_shutdown(signum: int, frame: Any) -> None:
    shutdown_event.set()


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
