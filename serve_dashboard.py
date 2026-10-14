"""Serves ONLY dashboard.html behind real server-side HTTP Basic Auth.

Deliberately does not serve the directory it lives in -- the repo has
.env, memory files, and other data that must never be reachable over the
tunnel. Every request, whatever the path, is answered from
dashboard.html or rejected; there is no directory listing and no other
file on disk is ever opened.

Credentials: DASHBOARD_USER / DASHBOARD_PASS_HASH (sha256 hex of the
password) come from .env. The plaintext password is never written to
disk -- only its hash is compared against what the browser sends.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
PORT = int(os.environ.get("DASHBOARD_PORT", "8899"))
USERNAME = os.environ["DASHBOARD_USER"]
PASS_HASH = os.environ["DASHBOARD_PASS_HASH"]


def _authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, pw = base64.b64decode(header[6:]).decode().split(":", 1)
    except Exception:
        return False
    pw_hash = hashlib.sha256(pw.encode()).hexdigest()
    return secrets.compare_digest(user, USERNAME) and secrets.compare_digest(pw_hash, PASS_HASH)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _authorized(self.headers.get("Authorization")):
            body = b"Authentication required."
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ramu Kaka Dashboard"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path not in ("/", "/dashboard.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            with open(DASHBOARD_PATH, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
