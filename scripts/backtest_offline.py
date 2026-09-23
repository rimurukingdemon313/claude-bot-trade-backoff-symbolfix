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

from bot.research.data import load_bars


def load_candles(path: Path, *, symbol: str, timeframe: str) -> tuple[list[Candle], float, int]:
    """Real-price candles plus (scale, digits), via the one shared loader.

    The scale logic used to live here AND be needed by the research
    engine. Two copies of the code that already produced two silent
    wrong-scale runs is how a third would happen, so there is one copy, in
    bot/research/data.py. A scale it cannot place with certainty stops the
    run rather than guessing.
    """

    try:
        return load_bars(path, symbol=symbol, timeframe=timeframe)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


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


def build_rate_lookup(root: Path) -> tuple[dict[str, float], "callable"]:
    """Quote-currency -> USD conversion, from the dataset's own pairs.

    The sizer only needs this for true CROSSES. A USD-quoted pair converts
    at 1.0 and a USD-based one (USDJPY) from its own price, both exactly,
    without ever calling this. EURCHF, EURGBP and the JPY crosses need a
    bridge, and without one the sizer correctly refuses — which is why the
    first run reported ZERO trades on every cross. That zero described
    this harness, not the strategy: EURCHF had ~3,000 setups reach sizing
    and every one died there.

    The bridge is each USD pair's MEDIAN close over the whole dataset, a
    deliberate approximation, stated on every run:

    * it moves LOT SIZE, so it scales the dollar P/L of a cross trade by
      up to the pair's range over the decade (USDJPY ran ~80-125);
    * it does NOT move a single R multiple, win or loss — those come from
      price distances on the traded pair alone;
    * it does not change which trades are taken: the risk engine works in
      risk amounts, not lots.

    So for crosses, trust the R column and read the dollar column as
    approximate. A per-bar rate would need the engine's sizing call to
    carry a timestamp, which is an engine change for a report column.
    """

    medians: dict[str, float] = {}
    for pair in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD"):
        path = root / pair / f"{pair}h1.csv"
        if not path.exists():
            continue
        candles, _, _ = load_candles(path, symbol=pair, timeframe="H1")
        closes = sorted(candle.close for candle in candles)
        medians[pair] = closes[len(closes) // 2]

    def lookup(base: str, target: str) -> float | None:
        base, target = base.upper(), target.upper()
        if base == target:
            return 1.0
        return medians.get(base + target)

    return medians, lookup


def _halt_point(result, limit: float) -> dict | None:
    """Where the live kill switch would have stopped this run.

    Replays the equity after each close against the production drawdown
    limit. With `halt_on_max_drawdown` off the run follows exactly the
    production path up to this bar, so this is the real stopping point,
    not an estimate.
    """

    closed = result.closed
    balance = result.starting_balance
    peak = balance
    for index, trade in enumerate(closed):
        balance += trade.pnl or 0.0
        peak = max(peak, balance)
        if peak > 0 and (peak - balance) / peak >= limit:
            return {
                "afterTrades": index + 1,
                "date": trade.exit_time.date().isoformat() if trade.exit_time else None,
                "drawdownPct": round((peak - balance) / peak * 100, 2),
            }
    return None


def _significance(rs: list[float]) -> dict:
    """Is the average R distinguishable from zero at all?

    A t-statistic, reported as that and nothing grander. |t| < 2 means the
    average could plausibly be zero — no edge in either direction has been
    shown, whatever the sign of the mean. This is the number that stops a
    +0.03R from being read as a strategy that works.
    """

    n = len(rs)
    if n < 2:
        return {"n": n, "std": None, "stderr": None, "t": None}
    mean = sum(rs) / n
    variance = sum((r - mean) ** 2 for r in rs) / (n - 1)
    std = math.sqrt(variance)
    stderr = std / math.sqrt(n) if std > 0 else None
    return {
        "n": n,
        "std": round(std, 3),
        "stderr": round(stderr, 4) if stderr else None,
        "t": round(mean / stderr, 2) if stderr else None,
    }


def _by_year(closed) -> dict[str, dict]:
    years: dict[str, list[float]] = {}
    for trade in closed:
        if trade.r_multiple is None or trade.exit_time is None:
            continue
        years.setdefault(str(trade.exit_time.year), []).append(trade.r_multiple)
    return {
        year: {"trades": len(rs), "avgR": round(sum(rs) / len(rs), 3), "sumR": round(sum(rs), 2)}
        for year, rs in sorted(years.items())
    }


def report(name: str, result, *, note: str = "", halt_limit: float | None = None) -> dict:
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
        "firstTrade": closed[0].entry_time.date().isoformat() if closed else None,
        "lastTrade": closed[-1].exit_time.date().isoformat()
        if closed and closed[-1].exit_time
        else None,
        "significance": _significance(rs),
        "sumR": round(sum(rs), 2) if rs else None,
        "byYear": _by_year(closed),
        "productionHalt": _halt_point(result, halt_limit) if halt_limit else None,
        # Every closed trade, so the loss can be taken apart afterwards
        # by grade, setup type, session, direction and exit reason —
        # without re-running a decade of bars to ask a new question.
        "tradeRecords": [trade.as_dict() for trade in closed],
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
    parser.add_argument(
        "--measure-edge",
        action="store_true",
        help="do not stop at the max-drawdown limit; report where it WOULD have stopped",
    )
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

    rate_medians, rate_lookup = build_rate_lookup(root)
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
    if args.measure_edge:
        print("Mode        : MEASURE EDGE — the max-drawdown halt is lifted so the whole")
        print("              history is measured. Everything else, including de-risking in")
        print("              drawdown, is exactly production. The live bot WOULD stop at the")
        print("              point reported as productionHalt.")
    else:
        print("Mode        : PRODUCTION LIMITS — stops for good at max drawdown, as live")
    print("Cross rates : fixed MEDIAN of each USD pair — R figures exact, $ figures on "
          "crosses approximate")
    print("              " + ", ".join(f"{k}={v:g}" for k, v in sorted(rate_medians.items())))
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
            rate_lookup=rate_lookup,
            halt_on_max_drawdown=not args.measure_edge,
        )
        result = tester.run(m15, h1, warmup=args.warmup, step=args.step)
        entry = report(symbol, result, halt_limit=config.risk.max_drawdown_pct)
        reports.append(entry)

        print(f"{'':8} trades={entry['trades']:<5} "
              f"win={_pct(entry['winRate'])} "
              f"avgR={_num(entry['avgR'])} "
              f"PF={_num(entry['profitFactor'])} "
              f"net=${entry['netProfit']:<10} "
              f"maxDD={entry['maxDrawdownPct']}%  [{entry['sample']}]")
        sig = entry["significance"]
        halt = entry["productionHalt"]
        print(f"{'':8} {entry['firstTrade']} .. {entry['lastTrade']}   "
              f"sumR={entry['sumR']}  t={sig['t']}  "
              f"{'(|t|<2: no edge shown either way)' if sig['t'] is not None and abs(sig['t']) < 2 else ''}")
        print(f"{'':8} production halt: "
              + (f"after {halt['afterTrades']} trades on {halt['date']} (DD {halt['drawdownPct']}%)"
                 if halt else "never reached"))

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
