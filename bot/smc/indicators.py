"""The small set of numeric primitives the SMC engine needs.

Deliberately minimal (MASTER_MISSION §69): ATR for volatility
normalisation and a couple of averages. No RSI/MACD/ADX — they would add
parameters without adding independent information to a structure-based
strategy.
"""

from __future__ import annotations

from typing import Sequence

from ..marketdata.candles import Candle


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            ranges.append(candle.range)
            continue
        previous_close = candles[index - 1].close
        ranges.append(
            max(
                candle.range,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return ranges


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Simple ATR over the last `period` true ranges.

    Returns 0.0 only for an empty series; callers treat a zero ATR as
    "cannot normalise" and stand aside rather than dividing by it.
    """

    if not candles:
        return 0.0
    ranges = true_ranges(candles)
    window = ranges[-period:] if len(ranges) >= period else ranges
    return sum(window) / len(window)


def atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Rolling ATR aligned to `candles`, using only past/current data.

    Index i uses true ranges up to and including i — never beyond, which
    is what keeps the backtester free of look-ahead.
    """

    ranges = true_ranges(candles)
    out: list[float] = []
    running = 0.0
    for index, value in enumerate(ranges):
        if index < period:
            running += value
            out.append(running / (index + 1))
        else:
            window = ranges[index - period + 1 : index + 1]
            out.append(sum(window) / period)
    return out


def average_body(candles: Sequence[Candle]) -> float:
    if not candles:
        return 0.0
    return sum(candle.body for candle in candles) / len(candles)


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[position]
