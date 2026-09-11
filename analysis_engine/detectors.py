"""Pure, deterministic SMC feature detectors.

Each function accepts candles and returns value objects. They do not place
orders, call external services, or mutate input data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import copysign

from .models import (
    Candle,
    Displacement,
    EqualLevel,
    FairValueGap,
    LiquiditySweep,
    Momentum,
    OrderBlock,
    SwingPoint,
    StructureBreak,
    Volatility,
)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _close_to(left: float, right: float, tolerance: float) -> bool:
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= scale * tolerance


def detect_swings(candles: Sequence[Candle], window: int = 2) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """Find strict pivot highs and lows using confirmed neighboring candles."""

    if window < 1:
        raise ValueError("swing window must be at least 1")

    highs: list[SwingPoint] = []
    lows: list[SwingPoint] = []
    for index in range(window, len(candles) - window):
        candle = candles[index]
        neighbors = (
            candles[index - window : index]
            + candles[index + 1 : index + window + 1]
        )
        if all(candle.high > neighbor.high for neighbor in neighbors):
            highs.append(SwingPoint(index, candle.timestamp, candle.high, "high"))
        if all(candle.low < neighbor.low for neighbor in neighbors):
            lows.append(SwingPoint(index, candle.timestamp, candle.low, "low"))
    return highs, lows


def detect_equal_levels(
    points: Sequence[SwingPoint],
    tolerance: float = 0.0006,
) -> list[EqualLevel]:
    """Pair nearby confirmed swing points into equal high/low liquidity levels."""

    if tolerance <= 0:
        raise ValueError("equal-level tolerance must be positive")

    result: list[EqualLevel] = []
    for first, second in zip(points, points[1:]):
        if _close_to(first.price, second.price, tolerance):
            result.append(
                EqualLevel(
                    kind=first.kind,
                    level=(first.price + second.price) / 2,
                    first_index=first.index,
                    second_index=second.index,
                    tolerance=tolerance,
                )
            )
    return result


def _prior_levels(
    index: int,
    points: Iterable[SwingPoint],
    equal_levels: Iterable[EqualLevel],
    kind: str,
) -> list[tuple[float, int]]:
    levels: list[tuple[float, int]] = [
        (point.price, point.index) for point in points if point.kind == kind and point.index < index
    ]
    for level in equal_levels:
        if level.kind == kind and level.second_index < index:
            levels.append((level.level, level.second_index))
    return levels


def detect_liquidity_sweeps(
    candles: Sequence[Candle],
    swing_highs: Sequence[SwingPoint],
    swing_lows: Sequence[SwingPoint],
    equal_highs: Sequence[EqualLevel],
    equal_lows: Sequence[EqualLevel],
) -> list[LiquiditySweep]:
    """Detect wick-through-and-close-back events against prior swing liquidity."""

    sweeps: list[LiquiditySweep] = []
    for index, candle in enumerate(candles):
        high_levels = _prior_levels(index, swing_highs, equal_highs, "high")
        low_levels = _prior_levels(index, swing_lows, equal_lows, "low")

        if high_levels:
            eligible = [level for level in high_levels if candle.high > level[0] and candle.close < level[0]]
            if eligible:
                level, source_index = max(eligible, key=lambda item: item[0])
                sweeps.append(
                    LiquiditySweep(
                        index,
                        candle.timestamp,
                        "bearish",
                        "high",
                        level,
                        candle.high,
                        candle.close,
                        source_index,
                    )
                )

        if low_levels:
            eligible = [level for level in low_levels if candle.low < level[0] and candle.close > level[0]]
            if eligible:
                level, source_index = min(eligible, key=lambda item: item[0])
                sweeps.append(
                    LiquiditySweep(
                        index,
                        candle.timestamp,
                        "bullish",
                        "low",
                        level,
                        candle.low,
                        candle.close,
                        source_index,
                    )
                )
    return sweeps


def _structure_bias(
    swing_highs: Sequence[SwingPoint],
    swing_lows: Sequence[SwingPoint],
    latest_break: StructureBreak | None = None,
) -> str:
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        higher_high = swing_highs[-1].price > swing_highs[-2].price
        higher_low = swing_lows[-1].price > swing_lows[-2].price
        lower_high = swing_highs[-1].price < swing_highs[-2].price
        lower_low = swing_lows[-1].price < swing_lows[-2].price
        if higher_high and higher_low:
            return "bullish"
        if lower_high and lower_low:
            return "bearish"
    return latest_break.direction if latest_break else "range"


def detect_structure_breaks(
    candles: Sequence[Candle],
    swing_highs: Sequence[SwingPoint],
    swing_lows: Sequence[SwingPoint],
) -> list[StructureBreak]:
    """Detect close-through events and classify them as BOS or CHoCH."""

    breaks: list[StructureBreak] = []
    broken_levels: set[tuple[str, int]] = set()
    current_bias: str | None = None

    for index, candle in enumerate(candles):
        prior_highs = [point for point in swing_highs if point.index < index]
        prior_lows = [point for point in swing_lows if point.index < index]
        latest_high = prior_highs[-1] if prior_highs else None
        latest_low = prior_lows[-1] if prior_lows else None
        inferred_bias = _structure_bias(prior_highs, prior_lows, breaks[-1] if breaks else None)
        if current_bias is None and inferred_bias != "range":
            current_bias = inferred_bias

        event: StructureBreak | None = None
        if latest_high and candle.close > latest_high.price and ("high", latest_high.index) not in broken_levels:
            direction = "bullish"
            pre_break_bias = _structure_bias(
                prior_highs[:-1] if prior_highs else prior_highs,
                prior_lows,
                breaks[-1] if breaks else None,
            )
            effective_bias = current_bias or pre_break_bias
            event_type = "CHoCH" if effective_bias and effective_bias != direction else "BOS"
            event = StructureBreak(
                index,
                candle.timestamp,
                direction,
                event_type,
                "high",
                latest_high.price,
                latest_high.index,
                candle.close,
            )
            broken_levels.add(("high", latest_high.index))
        elif latest_low and candle.close < latest_low.price and ("low", latest_low.index) not in broken_levels:
            direction = "bearish"
            pre_break_bias = _structure_bias(
                prior_highs,
                prior_lows[:-1] if prior_lows else prior_lows,
                breaks[-1] if breaks else None,
            )
            effective_bias = current_bias or pre_break_bias
            event_type = "CHoCH" if effective_bias and effective_bias != direction else "BOS"
            event = StructureBreak(
                index,
                candle.timestamp,
                direction,
                event_type,
                "low",
                latest_low.price,
                latest_low.index,
                candle.close,
            )
            broken_levels.add(("low", latest_low.index))

        if event:
            breaks.append(event)
            current_bias = event.direction
    return breaks


def detect_displacement(
    candles: Sequence[Candle],
    lookback: int = 5,
    multiplier: float = 1.5,
    minimum_body_ratio: float = 0.6,
) -> list[Displacement]:
    """Find unusually large, directional candles relative to recent bodies."""

    if lookback < 1 or multiplier <= 0:
        raise ValueError("displacement lookback and multiplier must be positive")

    result: list[Displacement] = []
    for index, candle in enumerate(candles):
        prior = candles[max(0, index - lookback) : index]
        if not prior:
            continue
        average_body = sum(item.body for item in prior) / len(prior)
        body_ratio = candle.body / average_body if average_body else 0.0
        candle_range = candle.range or 1.0
        if body_ratio >= multiplier and candle.body / candle_range >= minimum_body_ratio:
            result.append(
                Displacement(
                    index,
                    candle.timestamp,
                    candle.direction,
                    candle.body,
                    average_body,
                    body_ratio,
                )
            )
    return result


def detect_fair_value_gaps(candles: Sequence[Candle]) -> list[FairValueGap]:
    """Detect three-candle gaps using the outer candles' wicks."""

    result: list[FairValueGap] = []
    for index in range(2, len(candles)):
        left = candles[index - 2]
        right = candles[index]
        if right.low > left.high:
            result.append(
                FairValueGap(
                    index,
                    right.timestamp,
                    "bullish",
                    left.high,
                    right.low,
                    index - 2,
                    index,
                )
            )
        elif right.high < left.low:
            result.append(
                FairValueGap(
                    index,
                    right.timestamp,
                    "bearish",
                    right.high,
                    left.low,
                    index - 2,
                    index,
                )
            )
    return result


