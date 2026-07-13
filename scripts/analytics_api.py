#!/usr/bin/env python3
"""Tiny HTTP API that forwards launcher usage events to a Discord channel.

Runs behind nginx at eclipsesmp.cubi-mc.fr/api/track. Configured entirely via
environment variables (see eclipsesmp-analytics.service on the VPS) so no
secret ever needs to live in this file or in git.

Env vars required:
  DISCORD_TOKEN       Bot token, "Authorization: Bot <token>"
  DISCORD_CHANNEL_ID  Channel to post launch events to
  SHARED_SECRET       Must match the X-Analytics-Secret header sent by the
                       launcher. This only deters casual abuse -- anyone who
                       extracts the launcher's app.asar can read it, since
                       the repo (and therefore the built app) is public.
  PORT                Defaults to 8081.
"""
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

# Minimal per-IP rate limit: one accepted event every 10s.
_last_seen = {}
RATE_LIMIT_SECONDS = 10


def post_to_discord(username, account_type, launcher_version):
    label = ACCOUNT_TYPES.get(account_type, account_type)
    content = f"**{username}** a lance le jeu -- `{label}` -- launcher v{launcher_version}"
    # ensure_ascii=False: the default escapes emoji as \uXXXX surrogate pairs,
    # which Discord's API silently drops (empty message content) instead of
    # rejecting outright. Raw UTF-8 bytes work correctly.
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
            # Cloudflare (in front of Discord's API) blocks the default
            # "Python-urllib/x.y" User-Agent with a 403 (error code 1010).
            "User-Agent": "EclipseSMPAnalytics (https://eclipsesmp.cubi-mc.fr, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


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

    def do_POST(self):
        if self.path != "/track":
            self._send(404)
            return

        if self.headers.get("X-Analytics-Secret") != SHARED_SECRET:
            self._send(401)
            return

        client_ip = self.client_address[0]
        now = time.monotonic()
        if now - _last_seen.get(client_ip, 0) < RATE_LIMIT_SECONDS:
            self._send(429)
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

        _last_seen[client_ip] = now

        try:
            post_to_discord(username, account_type, launcher_version)
        except Exception as e:
            print(f"Failed to post to Discord: {e}", flush=True)

        self._send(204)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
