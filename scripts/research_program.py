"""Run the eight-family program and apply docs/EXPERIMENT_EDGE_PROGRAM.md.

Every gate below is transcribed from the pre-registration, committed
before this file ran. Nothing here chooses a parameter: the base
parameters are the ones reported, the 3x3 grid is a stability test, and
the spread-doubled run is a robustness test.

Work is split by (family, symbol) so each worker holds one instrument's
bars at a time; every variant of that pair runs against the same load.

Usage:
    python3 scripts/research_program.py DATA_DIR [--families F1,F4] [--json OUT] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.research.daily import costs_for, summarise
from bot.research.data import load_bars, trading_date
from bot.research.engine import Context, daily_atr_series, simulate
from bot.research.families import (
    FAMILIES, CrossSectionalMomentum, align_by_trading_date, cross_sectional_schedule,
)

IS_END = datetime(2016, 1, 1, tzinfo=timezone.utc)
VAL_END = datetime(2019, 1, 1, tzinfo=timezone.utc)
T_THRESHOLD = 2.89
OOS_YEARS = (2019, 2020, 2021, 2022)
MULTIPLIERS = (0.75, 1.0, 1.25)
FX_PAIRS = ("AUDJPY", "AUDUSD", "EURCHF", "EURGBP", "EURJPY", "EURUSD",
            "GBPJPY", "GBPUSD", "USDCAD", "USDCHF", "USDJPY")
SUFFIX = {"D1": "d1", "H4": "h4", "H1": "h1", "M15": "m15"}


def variants(key):
    """(label, params, spread_multiple). Base first; spread x2 last."""

    cls, _, (p1, p2) = FAMILIES[key]
    base = cls()
    b1, b2 = getattr(base, p1), getattr(base, p2)

    def scaled(value, m):
        return int(round(value * m)) if isinstance(value, int) else value * m

    out = []
    for m1 in MULTIPLIERS:
        for m2 in MULTIPLIERS:
            label = "base" if (m1, m2) == (1.0, 1.0) else f"{p1}x{m1}/{p2}x{m2}"
            out.append((label, {p1: scaled(b1, m1), p2: scaled(b2, m2)}, 1.0))
    out.sort(key=lambda v: v[0] != "base")
    out.append(("spread_x2", {}, 2.0))
    return out


def _slim(trade):
    return {"symbol": trade.symbol, "entryTime": trade.entry_time.isoformat(),
            "exitTime": trade.exit_time.isoformat(), "r": trade.r, "costR": trade.cost_r,
            "exitReason": trade.exit_reason, "tag": trade.tag}


def _bars(root, symbol, timeframe):
    bars, _, _ = load_bars(root / symbol / f"{symbol}{SUFFIX[timeframe]}.csv",
                           symbol=symbol, timeframe=timeframe)
    return bars


def run_single(job):
    """One family on one instrument, every variant."""

    key, symbol, root = job
    root = Path(root)
    cls, timeframe, _ = FAMILIES[key]
    bars = _bars(root, symbol, timeframe)
    daily = bars if timeframe == "D1" else _bars(root, symbol, "D1")
    times, values = daily_atr_series(daily)
    out = {}
    for label, params, spread_mult in variants(key):
        costs = costs_for(symbol).variant(spread_multiple=spread_mult)
        ctx = Context(symbol=symbol, costs=costs, daily_atr_times=times, daily_atr_values=values)
        trades = simulate(bars, cls(**params), ctx)
        out[label] = [_slim(t) for t in trades if t.r is not None]
    return key, symbol, out


def run_cross_sectional(root):
    root = Path(root)
    aligned = align_by_trading_date({p: _bars(root, p, "D1") for p in FX_PAIRS})
    out = defaultdict(list)
    for label, params, spread_mult in variants("F3"):
        rule = CrossSectionalMomentum(**params)
        schedule = cross_sectional_schedule(aligned, rule.lookback)
        for pair, bars in aligned.items():
            times, values = daily_atr_series(bars)
            ctx = Context(symbol=pair, costs=costs_for(pair).variant(spread_multiple=spread_mult),
                          daily_atr_times=times, daily_atr_values=values,
                          extra={"schedule": schedule})
            out[label] += [_slim(t) for t in simulate(bars, rule, ctx) if t.r is not None]
    return "F3", "*", dict(out)


# -- statistics -------------------------------------------------------------------------


def _when(trade, field="entryTime"):
    return datetime.fromisoformat(trade[field])


def segment(trades, lo=None, hi=None):
    return [t for t in trades if (lo is None or _when(t) >= lo) and (hi is None or _when(t) < hi)]


def rs(trades):
    return [t["r"] for t in trades]


def daily_ratios(trades, start, end):
    """Sharpe and Sortino of the daily R series over every weekday in [start, end)."""

    by_day = defaultdict(float)
    for t in trades:
        by_day[trading_date(_when(t, "exitTime"))] += t["r"]
    days, day = [], start.date()
    while day < end.date():
        if day.weekday() < 5:
            days.append(by_day.get(day, 0.0))
        day = datetime.fromordinal(day.toordinal() + 1).date()
    if len(days) < 2:
        return None, None
    mean = sum(days) / len(days)
    sd = math.sqrt(sum((x - mean) ** 2 for x in days) / (len(days) - 1))
    down = math.sqrt(sum(min(0.0, x) ** 2 for x in days) / len(days))
    root252 = math.sqrt(252)
    return (mean / sd * root252 if sd > 0 else None, mean / down * root252 if down > 0 else None)


def monte_carlo(values, runs=10_000, seed=7):
    if len(values) < 10:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(runs))
    return {"p5": means[int(0.05 * runs)], "pAtMostZero": sum(1 for m in means if m <= 0) / runs}


def rolling_windows(trades):
    """12-month windows stepping monthly; share with avgR > 0 (≥ 10 trades)."""

    if not trades:
        return None
    first = min(_when(t) for t in trades)
    last = max(_when(t) for t in trades)
    y, m, positive, total = first.year, first.month, 0, 0
    while True:
        lo = datetime(y, m, 1, tzinfo=timezone.utc)
        hi = datetime(y + 1, m, 1, tzinfo=timezone.utc)
        if hi > last:
            break
        window = rs(segment(trades, lo, hi))
        if len(window) >= 10:
            total += 1
            positive += sum(window) / len(window) > 0
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return {"windows": total, "positive": positive}


def max_drawdown(trades):
    peak = cum = worst = 0.0
    for t in sorted(trades, key=lambda t: t["exitTime"]):
        cum += t["r"]
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def judge(key, by_variant):
    base = by_variant["base"]
    ins, val, oos = segment(base, None, IS_END), segment(base, IS_END, VAL_END), segment(base, VAL_END)
    s_is, s_val, s_oos = summarise(rs(ins)), summarise(rs(val)), summarise(rs(oos))
    positive = lambda s: s["avgR"] is not None and s["avgR"] > 0

    g1 = positive(s_is) and positive(s_val) and positive(s_oos)
    g2 = s_oos["t"] is not None and s_oos["t"] > T_THRESHOLD

    per_symbol = defaultdict(list)
    for t in oos:
        per_symbol[t["symbol"]].append(t["r"])
    eligible = {k: v for k, v in per_symbol.items() if len(v) >= 20}
    share_pos = (sum(1 for v in eligible.values() if sum(v) / len(v) > 0) / len(eligible)) if eligible else 0.0
    total = sum(rs(oos))
    top_share = (max(sum(v) for v in per_symbol.values()) / total) if per_symbol and total > 0 else None
    g3 = bool(eligible) and share_pos >= 2 / 3 and top_share is not None and top_share <= 0.5

    years = {y: rs([t for t in oos if _when(t).year == y]) for y in OOS_YEARS}
    positive_years = sum(1 for v in years.values() if v and sum(v) / len(v) > 0)
    g4 = positive_years >= 3

    plateau = [lbl for lbl in by_variant if lbl not in ("spread_x2",)]
    plateau_pos = sum(1 for lbl in plateau if positive(summarise(rs(segment(by_variant[lbl], VAL_END)))))
    g5 = plateau_pos >= 7

    wide = by_variant["spread_x2"]
    w_is, w_val, w_oos = (summarise(rs(segment(wide, None, IS_END))),
                          summarise(rs(segment(wide, IS_END, VAL_END))),
                          summarise(rs(segment(wide, VAL_END))))
    g6 = (positive(w_is) and positive(w_val) and positive(w_oos)
          and w_oos["t"] is not None and w_oos["t"] > T_THRESHOLD)

    wins = [r for r in rs(oos) if r > 0]
    losses = [r for r in rs(oos) if r < 0]
    sharpe, sortino = daily_ratios(oos, VAL_END, datetime(2022, 3, 5, tzinfo=timezone.utc))
    return {
        "family": key,
        "segments": {"IS": s_is, "VAL": s_val, "OOS": s_oos},
        "gates": {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5, "G6": g6},
        "passed": all([g1, g2, g3, g4, g5, g6]),
        "detail": {
            "pairsPositiveShare": share_pos, "pairsEligible": len(eligible),
            "topPairShareOfR": top_share, "oosYears": {y: (len(v), (sum(v) / len(v)) if v else None) for y, v in years.items()},
            "positiveYears": positive_years, "plateauPositive": plateau_pos, "plateauTotal": len(plateau),
            "spreadX2_OOS": w_oos,
        },
        "reported": {
            "avgWinR": (sum(wins) / len(wins)) if wins else None,
            "avgLossR": (sum(losses) / len(losses)) if losses else None,
            "avgCostR": (sum(t["costR"] for t in oos) / len(oos)) if oos else None,
            "maxDrawdownR_all": max_drawdown(base),
            "sharpeOOS": sharpe, "sortinoOOS": sortino,
            "monteCarloOOS": monte_carlo(rs(oos)),
            "rolling12m": rolling_windows(base),
        },
    }


def fmt(s):
    if not s["n"]:
        return "n=    0"
    t = "  n/a" if s["t"] is None else f"{s['t']:+5.2f}"
    pf = " n/a" if s["pfR"] is None else f"{s['pfR']:.2f}"
    return f"n={s['n']:>6} win={s['win']*100:5.1f}% avgR={s['avgR']:+.3f} PF_R={pf} t={t}"


def main(argv):
    parser = argparse.ArgumentParser(prog="research_program")
    parser.add_argument("data")
    parser.add_argument("--families", default=",".join(FAMILIES))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    root = str(Path(args.data).resolve())
    keys = [k.strip() for k in args.families.split(",") if k.strip()]
    symbols = [s for s in args.symbols.split(",") if s] or sorted(
        p.name for p in Path(root).iterdir() if p.is_dir() and len(p.name) == 6 and p.name.isupper()
    )
    jobs = [(k, s, root) for k in keys if k != "F3" for s in symbols]

    collected = defaultdict(lambda: defaultdict(list))
    with Pool(args.workers) as pool:
        async_f3 = pool.apply_async(run_cross_sectional, (root,)) if "F3" in keys else None
        for key, symbol, out in pool.imap_unordered(run_single, jobs):
            for label, trades in out.items():
                collected[key][label] += trades
            print(f"  done {key} {symbol}", flush=True)
        if async_f3 is not None:
            key, _, out = async_f3.get()
            for label, trades in out.items():
                collected[key][label] += trades
            print("  done F3 (portfolio)", flush=True)

    print("\n" + "=" * 78)
    print("EIGHT-FAMILY EDGE PROGRAM — judged by docs/EXPERIMENT_EDGE_PROGRAM.md")
    print("=" * 78)
    report = {}
    for key in keys:
        result = judge(key, collected[key])
        report[key] = result
        g = result["gates"]
        print(f"\n{key} {FAMILIES[key][0]().name}  [{FAMILIES[key][1]}]")
        for seg in ("IS", "VAL", "OOS"):
            print(f"  {seg:<4}: {fmt(result['segments'][seg])}")
        d, r = result["detail"], result["reported"]
        mc = r["monteCarloOOS"] or {}
        print(f"  gates : " + "  ".join(f"{k}={'PASS' if v else 'fail'}" for k, v in g.items()))
        top = "n/a" if d["topPairShareOfR"] is None else f"{d['topPairShareOfR']:.0%}"
        print(f"  pairs+ {d['pairsPositiveShare']:.0%} of {d['pairsEligible']}, top pair share {top}, "
              f"years+ {d['positiveYears']}/4, plateau+ {d['plateauPositive']}/{d['plateauTotal']}")
        print(f"  cost/trade {r['avgCostR'] if r['avgCostR'] is None else round(r['avgCostR'], 3)}R  "
              f"avgWin {r['avgWinR'] and round(r['avgWinR'], 2)}R avgLoss {r['avgLossR'] and round(r['avgLossR'], 2)}R  "
              f"maxDD {r['maxDrawdownR_all']:.1f}R  Sharpe {r['sharpeOOS'] and round(r['sharpeOOS'], 2)}  "
              f"MC p5 {mc.get('p5') and round(mc['p5'], 3)}")
        print(f"  VERDICT: {'PASSED — proceeds to paper trading' if result['passed'] else 'FAILED'}")

    passed = [k for k in keys if report[k]["passed"]]
    print("\n" + "=" * 78)
    print(f"RESULT: {'passed: ' + ', '.join(passed) if passed else 'no family passed all six gates.'}")
    print("=" * 78)
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"report": report, "trades": {k: collected[k]["base"] for k in keys}}, default=str))
        print(f"full report -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
