"""Run multi-timeframe SMC analysis (H4 bias, H1 confirmation, M15 entry)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis_engine import Candle, SMCAnalyzer
from market_data import SUPPORTED_SYMBOLS, fetch_live_candles


ANALYSIS_TIMEFRAMES = ("H4", "H1", "M15")


def _run_single_timeframe(symbol: str, timeframe: str, *, fixture: bool) -> tuple[dict, list[dict]]:
    if fixture:
        fixture_path = Path(__file__).parent / "analysis_engine" / "fixtures" / "m15_sample.json"
        rows = json.loads(fixture_path.read_text())
        data_source = "Synthetic verification fixture"
        live = False
    else:
        rows = fetch_live_candles(symbol, timeframe)
        data_source = "Yahoo Finance live chart data"
        live = True

    analysis = SMCAnalyzer(timeframe=timeframe).analyze(Candle.from_mapping(row) for row in rows)
    output = analysis.to_dict()
    output.update(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "data_source": data_source,
            "live": live,
            "latest_price": output["latest_candle"]["close"],
        }
    )
    return output, rows


def _combine_timeframes(per_timeframe: dict[str, dict]) -> dict:
    """Blend H4/H1/M15 into one payload: H4 sets bias, M15 stays the entry chart.

    The M15 result is used as the base (it has the finest order blocks / FVGs
    for entry timing) and gets an added `multiTimeframe` section summarizing
    the higher timeframes so the AI decision layer can require alignment
    before trading, instead of reacting to M15 noise alone.
    """

    m15 = per_timeframe["M15"]
    h1 = per_timeframe["H1"]
    h4 = per_timeframe["H4"]

    def direction_of(entry: dict) -> str | None:
        context = entry.get("overall_context") or {}
        return context.get("direction")

    def score_of(entry: dict) -> float:
        context = entry.get("overall_context") or {}
        return context.get("score", 0)

    h4_dir = direction_of(h4)
    h1_dir = direction_of(h1)
    m15_dir = direction_of(m15)

    aligned = bool(h4_dir) and h4_dir == h1_dir == m15_dir
    combined = dict(m15)
    combined["multiTimeframe"] = {
        "h4": {"direction": h4_dir, "score": score_of(h4), "latestPrice": h4.get("latest_price")},
        "h1": {"direction": h1_dir, "score": score_of(h1), "latestPrice": h1.get("latest_price")},
        "m15": {"direction": m15_dir, "score": score_of(m15), "latestPrice": m15.get("latest_price")},
        "aligned": aligned,
        "bias": h4_dir,
    }
    return combined


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="use deterministic verification candles instead of live data",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly request live data (the default)",
    )
    parser.add_argument(
        "--symbol",
        default="EURUSD",
        help=f"symbol to analyze; one of {', '.join(sorted(SUPPORTED_SYMBOLS))}",
    )
    args = parser.parse_args()
    symbol = args.symbol.upper()

    per_timeframe: dict[str, dict] = {}
    m15_candles: list[dict] = []
    for timeframe in ANALYSIS_TIMEFRAMES:
        analysis, rows = _run_single_timeframe(symbol, timeframe, fixture=args.fixture)
        per_timeframe[timeframe] = analysis
        if timeframe == "M15":
            m15_candles = rows

    combined = _combine_timeframes(per_timeframe)
    combined["candles"] = m15_candles
    print(json.dumps(combined, indent=2))


if __name__ == "__main__":
    main()
