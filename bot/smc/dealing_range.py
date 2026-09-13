"""Dealing range: premium / equilibrium / discount.

The range is anchored on the most recent CONFIRMED external swing high
and low, so it moves with structure instead of being a fixed window.

Per MASTER_MISSION §21 this is a preference, not a veto: a top-tier
structural setup at equilibrium is not thrown away because a simplistic
50% rule disliked it. The scorer expresses that as a graded contribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .swings import SwingSet


@dataclass(frozen=True, slots=True)
class DealingRange:
    high: float
    low: float
    price: float

    @property
    def size(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def equilibrium(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def position(self) -> float:
        """Where price sits in the range: 0.0 = low, 1.0 = high."""

        if self.size <= 0:
            return 0.5
        return max(0.0, min(1.0, (self.price - self.low) / self.size))

    @property
    def zone(self) -> str:
        position = self.position
        if position >= 0.62:
            return "premium"
        if position <= 0.38:
            return "discount"
        return "equilibrium"

    def alignment(self, direction: str) -> float:
        """0..1 score for trading `direction` from the current location.

        A buy scores 1.0 at the extreme of discount and decays toward 0 in
        deep premium; it never becomes a hard rejection.
        """

        position = self.position
        if direction == "BUY":
            return max(0.0, min(1.0, 1.0 - position / 0.85))
        return max(0.0, min(1.0, (position - 0.15) / 0.85))

    def as_dict(self) -> dict[str, Any]:
        return {
            "high": self.high,
            "low": self.low,
            "equilibrium": self.equilibrium,
            "position": round(self.position, 3),
            "zone": self.zone,
        }


def dealing_range(swings: SwingSet, at_index: int, price: float) -> DealingRange | None:
    """Build the range from confirmed swings visible at `at_index`."""

    highs = swings.known_highs(at_index)
    lows = swings.known_lows(at_index)
    if not highs or not lows:
        return None
    recent_high = max(highs[-5:], key=lambda point: point.price)
    recent_low = min(lows[-5:], key=lambda point: point.price)
    if recent_high.price <= recent_low.price:
        return None
    return DealingRange(high=recent_high.price, low=recent_low.price, price=price)
