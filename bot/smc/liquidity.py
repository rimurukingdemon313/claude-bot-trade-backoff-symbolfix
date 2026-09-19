"""Liquidity map and sweep detection.

The map answers "where are the stops?" — equal highs/lows, swing
extremes, previous-day extremes, session extremes. Buy-side liquidity
sits above highs, sell-side below lows.

Sweep detection then answers the harder question: "was that liquidity
actually taken, and did the market reject from it?" A wick through a
level is necessary but nowhere near sufficient (MASTER_MISSION §17). A
sweep is only graded highly when all five stages are present:

  1. identifiable liquidity (a level with real significance);
  2. approach (price came from the correct side);
  3. taken (wick through, close back);
  4. rejection (the close rejects a meaningful share of the excursion);
  5. reaction (displacement and/or a structure shift shortly after).

Stages 1-4 are knowable on the sweep candle. Stage 5 is scored from
candles AFTER the sweep — so a sweep is only ever "confirmed" at
`confirmed_index`, and the entry logic uses that, never the raw index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..marketdata.candles import Candle
from .displacement import Displacement
from .indicators import atr_series
from .sessions import session_extremes
from .structure import StructureEvent
from .swings import SwingPoint, SwingSet

#: Relative importance of each liquidity source. Previous-day and
#: session extremes are watched by far more participants than an
#: arbitrary intraday pivot, so sweeping them means more.
LEVEL_WEIGHTS = {
    "EQUAL_HIGHS": 1.0,
    "EQUAL_LOWS": 1.0,
    "PREVIOUS_DAY": 0.95,
    "SESSION": 0.8,
    "SWING": 0.6,
}


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    price: float
    side: str          # "buy" (above price, over highs) | "sell" (below lows)
    kind: str          # EQUAL_HIGHS | EQUAL_LOWS | PREVIOUS_DAY | SESSION | SWING
    label: str
    source_index: int  # candle index after which this level is knowable
    weight: float
    touches: int = 1
    scope: str = "external"  # external | internal

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "side": self.side,
            "kind": self.kind,
            "label": self.label,
            "weight": round(self.weight, 3),
            "touches": self.touches,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class LiquidityMap:
    levels: tuple[LiquidityLevel, ...] = field(default_factory=tuple)

    def visible(self, at_index: int) -> list[LiquidityLevel]:
        return [level for level in self.levels if level.source_index <= at_index]

    def buy_side(self, at_index: int) -> list[LiquidityLevel]:
        return sorted(
            (l for l in self.visible(at_index) if l.side == "buy"), key=lambda l: l.price
        )

    def sell_side(self, at_index: int) -> list[LiquidityLevel]:
        return sorted(
            (l for l in self.visible(at_index) if l.side == "sell"), key=lambda l: l.price, reverse=True
        )

    def nearest_target(
        self,
        price: float,
        direction: str,
        at_index: int,
        *,
        min_distance: float = 0.0,
    ) -> LiquidityLevel | None:
        """The liquidity pool price is likely heading toward.

        For a long that means the nearest untouched buy-side pool ABOVE
        the current price — a natural, non-arbitrary take-profit anchor.

        `min_distance` skips pools too close to pay for the stop. This is
        not the same thing as moving the target: every candidate here is a
        real level with real resting orders, and the ones skipped become
        hurdles the trade has to pass through rather than places to aim
        at. A discretionary trader does the same — the first minor high is
        not a target, it is something in the way.

        The alternative, and what this replaced, was projecting a target
        at exactly the minimum R whenever the nearest pool fell short.
        That could never fail the ratio check downstream, so the check was
        decorative, and a setup whose real structural reward was 0.23R
        went into the record as 1:2.
        """

        if direction == "BUY":
            above = [
                level
                for level in self.buy_side(at_index)
                if level.price > price and (level.price - price) >= min_distance
            ]
            return above[0] if above else None
        below = [
            level
            for level in self.sell_side(at_index)
            if level.price < price and (price - level.price) >= min_distance
        ]
        return below[0] if below else None

    def as_dict(self, at_index: int, limit: int = 12) -> dict[str, Any]:
        visible = sorted(self.visible(at_index), key=lambda l: -l.weight)[:limit]
        return {
            "levels": [level.as_dict() for level in visible],
            "buySideCount": len(self.buy_side(at_index)),
            "sellSideCount": len(self.sell_side(at_index)),
        }


def _cluster_equal_levels(
    points: Sequence[SwingPoint], tolerance: float, kind: str
) -> list[LiquidityLevel]:
    """Group swings that sit within `tolerance` of each other.

    Two or more near-identical highs are where stop orders stack up; the
    more touches, the more liquidity and the higher the weight.
    """

    if not points or tolerance <= 0:
        return []
    ordered = sorted(points, key=lambda point: point.price)
    clusters: list[list[SwingPoint]] = [[ordered[0]]]
    for point in ordered[1:]:
        if abs(point.price - clusters[-1][-1].price) <= tolerance:
            clusters[-1].append(point)
        else:
            clusters.append([point])

    levels: list[LiquidityLevel] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        price = sum(point.price for point in cluster) / len(cluster)
        confirmed = max(point.confirmed_index for point in cluster)
        weight = LEVEL_WEIGHTS[kind] * min(1.0, 0.7 + 0.15 * len(cluster))
        levels.append(
            LiquidityLevel(
                price=price,
                side="buy" if kind == "EQUAL_HIGHS" else "sell",
                kind=kind,
                label=f"{len(cluster)}x equal {'highs' if kind == 'EQUAL_HIGHS' else 'lows'}",
                source_index=confirmed,
                weight=weight,
                touches=len(cluster),
            )
        )
    return levels


def build_liquidity_map(
    candles: Sequence[Candle],
    swings: SwingSet,
    *,
    atr_period: int = 14,
    equal_tolerance_atr: float = 0.12,
    now: datetime | None = None,
) -> LiquidityMap:
    if not candles:
        return LiquidityMap(())
    atrs = atr_series(candles, atr_period)
    reference_atr = atrs[-1] if atrs else 0.0
    tolerance = reference_atr * equal_tolerance_atr
    last_index = len(candles) - 1

    levels: list[LiquidityLevel] = []
    levels.extend(_cluster_equal_levels(swings.highs, tolerance, "EQUAL_HIGHS"))
    levels.extend(_cluster_equal_levels(swings.lows, tolerance, "EQUAL_LOWS"))

    # Individual swing liquidity — the most recent pivots only, so the map
    # describes the live battlefield rather than ancient history.
    for point in list(swings.highs)[-8:]:
        levels.append(
            LiquidityLevel(
                price=point.price,
                side="buy",
                kind="SWING",
                label="swing high",
                source_index=point.confirmed_index,
                weight=LEVEL_WEIGHTS["SWING"],
                scope="internal",
            )
        )
    for point in list(swings.lows)[-8:]:
        levels.append(
            LiquidityLevel(
                price=point.price,
                side="sell",
                kind="SWING",
                label="swing low",
                source_index=point.confirmed_index,
                weight=LEVEL_WEIGHTS["SWING"],
                scope="internal",
            )
        )

    moment = now or candles[-1].close_time
    for name, extremes in session_extremes(candles, moment).items():
        kind = "PREVIOUS_DAY" if name == "PREVIOUS_DAY" else "SESSION"
        # A session level is knowable only after that session's candles
        # exist; using last_index would make it visible from bar zero.
        source = last_index
        for candle_index in range(len(candles) - 1, -1, -1):
            if candles[candle_index].high <= extremes["high"] and candles[candle_index].low >= extremes["low"]:
                source = min(source, candle_index)
        levels.append(
            LiquidityLevel(
                price=extremes["high"],
                side="buy",
                kind=kind,
                label=f"{name.replace('_', ' ').title()} high",
                source_index=source,
                weight=LEVEL_WEIGHTS[kind],
            )
        )
        levels.append(
            LiquidityLevel(
                price=extremes["low"],
                side="sell",
                kind=kind,
                label=f"{name.replace('_', ' ').title()} low",
                source_index=source,
                weight=LEVEL_WEIGHTS[kind],
            )
        )
    return LiquidityMap(tuple(levels))


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    index: int
    confirmed_index: int
    timestamp: datetime
    direction: str        # bullish (sell-side swept, expect up) | bearish
    level: LiquidityLevel
    excursion: float      # how far past the level price went
    rejection_ratio: float
    displaced: bool
    structure_shift: bool
    quality: float        # 0..1

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "confirmedIndex": self.confirmed_index,
            "timestamp": self.timestamp.isoformat(),
            "direction": self.direction,
            "level": self.level.as_dict(),
            "rejectionRatio": round(self.rejection_ratio, 3),
            "displaced": self.displaced,
            "structureShift": self.structure_shift,
            "quality": round(self.quality, 3),
        }


def detect_sweeps(
    candles: Sequence[Candle],
    liquidity: LiquidityMap,
    displacements: Sequence[Displacement],
    structure_events: Sequence[StructureEvent],
    *,
    atr_period: int = 14,
    reaction_candles: int = 4,
    min_rejection_ratio: float = 0.45,
    min_excursion_atr: float = 0.05,
) -> list[LiquiditySweep]:
    """Find graded sweeps. Returns them ordered by candle index."""

    if not candles:
        return []
    atrs = atr_series(candles, atr_period)
    displacement_by_index = {d.index: d for d in displacements}
    sweeps: list[LiquiditySweep] = []

    for index, candle in enumerate(candles):
        current_atr = atrs[index]
        if current_atr <= 0:
            continue
        visible = liquidity.visible(index - 1)  # level must predate the sweep
        if not visible:
            continue

        # --- sell-side taken: wick BELOW a low, close back above ---
        candidates = [
            level
            for level in visible
            if level.side == "sell" and candle.low < level.price <= candle.close
        ]
        if candidates:
            level = min(candidates, key=lambda l: l.price)
            excursion = level.price - candle.low
            if excursion >= current_atr * min_excursion_atr:
                rejection = (candle.close - candle.low) / candle.range if candle.range else 0.0
                if rejection >= min_rejection_ratio:
                    sweep = _grade(
                        candles,
                        index,
                        "bullish",
                        level,
                        excursion,
                        rejection,
                        displacement_by_index,
                        structure_events,
                        reaction_candles,
                        current_atr,
                    )
                    if sweep is not None:
                        sweeps.append(sweep)

        # --- buy-side taken: wick ABOVE a high, close back below ---
        candidates = [
            level
            for level in visible
            if level.side == "buy" and candle.high > level.price >= candle.close
        ]
        if candidates:
            level = max(candidates, key=lambda l: l.price)
            excursion = candle.high - level.price
            if excursion >= current_atr * min_excursion_atr:
                rejection = (candle.high - candle.close) / candle.range if candle.range else 0.0
                if rejection >= min_rejection_ratio:
                    sweep = _grade(
                        candles,
                        index,
                        "bearish",
                        level,
                        excursion,
                        rejection,
                        displacement_by_index,
                        structure_events,
                        reaction_candles,
                        current_atr,
                    )
                    if sweep is not None:
                        sweeps.append(sweep)

    sweeps.sort(key=lambda sweep: sweep.index)
    return sweeps


def _grade(
    candles: Sequence[Candle],
    index: int,
    direction: str,
    level: LiquidityLevel,
    excursion: float,
    rejection: float,
    displacement_by_index: dict[int, Displacement],
    structure_events: Sequence[StructureEvent],
    reaction_candles: int,
    current_atr: float,
) -> LiquiditySweep | None:
    """Stage 5: did the market actually react after the liquidity grab?"""

    window_end = min(len(candles) - 1, index + reaction_candles)
    displaced = False
    displacement_quality = 0.0
    for probe in range(index, window_end + 1):
        move = displacement_by_index.get(probe)
        if move is not None and move.direction == direction:
            displaced = True
            displacement_quality = max(displacement_quality, move.quality)

    structure_shift = any(
        index <= event.index <= window_end and event.direction == direction
        for event in structure_events
    )

    # The sweep is only knowable once its reaction window has printed.
    confirmed_index = window_end

    excursion_score = min(1.0, excursion / (current_atr * 0.6)) if current_atr > 0 else 0.0
    quality = (
        0.28 * level.weight
        + 0.22 * min(1.0, rejection / 0.85)
        + 0.14 * excursion_score
        + 0.22 * (displacement_quality if displaced else 0.0)
        + 0.14 * (1.0 if structure_shift else 0.0)
    )
    return LiquiditySweep(
        index=index,
        confirmed_index=confirmed_index,
        timestamp=candles[index].timestamp,
        direction=direction,
        level=level,
        excursion=excursion,
        rejection_ratio=rejection,
        displaced=displaced,
        structure_shift=structure_shift,
        quality=min(1.0, quality),
    )
