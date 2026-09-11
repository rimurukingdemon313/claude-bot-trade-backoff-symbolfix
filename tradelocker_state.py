"""Shapes live TradeLocker account/position/history data into the exact
JSON contract the dashboard (src/pages/dashboard.tsx) already expects.

This is the module trading.ts calls for live account/position/trade
state instead of a local ledger. Nothing
here is a simulation: balance, PnL, open positions, and trade history are
all read live from TradeLocker on every call. Nothing is cached to disk,
so a restart never loses real state — the next call simply re-reads
current truth from TradeLocker.

Trade accounting note (load-bearing, do not "simplify" this away):
TradeLocker's /ordersHistory endpoint lists every FILLED order, and a
single completed round-trip trade appears as (at least) two separate
FILLED rows — one for the order that opened the position, one for the
order that closed it — both sharing the same positionId. Counting every
FILLED row as an independent trade double-counts every completed trade
(confirmed against TradeLocker's own docs: see
https://public-api.tradelocker.com/docs/difference-between-orderid-and-positionid).
This module therefore groups ordersHistory rows by positionId first and
derives one logical trade per position, using the row with a non-zero
realizedPl (the closing fill) for PnL and result.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

import tradelocker_client as tl


class IncompleteAccountData(RuntimeError):
    """Raised when TradeLocker's account-state response is missing a
    field this module needs (most importantly: balance). Callers must
    surface this as a clear "data incomplete" condition — never
    substitute a made-up placeholder balance, which would silently
    misrepresent real account state."""


def _iso(ms_or_s: Any) -> str | None:
    """TradeLocker timestamps are epoch milliseconds; normalize to ISO 8601 UTC."""

    if ms_or_s is None or ms_or_s == "":
        return None
    try:
        value = float(ms_or_s)
    except (TypeError, ValueError):
        return None
    # Heuristic: values above ~10^12 are milliseconds, otherwise seconds.
    seconds = value / 1000 if value > 10**12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def _epoch_seconds(ms_or_s: Any) -> float | None:
    if ms_or_s is None or ms_or_s == "":
        return None
    try:
        value = float(ms_or_s)
    except (TypeError, ValueError):
        return None
    return value / 1000 if value > 10**12 else value


def _num_or_none(value: Any) -> float | None:
    """Unlike a default-substituting numeric parser, this returns None on
    anything unparseable so callers can distinguish "real zero" from
    "field missing/malformed" instead of conflating the two."""

    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    parsed = _num_or_none(value)
    return default if parsed is None else parsed


def _side_label(side: Any) -> str:
    return "LONG" if str(side).lower() == "buy" else "SHORT"


def _group_closed_trades_by_position(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse ordersHistory's per-order FILLED rows into one logical
    trade per positionId. Each completed trade is represented by two (or
    more, if partially filled) FILLED order rows sharing a positionId;
    the opening fill(s) carry zero/no realizedPl and the closing fill
    carries the position's realized PnL. This returns one merged record
    per positionId — the row with the non-zero realizedPl for financials,
    falling back to the last-modified row of the group if every row in
    it reports zero (a legitimate breakeven close)."""

    filled = [row for row in history if str(row.get("status", "")).upper() == "FILLED"]

    by_position: dict[str, list[dict[str, Any]]] = {}
    for row in filled:
        position_id = str(row.get("positionId") or row.get("id"))
        by_position.setdefault(position_id, []).append(row)

    merged: list[dict[str, Any]] = []
    for position_id, rows in by_position.items():
        rows_with_pnl = [r for r in rows if _num_or_none(r.get("realizedPl")) not in (None, 0.0)]
        # Prefer the fill that actually carries realized PnL (the closing
        # fill). If none of the rows in this group show nonzero PnL —
        # either because the position closed exactly at breakeven, or
        # because this account's ordersHistory only ever reports PnL on
        # one specific row — fall back to whichever row was modified
        # last, since that is the closing event chronologically.
        primary = (
            max(rows_with_pnl, key=lambda r: _num(r.get("lastModified") or r.get("createdDate")))
            if rows_with_pnl
            else max(rows, key=lambda r: _num(r.get("lastModified") or r.get("createdDate")))
        )
        opening_row = min(rows, key=lambda r: _num(r.get("createdDate")))
        merged.append(
            {
                **primary,
                "positionId": position_id,
                "_openedAt": opening_row.get("createdDate"),
                "_closedAt": primary.get("lastModified") or primary.get("createdDate"),
                "_fillCount": len(rows),
            }
        )
    return merged


