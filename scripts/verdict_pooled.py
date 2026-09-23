"""Apply a pre-registered pooled pass rule to offline-backtest trade records.

The rule every experiment here shares: pooled over all symbols,
held-out avgR > 0, held-out t > THRESHOLD, and design avgR > 0. The split is
2017-01-01. THRESHOLD is whatever the pre-registration fixed (2.0 for a
single test, higher under a multiple-comparison correction) and is passed
explicitly so this script cannot quietly pick one.

Usage:
    python3 scripts/verdict_pooled.py DIR --t 2.0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.research.daily import summarise

SPLIT = datetime(2017, 1, 1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="verdict_pooled")
    parser.add_argument("dir")
    parser.add_argument("--t", type=float, required=True, help="pre-registered t threshold")
    args = parser.parse_args(argv)

    rows = []
    symbols = []
    for path in sorted(Path(args.dir).glob("*.json")):
        entry = json.loads(path.read_text())[0]
        records = entry.get("tradeRecords") or []
        symbols.append((entry["name"], len(records)))
        for trade in records:
            if trade.get("rMultiple") is None:
                continue
            when = datetime.fromisoformat(trade["entryTime"]).replace(tzinfo=None)
            rows.append((when, float(trade["rMultiple"])))

    def show(label, s):
        if not s["n"]:
            print(f"  {label:<9}: n=0")
            return
        t = "n/a" if s["t"] is None else f"{s['t']:+.2f}"
        print(f"  {label:<9}: n={s['n']:>6}  win={s['win']*100:5.1f}%  avgR={s['avgR']:+.4f}  "
              f"sumR={s['sumR']:+9.1f}  t={t}")

    every = summarise([r for _, r in rows])
    design = summarise([r for w, r in rows if w < SPLIT])
    held = summarise([r for w, r in rows if w >= SPLIT])
    print(f"symbols: {', '.join(f'{n}({k})' for n, k in symbols)}")
    show("all", every)
    show("design", design)
    show("held-out", held)
    c1 = held["avgR"] is not None and held["avgR"] > 0
    c2 = held["t"] is not None and held["t"] > args.t
    c3 = design["avgR"] is not None and design["avgR"] > 0
    print(f"  (1) held-out avgR > 0   : {'PASS' if c1 else 'FAIL'}")
    print(f"  (2) held-out t > {args.t:<5}  : {'PASS' if c2 else 'FAIL'}")
    print(f"  (3) design avgR > 0     : {'PASS' if c3 else 'FAIL'}")
    print(f"  VERDICT: {'PASSED' if c1 and c2 and c3 else 'FAILED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
