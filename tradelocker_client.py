"""TradeLocker REST API client.

Replaces the old paper-trading SQLite ledger as the single source of truth
for account balance, open positions, and trade history. Every call in this
module hits TradeLocker's backend-api directly — nothing about account
state is cached to disk, so a Railway restart or redeploy never loses real
data (it is always re-read live from TradeLocker).

Environment variables required:
    TRADELOCKER_EMAIL
    TRADELOCKER_PASSWORD
    TRADELOCKER_SERVER
    TRADELOCKER_ACC_ID      (the numeric accountId shown in the account
                             switcher after the '#', used in the URL path)
    TRADELOCKER_URL         (e.g. https://demo.tradelocker.com/backend-api)

TradeLocker API shape notes (from https://public-api.tradelocker.com):
  - Every /trade/* response is wrapped as {"s": "ok", "d": {...}}.
  - Every /trade/* request needs header 'accNum' = the account's accNum
    (NOT the same as accountId — resolved once via /auth/jwt/all-accounts
    and cached in-memory alongside the token).
  - positions / orders / ordersHistory come back as positional arrays
    (["7277...", "206", ...]) whose column order is defined by
    /trade/config (positionsConfig / ordersConfig / ordersHistoryConfig).
    This module fetches /trade/config once per process and zips columns
    with values into named dicts, so the rest of the code never touches
    raw indices.
  - accountDetailsData (from /trade/accounts/{id}/state) is a flat
    positional number array, named the same way via accountDetailsConfig.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://demo.tradelocker.com/backend-api"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass
class TradeLockerConfig:
    email: str
    password: str
    server: str
    acc_id: str
    base_url: str

    @classmethod
    def from_env(cls) -> "TradeLockerConfig":
        return cls(
            email=_env("TRADELOCKER_EMAIL"),
            password=_env("TRADELOCKER_PASSWORD"),
            server=_env("TRADELOCKER_SERVER"),
            acc_id=_env("TRADELOCKER_ACC_ID"),
            base_url=os.environ.get("TRADELOCKER_URL", DEFAULT_BASE_URL).rstrip("/"),
        )


class TradeLockerError(RuntimeError):
    """Raised for any non-2xx response, transport failure, or bad payload shape."""


# In-memory session cache. A fresh python3 process is spawned per CLI call
# (subprocess-per-invocation, matching how server/routes/trading.ts's
# runPython() shells out to every Python helper in this project), so
# re-login on every invocation is the norm; this cache only helps if a
# single process makes several calls (e.g. the small helper functions below calling each
# other within one `main()` run).
_session: dict[str, Any] = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": 0.0,
    "acc_num": None,
    "config": None,
}


def _http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    retry_on_rate_limit: bool = False,
) -> Any:
    """retry_on_rate_limit enables automatic exponential-backoff retry on
    HTTP 429 (Cloudflare error 1015 "rate_limited"). This is deliberately
    opt-in and MUST stay False for any write (POST/DELETE — placing or
    closing an order): TradeLocker's placeOrder has no documented
    idempotency key, so if a write actually reached TradeLocker's order
    engine but the 429 response arrived after (or instead of) the real
    response, blindly retrying could submit a second real order on top
    of one that already went through. Every GET call in this module
    passes retry_on_rate_limit=True; every write explicitly does not."""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    # Without a browser-like User-Agent, Python's default
    # ("Python-urllib/3.x") is a well-known bot signature that
    # TradeLocker's Cloudflare protection rejects outright with a 403
    # "browser_signature_banned" error before the request ever reaches
    # TradeLocker's own servers — this is not an auth or account problem,
    # it is Cloudflare's edge layer blocking the HTTP client itself.
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    req.add_header("Origin", "https://demo.tradelocker.com")
    req.add_header("Referer", "https://demo.tradelocker.com/")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    # 4 attempts total: initial + 3 retries, waiting 1s, 2s, 4s between
    # them (classic exponential backoff). Only reached at all when a 429
    # occurs and retry_on_rate_limit is True; any other error raises
    # immediately on the first attempt, same as before.
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            is_rate_limited = exc.code == 429
            if retry_on_rate_limit and is_rate_limited and attempt < max_attempts - 1:
                wait_seconds = 2**attempt  # 1, 2, 4
                time.sleep(wait_seconds)
                continue
            raise TradeLockerError(
                f"TradeLocker {method} {url} failed ({exc.code}): {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TradeLockerError(f"TradeLocker {method} {url} unreachable: {exc}") from exc

    # Unreachable in practice (the loop above always returns or raises),
    # but keeps type checkers happy about the function's return path.
    raise TradeLockerError(f"TradeLocker {method} {url} failed after {max_attempts} attempts")


def _unwrap(payload: Any) -> Any:
    """Every /trade/* response is {"s": "ok", "d": {...}}. Unwrap to 'd'."""

    if isinstance(payload, Mapping) and "d" in payload and "s" in payload:
        if payload.get("s") != "ok":
            raise TradeLockerError(f"TradeLocker returned non-ok status: {payload}")
        return payload["d"]
    return payload


def _login(config: TradeLockerConfig) -> None:
    """POST /auth/jwt/token — obtain a fresh access + refresh token pair.

    retry_on_rate_limit=True here is safe: logging in twice in a row
    creates no duplicate financial side effect (unlike placing an order
    twice), and this is exactly the call most likely to be hit by rate
    limiting in practice — every fresh python3 subprocess re-authenticates
    from scratch (see the module docstring), so this is the highest-
    frequency request this client makes.
    """

    result = _http(
        "POST",
        f"{config.base_url}/auth/jwt/token",
        body={"email": config.email, "password": config.password, "server": config.server},
        retry_on_rate_limit=True,
    )
    access_token = result.get("accessToken")
    refresh_token = result.get("refreshToken")
    if not access_token:
        raise TradeLockerError(f"TradeLocker login did not return an accessToken: {result}")
    _session["access_token"] = access_token
    _session["refresh_token"] = refresh_token
    # expireDate is provided, but access tokens are short-lived (~1hr on
    # demo, can be shorter); refresh 2 minutes early to avoid a request
    # straddling expiry.
    _session["expires_at"] = time.time() + 55 * 60
    _session["acc_num"] = None  # force re-resolution against the new session
    _session["config"] = None


def _refresh(config: TradeLockerConfig) -> bool:
    """POST /auth/jwt/refresh — exchange the refresh token for a new access token.

    Returns False on failure so the caller falls back to a full re-login
    instead of raising mid-request.
    """

    refresh_token = _session.get("refresh_token")
    if not refresh_token:
        return False
    try:
        result = _http(
            "POST",
            f"{config.base_url}/auth/jwt/refresh",
            body={"refreshToken": refresh_token},
            retry_on_rate_limit=True,  # safe: refreshing a token twice has no financial side effect
        )
    except TradeLockerError:
        return False
    access_token = result.get("accessToken")
    if not access_token:
        return False
    _session["access_token"] = access_token
    if result.get("refreshToken"):
        _session["refresh_token"] = result["refreshToken"]
    _session["expires_at"] = time.time() + 55 * 60
    return True


def _resolve_acc_num(config: TradeLockerConfig) -> str:
    """GET /auth/jwt/all-accounts — map our accountId to its accNum.

    accountId (path parameter) and accNum (required header on every
    /trade/* call) are different identifiers in TradeLocker's model; this
    resolves the second from the first exactly once per session.
    """

    if _session["acc_num"] is not None:
        return _session["acc_num"]
    result = _http(
        "GET",
        f"{config.base_url}/auth/jwt/all-accounts",
        headers={"Authorization": f"Bearer {_session['access_token']}"},
        retry_on_rate_limit=True,  # safe: GET, no side effects
    )
    accounts = result.get("accounts", [])
    match = next((a for a in accounts if str(a.get("id")) == str(config.acc_id)), None)
    if match is None and accounts:
        # Fall back to the first account rather than hard-failing, in case
        # TRADELOCKER_ACC_ID was actually already an accNum.
        match = accounts[0]
    if match is None:
        raise TradeLockerError(
            f"Could not find account id {config.acc_id} in /auth/jwt/all-accounts response: {result}"
        )
    acc_num = str(match.get("accNum"))
    _session["acc_num"] = acc_num
    return acc_num


def _ensure_session(config: TradeLockerConfig) -> None:
    if not _session["access_token"]:
        _login(config)
    elif time.time() >= _session["expires_at"]:
        if not _refresh(config):
            _login(config)
    if _session["acc_num"] is None:
        _resolve_acc_num(config)


def _authed(
    config: TradeLockerConfig,
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | None = None,
    query: Mapping[str, str] | None = None,
    allow_retry: bool = True,
    _retried: bool = False,
) -> Any:
    """Make an authenticated request.

    allow_retry controls two related-but-distinct automatic retries, both
    gated by the same flag because both are only ever safe for idempotent
    reads (GET):
      1. A 401/403 triggers one re-login + resend (the token was
         invalidated server-side, not just expired).
      2. A 429 (Cloudflare "rate_limited") triggers exponential-backoff
         retries inside _http (1s, 2s, 4s) before giving up.

    allow_retry MUST be False for anything that places or modifies an
    order: TradeLocker's placeOrder has no documented idempotency key, so
    if a write's response is delayed or lost (whether by a 401/403 or by
    rate limiting), blindly resending it could submit a second real order
    on top of one that already went through. Every write call site in
    this module (place_market_order, close_position, close_all_positions)
    explicitly passes allow_retry=False as a hard rule — this function
    never resends a write on a caller's behalf.
    """

    _ensure_session(config)
    url = f"{config.base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {
        "Authorization": f"Bearer {_session['access_token']}",
        "accNum": _session["acc_num"],
    }
    try:
        return _unwrap(
            _http(method, url, headers=headers, body=body, retry_on_rate_limit=allow_retry)
        )
    except TradeLockerError as exc:
        if (
            allow_retry
            and not _retried
            and ("(401)" in str(exc) or "(403)" in str(exc))
        ):
            _login(config)
            return _authed(
                config, method, path, body=body, query=query, allow_retry=allow_retry, _retried=True
            )
        raise


def _get_config(config: TradeLockerConfig) -> dict[str, Any]:
    """GET /trade/config — cached per process; defines column order for
    positions / orders / ordersHistory / accountDetails."""

    if _session["config"] is not None:
        return _session["config"]
    result = _authed(config, "GET", "/trade/config")
    _session["config"] = result
    return result


def _columns(panel_config: Mapping[str, Any]) -> list[str]:
    return [str(col.get("id")) for col in panel_config.get("columns", [])]


def _rows_to_dicts(rows: list[list[Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Public account / market data
# --------------------------------------------------------------------------


def get_account_details(config: TradeLockerConfig) -> dict[str, Any]:
    """GET /trade/accounts/{accountId} — static account info (currency etc)."""

    return _authed(config, "GET", f"/trade/accounts/{config.acc_id}")


def get_account_state(config: TradeLockerConfig) -> dict[str, Any]:
    """Balance, PnL, margin, etc — named via accountDetailsConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("accountDetailsConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/state")
    values = result.get("accountDetailsData", [])
    return dict(zip(columns, values))


def get_positions(config: TradeLockerConfig) -> list[dict[str, Any]]:
    """All currently open positions, named via positionsConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("positionsConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/positions")
    return _rows_to_dicts(result.get("positions", []), columns)


def get_orders(config: TradeLockerConfig) -> list[dict[str, Any]]:
    """Pending/working (non-final) orders, named via ordersConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("ordersConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/orders")
    return _rows_to_dicts(result.get("orders", []), columns)


def get_order_history(config: TradeLockerConfig, limit: int = 50) -> list[dict[str, Any]]:
    """Closed/final orders (filled, canceled, rejected) — used to build the
    trade log and realized PnL history. Named via ordersHistoryConfig."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("ordersHistoryConfig", {}))
    result = _authed(
        config, "GET", f"/trade/accounts/{config.acc_id}/ordersHistory"
    )
    rows = _rows_to_dicts(result.get("ordersHistory", []), columns)
    # Most-recent-first, matching the old paper_trades ordering the
    # dashboard expects.
    rows.sort(key=lambda r: _num(r.get("lastModified") or r.get("createdDate")), reverse=True)
    return rows[:limit]


def get_instruments(config: TradeLockerConfig) -> list[dict[str, Any]]:
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/instruments")
    return result.get("instruments", [])


def _normalize_symbol(raw: str) -> str:
    """Normalize a symbol name for comparison: uppercase, and strip
    characters that commonly vary between brokers for the exact same
    instrument (slashes, dots, underscores, spaces) — e.g. 'EUR/USD',
    'EURUSD.', 'EURUSD_i', and 'EURUSD' should all match 'EURUSD'."""

    return "".join(ch for ch in raw.upper() if ch.isalnum())


def find_instrument(config: TradeLockerConfig, symbol: str) -> dict[str, Any] | None:
    """Resolve a human symbol like 'EURUSD' to its tradableInstrumentId and
    TRADE routeId (both required by place_market_order).

    Matching tries, in order:
      1. Exact match on the normalized name (handles 'EUR/USD' -> 'EURUSD').
      2. Prefix match — the broker's name starts with our symbol, e.g.
         GATESFX lists 'EURUSD.R' (a dot-suffixed variant) rather than a
         bare 'EURUSD'. This is common: brokers append suffixes like '.R',
         '_i', 'm', etc. to mark account type/routing on otherwise
         identical instruments.
    Prefix matching risks ambiguity (e.g. 'EURUSD' could prefix-match both
    'EURUSD.R' and a hypothetical unrelated 'EURUSDGBP'), so if more than
    one instrument matches by prefix, this raises rather than silently
    picking one — a silent wrong pick here means placing a real order on
    the wrong instrument.
    """

    target = _normalize_symbol(symbol)
    instruments = get_instruments(config)

    exact = [i for i in instruments if _normalize_symbol(str(i.get("name", ""))) == target]
    candidates = exact
    if not candidates:
        candidates = [
            i
            for i in instruments
            if _normalize_symbol(str(i.get("name", ""))).startswith(target)
        ]

    if len(candidates) > 1:
        names = ", ".join(sorted(str(i.get("name", "")) for i in candidates))
        raise TradeLockerError(
            f"Symbol '{symbol}' matches multiple instruments on this TradeLocker "
            f"account ({names}) — refusing to guess which one to trade. Update "
            f"the symbol list in server/routes/trading.ts to use the exact name."
        )

    if not candidates:
        # No match at all — surface the actual available instrument names
        # so whoever reads this error can immediately see the real naming
        # convention on this account instead of guessing blindly.
        available = sorted({str(i.get("name", "")) for i in instruments if i.get("name")})
        raise TradeLockerError(
            f"No instrument matching '{symbol}' found on this TradeLocker account. "
            f"Available instrument names ({len(available)} total): {', '.join(available[:40])}"
            + (" ... (truncated)" if len(available) > 40 else "")
        )

    instrument = candidates[0]
    trade_route = next(
        (r for r in instrument.get("routes", []) if r.get("type") == "TRADE"),
        None,
    )
    return {
        "tradableInstrumentId": instrument.get("tradableInstrumentId") or instrument.get("id"),
        "routeId": trade_route.get("id") if trade_route else None,
    }


def get_quote(config: TradeLockerConfig, instrument_id: int, route_id: int) -> dict[str, Any]:
    """Latest bid/ask for an instrument."""

    return _authed(
        config,
        "GET",
        f"/trade/accounts/{config.acc_id}/instruments/{instrument_id}/quotes",
        query={"routeId": str(route_id)},
    )


# --------------------------------------------------------------------------
# Order execution
# --------------------------------------------------------------------------


class OrderSubmissionAmbiguous(TradeLockerError):
    """Raised when a write request's outcome is unknown — the network call
    timed out or the connection dropped after the request left this
    process, so whether TradeLocker actually received and processed the
    order cannot be determined from the response alone.

    Callers MUST NOT blindly retry on this error: TradeLocker's
    placeOrder does not document a client-supplied idempotency key (only
    qty, routeId, side, validity, type, tradableInstrumentId are
    documented as accepted fields), so a naive resend could submit a
    second real order on top of one that already went through. The
    correct recovery is to re-check live positions/orders for this
    symbol before deciding whether to resend.
    """


def place_market_order(
    config: TradeLockerConfig,
    *,
    tradable_instrument_id: int,
    route_id: int,
    side: str,
    quantity: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """POST /trade/accounts/{accountId}/orders — market order with attached SL/TP.

    allow_retry=False is intentional and load-bearing: TradeLocker's
    placeOrder has no documented client-order-id/idempotency field, so
    this module can never safely auto-resend a write on the caller's
    behalf. A 401/403 here surfaces immediately as an error instead of
    silently retrying, and a timeout/connection-reset surfaces as
    OrderSubmissionAmbiguous so the caller is forced to check current
    state before ever considering a resend.
    """

    if quantity <= 0:
        raise ValueError(f"Order quantity must be positive, got {quantity}")
    if stop_loss is not None and stop_loss == 0:
        raise ValueError("stop_loss must not be exactly 0 when provided")
    if take_profit is not None and take_profit == 0:
        raise ValueError("take_profit must not be exactly 0 when provided")

    body: dict[str, Any] = {
        "tradableInstrumentId": tradable_instrument_id,
        "routeId": route_id,
        "qty": quantity,
        "side": side.lower(),  # "buy" | "sell"
        "type": "market",
        "validity": "IOC",
        "price": 0,
    }
    if stop_loss is not None:
        body["stopLoss"] = stop_loss
        body["stopLossType"] = "absolute"
    if take_profit is not None:
        body["takeProfit"] = take_profit
        body["takeProfitType"] = "absolute"

    try:
        return _authed(
            config, "POST", f"/trade/accounts/{config.acc_id}/orders", body=body, allow_retry=False
        )
    except TradeLockerError as exc:
        message = str(exc)
        if "unreachable" in message or "timed out" in message.lower():
            raise OrderSubmissionAmbiguous(
                f"Order submission outcome unknown (network error, not retried automatically): {message}"
            ) from exc
        raise


def close_position(config: TradeLockerConfig, position_id: str) -> dict[str, Any]:
    """DELETE /trade/positions/{positionId} — qty 0 fully closes the position.

    allow_retry=False for the same reason as place_market_order: closing
    is also a write, and TradeLocker gives no idempotency guarantee for
    a resent DELETE either (a resend after the position is already
    closed would simply fail against a position that no longer exists,
    but a resend racing the original request's still-in-flight effect is
    not something this client should risk automatically).
    """

    try:
        return _authed(
            config, "DELETE", f"/trade/positions/{position_id}", body={"qty": 0}, allow_retry=False
        )
    except TradeLockerError as exc:
        message = str(exc)
        if "unreachable" in message or "timed out" in message.lower():
            raise OrderSubmissionAmbiguous(
                f"Position close outcome unknown (network error, not retried automatically): {message}"
            ) from exc
        raise


def close_all_positions(config: TradeLockerConfig) -> dict[str, Any]:
    return _authed(
        config, "DELETE", f"/trade/accounts/{config.acc_id}/positions", allow_retry=False
    )


# --------------------------------------------------------------------------
# CLI entry point — trading.ts's runPython() shells out to this file with
# `<action> <json-payload>` args, same as every other Python helper here.
# --------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: python3 tradelocker_client.py "
            "<state|positions|orders|history|instrument|place-order|close-position> '<json>'"
        )
    action = sys.argv[1]
    payload = json.loads(sys.argv[2])
    config = TradeLockerConfig.from_env()

    if action == "state":
        result = get_account_state(config)
    elif action == "positions":
        result = get_positions(config)
    elif action == "orders":
        result = get_orders(config)
    elif action == "history":
        result = get_order_history(config, limit=int(payload.get("limit", 50)))
    elif action == "instrument":
        result = find_instrument(config, str(payload["symbol"]))
    elif action == "place-order":
        result = place_market_order(
            config,
            tradable_instrument_id=int(payload["tradableInstrumentId"]),
            route_id=int(payload["routeId"]),
            side=str(payload["side"]),
            quantity=float(payload["quantity"]),
            stop_loss=payload.get("stopLoss"),
            take_profit=payload.get("takeProfit"),
        )
    elif action == "close-position":
        result = close_position(config, str(payload["positionId"]))
    else:
        raise SystemExit(f"unknown action: {action}")

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
