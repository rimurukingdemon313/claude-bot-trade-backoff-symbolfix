"""Break of Structure and Change of Character.

Rules that keep this from firing on noise (MASTER_MISSION §14/§15):

* a break is confirmed by a CLOSE beyond the level, never a wick;
* the close must clear the level by an ATR-scaled buffer, so a
  one-tick poke is not a break;
* the broken swing must itself be CONFIRMED at or before the breaking
  candle — this is the look-ahead guard;
* each level breaks once; re-crossing the same old level is not a new
  event;
* BOS continues the prevailing direction, CHoCH is the first break
  against it. The prevailing direction is tracked forward through the
  series, not recomputed from the end (which would be hindsight).
* displacement on the breaking candle is recorded, so the scorer can
  distinguish a decisive break from a drift-through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .displacement import Displacement, displacement_at
from .indicators import atr_series
from .swings import SwingSet, swing_bias, without_swept_points


@dataclass(frozen=True, slots=True)
class StructureEvent:
    index: int
    timestamp: datetime
    direction: str       # bullish | bearish
    event_type: str      # BOS | CHoCH
    level: float
    level_index: int
    close: float
    displaced: bool
    displacement_quality: float
    clearance_atr: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
            "type": self.event_type,
            "level": self.level,
            "close": self.close,
            "displaced": self.displaced,
            "displacementQuality": round(self.displacement_quality, 3),
            "clearanceAtr": round(self.clearance_atr, 3),
        }


def detect_structure_events(
    candles: Sequence[Candle],
    swings: SwingSet,
    displacements: Sequence[Displacement],
    *,
    atr_period: int = 14,
    buffer_atr: float = 0.08,
) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    atrs = atr_series(candles, atr_period)
    broken: set[tuple[str, int]] = set()
    trend: str | None = None

    for index, candle in enumerate(candles):
        current_atr = atrs[index] if atrs[index] > 0 else 0.0
        if current_atr <= 0:
            continue
        buffer = current_atr * buffer_atr

        # Only swings already CONFIRMED by this candle are visible.
        highs = [p for p in swings.known_highs(index) if p.index < index and ("high", p.index) not in broken]
        lows = [p for p in swings.known_lows(index) if p.index < index and ("low", p.index) not in broken]

        if trend is None:
            inferred = swing_bias(swings, index)
            if inferred != "range":
                trend = inferred

        event: StructureEvent | None = None

        target_high = highs[-1] if highs else None
        target_low = lows[-1] if lows else None

        if target_high is not None and candle.close > target_high.price + buffer:
            direction = "bullish"
            event_type = "CHoCH" if trend == "bearish" else "BOS"
            move = displacement_at(displacements, index, tolerance=1)
            event = StructureEvent(
                index=index,
                timestamp=candle.timestamp,
                direction=direction,
                event_type=event_type,
                level=target_high.price,
                level_index=target_high.index,
                close=candle.close,
                displaced=move is not None,
                displacement_quality=move.quality if move else 0.0,
                clearance_atr=(candle.close - target_high.price) / current_atr,
            )
            broken.add(("high", target_high.index))
        elif target_low is not None and candle.close < target_low.price - buffer:
            direction = "bearish"
            event_type = "CHoCH" if trend == "bullish" else "BOS"
            move = displacement_at(displacements, index, tolerance=1)
            event = StructureEvent(
                index=index,
                timestamp=candle.timestamp,
                direction=direction,
                event_type=event_type,
                level=target_low.price,
                level_index=target_low.index,
                close=candle.close,
                displaced=move is not None,
                displacement_quality=move.quality if move else 0.0,
                clearance_atr=(target_low.price - candle.close) / current_atr,
            )
            broken.add(("low", target_low.index))

        if event is not None:
            events.append(event)
            trend = event.direction

    return events


def structural_bias(
    events: Sequence[StructureEvent],
    swings: SwingSet,
    at_index: int,
    sweeps: Sequence[Any] = (),
) -> tuple[str, str]:
    """Combine the last confirmed break with the swing sequence.

    Returns (bias, rationale). A recent CHoCH outranks the swing pattern
    because it is the earlier, more specific evidence of a turn.
    """

    visible = [event for event in events if event.index <= at_index]
    # Read the swing sequence with swept pivots removed: a stop run is
    # liquidity being taken, not a structural lower low.
    sequence_bias = swing_bias(without_swept_points(swings, sweeps), at_index)

    if visible:
        latest = visible[-1]
        age = at_index - latest.index
        if age <= 30:
            if latest.event_type == "CHoCH":
                return latest.direction, f"recent {latest.direction} CHoCH {age} candles ago"
            if sequence_bias in (latest.direction, "range"):
                return latest.direction, f"{latest.direction} BOS confirmed {age} candles ago"
            return sequence_bias, (
                f"swing sequence is {sequence_bias} while the last break was {latest.direction} — conflicted"
            )
    if sequence_bias != "range":
        return sequence_bias, f"swing sequence is {sequence_bias}"
    return "range", "no confirmed directional structure"
