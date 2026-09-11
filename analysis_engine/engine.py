"""High-level orchestration for deterministic M15 SMC analysis."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .detectors import (
    _structure_bias,
    calculate_momentum,
    calculate_volatility,
    detect_displacement,
    detect_equal_levels,
    detect_fair_value_gaps,
    detect_liquidity_sweeps,
    detect_order_blocks,
    detect_structure_breaks,
    detect_swings,
)
from .models import AnalysisResult, Candle, MarketStructure, SMCContext


class SMCAnalyzer:
    """Run all SMC detectors on a chronological M15 candle sequence.

    This class is intentionally analysis-only. It has no broker, MT5, AI, or
    order-routing dependencies.
    """

    def __init__(
        self,
        *,
        timeframe: str = "M15",
        swing_window: int = 2,
        equal_level_tolerance: float = 0.0006,
        displacement_lookback: int = 5,
    ) -> None:
        self.timeframe = timeframe.upper()
        supported_timeframes = ("M15", "H1", "H4")
        if self.timeframe not in supported_timeframes:
            raise ValueError(
                f"SMCAnalyzer execution timeframe must be one of {supported_timeframes}"
            )
        self.swing_window = swing_window
        self.equal_level_tolerance = equal_level_tolerance
        self.displacement_lookback = displacement_lookback

    def analyze(self, candles: Iterable[Candle | Mapping[str, object]]) -> AnalysisResult:
        normalized = tuple(
            candle if isinstance(candle, Candle) else Candle.from_mapping(candle)
            for candle in candles
        )
        self._validate_sequence(normalized)

        swing_highs, swing_lows = detect_swings(normalized, self.swing_window)
        equal_highs = detect_equal_levels(swing_highs, self.equal_level_tolerance)
        equal_lows = detect_equal_levels(swing_lows, self.equal_level_tolerance)
        liquidity_sweeps = detect_liquidity_sweeps(
            normalized,
            swing_highs,
            swing_lows,
            equal_highs,
            equal_lows,
        )
        structure_breaks = detect_structure_breaks(normalized, swing_highs, swing_lows)
        displacement = detect_displacement(normalized, self.displacement_lookback)
        fair_value_gaps = detect_fair_value_gaps(normalized)
        order_blocks = detect_order_blocks(normalized, displacement)
        momentum = calculate_momentum(normalized)
        volatility = calculate_volatility(normalized)

        latest_break = structure_breaks[-1] if structure_breaks else None
        market_bias = _structure_bias(swing_highs, swing_lows, latest_break)
        market_structure = MarketStructure(
            bias=market_bias,
            rationale=self._structure_rationale(market_bias, swing_highs, swing_lows, latest_break),
            latest_swing_high=swing_highs[-1] if swing_highs else None,
            previous_swing_high=swing_highs[-2] if len(swing_highs) >= 2 else None,
            latest_swing_low=swing_lows[-1] if swing_lows else None,
            previous_swing_low=swing_lows[-2] if len(swing_lows) >= 2 else None,
        )

        context = self._overall_context(
            market_bias,
            liquidity_sweeps,
            structure_breaks,
            fair_value_gaps,
            displacement,
            momentum.direction,
        )

        return AnalysisResult(
            timeframe=self.timeframe,
            candle_count=len(normalized),
            latest_candle=normalized[-1],
            market_structure=market_structure,
            previous_high=swing_highs[-1] if swing_highs else None,
            previous_low=swing_lows[-1] if swing_lows else None,
            swing_highs=tuple(swing_highs),
            swing_lows=tuple(swing_lows),
            equal_highs=tuple(equal_highs),
            equal_lows=tuple(equal_lows),
            liquidity_sweeps=tuple(liquidity_sweeps),
            structure_breaks=tuple(structure_breaks),
            fair_value_gaps=tuple(fair_value_gaps),
            order_blocks=tuple(order_blocks),
            displacement=tuple(displacement),
            momentum=momentum,
            volatility=volatility,
            overall_context=context,
        )

    @staticmethod
    def _validate_sequence(candles: Sequence[Candle]) -> None:
        if len(candles) < 7:
            raise ValueError("at least 7 candles are required for SMC analysis")
        timeframes = {candle.timeframe for candle in candles}
        if len(timeframes) > 1:
            raise ValueError(f"all candles must use the same timeframe, got {sorted(timeframes)}")
        if any(left.timestamp >= right.timestamp for left, right in zip(candles, candles[1:])):
            raise ValueError("candles must be strictly chronological")

    @staticmethod
    def _structure_rationale(
        bias: str,
        swing_highs: Sequence,
        swing_lows: Sequence,
        latest_break,
    ) -> str:
        if latest_break:
            return f"Latest confirmed {latest_break.event_type} is {latest_break.direction}."
        if bias == "bullish" and len(swing_highs) >= 2 and len(swing_lows) >= 2:
            return "Recent swing highs and lows are both stepping higher."
        if bias == "bearish" and len(swing_highs) >= 2 and len(swing_lows) >= 2:
            return "Recent swing highs and lows are both stepping lower."
        return "Swing sequence is not aligned into a clear directional trend."

    @staticmethod
    def _overall_context(
        market_bias: str,
        sweeps,
        structure_breaks,
        fair_value_gaps,
        displacement,
        momentum_direction: str,
    ) -> SMCContext:
        score = 0
        rationale: list[str] = []
        if market_bias == "bullish":
            score += 2
            rationale.append("market structure is bullish")
        elif market_bias == "bearish":
            score -= 2
            rationale.append("market structure is bearish")

        for event in structure_breaks[-3:]:
            score += 2 if event.direction == "bullish" else -2
            rationale.append(f"{event.event_type} {event.direction}")
        for sweep in sweeps[-3:]:
            score += 1 if sweep.direction == "bullish" else -1
            rationale.append(f"{sweep.swept_kind} liquidity swept {sweep.direction}")
        for gap in fair_value_gaps[-3:]:
            score += 1 if gap.direction == "bullish" else -1
        for move in displacement[-3:]:
            score += 1 if move.direction == "bullish" else -1
        if momentum_direction == "bullish":
            score += 1
            rationale.append("momentum is bullish")
        elif momentum_direction == "bearish":
            score -= 1
            rationale.append("momentum is bearish")

        direction = "bullish" if score >= 2 else "bearish" if score <= -2 else "mixed"
        if not rationale:
            rationale.append("no directional SMC evidence was confirmed")
        return SMCContext(direction, score, tuple(rationale))
