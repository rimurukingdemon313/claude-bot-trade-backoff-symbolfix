"""Typed value objects returned by the SMC analysis engine."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string or datetime")
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc


def _number(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLC candle. The analyzer accepts M15 candles only."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    timeframe: str = "M15"

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Candle":
        timestamp = _parse_timestamp(row.get("timestamp", row.get("time")))
        open_price = _number(row.get("open"), "open")
        high = _number(row.get("high"), "high")
        low = _number(row.get("low"), "low")
        close = _number(row.get("close"), "close")
        volume_value = row.get("volume")
        volume = None if volume_value is None else _number(volume_value, "volume")
        timeframe = str(row.get("timeframe", "M15")).upper()

        if high < max(open_price, close):
            raise ValueError("high must be at least as large as open and close")
        if low > min(open_price, close):
            raise ValueError("low must be at most as small as open and close")
        if low > high:
            raise ValueError("low must not be greater than high")
        SUPPORTED_TIMEFRAMES = ("M15", "H1", "H4")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"SMCAnalyzer supports {', '.join(SUPPORTED_TIMEFRAMES)} candles, got {timeframe}"
            )

        return cls(timestamp, open_price, high, low, close, volume, timeframe)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def direction(self) -> str:
        if self.close > self.open:
            return "bullish"
        if self.close < self.open:
            return "bearish"
        return "neutral"


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    timestamp: datetime
    price: float
    kind: str


@dataclass(frozen=True, slots=True)
class EqualLevel:
    kind: str
    level: float
    first_index: int
    second_index: int
    tolerance: float


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    index: int
    timestamp: datetime
    direction: str
    swept_kind: str
    level: float
    wick_price: float
    close_price: float
    source_index: int


@dataclass(frozen=True, slots=True)
class StructureBreak:
    index: int
    timestamp: datetime
    direction: str
    event_type: str
    broken_kind: str
    broken_level: float
    source_index: int
    close_price: float


@dataclass(frozen=True, slots=True)
class Displacement:
    index: int
    timestamp: datetime
    direction: str
    body: float
    average_body: float
    body_ratio: float


@dataclass(frozen=True, slots=True)
class FairValueGap:
    index: int
    timestamp: datetime
    direction: str
    lower: float
    upper: float
    left_index: int
    right_index: int


@dataclass(frozen=True, slots=True)
class OrderBlock:
    index: int
    timestamp: datetime
    direction: str
    lower: float
    upper: float
    source_displacement_index: int
    candle_direction: str


@dataclass(frozen=True, slots=True)
class Momentum:
    direction: str
    score: float
    lookback: int
    price_change: float
    price_change_percent: float
    average_signed_body: float


@dataclass(frozen=True, slots=True)
class Volatility:
    regime: str
    atr: float
    normalized_atr_percent: float
    lookback: int
    average_true_range: float


@dataclass(frozen=True, slots=True)
class MarketStructure:
    bias: str
    rationale: str
    latest_swing_high: SwingPoint | None
    previous_swing_high: SwingPoint | None
    latest_swing_low: SwingPoint | None
    previous_swing_low: SwingPoint | None


@dataclass(frozen=True, slots=True)
class SMCContext:
    direction: str
    score: int
    rationale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    timeframe: str
    candle_count: int
    latest_candle: Candle
    market_structure: MarketStructure
    previous_high: SwingPoint | None
    previous_low: SwingPoint | None
    swing_highs: tuple[SwingPoint, ...]
    swing_lows: tuple[SwingPoint, ...]
    equal_highs: tuple[EqualLevel, ...]
    equal_lows: tuple[EqualLevel, ...]
    liquidity_sweeps: tuple[LiquiditySweep, ...]
    structure_breaks: tuple[StructureBreak, ...]
    fair_value_gaps: tuple[FairValueGap, ...]
    order_blocks: tuple[OrderBlock, ...]
    displacement: tuple[Displacement, ...]
    momentum: Momentum
    volatility: Volatility
    overall_context: SMCContext

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-friendly structured output for an API or paper bot."""

        def convert(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if is_dataclass(value):
                return {
                    field.name: convert(getattr(value, field.name))
                    for field in fields(value)
                }
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        return convert(self)
