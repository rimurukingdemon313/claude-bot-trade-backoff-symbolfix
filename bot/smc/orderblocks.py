"""Order blocks — the origin of a displacement leg, not "any red candle".

Qualification (MASTER_MISSION §20):
  * the candle is the last opposing-direction candle before a displacement
    leg (or the last down-close before an up impulse);
  * the impulse that followed must have actually broken structure or left
    an imbalance — otherwise it is just a pullback candle;
  * the block is tracked for mitigation (price traded back into it) and
    invalidation (price closed decisively through it).

Strength blends displacement quality, whether the leg broke structure,
and whether the block sits on an imbalance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .displacement import Displacement
from .fvg import FairValueGap
from .indicators import atr_series
from .structure import StructureEvent


@dataclass(frozen=True, slots=True)
class OrderBlock:
    index: int
    confirmed_index: int
    timestamp: datetime
    direction: str        # bullish (demand) | bearish (supply)
    lower: float
    upper: float
    displacement_index: int
    strength: float       # 0..1
    broke_structure: bool
    has_imbalance: bool
    mitigated_index: int | None
    invalidated_index: int | None

    @property
    def midpoint(self) -> float:
        return (self.upper + self.lower) / 2.0

    def is_live(self, at_index: int, max_age: int) -> bool:
        if self.confirmed_index > at_index:
            return False
        if self.invalidated_index is not None and self.invalidated_index <= at_index:
            return False
        if self.mitigated_index is not None and self.mitigated_index <= at_index:
            return False
        return (at_index - self.index) <= max_age

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
            "lower": self.lower,
            "upper": self.upper,
            "midpoint": self.midpoint,
            "strength": round(self.strength, 3),
            "brokeStructure": self.broke_structure,
            "hasImbalance": self.has_imbalance,
            "mitigated": self.mitigated_index is not None,
            "invalidated": self.invalidated_index is not None,
        }


def detect_order_blocks(
    candles: Sequence[Candle],
    displacements: Sequence[Displacement],
    structure_events: Sequence[StructureEvent] = (),
    gaps: Sequence[FairValueGap] = (),
    *,
    lookback: int = 6,
    atr_period: int = 14,
) -> list[OrderBlock]:
    if not candles or not displacements:
        return []
    atrs = atr_series(candles, atr_period)
    blocks: list[OrderBlock] = []
    seen: set[tuple[str, int]] = set()

    for move in displacements:
        wanted_candle = "bearish" if move.direction == "bullish" else "bullish"
        origin_index: int | None = None
        for probe in range(move.index - 1, max(-1, move.index - lookback - 1), -1):
            if candles[probe].direction == wanted_candle:
                origin_index = probe
                break
        if origin_index is None:
            continue
        key = (move.direction, origin_index)
        if key in seen:
            continue
        seen.add(key)

        origin = candles[origin_index]
        current_atr = atrs[move.index] or 1.0

        broke_structure = any(
            move.index <= event.index <= move.index + 3 and event.direction == move.direction
            for event in structure_events
        )
        has_imbalance = any(
            gap.direction == move.direction and move.index - 1 <= gap.index <= move.index + 2
            for gap in gaps
        )
        # A pullback candle that produced neither a structure break nor an
        # imbalance is not an order block; it is just a red candle.
        if not broke_structure and not has_imbalance and not move.leaves_gap:
            continue

        # Mitigation is measured at the FAR edge, not the near one.
        #
        # This read `candle.low <= origin.high` for a bullish block: the
        # instant price came back and TOUCHED the top of the demand zone,
        # the block was marked mitigated and `is_live` refused it from
        # then on. That touch is the entry. So a block died on exactly
        # the bar it was meant to be used, and the two conditions the
        # engine needs - "price is at the block" and "the block is still
        # live" - could never hold at the same time. `best_entry_block`
        # was unreachable code, and measuring it said so: across 24 days
        # of M15 decision points, 661 of the 747 refusals for "no live
        # fair value gap or order block" had every order block dead.
        #
        # A fair value gap has always used the far edge (`candle.low <=
        # lower`, a full fill), and the two are the same idea: the zone
        # is spent once price has traded THROUGH it. Touching it is
        # arrival.
        mitigated_index: int | None = None
        invalidated_index: int | None = None
        for probe in range(move.index + 1, len(candles)):
            candle = candles[probe]
            if move.direction == "bullish":
                if candle.close < origin.low - current_atr * 0.1:
                    invalidated_index = probe
                    break
                if candle.low <= origin.low:
                    mitigated_index = probe
                    break
            else:
                if candle.close > origin.high + current_atr * 0.1:
                    invalidated_index = probe
                    break
                if candle.high >= origin.high:
                    mitigated_index = probe
                    break

        strength = min(
            1.0,
            0.45 * move.quality
            + (0.30 if broke_structure else 0.0)
            + (0.15 if has_imbalance else 0.0)
            + 0.10 * min(1.0, origin.range / current_atr),
        )
        blocks.append(
            OrderBlock(
                index=origin_index,
                confirmed_index=move.index,
                timestamp=origin.timestamp,
                direction=move.direction,
                lower=origin.low,
                upper=origin.high,
                displacement_index=move.index,
                strength=strength,
                broke_structure=broke_structure,
                has_imbalance=has_imbalance,
                mitigated_index=mitigated_index,
                invalidated_index=invalidated_index,
            )
        )
    blocks.sort(key=lambda block: block.index)
    return blocks


def best_entry_block(
    blocks: Sequence[OrderBlock],
    *,
    direction: str,
    at_index: int,
    max_age: int,
    reference_index: int | None = None,
    lookback: int = 12,
) -> OrderBlock | None:
    wanted = "bullish" if direction == "BUY" else "bearish"
    live = [
        block
        for block in blocks
        if block.direction == wanted
        and block.is_live(at_index, max_age)
        and (reference_index is None or block.index >= reference_index - lookback)
    ]
    if not live:
        return None
    return max(live, key=lambda block: (block.strength, block.index))
