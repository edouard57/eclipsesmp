#!/usr/bin/env python3
"""Tiny HTTP API that forwards launcher usage events to a Discord channel and
stores them in SQLite for the admin panel (served at /admin, gated by nginx
basic auth -- see eclipsesmp.cubi-mc.fr's nginx config).

Runs behind nginx at eclipsesmp.cubi-mc.fr/api/{track,crash} and /admin.
Configured entirely via environment variables (see start.sh on the VPS) so
no secret ever needs to live in this file or in git.

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
import sqlite3
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
SHARED_SECRET = os.environ["SHARED_SECRET"]
PORT = int(os.environ.get("PORT", "8081"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
ACCOUNT_TYPES = {"microsoft": "Premium", "offline": "Crack"}

# Cloudflare (in front of Discord's API) blocks the default
# "Python-urllib/x.y" User-Agent with a 403 (error code 1010).
USER_AGENT = "EclipseSMPAnalytics (https://eclipsesmp.cubi-mc.fr, 1.0)"

MAX_CRASH_REPORT_SIZE = 8 * 1024 * 1024  # Discord's own attachment cap.

# Minimal per-IP rate limit: one accepted event every 10s per endpoint.
_last_seen = {}
RATE_LIMIT_SECONDS = 10


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS launches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            account_type TEXT NOT NULL,
            launcher_version TEXT,
            ip TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS crashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            account_type TEXT NOT NULL,
            launcher_version TEXT,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            ip TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """)
    conn.commit()
    conn.close()


def record_launch(username, account_type, launcher_version, ip):
    conn = get_db()
    conn.execute(
        "INSERT INTO launches (username, account_type, launcher_version, ip) VALUES (?, ?, ?, ?)",
        (username, account_type, launcher_version, ip),
    )
    conn.commit()
    conn.close()


def record_crash(username, account_type, launcher_version, filename, content, ip):
    conn = get_db()
    conn.execute(
        "INSERT INTO crashes (username, account_type, launcher_version, filename, content, ip) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, account_type, launcher_version, filename, content, ip),
    )
    conn.commit()
    conn.close()


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


ADMIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Eclipse SMP -- Admin</title>
<style>
  body { background:#111; color:#ddd; font-family:system-ui,sans-serif; margin:0; padding:2rem; }
  h1 { color:#fff; }
  .cards { display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:2rem; }
  .card { background:#1c1c1c; border:1px solid #333; border-radius:8px; padding:1rem 1.5rem; min-width:140px; }
  .card .n { font-size:1.8rem; font-weight:bold; color:#fff; }
  .card .l { font-size:0.8rem; color:#999; text-transform:uppercase; }
  table { width:100%; border-collapse:collapse; margin-bottom:2rem; }
  th, td { text-align:left; padding:0.5rem; border-bottom:1px solid #2a2a2a; font-size:0.9rem; }
  th { color:#999; font-weight:normal; text-transform:uppercase; font-size:0.75rem; }
  a { color:#7cb0ff; }
  .premium { color:#7cffb0; }
  .crack { color:#ffb07c; }
</style></head>
<body>
<h1>Eclipse SMP -- Panel Admin</h1>
<div class="cards" id="cards">Chargement...</div>
<h2>Derniers lancements</h2>
<table id="launches"><thead><tr><th>Pseudo</th><th>Compte</th><th>Launcher</th><th>IP</th><th>Date</th></tr></thead><tbody></tbody></table>
<h2>Derniers crashs</h2>
<table id="crashes"><thead><tr><th>Pseudo</th><th>Compte</th><th>Launcher</th><th>Rapport</th><th>Date</th></tr></thead><tbody></tbody></table>
<script>
function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function typeLabel(t){ return t === 'microsoft' ? '<span class="premium">Premium</span>' : '<span class="crack">Crack</span>'; }

fetch('api/stats').then(r => r.json()).then(s => {
    document.getElementById('cards').innerHTML = `
        <div class="card"><div class="n">${s.total_launches}</div><div class="l">Lancements</div></div>
        <div class="card"><div class="n">${s.unique_players}</div><div class="l">Joueurs uniques</div></div>
        <div class="card"><div class="n">${s.premium_count}</div><div class="l">Premium</div></div>
        <div class="card"><div class="n">${s.crack_count}</div><div class="l">Crack</div></div>
        <div class="card"><div class="n">${s.launches_today}</div><div class="l">Aujourd'hui</div></div>
        <div class="card"><div class="n">${s.total_crashes}</div><div class="l">Crashs</div></div>
    `;
});

fetch('api/launches?limit=50').then(r => r.json()).then(rows => {
    document.querySelector('#launches tbody').innerHTML = rows.map(r => `
        <tr><td>${esc(r.username)}</td><td>${typeLabel(r.account_type)}</td>
        <td>${esc(r.launcher_version)}</td><td>${esc(r.ip || '')}</td><td>${esc(r.created_at)}</td></tr>
    `).join('');
});

fetch('api/crashes?limit=50').then(r => r.json()).then(rows => {
    document.querySelector('#crashes tbody').innerHTML = rows.map(r => `
        <tr><td>${esc(r.username)}</td><td>${typeLabel(r.account_type)}</td>
        <td>${esc(r.launcher_version)}</td>
        <td><a href="api/crashes/${r.id}/download">${esc(r.filename)}</a></td>
        <td>${esc(r.created_at)}</td></tr>
    `).join('');
});
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", content_type=None, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if content_type:
            self.send_header("Content-Type", content_type)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
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
        """Auth + rate limit for public /track and /crash. Returns True to proceed."""
        if self.headers.get("X-Analytics-Secret") != SHARED_SECRET:
            self._send(401)
            return False
        client_ip = self.headers.get("X-Real-IP", self.client_address[0])
        now = time.monotonic()
        key = (client_ip, self.path)
        if now - _last_seen.get(key, 0) < RATE_LIMIT_SECONDS:
            self._send(429)
            return False
        _last_seen[key] = now
        return True

    def do_GET(self):
        # Everything under /admin is only reachable through nginx's basic-auth
        # gated location -- no additional check needed here.
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self._query = urllib.parse.parse_qs(parsed.query)

        if path == "/admin/" or path == "/admin":
            self._send(200, ADMIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/admin/api/stats":
            self._handle_stats()
        elif path == "/admin/api/launches":
            self._handle_launches()
        elif path.startswith("/admin/api/crashes/") and path.endswith("/download"):
            self._handle_crash_download(path)
        elif path == "/admin/api/crashes":
            self._handle_crashes()
        else:
            self._send(404)

    def do_POST(self):
        if self.path == "/track":
            self._handle_track()
        elif self.path == "/crash":
            self._handle_crash()
        else:
            self._send(404)

    def _client_ip(self):
        return self.headers.get("X-Real-IP", self.client_address[0])

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

        record_launch(username, account_type, launcher_version, self._client_ip())

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

        record_crash(
            username, account_type, launcher_version, filename,
            file_bytes.decode("utf-8", errors="replace"), self._client_ip()
        )

        try:
            post_crash(username, account_type, launcher_version, filename, file_bytes)
        except Exception as e:
            print(f"Failed to post crash to Discord: {e}", flush=True)

        self._send(204)

    def _handle_stats(self):
        conn = get_db()
        total_launches = conn.execute("SELECT COUNT(*) c FROM launches").fetchone()["c"]
        unique_players = conn.execute("SELECT COUNT(DISTINCT username) c FROM launches").fetchone()["c"]
        premium_count = conn.execute(
            "SELECT COUNT(DISTINCT username) c FROM launches WHERE account_type='microsoft'"
        ).fetchone()["c"]
        crack_count = conn.execute(
            "SELECT COUNT(DISTINCT username) c FROM launches WHERE account_type='offline'"
        ).fetchone()["c"]
        launches_today = conn.execute(
            "SELECT COUNT(*) c FROM launches WHERE date(created_at) = date('now')"
        ).fetchone()["c"]
        total_crashes = conn.execute("SELECT COUNT(*) c FROM crashes").fetchone()["c"]
        conn.close()
        self._send(200, json.dumps({
            "total_launches": total_launches,
            "unique_players": unique_players,
            "premium_count": premium_count,
            "crack_count": crack_count,
            "launches_today": launches_today,
            "total_crashes": total_crashes,
        }).encode("utf-8"), "application/json")

    def _limit(self):
        try:
            return min(int(self._query.get("limit", ["50"])[0]), 200)
        except (ValueError, IndexError):
            return 50

    def _handle_launches(self):
        limit = self._limit()
        conn = get_db()
        rows = conn.execute(
            "SELECT username, account_type, launcher_version, ip, created_at "
            "FROM launches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

    def _handle_crashes(self):
        limit = self._limit()
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, account_type, launcher_version, filename, created_at "
            "FROM crashes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

    def _handle_crash_download(self, path):
        try:
            crash_id = int(path.split("/")[3])
        except Exception:
            self._send(400)
            return
        conn = get_db()
        row = conn.execute("SELECT filename, content FROM crashes WHERE id = ?", (crash_id,)).fetchone()
        conn.close()
        if row is None:
            self._send(404)
            return
        self._send(
            200,
            row["content"].encode("utf-8"),
            "text/plain; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
        )


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
