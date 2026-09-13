"""Swing detection with explicit confirmation timing.

The subtle correctness requirement, and the bug in the previous engine:
a pivot at index i cannot be known until `window` candles AFTER it have
printed. Any detector that iterates forward in time and consults a swing
at index i while standing at index i+1 is reading the future.

Every SwingPoint here therefore carries `confirmed_index`, and every
downstream detector filters on `confirmed_index <= current_index`. That
single discipline is what makes the engine non-repainting and what makes
the backtester's results meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int            # candle that formed the pivot
    confirmed_index: int  # earliest candle at which it is knowable
    timestamp: datetime
    price: float
    kind: str             # "high" | "low"
    strength: int         # how many candles on each side it dominates

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "confirmedIndex": self.confirmed_index,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "kind": self.kind,
            "strength": self.strength,
        }


@dataclass(frozen=True, slots=True)
class SwingSet:
    highs: tuple[SwingPoint, ...]
    lows: tuple[SwingPoint, ...]

    def known_highs(self, at_index: int) -> list[SwingPoint]:
        return [point for point in self.highs if point.confirmed_index <= at_index]

    def known_lows(self, at_index: int) -> list[SwingPoint]:
        return [point for point in self.lows if point.confirmed_index <= at_index]


def detect_swings(candles: Sequence[Candle], window: int = 2) -> SwingSet:
    """Strict fractal pivots.

    A high qualifies when it strictly exceeds every high in the `window`
    candles on both sides. Strictness (rather than >=) avoids emitting a
    cluster of identical pivots across a flat range, which is the main
    source of swing noise.
    """

    if window < 1:
        raise ValueError("swing window must be at least 1")

    highs: list[SwingPoint] = []
    lows: list[SwingPoint] = []
    for index in range(window, len(candles) - window):
        candle = candles[index]
        left = candles[index - window : index]
        right = candles[index + 1 : index + window + 1]
        neighbours = list(left) + list(right)
        if all(candle.high > other.high for other in neighbours):
            highs.append(
                SwingPoint(index, index + window, candle.timestamp, candle.high, "high", window)
            )
        if all(candle.low < other.low for other in neighbours):
            lows.append(
                SwingPoint(index, index + window, candle.timestamp, candle.low, "low", window)
            )
    return SwingSet(tuple(highs), tuple(lows))


def classify_structure(swings: SwingSet, at_index: int) -> dict[str, Any]:
    """Internal vs external structure and the protected levels.

    * external: the most recent major swing that defines the dealing range;
    * internal: the most recent minor swing inside it;
    * protected high/low: the level whose break would invalidate the
      current directional read — the level a stop sits behind.
    """

    highs = swings.known_highs(at_index)
    lows = swings.known_lows(at_index)

    external_high = max(highs[-6:], key=lambda point: point.price) if highs else None
    external_low = min(lows[-6:], key=lambda point: point.price) if lows else None
    internal_high = highs[-1] if highs else None
    internal_low = lows[-1] if lows else None

    # The protected low in an uptrend is the low that produced the most
    # recent higher high; breaking it is what ends the uptrend.
    protected_low = None
    protected_high = None
    if internal_high is not None:
        prior_lows = [point for point in lows if point.index < internal_high.index]
        protected_low = prior_lows[-1] if prior_lows else None
    if internal_low is not None:
        prior_highs = [point for point in highs if point.index < internal_low.index]
        protected_high = prior_highs[-1] if prior_highs else None

    return {
        "externalHigh": external_high,
        "externalLow": external_low,
        "internalHigh": internal_high,
        "internalLow": internal_low,
        "protectedHigh": protected_high,
        "protectedLow": protected_low,
    }


def without_swept_points(swings: SwingSet, sweeps: Sequence[Any]) -> SwingSet:
    """Drop pivots that a confirmed sweep created and price immediately reclaimed.

    This is the single most important correction to a naive
    higher-high/lower-low reading. A stop run prints a textbook lower low:
    price spikes under an old low and closes straight back above it. Counted
    literally, that reads as "bearish structure" at the exact moment the
    setup is bullish — which is how a structure-following system ends up
    fading its own signal.

    A swept pivot is not structure; it is liquidity that has been removed.
    """

    swept_low_indices = {
        sweep.index for sweep in sweeps if getattr(sweep, "direction", None) == "bullish"
    }
    swept_high_indices = {
        sweep.index for sweep in sweeps if getattr(sweep, "direction", None) == "bearish"
    }
    highs = tuple(point for point in swings.highs if point.index not in swept_high_indices)
    lows = tuple(point for point in swings.lows if point.index not in swept_low_indices)
    return SwingSet(highs or swings.highs, lows or swings.lows)


def swing_bias(swings: SwingSet, at_index: int) -> str:
    """Read trend from confirmed swings only.

    Higher highs AND higher lows is the textbook uptrend, but a clean
    impulsive trend often prints no confirmed pivot HIGH at all — each new
    high is immediately exceeded, so no fractal ever completes. Requiring
    both sides would call such a market "range", which is exactly wrong
    and was why the higher timeframes read as directionless.

    So: one side establishing a direction is sufficient as long as the
    other side does not contradict it.
    """

    highs = swings.known_highs(at_index)
    lows = swings.known_lows(at_index)
    if len(highs) < 2 and len(lows) < 2:
        return "range"

    higher_highs = len(highs) >= 2 and highs[-1].price > highs[-2].price
    lower_highs = len(highs) >= 2 and highs[-1].price < highs[-2].price
    higher_lows = len(lows) >= 2 and lows[-1].price > lows[-2].price
    lower_lows = len(lows) >= 2 and lows[-1].price < lows[-2].price

    bullish = (higher_highs or higher_lows) and not lower_highs and not lower_lows
    bearish = (lower_highs or lower_lows) and not higher_highs and not higher_lows
    if bullish:
        return "bullish"
    if bearish:
        return "bearish"
    return "range"
