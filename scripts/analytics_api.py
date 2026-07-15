#!/usr/bin/env python3
"""Tiny HTTP API that forwards launcher usage events to a Discord channel,
stores them in SQLite, and serves a session-authenticated admin dashboard
at /admin (see eclipsesmp.cubi-mc.fr's nginx config -- nginx just proxies
/admin and /api/{track,crash,screenshot,custommods} through, auth is handled here).

Configured entirely via environment variables (see start.sh on the VPS) so
no secret ever needs to live in this file or in git.

Env vars required:
  DISCORD_TOKEN        Bot token, "Authorization: Bot <token>"
  DISCORD_CHANNEL_ID   Channel to post launch/crash events to
  STAFF_CHANNEL_ID     Channel to post custom-mod alerts to (defaults to
                        DISCORD_CHANNEL_ID if unset)
  SHARED_SECRET        Must match the X-Analytics-Secret header sent by the
                        launcher. This only deters casual abuse -- anyone
                        who extracts the launcher's app.asar can read it,
                        since the repo (and therefore the built app) is
                        public.
  ADMIN_PASSWORD_HASH   "<salt_hex>:<pbkdf2_sha256_hex>", see generate_hash()
                        below to create one.
  PORT                  Defaults to 8081.
"""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as xml_escape


def generate_hash(password):
    """Run `python3 analytics_api.py hash <password>` to produce a value
    for ADMIN_PASSWORD_HASH."""
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return salt.hex() + ":" + h.hex()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "hash":
    print(generate_hash(sys.argv[2]))
    sys.exit(0)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
