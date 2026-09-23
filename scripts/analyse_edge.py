"""Analyse offline-backtest trade records against a pre-registration.

Reads the per-trade dumps written by `scripts/backtest_offline.py` and
applies `docs/EXPERIMENT_HISTORICAL_EDGE.md` exactly as written:

* H1 (grade A or better only) is judged on the HELD-OUT period alone,
  pooled across symbols, by the three conditions fixed in advance.
* Exploratory breakdowns are computed on the DESIGN period ONLY. This
  script has no option to print them for the held-out period, on
  purpose: looking at held-out buckets before writing a hypothesis about
  them is how a held-out set stops being held out.

Usage:
    python3 scripts/analyse_edge.py DIR [DIR ...]
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config
from bot.smc.sessions import classify_session

SPLIT = datetime(2017, 1, 1)
A_OR_BETTER = {"A+", "A"}


def load_trades(dirs: list[Path]) -> tuple[list[dict], dict[str, str]]:
    """Every trade from every dump, one source per symbol.

    If a symbol appears in more than one directory, the first directory
    given that holds trade records wins — and the others are reported, so
    a silent double count is impossible.
    """

    trades: list[dict] = []
    source: dict[str, str] = {}
    for directory in dirs:
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for entry in payload if isinstance(payload, list) else [payload]:
                symbol = entry.get("name")
                records = entry.get("tradeRecords")
                if not symbol or not records:
                    continue
                if symbol in source:
                    continue
                source[symbol] = str(path)
                trades.extend(records)
    return trades, source


def stats(rs: list[float]) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "win": None, "avgR": None, "sumR": None, "t": None}
    mean = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    t = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        if var > 0:
            t = mean / (math.sqrt(var) / math.sqrt(n))
    return {"n": n, "win": wins / n, "avgR": mean, "sumR": sum(rs), "t": t}


def fmt(s: dict) -> str:
    if s["n"] == 0:
        return "   n=0"
    t = "  n/a" if s["t"] is None else f"{s['t']:+5.2f}"
    flag = "" if s["t"] is None or abs(s["t"]) < 2 else "  *"
    return (f"n={s['n']:>5}  win={s['win']*100:5.1f}%  avgR={s['avgR']:+.3f}  "
            f"sumR={s['sumR']:+8.1f}  t={t}{flag}")


def entry_time(trade: dict) -> datetime:
    return datetime.fromisoformat(trade["entryTime"]).replace(tzinfo=None)


def session_of(trade: dict, config) -> str:
    return classify_session(datetime.fromisoformat(trade["entryTime"]), config.sessions).name


def breakdown(title: str, trades: list[dict], key) -> None:
    buckets: dict[str, list[float]] = {}
    for trade in trades:
        if trade.get("rMultiple") is None:
            continue
        buckets.setdefault(str(key(trade)), []).append(float(trade["rMultiple"]))
    print(f"\n  {title}")
    for name, rs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"    {name:<22} {fmt(stats(rs))}")


def main(argv: list[str]) -> int:
    dirs = [Path(arg) for arg in argv] or [Path(".")]
    trades, source = load_trades(dirs)
    config = load_config()
    if not trades:
        print("No trade records found. Nothing to analyse — and nothing is claimed.")
        return 1

    trades = [t for t in trades if t.get("rMultiple") is not None]
    design = [t for t in trades if entry_time(t) < SPLIT]
    held = [t for t in trades if entry_time(t) >= SPLIT]

    print("=" * 78)
    print("HISTORICAL EDGE — judged against docs/EXPERIMENT_HISTORICAL_EDGE.md")
    print("=" * 78)
    print(f"symbols    : {', '.join(sorted(source))}")
    print(f"all trades : {fmt(stats([float(t['rMultiple']) for t in trades]))}")
    print(f"design     : {fmt(stats([float(t['rMultiple']) for t in design]))}   (< {SPLIT.date()})")
    print(f"held-out   : {fmt(stats([float(t['rMultiple']) for t in held]))}   (>= {SPLIT.date()})")
    print("  * marks |t| >= 2. With many buckets, some will be marked by chance alone.")

    # ---- H1, on held-out only, exactly as pre-registered ----
    a_held = [float(t["rMultiple"]) for t in held if t.get("setupGrade") in A_OR_BETTER]
    b_held = [float(t["rMultiple"]) for t in held if t.get("setupGrade") == "B"]
    sa, sb = stats(a_held), stats(b_held)
    print("\n" + "-" * 78)
    print("H1 — grade A or better only  (HELD-OUT period, pooled)")
    print("-" * 78)
    print(f"  A or better : {fmt(sa)}")
    print(f"  B           : {fmt(sb)}")
    c1 = sa["avgR"] is not None and sa["avgR"] > 0
    c2 = sa["t"] is not None and sa["t"] > 2.0
    c3 = sa["avgR"] is not None and sb["avgR"] is not None and sa["avgR"] > sb["avgR"]
    print(f"  (1) A avgR > 0      : {'PASS' if c1 else 'FAIL'}")
    print(f"  (2) A t > 2.0       : {'PASS' if c2 else 'FAIL'}")
    print(f"  (3) A avgR > B avgR : {'PASS' if c3 else 'FAIL'}")
    if c1 and c2 and c3:
        verdict = "PASSED — SCORING_MIN_TIER=A is supported on held-out data."
    elif not c1:
        verdict = "REJECTED — grade A does not make the strategy profitable out of sample."
    else:
        verdict = "INCONCLUSIVE — not enough evidence to recommend SCORING_MIN_TIER=A."
    print(f"  VERDICT: {verdict}")

    # ---- exploratory, design period ONLY ----
    print("\n" + "-" * 78)
    print("EXPLORATORY — DESIGN PERIOD ONLY. Hypotheses, not findings.")
    print("-" * 78)
    breakdown("by grade", design, lambda t: t.get("setupGrade"))
    breakdown("by setup type", design, lambda t: t.get("setupType") or "?")
    breakdown("by session", design, lambda t: session_of(t, config))
    breakdown("by direction", design, lambda t: t.get("direction"))
    breakdown("by exit reason", design, lambda t: t.get("exitReason"))
    breakdown("by target kind", design, lambda t: "projected" if t.get("projectedTarget") else "structural")
    breakdown("by symbol", design, lambda t: t.get("symbol"))
    print("\n  Held-out breakdowns are deliberately not printed. Any bucket above that")
    print("  looks promising must be written down as its own hypothesis and tested on")
    print("  the held-out period before it can be recommended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
