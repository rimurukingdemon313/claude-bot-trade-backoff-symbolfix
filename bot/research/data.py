"""Loading the public OHLC dataset used for offline research.

The files store prices as points — 109305.0 for EURUSD's 1.09305 — so the
divisor has to be derived. Getting it wrong raises nothing: every level
stays consistent with every other level and only the volatility-relative
logic silently compares against the wrong magnitude. Two such bugs have
already produced complete, normal-looking runs that measured nothing. So:

* the plausible price band is per instrument class and narrower than a
  power of ten, so at most one divisor can fit it;
* more than one fit, or none, is an error — never a default;
* the chosen scale is returned so callers print it.

This is the single copy of that logic. `scripts/backtest_offline.py`
imports it rather than keeping its own.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from ..marketdata.candles import Candle

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def plausible_band(symbol: str) -> tuple[float, float]:
    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return 200.0, 5000.0
    if symbol.startswith("XAG"):
        return 5.0, 100.0
    if symbol.endswith("JPY"):
        return 40.0, 400.0
    # Every non-JPY major and cross of the last two decades sits between
    # ~0.5 and ~2.1. [0.3, 10] admitted two divisors for anything under
    # 1.0 and ran AUDUSD at 7.37 for a full backtest.
    return 0.4, 3.0


def scale_for(symbol: str, raw_median: float) -> float:
    low, high = plausible_band(symbol)
    fits = [
        10.0**exponent
        for exponent in range(0, 9)
        if low <= raw_median / 10.0**exponent <= high
    ]
    if len(fits) == 1:
        return fits[0]
    if len(fits) > 1:
        raise ValueError(
            f"{symbol}: a median raw price of {raw_median:g} fits [{low}, {high}] at "
            f"{len(fits)} scales ({', '.join(f'/{d:g}' for d in fits)}). Refusing to pick one."
        )
    raise ValueError(
        f"{symbol}: a median raw price of {raw_median:g} fits no scale inside "
        f"[{low}, {high}]. Refusing to guess — a wrong scale measures nothing."
    )


def digits_for(symbol: str) -> int:
    symbol = symbol.upper()
    if symbol.endswith("JPY"):
        return 3
    if symbol.startswith("XAU"):
        return 2
    return 5


def _parse(stamp: str) -> datetime:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp {stamp!r}")


def load_bars(path: Path, *, symbol: str, timeframe: str) -> tuple[list[Candle], float, int]:
    """Candles in real prices, oldest first, plus (scale, digits).

    Malformed rows are dropped, never interpolated (rule 6). Daily files
    carry a bare date and intraday files a timestamp; both are accepted.
    """

    rows: list[tuple[datetime, float, float, float, float, float]] = []
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append(
                    (
                        _parse(row["Date"]),
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("tick_volume") or 0.0),
                    )
                )
            except (KeyError, ValueError):
                continue
    if not rows:
        raise ValueError(f"{path}: no usable rows")

    closes = sorted(item[4] for item in rows)
    scale = scale_for(symbol, closes[len(closes) // 2])
    candles = [
        Candle(stamp, o / scale, h / scale, lo / scale, c / scale, v, timeframe)
        for stamp, o, h, lo, c, v in rows
    ]
    candles.sort(key=lambda candle: candle.timestamp)
    return candles, scale, digits_for(symbol)