def detect_order_blocks(
    candles: Sequence[Candle],
    displacement: Sequence[Displacement],
    lookback: int = 5,
) -> list[OrderBlock]:
    """Use the last opposing candle before displacement as a basic order block."""

    result: list[OrderBlock] = []
    seen: set[tuple[str, int]] = set()
    for move in displacement:
        start = max(0, move.index - lookback)
        target_direction = "bullish" if move.direction == "bullish" else "bearish"
        expected_candle_direction = "bearish" if target_direction == "bullish" else "bullish"
        for index in range(move.index - 1, start - 1, -1):
            candle = candles[index]
            if candle.direction == expected_candle_direction:
                key = (target_direction, index)
                if key not in seen:
                    result.append(
                        OrderBlock(
                            index,
                            candle.timestamp,
                            target_direction,
                            candle.low,
                            candle.high,
                            move.index,
                            candle.direction,
                        )
                    )
                    seen.add(key)
                break
    return result


def calculate_momentum(candles: Sequence[Candle], lookback: int = 5) -> Momentum:
    if len(candles) < 2:
        raise ValueError("at least two candles are required for momentum")
    effective_lookback = min(lookback, len(candles) - 1)
    baseline = candles[-effective_lookback - 1].close
    latest = candles[-1].close
    price_change = latest - baseline
    price_change_percent = (price_change / baseline * 100) if baseline else 0.0
    recent = candles[-effective_lookback:]
    average_signed_body = sum(
        copysign(candle.body, candle.close - candle.open) if candle.direction != "neutral" else 0.0
        for candle in recent
    ) / effective_lookback
    score = _clamp(
        (price_change_percent / 1.0) * 0.7 + (average_signed_body / max(baseline * 0.01, 1e-9)) * 0.3,
        -1.0,
        1.0,
    )
    direction = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
    return Momentum(direction, round(score, 6), effective_lookback, price_change, price_change_percent, average_signed_body)


def _true_ranges(candles: Sequence[Candle]) -> list[float]:
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            ranges.append(candle.range)
            continue
        previous_close = candles[index - 1].close
        ranges.append(max(candle.range, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    return ranges


def calculate_volatility(candles: Sequence[Candle], lookback: int = 5) -> Volatility:
    if not candles:
        raise ValueError("at least one candle is required for volatility")
    true_ranges = _true_ranges(candles)
    effective_lookback = min(lookback, len(true_ranges))
    atr = sum(true_ranges[-effective_lookback:]) / effective_lookback
    average_true_range = sum(true_ranges) / len(true_ranges)
    latest_close = candles[-1].close or 1.0
    normalized_atr_percent = atr / latest_close * 100
    relative = atr / average_true_range if average_true_range else 1.0
    regime = "high" if relative >= 1.25 else "low" if relative <= 0.75 else "normal"
    return Volatility(regime, atr, normalized_atr_percent, effective_lookback, average_true_range)