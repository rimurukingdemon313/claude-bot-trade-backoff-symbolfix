"""Market regime classification.

The same setup means different things in a trending expansion and in a
dead compression. The regime is fed to the scorer as a multiplier-style
input (MASTER_MISSION §23), and an extreme regime can veto outright:
trading a structure break inside a violent news expansion is how a
stop-loss becomes a slippage event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .indicators import atr_series, percentile
from .structure import StructureEvent


@dataclass(frozen=True, slots=True)
class Regime:
    trend: str        # trending | ranging | transitional
    volatility: str   # compressed | normal | expanded | extreme
    atr: float
    atr_percentile: float
    directional_strength: float  # 0..1
    tradeable: bool
    note: str

    def quality(self) -> float:
        """0..1 suitability for a structure-following strategy."""

        base = {"trending": 1.0, "transitional": 0.7, "ranging": 0.45}[self.trend]
        volatility_factor = {
            "compressed": 0.55,
            "normal": 1.0,
            "expanded": 0.85,
            "extreme": 0.2,
        }[self.volatility]
        return round(base * volatility_factor, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trend": self.trend,
            "volatility": self.volatility,
            "atr": self.atr,
            "atrPercentile": round(self.atr_percentile, 3),
            "directionalStrength": round(self.directional_strength, 3),
            "tradeable": self.tradeable,
            "quality": self.quality(),
            "note": self.note,
        }


def classify_regime(
    candles: Sequence[Candle],
    structure_events: Sequence[StructureEvent] = (),
    *,
    atr_period: int = 14,
    lookback: int = 60,
) -> Regime:
    if len(candles) < 10:
        return Regime("ranging", "normal", 0.0, 0.5, 0.0, False, "insufficient history")

    window = candles[-lookback:]
    atrs = atr_series(candles, atr_period)
    current_atr = atrs[-1]
    history = atrs[-lookback:]
    low_band = percentile(history, 0.25)
    high_band = percentile(history, 0.75)
    extreme_band = percentile(history, 0.95)

    if current_atr <= 0:
        volatility = "normal"
        atr_percentile = 0.5
    else:
        ranked = sorted(history)
        position = sum(1 for value in ranked if value <= current_atr) / len(ranked)
        atr_percentile = position
        if current_atr >= max(extreme_band * 1.6, high_band * 2.2):
            volatility = "extreme"
        elif current_atr >= high_band * 1.05:
            # The 5% margin matters: in a series with near-uniform ranges
            # the upper quartile equals the current value, and a bare >=
            # would label a dead-flat market "expanded".
            volatility = "expanded"
        elif current_atr <= low_band * 0.95:
            volatility = "compressed"
        else:
            volatility = "normal"

    # Directional strength: net displacement vs total path travelled.
    # A trend covers ground; a range travels the same distance and ends
    # where it started.
    net = abs(window[-1].close - window[0].open)
    path = sum(candle.range for candle in window) or 1.0
    directional_strength = max(0.0, min(1.0, (net / path) * 6.0))

    recent_breaks = [event for event in structure_events if event.index >= len(candles) - lookback]
    same_direction = 0
    if recent_breaks:
        last_direction = recent_breaks[-1].direction
        same_direction = sum(1 for event in recent_breaks if event.direction == last_direction)
    consistency = (same_direction / len(recent_breaks)) if recent_breaks else 0.0

    if directional_strength >= 0.45 and consistency >= 0.6:
        trend = "trending"
    elif directional_strength <= 0.2:
        trend = "ranging"
    else:
        trend = "transitional"

    tradeable = volatility != "extreme"
    note = f"{trend} market in {volatility} volatility"
    if not tradeable:
        note += " — standing aside until volatility normalises"

    return Regime(
        trend=trend,
        volatility=volatility,
        atr=current_atr,
        atr_percentile=atr_percentile,
        directional_strength=directional_strength,
        tradeable=tradeable,
        note=note,
    )
