#!/usr/bin/env python3
"""Tiny HTTP API that forwards launcher usage events to a Discord channel,
stores them in SQLite, and serves a session-authenticated admin dashboard
at /admin (see eclipsesmp.cubi-mc.fr's nginx config -- nginx just proxies
/admin and /api/{track,crash} through, auth is handled here).

Configured entirely via environment variables (see start.sh on the VPS) so
no secret ever needs to live in this file or in git.

Env vars required:
  DISCORD_TOKEN        Bot token, "Authorization: Bot <token>"
  DISCORD_CHANNEL_ID   Channel to post launch/crash events to
  SHARED_SECRET        Must match the X-Analytics-Secret header sent by the
                        launcher. This only deters casual abuse -- anyone
                        who extracts the launcher's app.asar can read it,
                        since the repo (and therefore the built app) is
                        public.
  ADMIN_PASSWORD_HASH   "<salt_hex>:<pbkdf2_sha256_hex>", see generate_hash()
                        below to create one.
  PORT                  Defaults to 8081.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

# Minimal per-IP rate limit: one accepted event every 10s per endpoint.
_last_seen = {}
RATE_LIMIT_SECONDS = 10

# In-memory admin sessions: token -> expiry (monotonic seconds). Resets on
# service restart, which is fine for a single-admin internal tool.
_sessions = {}
SESSION_TTL_SECONDS = 7 * 24 * 3600
LOGIN_RATE_LIMIT_SECONDS = 3


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
  }
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

  .panel { background: var(--bg-card); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }

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
      <span class="dot"></span>
      <h1>Eclipse<span>SMP</span></h1>
      <span class="sub">panel admin</span>
    </div>
    <button class="logout-btn" id="logout-btn">Se deconnecter</button>
  </div>

  <div class="kpis" id="kpis"></div>

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
        <input class="search-input" id="launch-search" placeholder="Filtrer par pseudo...">
      </div>
      <div class="panel">
        <table>
          <thead><tr><th>Pseudo</th><th>Compte</th><th>Version</th><th>IP</th><th>Date</th></tr></thead>
          <tbody id="launches-body"></tbody>
        </table>
        <div class="empty" id="launches-empty" style="display:none">Aucun lancement pour l'instant.</div>
      </div>
    </section>

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
  </div>

  <section>
    <div class="section-head"><h2>Rapports de crash</h2></div>
    <div class="panel">
      <table>
        <thead><tr><th>Pseudo</th><th>Compte</th><th>Version</th><th>Date</th><th>Rapport</th></tr></thead>
        <tbody id="crashes-body"></tbody>
      </table>
      <div class="empty" id="crashes-empty" style="display:none">Aucun crash signale. Bon signe.</div>
    </div>
  </section>
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
let allLaunches = [];

function showDashboard(){
  loginView.style.display = 'none';
  dashView.style.display = 'block';
  loadAll();
}
function showLogin(){
  loginView.style.display = 'flex';
  dashView.style.display = 'none';
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
    <tr><td class="rank">${i+1}</td><td class="user">${esc(r.username)} ${badge(r.account_type)}</td><td>${r.launches}</td></tr>
  `).join('');
}

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
  const [stats, days, launches, leaderboard, crashes] = await Promise.all([
    fetch('/admin/api/stats').then(r => r.json()),
    fetch('/admin/api/timeseries').then(r => r.json()),
    fetch('/admin/api/launches?limit=100').then(r => r.json()),
    fetch('/admin/api/leaderboard').then(r => r.json()),
    fetch('/admin/api/crashes?limit=50').then(r => r.json()),
  ]);
  renderKpis(stats);
  renderChart(days);
  allLaunches = launches;
  renderLaunches(launches);
  renderLeaderboard(leaderboard);
  renderCrashes(crashes);
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

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self._query = urllib.parse.parse_qs(parsed.query)

        if path in ("/admin", "/admin/"):
            self._send(200, ADMIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
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
            elif path == "/admin/api/launches":
                self._handle_launches()
            elif path.startswith("/admin/api/crashes/") and path.endswith("/download"):
                self._handle_crash_download(path)
            elif path == "/admin/api/crashes":
                self._handle_crashes()
            else:
                self._send(404)
            return

        self._send(404)

    def do_POST(self):
        if self.path == "/track":
            self._handle_track()
        elif self.path == "/crash":
            self._handle_crash()
        elif self.path == "/admin/api/login":
            self._handle_login()
        elif self.path == "/admin/api/logout":
            self._handle_logout()
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

    def _handle_timeseries(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT date(created_at) as day, COUNT(*) as count FROM launches "
            "WHERE created_at >= datetime('now', '-13 days') GROUP BY day"
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
                    ORDER BY l2.id DESC LIMIT 1) as account_type
            FROM launches l1 GROUP BY username ORDER BY launches DESC LIMIT 10
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
            "FROM launches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        self._send(200, json.dumps([dict(r) for r in rows]).encode("utf-8"), "application/json")

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
