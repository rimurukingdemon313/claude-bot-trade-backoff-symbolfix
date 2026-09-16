"""Backtest / walk-forward / Monte Carlo CLI.

    python3 -m bot.backtest --symbol EURUSD --bars 3000
    python3 -m bot.backtest --symbol EURUSD --walk-forward --folds 3

Candles come from the broker, so this validates the strategy against the
same price series it will trade on. It refuses to run without broker
credentials rather than falling back to a different data source — a
backtest on prices you will not trade is a simulation of a different
market.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..broker.tradelocker import TradeLockerBroker
from ..config import load_config
from ..errors import BotError
from ..marketdata.candles import to_candles
from ..marketdata.validation import validate_series
from .engine import Backtester, BacktestCosts
from .montecarlo import monte_carlo
from .walkforward import walk_forward


def _fetch(broker: TradeLockerBroker, spec, timeframe: str, count: int):
    raw = broker.candles(spec, timeframe, count=count)
    candles = to_candles(raw)
    cleaned, _ = validate_series(
        candles, timeframe=timeframe, min_candles=40, max_age_multiple=10_000.0
    )
    return cleaned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.backtest")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--bars", type=int, default=2000, help="M15 bars to evaluate")
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--step", type=int, default=1, help="evaluate every Nth bar")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--spread-points", type=float, default=8.0)
    parser.add_argument("--slippage-points", type=float, default=3.0)
    parser.add_argument("--commission", type=float, default=7.0, help="per lot, round turn")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--monte-carlo", action="store_true")
    args = parser.parse_args(argv)

    config = load_config()
    if not config.broker.configured:
        print(
            "TradeLocker credentials are not configured. This tool deliberately has no "
            "alternative data source: backtesting prices you will not trade is a "
            "simulation of a different market.",
            file=sys.stderr,
        )
        return 2

    broker = TradeLockerBroker(config)
    try:
        broker.ensure_session()
        spec = broker.instrument(args.symbol)
        m15 = _fetch(broker, spec, "M15", args.bars)
        h1 = _fetch(broker, spec, "H1", max(400, args.bars // 4))
    except BotError as exc:
        print(f"Could not load broker data: {exc}", file=sys.stderr)
        return 1

    costs = BacktestCosts(
        spread_points=args.spread_points,
        slippage_points=args.slippage_points,
        commission_per_lot=args.commission,
    )

    if args.walk_forward:
        result = walk_forward(
            config, spec, m15, h1, folds=args.folds, costs=costs, step=args.step
        )
        print(json.dumps(result.summary(), indent=2, default=str))
        return 0

    backtester = Backtester(config, spec, costs=costs, starting_balance=args.balance)
    result = backtester.run(m15, h1, warmup=args.warmup, step=args.step)
    payload: dict = {"symbol": args.symbol, "statistics": result.statistics()}

    if args.monte_carlo:
        simulation = monte_carlo(
            [trade.pnl or 0.0 for trade in result.closed], starting_balance=args.balance
        )
        payload["monteCarlo"] = (
            simulation.as_dict()
            if simulation
            else {"note": "fewer than 10 closed trades — no dispersion analysis is meaningful"}
        )

    payload["trades"] = [trade.as_dict() for trade in result.closed[-50:]]
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
