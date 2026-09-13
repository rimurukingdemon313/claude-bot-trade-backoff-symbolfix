"""Displacement: energetic, directional, gap-creating movement.

"A large candle" is not displacement. This requires four things at once
(MASTER_MISSION §18):

  1. body large relative to ATR (not relative to the last 5 bodies, which
     collapses during a squeeze and fires on noise);
  2. body dominating its own range — a big candle that closes mid-range
     is a rejection, not displacement;
  3. directional continuity — the move is in the same direction as the
     net movement across the impulse window;
  4. an imbalance left behind, or an impulse leg of several candles.

Scoring is continuous so the setup scorer can reward strong displacement
rather than treating a threshold crossing as binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .indicators import atr_series


@dataclass(frozen=True, slots=True)
class Displacement:
    index: int
    timestamp: datetime
    direction: str
    body: float
    atr: float
    atr_multiple: float
    body_ratio: float
    leaves_gap: bool
    quality: float  # 0..1

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
            "atrMultiple": round(self.atr_multiple, 3),
            "bodyRatio": round(self.body_ratio, 3),
            "leavesGap": self.leaves_gap,
            "quality": round(self.quality, 3),
        }


def _creates_gap(candles: Sequence[Candle], index: int, direction: str) -> bool:
    """Did this candle's impulse leave a three-candle imbalance?"""

    if index < 1 or index + 1 >= len(candles):
        # At the series edge, check the two-candle form (prev high vs
        # current low) which is knowable without the next candle.
        if index >= 1:
            previous = candles[index - 1]
            current = candles[index]
            if direction == "bullish":
                return current.low > previous.high
            return current.high < previous.low
        return False
    left = candles[index - 1]
    right = candles[index + 1]
    if direction == "bullish":
        return right.low > left.high
    return right.high < left.low


def detect_displacement(
    candles: Sequence[Candle],
    *,
    atr_period: int = 14,
    atr_multiple: float = 1.3,
    body_ratio: float = 0.55,
    impulse_window: int = 3,
) -> list[Displacement]:
    """Scan for displacement candles. Uses only data up to each index."""

    if not candles:
        return []
    atrs = atr_series(candles, atr_period)
    result: list[Displacement] = []

    for index, candle in enumerate(candles):
        if index < atr_period // 2:
            continue
        current_atr = atrs[index]
        if current_atr <= 0:
            continue
        multiple = candle.body / current_atr
        candle_range = candle.range or current_atr
        ratio = candle.body / candle_range
        if multiple < atr_multiple or ratio < body_ratio:
            continue
        if candle.direction == "neutral":
            continue

        window = candles[max(0, index - impulse_window + 1) : index + 1]
        net_move = window[-1].close - window[0].open
        if candle.direction == "bullish" and net_move <= 0:
            continue
        if candle.direction == "bearish" and net_move >= 0:
            continue

        leaves_gap = _creates_gap(candles, index, candle.direction)
        # Quality blends the three continuous measures; the gap is a
        # bonus rather than a requirement because a strong impulse leg
        # without a clean FVG is still displacement.
        quality = min(
            1.0,
            0.45 * min(multiple / (atr_multiple * 2.0), 1.0)
            + 0.35 * min((ratio - body_ratio) / (1.0 - body_ratio), 1.0)
            + (0.20 if leaves_gap else 0.05),
        )
        result.append(
            Displacement(
                index=index,
                timestamp=candle.timestamp,
                direction=candle.direction,
                body=candle.body,
                atr=current_atr,
                atr_multiple=multiple,
                body_ratio=ratio,
                leaves_gap=leaves_gap,
                quality=quality,
            )
        )
    return result


def displacement_at(displacements: Sequence[Displacement], index: int, tolerance: int = 3) -> Displacement | None:
    """Most recent displacement within `tolerance` candles of `index`."""

    candidates = [d for d in displacements if index - tolerance <= d.index <= index]
    return max(candidates, key=lambda d: d.index) if candidates else None
