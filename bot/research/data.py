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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..marketdata.candles import Candle

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")

#: The files are in MetaTrader SERVER time, not UTC.
#:
#: Measured, not assumed: in 481 of 485 weekends the first bar is Monday
#: 00:00 and the last is Friday 23:45. The FX week opens Sunday 21:00/22:00
#: UTC, so midnight here is the New York close — the common broker clock,
#: New York time plus seven hours, which follows US daylight saving:
#: UTC+2 in winter, UTC+3 in summer.
#:
#: Reading these timestamps as UTC — which the first version of this loader
#: did — shifts every intraday bar two to three hours. Nothing breaks; the
#: session filters, the rollover blackout and the weekend gate simply run
#: on the wrong hours, and a backtest measures a strategy trading sessions
#: it was never meant to trade.
_NEW_YORK = ZoneInfo("America/New_York")
SERVER_OFFSET_FROM_NEW_YORK = timedelta(hours=7)


def server_to_utc(naive: datetime) -> datetime:
    """A server-clock timestamp as an aware UTC datetime."""

    local = (naive - SERVER_OFFSET_FROM_NEW_YORK).replace(tzinfo=_NEW_YORK)
    return local.astimezone(timezone.utc)


def trading_date(moment: datetime) -> date:
    """The trading day a UTC instant belongs to: days end at the New York
    close. A daily bar's calendar logic (month boundaries) uses this, not
    the UTC date of its open, which falls on the previous evening."""

    return (moment.astimezone(_NEW_YORK) + SERVER_OFFSET_FROM_NEW_YORK).date()


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
            return server_to_utc(datetime.strptime(stamp, fmt))
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
