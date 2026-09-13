"""Fair Value Gaps with full lifecycle tracking.

A gap is only interesting while it is unmitigated and recent, and only
tradeable when it was created by displacement. This tracks size, age,
partial fill, full mitigation, and invalidation, so the entry logic can
prefer a fresh, displacement-born, structure-aligned gap over a stale
three-candle artefact (MASTER_MISSION §19).

Mitigation is evaluated candle by candle going forward — never by
looking at the final candle and asking "did price ever come back", which
would be hindsight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .displacement import Displacement
from .indicators import atr_series


@dataclass(frozen=True, slots=True)
class FairValueGap:
    index: int            # index of the third candle (gap is knowable here)
    timestamp: datetime
    direction: str        # bullish | bearish
    lower: float
    upper: float
    size: float
    size_atr: float
    displaced: bool
    displacement_quality: float
    mitigated_index: int | None
    invalidated_index: int | None
    fill_fraction: float

    @property
    def midpoint(self) -> float:
        return (self.upper + self.lower) / 2.0

    @property
    def consequent_encroachment(self) -> float:
        """The 50% level — the standard entry reference inside a gap."""

        return self.midpoint

    def age(self, at_index: int) -> int:
        return at_index - self.index

    def is_live(self, at_index: int, max_age: int) -> bool:
        if self.index > at_index:
            return False
        if self.mitigated_index is not None and self.mitigated_index <= at_index:
            return False
        if self.invalidated_index is not None and self.invalidated_index <= at_index:
            return False
        return self.age(at_index) <= max_age

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
            "lower": self.lower,
            "upper": self.upper,
            "midpoint": self.midpoint,
            "sizeAtr": round(self.size_atr, 3),
            "displaced": self.displaced,
            "fillFraction": round(self.fill_fraction, 3),
            "mitigated": self.mitigated_index is not None,
            "invalidated": self.invalidated_index is not None,
        }


def detect_fair_value_gaps(
    candles: Sequence[Candle],
    displacements: Sequence[Displacement] = (),
    *,
    atr_period: int = 14,
    min_size_atr: float = 0.12,
) -> list[FairValueGap]:
    """Three-candle imbalances, graded and lifecycle-tracked."""

    if len(candles) < 3:
        return []
    atrs = atr_series(candles, atr_period)
    displacement_by_index = {d.index: d for d in displacements}
    gaps: list[FairValueGap] = []

    for index in range(2, len(candles)):
        left, middle, right = candles[index - 2], candles[index - 1], candles[index]
        current_atr = atrs[index]
        if current_atr <= 0:
            continue

        if right.low > left.high:
            direction, lower, upper = "bullish", left.high, right.low
        elif right.high < left.low:
            direction, lower, upper = "bearish", right.high, left.low
        else:
            continue

        size = upper - lower
        if size < current_atr * min_size_atr:
            continue

        # The middle candle is the impulse that created the gap.
        move = displacement_by_index.get(index - 1) or displacement_by_index.get(index)
        displaced = move is not None and move.direction == direction

        mitigated_index: int | None = None
        invalidated_index: int | None = None
        max_fill = 0.0
        for probe in range(index + 1, len(candles)):
            candle = candles[probe]
            if direction == "bullish":
                penetration = max(0.0, upper - candle.low)
                if candle.close < lower:
                    invalidated_index = probe
                    break
                if candle.low <= lower:
                    mitigated_index = probe
                    max_fill = 1.0
                    break
            else:
                penetration = max(0.0, candle.high - lower)
                if candle.close > upper:
                    invalidated_index = probe
                    break
                if candle.high >= upper:
                    mitigated_index = probe
                    max_fill = 1.0
                    break
            max_fill = max(max_fill, min(1.0, penetration / size if size else 0.0))

        gaps.append(
            FairValueGap(
                index=index,
                timestamp=right.timestamp,
                direction=direction,
                lower=lower,
                upper=upper,
                size=size,
                size_atr=size / current_atr,
                displaced=displaced,
                displacement_quality=move.quality if move else 0.0,
                mitigated_index=mitigated_index,
                invalidated_index=invalidated_index,
                fill_fraction=max_fill,
            )
        )
    return gaps


def best_entry_gap(
    gaps: Sequence[FairValueGap],
    *,
    direction: str,
    at_index: int,
    max_age: int,
    reference_index: int | None = None,
) -> FairValueGap | None:
    """Pick the gap a retracement entry should use.

    Preference order: created after the structural event, unmitigated,
    displacement-born, then largest relative to ATR, then freshest.
    """

    wanted = "bullish" if direction == "BUY" else "bearish"
    live = [
        gap
        for gap in gaps
        if gap.direction == wanted
        and gap.is_live(at_index, max_age)
        and (reference_index is None or gap.index >= reference_index - 2)
    ]
    if not live:
        return None
    return max(live, key=lambda gap: (gap.displaced, gap.size_atr, gap.index))
