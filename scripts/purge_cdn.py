#!/usr/bin/env python3
"""Purges jsDelivr's cache for distribution.json after a push.
Run this after every `git push` that touches distribution.json --
jsDelivr's default cache is otherwise up to ~12h, which would leave
players fetching a stale server config.
"""
import urllib.request
import json

URL = "https://purge.jsdelivr.net/gh/edouard57/eclipsesmp@main/distribution.json"

req = urllib.request.Request(URL, headers={"User-Agent": "eclipse-smp-launcher-setup/1.0"})
with urllib.request.urlopen(req, timeout=15) as resp:
    print(json.dumps(json.load(resp), indent=2))
