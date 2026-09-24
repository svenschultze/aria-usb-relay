#!/usr/bin/env python3
"""Loopback-only desktop interface. No third-party Python packages required."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
from urllib.parse import urlsplit
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.control import Controller

STATIC = Path(__file__).resolve().parent / "static"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], controller: Controller):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(20)
        super().__init__(address, DashboardHandler)
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {"http://" + host for host in self.hosts}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"
    server_version = "AriaUSBRelay"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *_):
        pass

    def reply(self, code: int, data: bytes, mime: str = "application/json", *, download: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="aria-relay-diagnostics.json"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def json_reply(self, code: int, value: dict):
        self.reply(code, json.dumps(value, allow_nan=False).encode("utf-8"))

    def allowed(self, *, token: bool = False, mutation: bool = False) -> bool:
        if self.headers.get("Host", "") not in self.server.hosts:
            self.json_reply(403, {"error": "Invalid local host"})
            return False
        origin = self.headers.get("Origin")
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin not in self.server.origins):
            self.json_reply(403, {"error": "Cross-origin access is not allowed"})
            return False
        if mutation and origin not in self.server.origins:
            self.json_reply(403, {"error": "A same-origin request is required"})
            return False
        if token and not secrets.compare_digest(self.headers.get("X-Aria-Token", ""), self.server.token):
            self.json_reply(403, {"error": "Reload the dashboard to refresh its session"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.allowed(token=path.startswith("/api/")):
            return
        if path == "/":
            page = (STATIC / "index.html").read_bytes().replace(b"__ARIA_TOKEN__", self.server.token.encode("ascii"))
            self.reply(200, page, "text/html; charset=utf-8")
        elif path in ("/app.js", "/style.css"):
            self.reply(200, (STATIC / path[1:]).read_bytes(), "text/javascript; charset=utf-8" if path.endswith("js") else "text/css; charset=utf-8")
        elif path == "/api/status":
            self.json_reply(200, self.server.controller.status())
        elif path == "/api/diagnostics":
            self.reply(200, json.dumps(self.server.controller.diagnostics(), indent=2).encode(), download=True)
        elif path in ("/api/frame/rgb", "/api/frame/slam1", "/api/frame/slam2"):
            try:
                frame, mime = self.server.controller.fetch_preview("/frame/" + path.rsplit("/", 1)[1] + ".jpg", 3 * 1024 * 1024)
                if mime != "image/jpeg":
                    raise ValueError("Invalid camera response")
                self.reply(200, frame, "image/jpeg")
            except (OSError, ValueError):
                self.json_reply(503, {"error": "Waiting for a camera frame"})
        elif path == "/favicon.ico":
            self.reply(204, b"", "image/x-icon")
        else:
            self.json_reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.allowed(token=True, mutation=True):
            self.close_connection = True
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self.json_reply(415, {"error": "Use a bounded JSON request"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 8192:
                # Drain small rejected bodies so Windows does not reset the
                # socket before the client receives 413. Never drain unbounded
                # input; socket and worker limits still apply.
                if 0 < length <= 65536:
                    self.rfile.read(length)
                self.close_connection = True
                self.json_reply(413, {"error": "Request is too large or empty"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            path = urlsplit(self.path).path
            if not path.startswith("/api/action/"):
                self.json_reply(404, {"error": "Not found"})
                return
            self.server.controller.submit(path[len("/api/action/"):], payload)
            self.json_reply(202, {"accepted": True})
        except (ValueError, UnicodeError) as error:
            self.json_reply(400, {"error": str(error)})
        except RuntimeError as error:
            self.json_reply(409, {"error": str(error)})
        except (TimeoutError, socket.timeout):
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open the browser")
    args = parser.parse_args()
    if sys.platform not in ("win32", "linux"):
        parser.error("This release supports Windows with WSL2 and native Linux.")
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    controller = Controller()
    try:
        server = DashboardServer(("127.0.0.1", args.port), controller)
    except OSError as error:
        controller.close()
        print(f"Cannot open dashboard port {args.port}: {error}. An existing dashboard may already be running.", file=sys.stderr)
        return 1
    controller.start_monitor()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Aria USB Relay: {url}\nKeep this launcher running. Closing the browser does not stop the stream.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("Dashboard closing.", flush=True)
    finally:
        controller.begin_close()
        if controller.session is not None and controller.session.poll() is None:
            print("Stopping the session started by this dashboard…", flush=True)
            try:
                controller._perform("stop", {}, controller.config)
            except (OSError, RuntimeError) as error:
                print(f"Session shutdown needs attention: {error}. Use scripts/stop-live.", file=sys.stderr)
        server.server_close()
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