def build_account(
    balance_state: dict[str, Any],
    positions: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    balance = _num_or_none(balance_state.get("balance"))
    if balance is None:
        raise IncompleteAccountData(
            "TradeLocker account state did not include a usable 'balance' field"
        )

    today_net = _num(balance_state.get("todayNet"))
    open_net_pnl = _num(balance_state.get("openNetPnL"))
    closed_trades = _group_closed_trades_by_position(history)

    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    monthly_pnl = 0.0
    pnl30d = 0.0
    wins = 0
    losses = 0
    cutoff_30d = now.timestamp() - 30 * 86400

    for trade in closed_trades:
        pnl = _num(trade.get("realizedPl"))
        closed_seconds = _epoch_seconds(trade.get("_closedAt"))
        closed_iso = _iso(trade.get("_closedAt"))
        if closed_iso and closed_iso.startswith(month_prefix):
            monthly_pnl += pnl
        if closed_seconds is not None and closed_seconds >= cutoff_30d:
            pnl30d += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    trades_count = len(closed_trades) + len(positions)
    peak = max(balance, balance - open_net_pnl if open_net_pnl else balance)
    drawdown = max(0.0, ((peak - balance) / peak) * 100) if peak else 0.0

    return {
        "balance": round(balance, 2),
        "dailyPnl": round(today_net, 2),
        "monthlyPnl": round(monthly_pnl, 2),
        "pnl30d": round(pnl30d, 2),
        "roi30d": round((pnl30d / balance) * 100, 2) if balance else 0.0,
        "trades": trades_count,
        "wins": wins,
        "losses": losses,
        "drawdown": round(drawdown, 2),
        "peak": round(peak, 2),
        "openPositions": len(positions),
    }


def build_open_trade(
    positions: list[dict[str, Any]], names: dict[str, str]
) -> dict[str, Any] | None:
    if not positions:
        return None
    # Most recently opened first — the dashboard's "open trade" card shows
    # a single position, so pick the newest when more than one is open.
    newest_first = sorted(positions, key=lambda p: _num(p.get("openDate")), reverse=True)
    position = newest_first[0]
    instrument_id = str(position.get("tradableInstrumentId"))
    symbol = names.get(instrument_id, instrument_id)
    entry_price = _num(position.get("avgPrice"))
    quantity = _num(position.get("qty"))
    unrealized = _num(position.get("unrealizedPl"))
    side = str(position.get("side", "buy")).upper()

    return {
        "id": str(position.get("id")),
        "symbol": symbol,
        "side": side,
        "status": "OPEN",
        "entryPrice": entry_price,
        # Populated by _attach_sl_tp below; TradeLocker positions carry
        # SL/TP as linked order ids (stopLossId/takeProfitId), not raw
        # prices, so a lookup against the already-fetched orders list is
        # required.
        "stopLoss": 0.0,
        "takeProfit": 0.0,
        "riskAmount": None,
        "quantity": quantity,
        "openedAt": _iso(position.get("openDate")),
        "currentPrice": None,
        "unrealizedPnl": round(unrealized, 2),
        "unrealizedPnlPct": None,
    }


def _attach_sl_tp(
    open_trade: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> None:
    """Fill in real SL/TP prices for the open position, using the already-
    fetched positions/orders lists — this makes no HTTP calls of its own,
    unlike the earlier version of this function."""

    if open_trade is None:
        return
    position = next((p for p in positions if str(p.get("id")) == open_trade["id"]), None)
    if position is None:
        return
    orders_by_id = {str(o.get("id")): o for o in orders}
    sl_order = orders_by_id.get(str(position.get("stopLossId")))
    tp_order = orders_by_id.get(str(position.get("takeProfitId")))
    if sl_order is not None:
        open_trade["stopLoss"] = _num(sl_order.get("stopPrice") or sl_order.get("price"))
    if tp_order is not None:
        open_trade["takeProfit"] = _num(tp_order.get("price"))


def build_trades(
    positions: list[dict[str, Any]],
    history: list[dict[str, Any]],
    names: dict[str, str],
    limit: int = 50,
) -> list[dict[str, Any]]:
    closed_trades = _group_closed_trades_by_position(history)
    rows: list[dict[str, Any]] = []

    for position in positions:
        instrument_id = str(position.get("tradableInstrumentId"))
        rows.append(
            {
                "id": str(position.get("id")),
                "time": _iso(position.get("openDate")),
                "symbol": names.get(instrument_id, instrument_id),
                "side": _side_label(position.get("side")),
                "setup": "Open position",
                "result": "OPEN",
                "pnl": round(_num(position.get("unrealizedPl")), 2),
                "pnlPct": 0.0,
                "confidence": 0,
                "entryPrice": _num(position.get("avgPrice")),
                "stopLoss": None,
                "takeProfit": None,
                "openedAt": _iso(position.get("openDate")),
                "closedAt": None,
            }
        )

    for trade in closed_trades:
        instrument_id = str(trade.get("tradableInstrumentId"))
        pnl = _num(trade.get("realizedPl"))
        rows.append(
            {
                "id": str(trade.get("positionId")),
                "time": _iso(trade.get("_closedAt")),
                "symbol": names.get(instrument_id, instrument_id),
                "side": _side_label(trade.get("side")),
                "setup": "TradeLocker fill",
                "result": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
                "pnl": round(pnl, 2),
                "pnlPct": 0.0,
                "confidence": 0,
                "entryPrice": _num(trade.get("avgPrice") or trade.get("price")),
                "stopLoss": _num_or_none(trade.get("stopLoss")),
                "takeProfit": _num_or_none(trade.get("takeProfit")),
                "openedAt": _iso(trade.get("_openedAt")),
                "closedAt": _iso(trade.get("_closedAt")),
            }
        )

    rows.sort(key=lambda r: r.get("time") or "", reverse=True)
    return rows[:limit]


def build_equity_curve(account: dict[str, Any], trades: list[dict[str, Any]]) -> list[float]:
    """Reconstruct a simple equity curve by walking closed trades backward
    from the current balance. Not persisted — recomputed fresh from
    TradeLocker's own trade history on every call, so it survives restarts
    by definition (there is no separate state to lose)."""

    closed = [t for t in trades if t.get("result") in ("WIN", "LOSS", "BREAKEVEN")]
    closed.sort(key=lambda t: t.get("time") or "")
    running = account["balance"] - sum(t["pnl"] for t in closed)
    curve = [round(running, 2)]
    for trade in closed:
        running += trade["pnl"]
        curve.append(round(running, 2))
    return curve[-30:] if len(curve) > 1 else curve


def current_state(config: tl.TradeLockerConfig) -> dict[str, Any]:
    """Fetches every raw dataset from TradeLocker exactly once, then builds
    the dashboard's full state from those in-memory results.

    This is deliberately NOT structured as build_account() / build_open_trade()
    / build_trades() each independently calling tl.get_positions() etc. An
    earlier version did that, and one call to current_state() ended up
    making 12+ HTTP round-trips (get_positions alone was called 4 times,
    get_instruments twice, get_order_history twice) — which is both slow
    and, in production, enough request volume in a short window to trip
    TradeLocker's Cloudflare rate limiting (HTTP 429 / error 1015) when
    trading.ts's per-symbol scan loop calls this repeatedly. Fetching each
    dataset once here and passing it into the pure builder functions above
    cuts that to exactly 5 HTTP calls per invocation: state, positions,
    orders, order history, and instruments.
    """

    balance_state = tl.get_account_state(config)  # includes one /trade/config call internally
    positions = tl.get_positions(config)
    orders = tl.get_orders(config)
    history = tl.get_order_history(config, limit=500)
    instruments = tl.get_instruments(config)
    names = {
        str(i.get("tradableInstrumentId") or i.get("id")): str(i.get("name", ""))
        for i in instruments
    }

    account = build_account(balance_state, positions, history)  # raises IncompleteAccountData if balance is unusable
    open_trade = build_open_trade(positions, names)
    _attach_sl_tp(open_trade, positions, orders)
    trades = build_trades(positions, history, names, limit=50)
    equity_curve = build_equity_curve(account, trades)
    return {
        "account": account,
        "openTrade": open_trade,
        "trades": trades,
        "equityCurve": equity_curve,
        # latestAiDecision / latestRiskDecision / latestScan are populated
        # by trading.ts from the current in-memory AI+risk cycle result,
        # not by TradeLocker (TradeLocker has no concept of "AI decision").
        "latestAiDecision": None,
        "latestRiskDecision": None,
        "latestScan": None,
        "schedulerEnabled": True,
    }


def main() -> None:
    # Accepts the same <action> <json-payload> shape as tradelocker_client.py
    # so every script invoked by server/routes/trading.ts's runPython() shares
    # one uniform CLI contract. The payload is currently unused by every
    # action this script exposes (state takes no parameters), but the
    # argument is still required and parsed — not silently ignored — so a
    # malformed payload fails loudly here instead of masking a caller bug.
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 tradelocker_state.py <state> <json-payload>")
    action = sys.argv[1]
    try:
        json.loads(sys.argv[2])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON payload: {exc}") from exc

    config = tl.TradeLockerConfig.from_env()
    if action != "state":
        raise SystemExit(f"unknown action: {action}")

    try:
        result = current_state(config)
    except IncompleteAccountData as exc:
        # Surface as valid JSON on stdout (not a stack trace on stderr) so
        # the Node caller can parse and display this cleanly, per the
        # requirement that missing data must be reported explicitly
        # rather than papered over with a fabricated balance.
        print(json.dumps({"status": "incomplete", "error": str(exc)}, separators=(",", ":")))
        raise SystemExit(1)

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
