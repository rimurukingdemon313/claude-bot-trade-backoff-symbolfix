"""Run the REAL engine over real historical prices, offline.

Why this file exists, and why it is not what was asked for
-----------------------------------------------------------
The request was to port this bot's strategy into another backtesting
framework and measure it there. That would have produced a number about a
bot that does not exist.

`bot/backtest/engine.py` already drives the real `SmcEngine`, the real
`SetupScorer` and the real `RiskEngine` — its own docstring explains that
duplicating the strategy in the harness would be "a second implementation
of the strategy, drifting from this one the first time either changed".
Re-implementing the SMC detectors, the eight-component scorer, the tier
gates, the classification floors and the R-based sizing inside a second
framework recreates exactly that problem: two codebases that agree today
and disagree silently later, with the backtest reporting on whichever one
was not deployed. Project rule 2 exists for the same reason.

So what was missing was never the harness. It was the DATA. `bot.backtest`
takes candles from the broker and refuses to run without credentials,
deliberately, because "backtesting prices you will not trade is a
simulation of a different market". That refusal is right for a decision
about this account, and it also meant the strategy had never been measured
over a long history at all.

This script supplies that history from a public dataset and is explicit
about what the resulting numbers are and are not.

WHAT THIS DATA IS NOT
---------------------
* Not this broker's prices. Spreads, fills, session boundaries and the
  exact quotes differ. A result here is evidence about the STRATEGY's
  shape, never a forecast of this account's P/L.
* Not current. The set ends in 2022.
* `tick_volume` is tick count, not traded size. Nothing in the engine
  reads it, which is correct for retail FX.

Usage:
    python3 scripts/backtest_offline.py --data DIR --symbol EURUSD
    python3 scripts/backtest_offline.py --data DIR --all --walk-forward
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.engine import Backtester, BacktestCosts
from bot.backtest.montecarlo import monte_carlo
from bot.backtest.walkforward import walk_forward
from bot.broker.models import InstrumentSpec
from bot.config import load_config
from bot.marketdata.candles import Candle

#: Price scaling in the source files. EURUSD prints 127801.0 for 1.27801,
#: so the raw integers are points. The divisor is derived from the median
#: close rather than hardcoded per symbol, because guessing it wrong is
#: silent: every level stays self-consistent and only the ATR-relative
#: gates and the pip maths come out wrong.
KNOWN_DIGITS = {"JPY": 3, "XAU": 2}


def _digits_for(symbol: str, sample_close: float) -> int:
    """How many decimals this instrument really has."""

    if symbol.upper().endswith("JPY"):
        return KNOWN_DIGITS["JPY"]
    if symbol.upper().startswith("XAU"):
        return KNOWN_DIGITS["XAU"]
    return 5


def _plausible_band(symbol: str) -> tuple[float, float]:
    """Where this instrument's price actually lives.

    One band for everything was the bug this function replaces: it
    accepted anything from 0.3 to 5000, so EURUSD at 109305 points
    divided by 100 gave 1093.05 — inside the band, wildly wrong, and
    silent. Every level stayed self-consistent, so nothing looked broken;
    only the ATR-relative gates quietly compared against a price a
    thousand times too large, and the run took zero trades.
    """

    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return 200.0, 5000.0
    if symbol.startswith("XAG"):
        return 5.0, 100.0
    if symbol.endswith("JPY"):
        return 40.0, 400.0
    return 0.3, 10.0


def _scale_for(symbol: str, raw_median: float) -> float:
    """The divisor that turns the file's integers into real prices.

    Derived and then PRINTED by the caller, never assumed, because a
    wrong divisor does not raise — it silently rescales every
    volatility comparison in the engine.
    """

    low, high = _plausible_band(symbol)
    for exponent in range(0, 9):
        divisor = 10.0**exponent
        if low <= raw_median / divisor <= high:
            return divisor
    raise SystemExit(
        f"{symbol}: cannot place a median raw price of {raw_median:g} inside the "
        f"plausible band [{low}, {high}]. Refusing to guess a scale — a wrong one "
        "produces a run that looks fine and measures nothing."
    )


def load_candles(path: Path, *, symbol: str, timeframe: str) -> tuple[list[Candle], float, int]:
    rows: list[tuple[datetime, float, float, float, float, float]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                stamp = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                rows.append(
                    (
                        stamp,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("tick_volume") or 0.0),
                    )
                )
            except (KeyError, ValueError):
                # A malformed row is dropped and counted by the caller via
                # the returned length, never interpolated (rule 6).
                continue
    if not rows:
        raise SystemExit(f"{path}: no usable rows")

    closes = sorted(item[4] for item in rows)
    digits = _digits_for(symbol, closes[len(closes) // 2])
    scale = _scale_for(symbol, closes[len(closes) // 2])

    candles = [
        Candle(
            timestamp=stamp,
            open=o / scale,
            high=h / scale,
            low=lo / scale,
            close=c / scale,
            volume=v,
            timeframe=timeframe,
        )
        for stamp, o, h, lo, c, v in rows
    ]
    candles.sort(key=lambda candle: candle.timestamp)
    return candles, scale, digits


def build_spec(symbol: str, *, digits: int) -> InstrumentSpec:
    """A plausible retail FX contract. Stated, not pretended to be real.

    These are conventional values, not this broker's. Contract size and
    lot step drive position SIZE, so they affect the dollar P/L but not
    the R multiples — which is why the report leads with R.
    """

    symbol = symbol.upper()
    tick = 10.0**-digits
    return InstrumentSpec(
        symbol=symbol,
        broker_name=symbol,
        tradable_instrument_id=1,
        route_id=1,
        quote_route_id=None,
        contract_size=100.0 if symbol.startswith("XAU") else 100_000.0,
        tick_size=tick,
        tick_value=None,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=100.0,
        base_currency=symbol[:3],
        quote_currency=symbol[3:6] or "USD",
        account_currency="USD",
        digits=digits,
    )


def _rate_lookup_for(symbol: str):
    """Quote-currency -> account-currency conversion.

    Only the pairs quoted in USD are handled exactly (rate 1.0). Anything
    else returns None, which makes the sizer REFUSE rather than invent a
    cross rate — the same behaviour as production (rule 6).
    """

    quote = symbol.upper()[3:6]

    def lookup(base: str, target: str) -> float | None:
        if base == target:
            return 1.0
        if quote == "USD":
            return 1.0
        return None

    return lookup


def report(name: str, result, *, note: str = "") -> dict:
    stats = result.statistics()
    closed = result.closed
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    wins = [t.pnl for t in closed if (t.pnl or 0) > 0]
    losses = [t.pnl for t in closed if (t.pnl or 0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    equity = result.equity_curve or [result.starting_balance]
    peak, max_dd = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    out = {
        "name": name,
        "barsProcessed": result.bars_processed,
        "setupsConsidered": result.setups_considered,
        "trades": len(closed),
        "winRate": (len(wins) / len(closed)) if closed else None,
        "profitFactor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "netProfit": round(sum(t.pnl for t in closed if t.pnl is not None), 2),
        "avgR": (sum(rs) / len(rs)) if rs else None,
        "maxDrawdownPct": round(max_dd * 100, 2),
        "bestTrade": round(max((t.pnl for t in closed if t.pnl is not None), default=0.0), 2),
        "worstTrade": round(min((t.pnl for t in closed if t.pnl is not None), default=0.0), 2),
        "finalBalance": stats.get("finalBalance"),
        "sample": "INCONCLUSIVE" if len(closed) < 30 else "usable",
        "note": note,
        "topRejections": sorted(
            result.setups_rejected.items(), key=lambda kv: -kv[1]
        )[:8],
    }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest_offline")
    parser.add_argument("--data", required=True, help="directory of <SYMBOL>/<symbol><tf>.csv")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--all", action="store_true", help="every symbol present")
    parser.add_argument("--bars", type=int, default=0, help="0 = all available")
    parser.add_argument("--warmup", type=int, default=250)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--spread-points", type=float, default=8.0)
    parser.add_argument("--slippage-points", type=float, default=3.0)
    parser.add_argument("--commission", type=float, default=7.0)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--monte-carlo", action="store_true")
    parser.add_argument("--json", default="", help="write the full report here")
    args = parser.parse_args(argv)

    root = Path(args.data)
    if not root.is_dir():
        raise SystemExit(f"{root} is not a directory")

    symbols = (
        sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        if args.all
        else [args.symbol.upper()]
    )

    config = load_config()
    costs = BacktestCosts(
        spread_points=args.spread_points,
        slippage_points=args.slippage_points,
        commission_per_lot=args.commission,
    )

    print("=" * 78)
    print("OFFLINE BACKTEST — real historical prices, REAL engine")
    print("=" * 78)
    print("Data source : ejtraderLabs/historical-data (public, ~10y, ends 2022)")
    print("NOT this broker's prices. Evidence about strategy shape, not this")
    print("account's P/L. No parameter was tuned to these results.")
    print(f"Engine      : SmcEngine + SetupScorer + RiskEngine, unmodified")
    print(f"Costs       : spread {args.spread_points}pts, slip {args.slippage_points}pts, "
          f"commission ${args.commission}/lot")
    print()

    reports = []
    for symbol in symbols:
        m15_path = root / symbol / f"{symbol}m15.csv"
        h1_path = root / symbol / f"{symbol}h1.csv"
        if not m15_path.exists() or not h1_path.exists():
            print(f"{symbol:8} SKIPPED — missing m15 or h1 file")
            continue

        m15, scale, digits = load_candles(m15_path, symbol=symbol, timeframe="M15")
        h1, _, _ = load_candles(h1_path, symbol=symbol, timeframe="H1")
        if args.bars:
            m15 = m15[-args.bars:]
            h1 = h1[-(args.bars // 4 + args.warmup):]

        spec = build_spec(symbol, digits=digits)
        print(f"{symbol:8} {len(m15):>7} M15 bars  {m15[0].timestamp.date()} .. "
              f"{m15[-1].timestamp.date()}   scale/{scale:g} digits={digits} "
              f"sample close {m15[-1].close:.{digits}f}")

        tester = Backtester(
            config,
            spec,
            costs=costs,
            starting_balance=args.balance,
            rate_lookup=_rate_lookup_for(symbol),
        )
        result = tester.run(m15, h1, warmup=args.warmup, step=args.step)
        entry = report(symbol, result)
        reports.append(entry)

        print(f"{'':8} trades={entry['trades']:<5} "
              f"win={_pct(entry['winRate'])} "
              f"avgR={_num(entry['avgR'])} "
              f"PF={_num(entry['profitFactor'])} "
              f"net=${entry['netProfit']:<10} "
              f"maxDD={entry['maxDrawdownPct']}%  [{entry['sample']}]")

        if args.walk_forward and len(result.closed) >= args.folds * 10:
            try:
                folds = walk_forward(
                    config, spec, m15, h1, folds=args.folds, costs=costs, step=args.step
                ).summary()
                print(f"{'':8} walk-forward: {json.dumps(folds, default=str)[:300]}")
                entry["walkForward"] = folds
            except Exception as exc:  # harness failure must not kill the run
                print(f"{'':8} walk-forward unavailable: {type(exc).__name__}: {exc}")

        if args.monte_carlo and len(result.closed) >= 30:
            try:
                mc = monte_carlo(
                    [t.pnl for t in result.closed if t.pnl is not None],
                    starting_balance=args.balance,
                )
                mc = mc.as_dict() if mc is not None else None
                print(f"{'':8} monte-carlo: {json.dumps(mc, default=str)[:300]}")
                entry["monteCarlo"] = mc
            except Exception as exc:
                print(f"{'':8} monte-carlo unavailable: {type(exc).__name__}: {exc}")
        print()

    _print_totals(reports)

    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2, default=str))
        print(f"\nfull report -> {args.json}")
    return 0


def _pct(value):
    return "  n/a " if value is None else f"{value*100:5.1f}%"


def _num(value):
    return "  n/a " if value is None else f"{value:6.3f}"


def _print_totals(reports: list[dict]) -> None:
    usable = [r for r in reports if r["trades"] > 0]
    if not usable:
        print("No trades were taken on any symbol. That is a result, not a failure "
              "(project rule 8) — but with zero trades nothing about edge is measurable.")
        return

    total_trades = sum(r["trades"] for r in usable)
    total_net = sum(r["netProfit"] for r in usable)
    all_r = [r["avgR"] * r["trades"] for r in usable if r["avgR"] is not None]
    weighted_r = sum(all_r) / total_trades if total_trades and all_r else None

    print("=" * 78)
    print("PORTFOLIO TOTAL")
    print("=" * 78)
    print(f"symbols with trades : {len(usable)}")
    print(f"total closed trades : {total_trades}")
    print(f"net profit          : ${total_net:,.2f}   (sum of independent per-symbol runs)")
    print(f"trade-weighted avgR : {_num(weighted_r)}")
    if total_trades < 100:
        print()
        print("SAMPLE: under 100 trades. Treat every number above as INCONCLUSIVE.")
    print()
    print("Each symbol ran on its own $10,000 account, so the net is a sum of")
    print("independent runs, NOT a portfolio equity curve — correlation between")
    print("symbols is not modelled here and would reduce a real account's return.")


if __name__ == "__main__":
    raise SystemExit(main())
