"""Run the pre-registered trend-following test and apply its verdict.

Everything this script decides was written down first, in
docs/EXPERIMENT_TREND_FOLLOWING.md, and committed before this file ran:
the three candidates, their canonical parameters, the costs (including a
swap assumption charged both ways), the split at 2017-01-01, and the pass
rule — held-out avgR > 0, held-out t > 2.39 (Bonferroni for three), and
design avgR > 0 — plus the clarification that a verdict which changes when
the spread is doubled counts as a FAIL.

Usage:
    python3 scripts/research_trend.py DATA_DIR [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.research.daily import CANDIDATES, costs_for, max_drawdown_r, simulate, summarise
from bot.research.data import load_bars

SPLIT = datetime(2017, 1, 1, tzinfo=timezone.utc)
T_THRESHOLD = 2.39
VARIANTS = {
    "base": dict(spread_multiple=1.0, swap=True),
    "spread_x2": dict(spread_multiple=2.0, swap=True),
    "no_swap": dict(spread_multiple=1.0, swap=False),
}


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=    0"
    t = "  n/a" if s["t"] is None else f"{s['t']:+5.2f}"
    pf = " n/a" if s["pfR"] is None else f"{s['pfR']:.2f}"
    return (f"n={s['n']:>5}  win={s['win']*100:5.1f}%  avgR={s['avgR']:+.3f}  "
            f"sumR={s['sumR']:+8.1f}  PF_R={pf}  t={t}")


def verdict(design: dict, held: dict) -> tuple[bool, list[str]]:
    c1 = held["avgR"] is not None and held["avgR"] > 0
    c2 = held["t"] is not None and held["t"] > T_THRESHOLD
    c3 = design["avgR"] is not None and design["avgR"] > 0
    return c1 and c2 and c3, [
        f"(1) held-out avgR > 0        : {'PASS' if c1 else 'FAIL'}",
        f"(2) held-out t > {T_THRESHOLD}        : {'PASS' if c2 else 'FAIL'}",
        f"(3) design avgR > 0          : {'PASS' if c3 else 'FAIL'}",
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="research_trend")
    parser.add_argument("data")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    root = Path(args.data)

    symbols = sorted(p.name for p in root.iterdir() if p.is_dir() and len(p.name) == 6 and p.name.isupper())
    data = {}
    print("=" * 78)
    print("PRE-REGISTERED TREND-FOLLOWING TEST — docs/EXPERIMENT_TREND_FOLLOWING.md")
    print("=" * 78)
    for symbol in symbols:
        bars, scale, digits = load_bars(root / symbol / f"{symbol}d1.csv", symbol=symbol, timeframe="D1")
        data[symbol] = bars
        print(f"  {symbol}: {len(bars)} D1 bars {bars[0].timestamp.date()}..{bars[-1].timestamp.date()} "
              f"scale/{scale:g} last close {bars[-1].close}")
    print("  Not this broker's prices. Swap is an ASSUMPTION (1% ATR/bar, both ways).")

    report: dict = {"candidates": {}}
    passed: dict[str, dict] = {}
    for rule in CANDIDATES:
        print("\n" + "-" * 78)
        print(f"{rule.name}")
        print("-" * 78)
        per_variant = {}
        for variant, knobs in VARIANTS.items():
            trades = []
            for symbol, bars in data.items():
                trades += simulate(bars, rule, costs_for(symbol).variant(**knobs), symbol=symbol)
            rs_all = [t.r for t in trades if t.r is not None]
            design = summarise([t.r for t in trades if t.r is not None and t.entry_time < SPLIT])
            held = summarise([t.r for t in trades if t.r is not None and t.entry_time >= SPLIT])
            ok, lines = verdict(design, held)
            per_variant[variant] = {
                "all": summarise(rs_all), "design": design, "held": held,
                "maxDrawdownR": max_drawdown_r(trades), "pass": ok, "conditions": lines,
                "trades": [t.as_dict() for t in trades] if variant == "base" else None,
            }
            label = {"base": "BASE (verdict)", "spread_x2": "spread x2 (robustness)",
                     "no_swap": "no swap (sensitivity only)"}[variant]
            print(f"\n  [{label}]")
            print(f"    all      : {fmt(summarise(rs_all))}   maxDD {max_drawdown_r(trades):+.1f}R")
            print(f"    design   : {fmt(design)}")
            print(f"    held-out : {fmt(held)}")
            for line in lines:
                print(f"      {line}")

        base_ok = per_variant["base"]["pass"]
        robust = base_ok == per_variant["spread_x2"]["pass"]
        final = base_ok and robust
        reason = ("PASSED" if final else
                  "FAILED — the verdict changes when the spread is doubled" if base_ok and not robust else
                  "FAILED")
        print(f"\n  VERDICT: {reason}")
        report["candidates"][rule.name] = {"variants": per_variant, "verdict": reason}
        if final:
            passed[rule.name] = per_variant["base"]["design"]

    print("\n" + "=" * 78)
    if not passed:
        print("RESULT: no candidate passed. As pre-registered, none is recommended, and")
        print("nothing further is added to the search in this round.")
        report["selected"] = None
    else:
        chosen = max(passed, key=lambda name: passed[name]["t"] or float("-inf"))
        print(f"RESULT: passed: {', '.join(passed)}. Selected by DESIGN-period t: {chosen}")
        report["selected"] = chosen
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str))
        print(f"full report -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
