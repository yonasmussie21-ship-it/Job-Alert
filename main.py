import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import time
import signal
import sys

HOST = "0.0.0.0"
PORT = 3000

# Reduce logging noise
logging.getLogger("http.server").setLevel(logging.WARNING)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        elif self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Amazon Jobs Bot Running")

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        # Disable request logs
        return


def run_server():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"✅ Health server running on {HOST}:{PORT}")

    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    return server


def shutdown(server):
    print("🛑 Shutting down server...")
    server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    server = run_server()

    # Handle systemd stop/restart cleanly
    signal.signal(signal.SIGTERM, lambda sig, frame: shutdown(server))
    signal.signal(signal.SIGINT, lambda sig, frame: shutdown(server))

    # Keep process alive (safe way)
    try:
        while True:
            time.sleep(60)
    except Exception:
        shutdown(server)
