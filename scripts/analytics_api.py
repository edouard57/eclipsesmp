#!/usr/bin/env python3
"""Tiny HTTP API that forwards launcher usage events to a Discord channel.

Runs behind nginx at eclipsesmp.cubi-mc.fr/api/{track,crash}. Configured
entirely via environment variables (see start.sh on the VPS) so no secret
ever needs to live in this file or in git.

Env vars required:
  DISCORD_TOKEN       Bot token, "Authorization: Bot <token>"
  DISCORD_CHANNEL_ID  Channel to post launch/crash events to
  SHARED_SECRET       Must match the X-Analytics-Secret header sent by the
                       launcher. This only deters casual abuse -- anyone who
                       extracts the launcher's app.asar can read it, since
                       the repo (and therefore the built app) is public.
  PORT                Defaults to 8081.
"""
import io
import json
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
SHARED_SECRET = os.environ["SHARED_SECRET"]
PORT = int(os.environ.get("PORT", "8081"))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
ACCOUNT_TYPES = {"microsoft": "Premium", "offline": "Crack"}

# Cloudflare (in front of Discord's API) blocks the default
# "Python-urllib/x.y" User-Agent with a 403 (error code 1010).
USER_AGENT = "EclipseSMPAnalytics (https://eclipsesmp.cubi-mc.fr, 1.0)"

MAX_CRASH_REPORT_SIZE = 8 * 1024 * 1024  # Discord's own attachment cap.

# Minimal per-IP rate limit: one accepted event every 10s per endpoint.
_last_seen = {}
RATE_LIMIT_SECONDS = 10


def discord_request(path, data, content_type):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": content_type,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def post_launch(username, account_type, launcher_version):
    label = ACCOUNT_TYPES.get(account_type, account_type)
    content = f"**{username}** a lance le jeu -- `{label}` -- launcher v{launcher_version}"
    # ensure_ascii=False: the default escapes non-ascii as \uXXXX surrogate
    # pairs, which Discord's API silently drops (empty message content)
    # instead of rejecting outright. Raw UTF-8 bytes work correctly.
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    discord_request(f"/channels/{DISCORD_CHANNEL_ID}/messages", body, "application/json")


def post_crash(username, account_type, launcher_version, filename, file_bytes):
    label = ACCOUNT_TYPES.get(account_type, account_type)
    content = f"**{username}** a crash -- `{label}` -- launcher v{launcher_version}"
    boundary = "EclipseSMPCrashBoundary"

    def field(name, value):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    payload_json = json.dumps({"content": content}, ensure_ascii=False)
    body = io.BytesIO()
    body.write(field("payload_json", payload_json))
    body.write(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode("utf-8")
    )
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    discord_request(
        f"/channels/{DISCORD_CHANNEL_ID}/messages",
        body.getvalue(),
        f"multipart/form-data; boundary={boundary}",
    )


def parse_multipart(body, boundary):
    """Minimal multipart/form-data parser. Returns {name: (value_bytes, filename_or_None)}."""
    boundary_bytes = ("--" + boundary).encode("utf-8")
    parts = body.split(boundary_bytes)
    fields = {}
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, _, content = part.partition(b"\r\n\r\n")
        content = content.rstrip(b"\r\n")
        headers_text = headers_raw.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', headers_text)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers_text)
        filename = filename_match.group(1) if filename_match else None
        fields[name_match.group(1)] = (content, filename)
    return fields


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Analytics-Secret")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def _check_common(self):
        """Auth + rate limit. Returns True if the request should proceed."""
        if self.headers.get("X-Analytics-Secret") != SHARED_SECRET:
            self._send(401)
            return False
        client_ip = self.client_address[0]
        now = time.monotonic()
        key = (client_ip, self.path)
        if now - _last_seen.get(key, 0) < RATE_LIMIT_SECONDS:
            self._send(429)
            return False
        _last_seen[key] = now
        return True

    def do_POST(self):
        if self.path == "/track":
            self._handle_track()
        elif self.path == "/crash":
            self._handle_crash()
        else:
            self._send(404)

    def _handle_track(self):
        if not self._check_common():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            username = data["username"]
            account_type = data["type"]
            launcher_version = str(data.get("launcherVersion", "?"))[:20]
        except Exception:
            self._send(400)
            return

        if not USERNAME_RE.match(username) or account_type not in ACCOUNT_TYPES:
            self._send(400)
            return

        try:
            post_launch(username, account_type, launcher_version)
        except Exception as e:
            print(f"Failed to post launch to Discord: {e}", flush=True)

        self._send(204)

    def _handle_crash(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_CRASH_REPORT_SIZE + 4096:
            self._send(413)
            # Drain to keep the connection usable, then bail.
            self.rfile.read(length)
            return

        if not self._check_common():
            return

        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        if not boundary_match:
            self._send(400)
            return

        try:
            body = self.rfile.read(length)
            fields = parse_multipart(body, boundary_match.group(1))
            username = fields["username"][0].decode("utf-8")
            account_type = fields["type"][0].decode("utf-8")
            launcher_version = fields.get("launcherVersion", (b"?", None))[0].decode("utf-8")[:20]
            file_bytes, filename = fields["file"]
        except Exception:
            self._send(400)
            return

        if not USERNAME_RE.match(username) or account_type not in ACCOUNT_TYPES or not filename:
            self._send(400)
            return

        try:
            post_crash(username, account_type, launcher_version, filename, file_bytes)
        except Exception as e:
            print(f"Failed to post crash to Discord: {e}", flush=True)

        self._send(204)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
