"""Standalone diagnostic: which HTTP approach gets past TradeLocker's
Cloudflare protection for /auth/jwt/token?

This is NOT part of the live trading path. It exists only to answer one
question with certainty — which of several plausible fixes actually
works — instead of guessing and asking you to redeploy repeatedly.

Run this directly (not through the bot) with your real credentials:

    export TRADELOCKER_EMAIL="you@example.com"
    export TRADELOCKER_PASSWORD="..."
    export TRADELOCKER_SERVER="GATESFX"
    export TRADELOCKER_URL="https://demo.tradelocker.com/backend-api"
    python3 diagnose_tradelocker_connection.py

It prints a clear PASS/FAIL for each approach it tries, and the raw
response body on failure so we can see exactly what blocked it (a
Cloudflare 1010 page looks very different from a normal TradeLocker
401/403 JSON error — telling those apart is the whole point).
"""

from __future__ import annotations

import json
import os
import sys


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FATAL: missing required env var {name}")
        sys.exit(1)
    return value


EMAIL = _env("TRADELOCKER_EMAIL")
PASSWORD = _env("TRADELOCKER_PASSWORD")
SERVER = _env("TRADELOCKER_SERVER")
BASE_URL = os.environ.get("TRADELOCKER_URL", "https://demo.tradelocker.com/backend-api").rstrip("/")
LOGIN_URL = f"{BASE_URL}/auth/jwt/token"

BODY = {"email": EMAIL, "password": PASSWORD, "server": SERVER}


def _summarize(label: str, status: int | None, body: str, error: str | None) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    if error:
        print(f"TRANSPORT ERROR (never reached the server): {error}")
        return
    print(f"HTTP status: {status}")
    lowered = body.lower()
    if "not in allowlist" in lowered or "egress" in lowered:
        print("RESULT: BLOCKED LOCALLY — this machine's own network/firewall/proxy "
              "rejected the request before it left the network. Not a TradeLocker "
              "or Cloudflare result at all; fix local network access first.")
    elif "cloudflare" in lowered and ("1010" in body or "error_code" in lowered):
        print("RESULT: BLOCKED BY CLOUDFLARE (browser_signature_banned / error 1010)")
    elif status == 200 and "accessToken" in body:
        print("RESULT: SUCCESS — got accessToken back")
    elif status in (400, 401, 403) and "accessToken" not in body:
        print("RESULT: Reached TradeLocker's own server (not a Cloudflare block), "
              "but login itself was rejected — check email/password/server values")
    else:
        print("RESULT: Unclear — inspect the raw body below")
    print(f"Body (first 400 chars): {body[:400]}")


def try_urllib_bare() -> None:
    """What the bot currently does before any fix: no User-Agent at all."""

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        LOGIN_URL,
        data=json.dumps(BODY).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _summarize("1. urllib, no User-Agent (baseline — expected to fail)", resp.status, resp.read().decode("utf-8", "replace"), None)
    except urllib.error.HTTPError as exc:
        _summarize("1. urllib, no User-Agent (baseline — expected to fail)", exc.code, exc.read().decode("utf-8", "replace"), None)
    except Exception as exc:  # noqa: BLE001 — diagnostic script, catch-all is intentional
        _summarize("1. urllib, no User-Agent (baseline — expected to fail)", None, "", str(exc))


def try_urllib_with_browser_headers() -> None:
    """The fix already applied to tradelocker_client.py: browser-like headers on urllib."""

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        LOGIN_URL,
        data=json.dumps(BODY).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://demo.tradelocker.com",
            "Referer": "https://demo.tradelocker.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _summarize("2. urllib + browser-like headers", resp.status, resp.read().decode("utf-8", "replace"), None)
    except urllib.error.HTTPError as exc:
        _summarize("2. urllib + browser-like headers", exc.code, exc.read().decode("utf-8", "replace"), None)
    except Exception as exc:  # noqa: BLE001
        _summarize("2. urllib + browser-like headers", None, "", str(exc))


def try_requests_plain() -> None:
    """requests with its own default headers (different TLS stack than urllib)."""

    try:
        import requests
    except ImportError:
        print("\n" + "=" * 70)
        print("3. requests (plain) — SKIPPED: 'requests' package is not installed")
        print("   Install it with: pip install requests")
        print("=" * 70)
        return

    try:
        resp = requests.post(LOGIN_URL, json=BODY, timeout=20)
        _summarize("3. requests, default headers", resp.status_code, resp.text, None)
    except Exception as exc:  # noqa: BLE001
        _summarize("3. requests, default headers", None, "", str(exc))


def try_requests_with_session_and_headers() -> None:
    """requests.Session with a full realistic browser header set, including
    header ORDER matching a real Chrome request more closely than a single
    one-off request typically does."""

    try:
        import requests
    except ImportError:
        print("\n" + "=" * 70)
        print("4. requests.Session + full headers — SKIPPED: 'requests' not installed")
        print("=" * 70)
        return

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Origin": "https://demo.tradelocker.com",
            "Referer": "https://demo.tradelocker.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    try:
        resp = session.post(LOGIN_URL, json=BODY, timeout=20)
        _summarize("4. requests.Session + full browser header set", resp.status_code, resp.text, None)
    except Exception as exc:  # noqa: BLE001
        _summarize("4. requests.Session + full browser header set", None, "", str(exc))


def try_official_tradelocker_package() -> None:
    """The official 'tradelocker' PyPI package, if installed."""

    try:
        from tradelocker import TLAPI
    except ImportError:
        print("\n" + "=" * 70)
        print("5. Official 'tradelocker' package — SKIPPED: not installed")
        print("   Install it with: pip install tradelocker")
        print("=" * 70)
        return

    environment = BASE_URL.split("/backend-api")[0]
    try:
        tl = TLAPI(environment=environment, username=EMAIL, password=PASSWORD, server=SERVER)
        instruments = tl.get_all_instruments()
        print("\n" + "=" * 70)
        print("5. Official 'tradelocker' package")
        print("=" * 70)
        print(f"RESULT: SUCCESS — logged in and fetched {len(instruments)} instrument rows")
    except Exception as exc:  # noqa: BLE001
        print("\n" + "=" * 70)
        print("5. Official 'tradelocker' package")
        print("=" * 70)
        print(f"RESULT: FAILED — {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    print(f"Testing against: {LOGIN_URL}")
    print(f"Server: {SERVER}  |  Email: {EMAIL[:3]}***")
    try_urllib_bare()
    try_urllib_with_browser_headers()
    try_requests_plain()
    try_requests_with_session_and_headers()
    try_official_tradelocker_package()
    print("\n\nDone. Send back the FULL output above — especially which")
    print("approach(es) say SUCCESS vs BLOCKED BY CLOUDFLARE.")