# Where player-shared screenshots are posted ("Media" channel) -- falls back
# to the main channel if unset so this doesn't hard-crash an old deployment.
SCREENSHOT_CHANNEL_ID = os.environ.get("SCREENSHOT_CHANNEL_ID", DISCORD_CHANNEL_ID)
# Where custom (drop-in) mod alerts are posted for staff review -- falls
# back to the main channel if unset.
STAFF_CHANNEL_ID = os.environ.get("STAFF_CHANNEL_ID", DISCORD_CHANNEL_ID)
SHARED_SECRET = os.environ["SHARED_SECRET"]
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]
PORT = int(os.environ.get("PORT", "8081"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
ACCOUNT_TYPES = {"microsoft": "Premium", "offline": "Crack"}

# Cloudflare (in front of Discord's API) blocks the default
# "Python-urllib/x.y" User-Agent with a 403 (error code 1010).
USER_AGENT = "EclipseSMPAnalytics (https://eclipsesmp.cubi-mc.fr, 1.0)"

MAX_CRASH_REPORT_SIZE = 8 * 1024 * 1024  # Discord's own attachment cap.
MAX_SCREENSHOT_SIZE = 8 * 1024 * 1024

# Minimal per-IP rate limit: one accepted event every 10s per endpoint.
_last_seen = {}
RATE_LIMIT_SECONDS = 10

# In-memory admin sessions: token -> expiry (monotonic seconds). Resets on
# service restart, which is fine for a single-admin internal tool.
_sessions = {}
SESSION_TTL_SECONDS = 7 * 24 * 3600
LOGIN_RATE_LIMIT_SECONDS = 3

# Connected admin dashboard sockets (WebSocket, RFC 6455). Broadcasting a
# tiny "something changed" ping lets the dashboard refetch instead of
# polling on a timer.
_ws_clients = set()
_ws_lock = threading.Lock()
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(client_key):
    digest = hashlib.sha1((client_key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_encode_frame(text):
    payload = text.encode("utf-8")
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x81, length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x81, 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 127, length)
    return header + payload


def ws_broadcast(event_type):
    frame = ws_encode_frame(json.dumps({"type": event_type}))
    with _ws_lock:
        dead = []
        for sock in _ws_clients:
            try:
                sock.sendall(frame)
            except OSError:
                dead.append(sock)
        for sock in dead:
            _ws_clients.discard(sock)


def check_password(password):
    try:
        salt_hex, hash_hex = ADMIN_PASSWORD_HASH.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return hmac.compare_digest(actual, expected)


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
            kind TEXT NOT NULL DEFAULT 'game_launch',
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
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """)
    # `kind` was added after the launches table already existed in production --
    # CREATE TABLE IF NOT EXISTS above is a no-op there, so migrate explicitly.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(launches)")}
    if "kind" not in existing_cols:
        conn.execute("ALTER TABLE launches ADD COLUMN kind TEXT NOT NULL DEFAULT 'game_launch'")
    conn.commit()
    conn.close()


def record_launch(username, account_type, launcher_version, ip, kind="game_launch"):
    conn = get_db()
    conn.execute(
        "INSERT INTO launches (username, account_type, launcher_version, ip, kind) VALUES (?, ?, ?, ?, ?)",
        (username, account_type, launcher_version, ip, kind),
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


def list_announcements(limit=30):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, content, author, created_at FROM announcements "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_announcement(title, content, author):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO announcements (title, content, author) VALUES (?, ?, ?)",
        (title, content, author),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_announcement(announcement_id):
    conn = get_db()
    conn.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    conn.commit()
    conn.close()


def render_news_rss():
    """Renders the announcements as an RSS 2.0 feed. The launcher's
    loadNews() (app/assets/js/scripts/landing.js) expects WordPress-style
    <item> fields (title, link, pubDate, content:encoded, dc:creator,
    slash:comments) -- keep this in sync with that parser."""
    items = []
    for a in list_announcements():
        try:
            dt = datetime.datetime.strptime(a["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            dt = datetime.datetime.now(datetime.timezone.utc)
        link = f"https://eclipsesmp.cubi-mc.fr/#news-{a['id']}"
        items.append(f"""    <item>
      <title>{xml_escape(a['title'])}</title>
      <link>{xml_escape(link)}</link>
      <guid isPermaLink="false">announcement-{a['id']}</guid>
      <pubDate>{format_datetime(dt)}</pubDate>
      <dc:creator>{xml_escape(a['author'])}</dc:creator>
      <slash:comments>0</slash:comments>
      <content:encoded><![CDATA[{a['content']}]]></content:encoded>
    </item>""")
    items_xml = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:slash="http://purl.org/rss/1.0/modules/slash/">
  <channel>
    <title>Eclipse SMP -- Actus</title>
    <link>https://eclipsesmp.cubi-mc.fr/</link>
    <description>Annonces du serveur Eclipse SMP</description>
{items_xml}
  </channel>
</rss>
"""


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


def post_launch(username, account_type, launcher_version, kind="game_launch"):
    label = ACCOUNT_TYPES.get(account_type, account_type)
    action = "a ouvert le launcher" if kind == "app_open" else "a lance le jeu"
    content = f"**{username}** {action} -- `{label}` -- launcher v{launcher_version}"
    # ensure_ascii=False: the default escapes non-ascii as \uXXXX surrogate
    # pairs, which Discord's API silently drops (empty message content)
    # instead of rejecting outright. Raw UTF-8 bytes work correctly.
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    discord_request(f"/channels/{DISCORD_CHANNEL_ID}/messages", body, "application/json")


def post_custom_mods(username, account_type, launcher_version, mods):
    label = ACCOUNT_TYPES.get(account_type, account_type)
    mods_list = "\n".join(f"- {m}" for m in mods)
    content = (
        f"**{username}** a des mods custom installes -- `{label}` -- launcher v{launcher_version}\n"
        f"{mods_list}"
    )
    if len(content) > 1900:
        content = content[:1900] + "\n... (troncque)"
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    discord_request(f"/channels/{STAFF_CHANNEL_ID}/messages", body, "application/json")


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


def post_screenshot(username, filename, file_bytes):
    content = f"**{username}** a partage un screenshot"
    boundary = "EclipseSMPScreenshotBoundary"
    content_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"

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
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    discord_request(
        f"/channels/{SCREENSHOT_CHANNEL_ID}/messages",
        body.getvalue(),
        f"multipart/form-data; boundary={boundary}",
    )


# The published port doesn't loop back to 127.0.0.1 on this host (Docker's
# port-publish NAT path only handles external/container traffic), so ping
# the container directly by its Docker network IP instead of localhost.
MC_HOST = "172.22.0.2"
MC_PORT = 25565
STATUS_CACHE_SECONDS = 5
_status_cache = {"at": 0, "value": None}


def _write_varint(value):
    if value < 0:
        # Python ints are arbitrary-precision: a negative value never
        # reaches 0 under repeated >>= 7, so this would spin forever.
        raise ValueError("_write_varint does not support negative values")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(sock):
    value = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Socket closed while reading varint")
        byte = b[0]
        value |= (byte & 0x7F) << (7 * i)
        if not (byte & 0x80):
            return value
    raise ValueError("VarInt too long")


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed while reading payload")
        buf += chunk
    return buf


def ping_minecraft_server(host, port, timeout=3):
    """Minimal Server List Ping (modern, post-1.7 protocol). Returns
    {"online": True, "players_online": int, "players_max": int, "motd": str}
    or {"online": False} if the server can't be reached."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            host_bytes = host.encode("utf-8")
            handshake = (
                _write_varint(0x00)
                # Protocol version is only meaningful for a login handshake;
                # vanilla ignores it for a status ping. -1 (a common "don't
                # care" convention) infinite-loops _write_varint though --
                # Python's negative ints never terminate a repeated right
                # shift -- so send a real placeholder value instead.
                + _write_varint(0)
                + _write_varint(len(host_bytes)) + host_bytes
                + struct.pack(">H", port)
                + _write_varint(1)  # next state: status
            )
            sock.sendall(_write_varint(len(handshake)) + handshake)

            status_request = _write_varint(0x00)
            sock.sendall(_write_varint(len(status_request)) + status_request)

            _read_varint(sock)  # packet length, unused
            packet_id = _read_varint(sock)
            if packet_id != 0x00:
                return {"online": False}
            str_len = _read_varint(sock)
            payload = _recv_exact(sock, str_len).decode("utf-8")
            data = json.loads(payload)

            description = data.get("description", "")
            if isinstance(description, dict):
                description = description.get("text", "") or "".join(
                    e.get("text", "") for e in description.get("extra", [])
                )

            players = data.get("players", {})
            sample = players.get("sample") or []
            return {
                "online": True,
                "players_online": players.get("online", 0),
                "players_max": players.get("max", 0),
                "motd": str(description)[:200],
                "players_sample": [
                    {"name": p.get("name", ""), "uuid": p.get("id", "")}
                    for p in sample
                    if isinstance(p, dict) and p.get("name")
                ][:40],
            }
    except Exception:
        return {"online": False}


def get_server_status():
    now = time.monotonic()
    if _status_cache["value"] is not None and now - _status_cache["at"] < STATUS_CACHE_SECONDS:
        return _status_cache["value"]
    value = ping_minecraft_server(MC_HOST, MC_PORT)
    _status_cache["at"] = now
    _status_cache["value"] = value
    return value


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


ADMIN_PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eclipse SMP -- Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #08070d;
    --bg-elevated: #121022;
    --bg-card: #161329;
    --line: #2a2645;
    --gold: #f3d9ad;
    --violet: #8b6cf2;
    --cyan: #4fd6e8;
    --ink: #ece9f7;
    --ink-dim: #a29ec2;
    --ink-faint: #6b6690;
    --radius: 14px;
    --font-display: "Space Grotesk", "Segoe UI", sans-serif;
    --font-body: "IBM Plex Sans", "Segoe UI", sans-serif;
    --font-mono: "IBM Plex Mono", ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink);
    font-family: var(--font-body);
    background-image: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(139,108,242,0.16), transparent 60%);
  }
  a { color: var(--cyan); }

  /* ---------- Login ---------- */
  #login-view {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .login-card {
    width: 100%; max-width: 360px; background: var(--bg-card); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 36px 32px; text-align: center;
  }
  .login-eyebrow {
    font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.18em;
    color: var(--ink-faint); text-transform: uppercase; margin-bottom: 6px;
  }
  .login-wordmark {
    font-family: var(--font-display); font-weight: 600; font-size: 22px; color: var(--ink);
    margin: 0 0 28px;
  }
  .login-wordmark span { color: var(--gold); }
  .field { text-align: left; margin-bottom: 16px; }
  .field label {
    display: block; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em;
    color: var(--ink-dim); text-transform: uppercase; margin-bottom: 6px;
  }
  .field input {
    width: 100%; background: var(--bg-elevated); border: 1px solid var(--line); border-radius: 8px;
    color: var(--ink); font-family: var(--font-mono); font-size: 14px; padding: 11px 12px;
    outline: none; transition: border-color .15s;
  }
  .field input:focus { border-color: var(--gold); }
  .login-btn {
    width: 100%; margin-top: 8px; background: var(--gold); color: #201a0a; border: none;
    border-radius: 8px; font-family: var(--font-display); font-weight: 600; font-size: 14px;
    padding: 12px; cursor: pointer; transition: filter .15s;
  }
  .login-btn:hover { filter: brightness(1.08); }
  .login-btn:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
  .login-error {
    font-family: var(--font-mono); font-size: 12.5px; color: #ff9b9b; min-height: 18px;
    margin-top: 14px;
  }

  /* ---------- Dashboard ---------- */
  #dashboard-view { display: none; max-width: 1180px; margin: 0 auto; padding: 28px 24px 60px; }
  .topbar {
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px;
    padding-bottom: 18px; border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand .dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--gold);
    box-shadow: 0 0 8px 1px var(--gold); animation: pulse 2.4s ease-in-out infinite;
    transition: background .2s, box-shadow .2s;
  }
  .brand .dot.offline { background: var(--ink-faint); box-shadow: none; animation: none; }
  @media (prefers-reduced-motion: reduce) { .brand .dot { animation: none; } }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  .brand h1 { font-family: var(--font-display); font-size: 17px; margin: 0; font-weight: 600; }
  .brand h1 span { color: var(--gold); }
  .brand .sub { font-family: var(--font-mono); font-size: 11px; color: var(--ink-faint); margin-left: 4px; }
  .logout-btn {
    background: transparent; border: 1px solid var(--line); color: var(--ink-dim);
    font-family: var(--font-mono); font-size: 12px; padding: 8px 14px; border-radius: 8px;
    cursor: pointer; transition: border-color .15s, color .15s;
  }
  .logout-btn:hover { border-color: var(--ink-faint); color: var(--ink); }
  .topbar-right { display: flex; align-items: center; gap: 12px; }
  .server-pill {
    display: inline-flex; align-items: center; gap: 7px; font-family: var(--font-mono);
    font-size: 11.5px; color: var(--ink-dim); border: 1px solid var(--line); border-radius: 999px;
    padding: 6px 12px;
  }
  .server-pill-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-faint); }
  .server-pill.online .server-pill-dot { background: #7cffb0; box-shadow: 0 0 6px 1px rgba(124,255,176,0.5); }
  .server-pill.offline .server-pill-dot { background: #ff8080; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 28px; }
  .kpi { background: var(--bg-card); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 18px; }
  .kpi .n { font-family: var(--font-mono); font-size: 26px; font-weight: 500; color: var(--ink); }
  .kpi .l { font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.06em; color: var(--ink-faint); text-transform: uppercase; margin-top: 4px; }
  .kpi.gold .n { color: var(--gold); }
  .kpi.cyan .n { color: var(--cyan); }

  section { margin-bottom: 32px; }
  .section-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; }
  .section-head h2 { font-family: var(--font-display); font-size: 15px; font-weight: 600; margin: 0; }
  .search-input {
    background: var(--bg-elevated); border: 1px solid var(--line); border-radius: 8px;
    color: var(--ink); font-family: var(--font-mono); font-size: 12.5px; padding: 7px 10px;
    outline: none; width: 180px;
  }
  .search-input:focus { border-color: var(--violet); }
  .section-actions { display: flex; align-items: center; gap: 10px; }
  .export-link {
    font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-dim);
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
    text-decoration: none; white-space: nowrap; transition: border-color .15s, color .15s;
  }
  .export-link:hover { border-color: var(--gold); color: var(--gold); }

  .stack { display: flex; flex-direction: column; gap: 32px; }
  .stack section { margin-bottom: 0; }

  .panel { background: var(--bg-card); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }

  .versions-row {
    display: flex; align-items: center; gap: 10px; padding: 10px 16px;
    font-family: var(--font-mono); font-size: 12px; color: var(--ink-dim);
  }
  .versions-row + .versions-row { border-top: 1px solid var(--line); }
  .versions-row .v-label { min-width: 56px; color: var(--ink); }
  .versions-row .v-bar-wrap { flex: 1; background: var(--bg-elevated); border-radius: 4px; overflow: hidden; height: 6px; }
  .versions-row .v-bar { height: 100%; background: linear-gradient(90deg, var(--violet), var(--gold)); }
  .versions-row .v-count { color: var(--ink-faint); min-width: 20px; text-align: right; }

  #toast-container {
    position: fixed; bottom: 20px; right: 20px; z-index: 50;
    display: flex; flex-direction: column; gap: 10px; align-items: flex-end;
  }
  .toast {
    background: var(--bg-card); border: 1px solid var(--line); border-left: 3px solid #ff8080;
    border-radius: 8px; padding: 12px 16px; font-family: var(--font-mono); font-size: 12.5px;
    color: var(--ink); box-shadow: 0 12px 30px rgba(0,0,0,0.35);
    animation: toast-in .25s ease-out;
    max-width: 300px;
  }
  .toast.out { animation: toast-out .25s ease-in forwards; }
  @keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  @keyframes toast-out { to { opacity: 0; transform: translateY(8px); } }
  @media (prefers-reduced-motion: reduce) { .toast, .toast.out { animation: none; } }

  .chart-wrap { padding: 18px 18px 8px; }
  .chart-wrap svg { width: 100%; height: 140px; display: block; overflow: visible; }
  .chart-bar { fill: url(#barGradient); cursor: pointer; }
  .chart-bar:hover { fill: var(--cyan); }
  .chart-label { font-family: var(--font-mono); font-size: 9.5px; fill: var(--ink-faint); }

  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 16px; font-size: 13px; border-bottom: 1px solid var(--line); }
  th {
    font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.06em; color: var(--ink-faint);
    font-weight: 400; text-transform: uppercase; background: rgba(255,255,255,0.015);
  }
  td { font-family: var(--font-mono); font-size: 12.5px; color: var(--ink-dim); }
  td.user { color: var(--ink); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; }
  .badge.premium { background: rgba(139,108,242,0.18); color: #c3b3ff; }
  .badge.crack { background: rgba(79,214,232,0.14); color: var(--cyan); }
  .rank { font-family: var(--font-mono); color: var(--ink-faint); width: 24px; display: inline-block; }
  .empty { padding: 28px; text-align: center; font-family: var(--font-mono); font-size: 12.5px; color: var(--ink-faint); }

  .crash-row { cursor: pointer; }
  .crash-preview {
    display: none; background: var(--bg-elevated); font-family: var(--font-mono); font-size: 11.5px;
    color: var(--ink-dim); padding: 14px 18px; white-space: pre-wrap; word-break: break-all;
    border-bottom: 1px solid var(--line); max-height: 260px; overflow-y: auto;
  }
  .crash-preview.open { display: block; }
  .dl-link {
    font-family: var(--font-mono); font-size: 11.5px; color: var(--gold); text-decoration: none;
    border: 1px solid rgba(243,217,173,0.3); padding: 3px 9px; border-radius: 6px; white-space: nowrap;
  }
  .dl-link:hover { border-color: var(--gold); }

  .two-col { display: grid; grid-template-columns: 1.6fr 1fr; gap: 20px; align-items: start; }
  @media (max-width: 820px) { .two-col { grid-template-columns: 1fr; } }

  .announcement-form { padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; border-bottom: 1px solid var(--line); }
  .announcement-form input, .announcement-form textarea {
    width: 100%; background: var(--bg-elevated); border: 1px solid var(--line); border-radius: 8px;
    padding: 9px 12px; color: var(--ink); font-family: var(--font-body); font-size: 13px; resize: vertical;
  }
  .announcement-form textarea { min-height: 90px; font-family: var(--font-mono); font-size: 12px; }
  .announcement-form input:focus, .announcement-form textarea:focus { border-color: var(--violet); outline: none; }
  .announcement-form-row { display: flex; gap: 10px; align-items: center; }
  .btn-publish {
    background: var(--violet); color: #fff; border: none; border-radius: 8px; padding: 9px 18px;
    font-weight: 600; font-size: 13px; cursor: pointer; white-space: nowrap;
  }
  .btn-publish:hover { filter: brightness(1.1); }
  .announcement-row {
    padding: 14px 18px; border-bottom: 1px solid var(--line); display: flex;
    justify-content: space-between; align-items: flex-start; gap: 12px;
  }
  .announcement-row:last-child { border-bottom: none; }
  .announcement-title { font-weight: 600; font-size: 13.5px; margin-bottom: 4px; }
  .announcement-meta { font-family: var(--font-mono); font-size: 11px; color: var(--ink-faint); margin-bottom: 6px; }
  .announcement-content { font-size: 12.5px; color: var(--ink-dim); white-space: pre-wrap; }
  .btn-delete-announcement {
    background: transparent; border: 1px solid rgba(255,128,128,0.35); color: #ff8080;
    border-radius: 6px; padding: 4px 10px; font-size: 11.5px; cursor: pointer; white-space: nowrap;
  }
  .btn-delete-announcement:hover { border-color: #ff8080; background: rgba(255,128,128,0.1); }
</style>
</head>
<body>

<div id="login-view">
  <div class="login-card">
    <div class="login-eyebrow">Acces restreint</div>
    <div class="login-wordmark">Eclipse<span>SMP</span> &middot; Admin</div>
    <form id="login-form">
      <div class="field">
        <label for="pw">Mot de passe</label>
        <input type="password" id="pw" autocomplete="current-password" autofocus>
      </div>
      <button type="submit" class="login-btn">Se connecter</button>
      <div class="login-error" id="login-error"></div>
    </form>
  </div>
</div>

<div id="dashboard-view">
  <div class="topbar">
    <div class="brand">
      <span class="dot" id="ws-dot" title="Deconnecte"></span>
      <h1>Eclipse<span>SMP</span></h1>
      <span class="sub">panel admin</span>
    </div>
    <div class="topbar-right">
      <span class="server-pill" id="server-pill" title="Statut du serveur">
        <span class="server-pill-dot"></span>
        <span id="server-pill-text">verification...</span>
      </span>
      <button class="logout-btn" id="logout-btn">Se deconnecter</button>
    </div>
  </div>

  <div class="kpis" id="kpis"></div>

  <section>
    <div class="section-head"><h2>Actus (flux du launcher)</h2></div>
    <div class="panel">
      <div class="announcement-form">
        <input id="announcement-title" placeholder="Titre de l'annonce" maxlength="200">
        <textarea id="announcement-content" placeholder="Contenu (du HTML simple est accepte, ex: &lt;p&gt;...&lt;/p&gt;)"></textarea>
        <div class="announcement-form-row">
          <input id="announcement-author" placeholder="Auteur" maxlength="50" style="max-width:200px">
          <button class="btn-publish" id="announcement-publish">Publier</button>
        </div>
      </div>
      <div id="announcements-rows"></div>
      <div class="empty" id="announcements-empty" style="display:none">Aucune annonce pour l'instant.</div>
    </div>
  </section>

  <section>
    <div class="section-head"><h2>Lancements (14 derniers jours)</h2></div>
    <div class="panel chart-wrap"><svg id="chart" viewBox="0 0 700 140" preserveAspectRatio="none">
      <defs>
        <linearGradient id="barGradient" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="#8b6cf2"/>
          <stop offset="100%" stop-color="#f3d9ad"/>
        </linearGradient>
      </defs>
    </svg></div>
  </section>

  <div class="two-col">
    <section>
      <div class="section-head">
        <h2>Derniers lancements</h2>
        <div class="section-actions">
          <input class="search-input" id="launch-search" placeholder="Filtrer par pseudo...">
          <a class="export-link" href="/admin/api/launches.csv">Exporter CSV</a>
        </div>
      </div>
      <div class="panel">
        <table>
          <thead><tr><th>Pseudo</th><th>Compte</th><th>Version</th><th>IP</th><th>Date</th></tr></thead>
          <tbody id="launches-body"></tbody>
        </table>
        <div class="empty" id="launches-empty" style="display:none">Aucun lancement pour l'instant.</div>
      </div>
    </section>

    <div class="stack">
      <section>
        <div class="section-head"><h2>Top joueurs</h2></div>
        <div class="panel">
          <table>
            <thead><tr><th></th><th>Pseudo</th><th>Lancements</th></tr></thead>
            <tbody id="leaderboard-body"></tbody>
          </table>
          <div class="empty" id="leaderboard-empty" style="display:none">Pas encore de donnees.</div>
        </div>
      </section>

      <section>
        <div class="section-head"><h2>Versions du launcher</h2></div>
        <div class="panel">
          <div id="versions-rows"></div>
          <div class="empty" id="versions-empty" style="display:none">Pas encore de donnees.</div>
        </div>
      </section>
    </div>
  </div>

  <section>
    <div class="section-head">
      <h2>Rapports de crash</h2>
      <a class="export-link" href="/admin/api/crashes.csv">Exporter CSV</a>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Pseudo</th><th>Compte</th><th>Version</th><th>Date</th><th>Rapport</th></tr></thead>
        <tbody id="crashes-body"></tbody>
      </table>
      <div class="empty" id="crashes-empty" style="display:none">Aucun crash signale. Bon signe.</div>
    </div>
  </section>

  <div id="toast-container"></div>
</div>

<script>
function esc(s){ return String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function badge(t){
  return t === 'microsoft'
    ? '<span class="badge premium">Premium</span>'
    : '<span class="badge crack">Crack</span>';
}
function fmtDate(iso){
  try {
    const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    return d.toLocaleString('fr-FR', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' });
  } catch(e){ return iso; }
}

const loginView = document.getElementById('login-view');
const dashView = document.getElementById('dashboard-view');
const wsDot = document.getElementById('ws-dot');
let allLaunches = [];
let ws = null;
let wsRetryDelay = 1000;

function connectLive(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/admin/ws`);
  ws.onopen = () => {
    wsRetryDelay = 1000;
    wsDot.classList.remove('offline');
    wsDot.title = 'Connecte en direct';
  };
  ws.onmessage = (event) => {
    loadAll();
    try {
      const msg = JSON.parse(event.data);
      if(msg.type === 'crash'){ showToast('Un joueur vient de crasher -- voir Rapports de crash.'); }
    } catch(e){ /* ignore malformed pings */ }
  };
  ws.onclose = () => {
    wsDot.classList.add('offline');
    wsDot.title = 'Deconnecte, reconnexion...';
    if(dashView.style.display !== 'none'){
      setTimeout(connectLive, wsRetryDelay);
      wsRetryDelay = Math.min(wsRetryDelay * 1.5, 15000);
    }
  };
  ws.onerror = () => ws.close();
}

let serverStatusTimer = null;

function showDashboard(){
  loginView.style.display = 'none';
  dashView.style.display = 'block';
  loadAll();
  connectLive();
  pollServerStatus();
  serverStatusTimer = setInterval(pollServerStatus, 30000);
}
function showLogin(){
  loginView.style.display = 'flex';
  dashView.style.display = 'none';
  if(ws){ ws.onclose = null; ws.close(); ws = null; }
  if(serverStatusTimer){ clearInterval(serverStatusTimer); serverStatusTimer = null; }
}

function showToast(message){
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 300);
  }, 6000);
}

async function pollServerStatus(){
  const pill = document.getElementById('server-pill');
  const text = document.getElementById('server-pill-text');
  try {
    const res = await fetch('/status');
    const data = await res.json();
    if(data.online){
      pill.classList.add('online'); pill.classList.remove('offline');
      text.textContent = `en ligne -- ${data.players_online}/${data.players_max}`;
    } else {
      pill.classList.add('offline'); pill.classList.remove('online');
      text.textContent = 'hors ligne';
    }
  } catch(e){
    pill.classList.add('offline'); pill.classList.remove('online');
    text.textContent = 'inconnu';
  }
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pw = document.getElementById('pw').value;
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  const res = await fetch('/admin/api/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: pw})
  });
  if(res.ok){ showDashboard(); }
  else if(res.status === 429){ errEl.textContent = 'Trop de tentatives, patiente un instant.'; }
  else { errEl.textContent = 'Mot de passe incorrect.'; document.getElementById('pw').value = ''; }
});

document.getElementById('logout-btn').addEventListener('click', async () => {
  await fetch('/admin/api/logout', { method: 'POST' });
  showLogin();
});

function renderKpis(s){
  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="n">${s.total_launches}</div><div class="l">Lancements</div></div>
    <div class="kpi"><div class="n">${s.unique_players}</div><div class="l">Joueurs uniques</div></div>
    <div class="kpi gold"><div class="n">${s.premium_count}</div><div class="l">Premium</div></div>
    <div class="kpi cyan"><div class="n">${s.crack_count}</div><div class="l">Crack</div></div>
    <div class="kpi"><div class="n">${s.launches_today}</div><div class="l">Aujourd'hui</div></div>
    <div class="kpi"><div class="n">${s.total_crashes}</div><div class="l">Crashs</div></div>
  `;
}

function renderChart(days){
  const svg = document.getElementById('chart');
  const max = Math.max(1, ...days.map(d => d.count));
  const w = 700, h = 140, barGap = 6;
  const barW = (w / days.length) - barGap;
  let bars = '';
  days.forEach((d, i) => {
    const bh = Math.max(2, (d.count / max) * 100);
    const x = i * (w / days.length) + barGap / 2;
    const y = 110 - bh;
    const shortDate = d.day.slice(5).replace('-', '/');
    bars += `<rect class="chart-bar" x="${x}" y="${y}" width="${barW}" height="${bh}" rx="3">
      <title>${d.day}: ${d.count} lancement(s)</title></rect>
      <text class="chart-label" x="${x + barW/2}" y="130" text-anchor="middle">${shortDate}</text>`;
  });
  svg.innerHTML = svg.innerHTML.split('</defs>')[0] + '</defs>' + bars;
}

function renderLaunches(rows){
  const tbody = document.getElementById('launches-body');
  document.getElementById('launches-empty').style.display = rows.length ? 'none' : 'block';
  tbody.innerHTML = rows.map(r => `
    <tr><td class="user">${esc(r.username)}</td><td>${badge(r.account_type)}</td>
    <td>${esc(r.launcher_version)}</td><td>${esc(r.ip)}</td><td>${fmtDate(r.created_at)}</td></tr>
  `).join('');
}

function renderLeaderboard(rows){
  const tbody = document.getElementById('leaderboard-body');
  document.getElementById('leaderboard-empty').style.display = rows.length ? 'none' : 'block';
  tbody.innerHTML = rows.map((r, i) => `
    <tr class="lb-row" data-user="${esc(r.username)}" style="cursor:pointer" title="Filtrer les lancements de ${esc(r.username)}">
      <td class="rank">${i+1}</td><td class="user">${esc(r.username)} ${badge(r.account_type)}</td><td>${r.launches}</td>
    </tr>
  `).join('');
  tbody.querySelectorAll('.lb-row').forEach(row => {
    row.addEventListener('click', () => {
      const search = document.getElementById('launch-search');
      search.value = row.dataset.user;
      search.dispatchEvent(new Event('input'));
      search.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  });
}

function renderVersions(rows){
  const container = document.getElementById('versions-rows');
  document.getElementById('versions-empty').style.display = rows.length ? 'none' : 'block';
  const max = Math.max(1, ...rows.map(r => r.count));
  container.innerHTML = rows.map(r => `
    <div class="versions-row">
      <span class="v-label">v${esc(r.launcher_version)}</span>
      <span class="v-bar-wrap"><span class="v-bar" style="width:${(r.count/max*100).toFixed(0)}%"></span></span>
      <span class="v-count">${r.count}</span>
    </div>
  `).join('');
}

function renderAnnouncements(rows){
  const container = document.getElementById('announcements-rows');
  document.getElementById('announcements-empty').style.display = rows.length ? 'none' : 'block';
  container.innerHTML = rows.map(r => `
    <div class="announcement-row">
      <div style="flex:1; min-width:0;">
        <div class="announcement-title">${esc(r.title)}</div>
        <div class="announcement-meta">${esc(r.author)} -- ${fmtDate(r.created_at)}</div>
        <div class="announcement-content">${esc(r.content)}</div>
      </div>
      <button class="btn-delete-announcement" data-id="${r.id}">Supprimer</button>
    </div>
  `).join('');
  container.querySelectorAll('.btn-delete-announcement').forEach(btn => {
    btn.addEventListener('click', async () => {
      if(!confirm('Supprimer cette annonce ?')) return;
      await fetch('/admin/api/announcements/' + btn.dataset.id, { method: 'DELETE' });
      loadAll();
    });
  });
}

document.getElementById('announcement-publish').addEventListener('click', async () => {
  const title = document.getElementById('announcement-title').value.trim();
  const content = document.getElementById('announcement-content').value.trim();
  const author = document.getElementById('announcement-author').value.trim() || 'Eclipse SMP';
  if(!title || !content) return;
  const btn = document.getElementById('announcement-publish');
  btn.disabled = true;
  try {
    const res = await fetch('/admin/api/announcements', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, content, author }),
    });
    if(res.ok){
      document.getElementById('announcement-title').value = '';
      document.getElementById('announcement-content').value = '';
      loadAll();
    }
  } finally {
    btn.disabled = false;
  }
});

function renderCrashes(rows){
  const tbody = document.getElementById('crashes-body');
  document.getElementById('crashes-empty').style.display = rows.length ? 'none' : 'block';
  tbody.innerHTML = rows.map(r => `
    <tr class="crash-row" data-id="${r.id}">
      <td class="user">${esc(r.username)}</td><td>${badge(r.account_type)}</td>
      <td>${esc(r.launcher_version)}</td><td>${fmtDate(r.created_at)}</td>
      <td><a class="dl-link" href="/admin/api/crashes/${r.id}/download" onclick="event.stopPropagation()">${esc(r.filename)}</a></td>
    </tr>
    <tr><td colspan="5" style="padding:0; border:none;">
      <div class="crash-preview" id="preview-${r.id}">${esc(r.preview)}${r.truncated ? '\n\n[...] telecharge le rapport complet ci-dessus.' : ''}</div>
    </td></tr>
  `).join('');
  tbody.querySelectorAll('.crash-row').forEach(row => {
    row.addEventListener('click', () => {
      document.getElementById('preview-' + row.dataset.id).classList.toggle('open');
    });
  });
}

document.getElementById('launch-search').addEventListener('input', (e) => {
  const q = e.target.value.trim().toLowerCase();
  renderLaunches(q ? allLaunches.filter(r => r.username.toLowerCase().includes(q)) : allLaunches);
});

async function loadAll(){
  const [stats, days, launches, leaderboard, versions, crashes, announcements] = await Promise.all([
    fetch('/admin/api/stats').then(r => r.json()),
    fetch('/admin/api/timeseries').then(r => r.json()),
    fetch('/admin/api/launches?limit=100').then(r => r.json()),
    fetch('/admin/api/leaderboard').then(r => r.json()),
    fetch('/admin/api/versions').then(r => r.json()),
    fetch('/admin/api/crashes?limit=50').then(r => r.json()),
    fetch('/admin/api/announcements').then(r => r.json()),
  ]);
  renderKpis(stats);
  renderChart(days);
  allLaunches = launches;
  renderLaunches(launches);
  renderLeaderboard(leaderboard);
  renderVersions(versions);
  renderCrashes(crashes);
  renderAnnouncements(announcements);
}

fetch('/admin/api/stats').then(r => { if(r.ok) showDashboard(); else showLogin(); }).catch(() => showLogin());
</script>
</body>
</html>
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

    def _check_common(self, key_suffix=None):
        """Auth + rate limit for public /track, /crash and /screenshot. Returns
        True to proceed. key_suffix separates distinct event kinds sharing the
        same path (e.g. /track's app_open vs game_launch) so one doesn't
        rate-limit-suppress the other -- callers that don't care can omit it."""
        if self.headers.get("X-Analytics-Secret") != SHARED_SECRET:
            self._send(401)
            return False
        client_ip = self.headers.get("X-Real-IP", self.client_address[0])
        now = time.monotonic()
        key = (client_ip, self.path, key_suffix)
        if now - _last_seen.get(key, 0) < RATE_LIMIT_SECONDS:
            self._send(429)
            return False
        _last_seen[key] = now
        return True

    def _session_token(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):]
        return None

    def _require_session(self):
        token = self._session_token()
        expiry = _sessions.get(token) if token else None
        if expiry is None or expiry < time.monotonic():
            self._send(401)
            return False
        return True

    def _handle_websocket(self):
        if not self._require_session():
            return

        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._send(400)
            return

        accept = ws_accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        self.connection.sendall(response.encode("utf-8"))

        sock = self.connection
        with _ws_lock:
            _ws_clients.add(sock)
        try:
            # We don't need anything the client sends (no client -> server
            # messages in this app); just block until the connection drops
            # so this thread's socket stays registered for broadcasts.
            while sock.recv(1024):
                pass
        except OSError:
            pass
        finally:
            with _ws_lock:
                _ws_clients.discard(sock)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self._query = urllib.parse.parse_qs(parsed.query)

        if path == "/status":
            status = get_server_status()
            self._send(200, json.dumps(status).encode("utf-8"), "application/json")
            return

        if path == "/news.xml":
            self._send(200, render_news_rss().encode("utf-8"), "application/rss+xml; charset=utf-8")
            return

        if path in ("/admin", "/admin/"):
            self._send(200, ADMIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/admin/ws":
            self._handle_websocket()
            return

        if path.startswith("/admin/api/"):
            if not self._require_session():
                return
            if path == "/admin/api/stats":
                self._handle_stats()
            elif path == "/admin/api/timeseries":
                self._handle_timeseries()
            elif path == "/admin/api/leaderboard":
                self._handle_leaderboard()
            elif path == "/admin/api/versions":
                self._handle_versions()
            elif path == "/admin/api/launches":
                self._handle_launches()
            elif path == "/admin/api/launches.csv":
                self._handle_launches_csv()
            elif path.startswith("/admin/api/crashes/") and path.endswith("/download"):
                self._handle_crash_download(path)
            elif path == "/admin/api/crashes.csv":
                self._handle_crashes_csv()
            elif path == "/admin/api/crashes":
                self._handle_crashes()
            elif path == "/admin/api/announcements":
                self._handle_announcements_list()
            else:
                self._send(404)
            return

        self._send(404)

    def do_POST(self):
        if self.path == "/track":
            self._handle_track()
        elif self.path == "/crash":
            self._handle_crash()
        elif self.path == "/screenshot":
            self._handle_screenshot()
        elif self.path == "/custommods":
            self._handle_custom_mods()
        elif self.path == "/admin/api/login":
            self._handle_login()
        elif self.path == "/admin/api/logout":
            self._handle_logout()
        elif self.path == "/admin/api/announcements":
            self._handle_announcement_create()
        else:
            self._send(404)

    def do_DELETE(self):
        if self.path.startswith("/admin/api/announcements/"):
            self._handle_announcement_delete(self.path)
        else:
            self._send(404)

    def _client_ip(self):
        return self.headers.get("X-Real-IP", self.client_address[0])

    def _handle_login(self):
        ip = self._client_ip()
        now = time.monotonic()
        key = ("login", ip)
        if now - _last_seen.get(key, 0) < LOGIN_RATE_LIMIT_SECONDS:
            self._send(429)
            return
        _last_seen[key] = now

        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            password = data["password"]
        except Exception:
            self._send(400)
            return

        if not check_password(password):
            self._send(401)
            return

        token = secrets.token_urlsafe(32)
        _sessions[token] = time.monotonic() + SESSION_TTL_SECONDS
        self._send(200, b"{}", "application/json", {
            "Set-Cookie": f"session={token}; HttpOnly; Secure; SameSite=Strict; "
                           f"Max-Age={SESSION_TTL_SECONDS}; Path=/admin"
        })

    def _handle_logout(self):
        token = self._session_token()
        _sessions.pop(token, None)
        self._send(200, b"{}", "application/json", {
            "Set-Cookie": "session=; HttpOnly; Secure; SameSite=Strict; Max-Age=0; Path=/admin"
        })

    def _handle_track(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            username = data["username"]
            account_type = data["type"]
            launcher_version = str(data.get("launcherVersion", "?"))[:20]
            kind = data.get("kind", "game_launch")
        except Exception:
            self._send(400)
            return

        if kind not in ("game_launch", "app_open"):
            self._send(400)
            return
        if not self._check_common(key_suffix=kind):
            return

        if not USERNAME_RE.match(username) or account_type not in ACCOUNT_TYPES:
            self._send(400)
            return

        record_launch(username, account_type, launcher_version, self._client_ip(), kind)
        ws_broadcast("launch")

        try:
            post_launch(username, account_type, launcher_version, kind)
        except Exception as e:
            print(f"Failed to post launch to Discord: {e}", flush=True)

        self._send(204)

    def _handle_custom_mods(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            username = data["username"]
            account_type = data["type"]
            launcher_version = str(data.get("launcherVersion", "?"))[:20]
            mods = data.get("mods", [])
        except Exception:
            self._send(400)
            return

        if not self._check_common():
            return

        if not USERNAME_RE.match(username) or account_type not in ACCOUNT_TYPES:
            self._send(400)
            return
        if not isinstance(mods, list) or not mods:
            self._send(400)
            return
        mods = [str(m)[:100] for m in mods[:50]]

        try:
            post_custom_mods(username, account_type, launcher_version, mods)
        except Exception as e:
            print(f"Failed to post custom mods to Discord: {e}", flush=True)

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
        ws_broadcast("crash")

        try:
            post_crash(username, account_type, launcher_version, filename, file_bytes)
        except Exception as e:
            print(f"Failed to post crash to Discord: {e}", flush=True)

        self._send(204)

    def _handle_screenshot(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_SCREENSHOT_SIZE + 4096:
            self._send(413)
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
            file_bytes, filename = fields["file"]
        except Exception:
            self._send(400)
            return

        if not USERNAME_RE.match(username) or not filename:
            self._send(400)
            return
        if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
            self._send(400)
            return

        try:
            post_screenshot(username, filename, file_bytes)
        except Exception as e:
            print(f"Failed to post screenshot to Discord: {e}", flush=True)
            self._send(502)
            return

        self._send(204)

    def _handle_stats(self):
        conn = get_db()
        # kind='game_launch' throughout this file's stats queries -- app_open
        # rows (just "the launcher was opened", no game involved) share this
        # table but must not inflate launch counts/leaderboards/etc.
        total_launches = conn.execute("SELECT COUNT(*) c FROM launches WHERE kind='game_launch'").fetchone()["c"]
        unique_players = conn.execute("SELECT COUNT(DISTINCT username) c FROM launches WHERE kind='game_launch'").fetchone()["c"]
        premium_count = conn.execute(
            "SELECT COUNT(DISTINCT username) c FROM launches WHERE kind='game_launch' AND account_type='microsoft'"
        ).fetchone()["c"]
        crack_count = conn.execute(
            "SELECT COUNT(DISTINCT username) c FROM launches WHERE kind='game_launch' AND account_type='offline'"
        ).fetchone()["c"]
        launches_today = conn.execute(
            "SELECT COUNT(*) c FROM launches WHERE kind='game_launch' AND date(created_at) = date('now')"
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

    def _handle_timeseries(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT date(created_at) as day, COUNT(*) as count FROM launches "
            "WHERE kind='game_launch' AND created_at >= datetime('now', '-13 days') GROUP BY day"
        ).fetchall()
        conn.close()
        counts = {r["day"]: r["count"] for r in rows}
        today = time.strftime("%Y-%m-%d", time.gmtime())
        today_ord = _date_to_ordinal(today)
        series = []
        for i in range(13, -1, -1):
            day = _ordinal_to_date(today_ord - i)
            series.append({"day": day, "count": counts.get(day, 0)})
        self._send(200, json.dumps(series).encode("utf-8"), "application/json")

    def _handle_leaderboard(self):
        conn = get_db()
        rows = conn.execute("""
            SELECT username, COUNT(*) as launches,
                   (SELECT account_type FROM launches l2 WHERE l2.username = l1.username
                    AND l2.kind='game_launch' ORDER BY l2.id DESC LIMIT 1) as account_type
            FROM launches l1 WHERE kind='game_launch' GROUP BY username ORDER BY launches DESC LIMIT 10
        """).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

    def _handle_versions(self):
        conn = get_db()
        rows = conn.execute("""
            SELECT launcher_version, COUNT(*) as count
            FROM launches WHERE kind='game_launch' GROUP BY launcher_version ORDER BY count DESC
        """).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

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
            "FROM launches WHERE kind='game_launch' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

    @staticmethod
    def _csv_escape(value):
        value = "" if value is None else str(value)
        if any(c in value for c in (",", '"', "\n")):
            value = '"' + value.replace('"', '""') + '"'
        return value

    def _handle_launches_csv(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT username, account_type, launcher_version, ip, created_at "
            "FROM launches WHERE kind='game_launch' ORDER BY id DESC"
        ).fetchall()
        conn.close()
        lines = ["username,account_type,launcher_version,ip,created_at"]
        for r in rows:
            lines.append(",".join(self._csv_escape(r[k]) for k in
                         ("username", "account_type", "launcher_version", "ip", "created_at")))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="launches.csv"'})

    def _handle_crashes_csv(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, account_type, launcher_version, filename, created_at "
            "FROM crashes ORDER BY id DESC"
        ).fetchall()
        conn.close()
        lines = ["id,username,account_type,launcher_version,filename,created_at"]
        for r in rows:
            lines.append(",".join(self._csv_escape(r[k]) for k in
                         ("id", "username", "account_type", "launcher_version", "filename", "created_at")))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="crashes.csv"'})

    def _handle_crashes(self):
        limit = self._limit()
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, account_type, launcher_version, filename, content, created_at "
            "FROM crashes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            content = d.pop("content")
            d["preview"] = content[:600]
            d["truncated"] = len(content) > 600
            result.append(d)
        self._send(200, json.dumps(result).encode("utf-8"), "application/json")

    def _handle_crash_download(self, path):
        try:
            # /admin/api/crashes/<id>/download -> ['', 'admin', 'api', 'crashes', '<id>', 'download']
            crash_id = int(path.split("/")[4])
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

    def _handle_announcements_list(self):
        self._send(200, json.dumps(list_announcements()).encode("utf-8"), "application/json")

    def _handle_announcement_create(self):
        if not self._require_session():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            title = str(data["title"]).strip()[:200]
            content = str(data["content"]).strip()[:20000]
            author = str(data.get("author", "Eclipse SMP")).strip()[:50] or "Eclipse SMP"
        except Exception:
            self._send(400)
            return
        if not title or not content:
            self._send(400)
            return
        new_id = create_announcement(title, content, author)
        ws_broadcast("announcement")
        self._send(200, json.dumps({"id": new_id}).encode("utf-8"), "application/json")

    def _handle_announcement_delete(self, path):
        if not self._require_session():
            return
        try:
            # /admin/api/announcements/<id> -> ['', 'admin', 'api', 'announcements', '<id>']
            announcement_id = int(path.split("/")[4])
        except Exception:
            self._send(400)
            return
        delete_announcement(announcement_id)
        ws_broadcast("announcement")
        self._send(204)


def _date_to_ordinal(date_str):
    import datetime
    return datetime.date.fromisoformat(date_str).toordinal()


def _ordinal_to_date(ordinal):
    import datetime
    return datetime.date.fromordinal(ordinal).isoformat()


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
