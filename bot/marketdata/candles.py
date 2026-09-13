"""Immutable candle type shared by the SMC engine and the backtester."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Mapping, Sequence

from ..clock import ensure_utc

TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime  # candle OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "Candle":
        raw_ts = row.get("timestamp", row.get("time"))
        if isinstance(raw_ts, datetime):
            timestamp = ensure_utc(raw_ts)
        elif isinstance(raw_ts, str):
            timestamp = ensure_utc(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))
        elif isinstance(raw_ts, (int, float)):
            from ..clock import from_epoch

            timestamp = from_epoch(float(raw_ts))
        else:
            raise ValueError(f"candle timestamp is missing or unusable: {raw_ts!r}")

        def number(name: str) -> float:
            value = row.get(name)
            try:
                result = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"candle {name} must be numeric, got {value!r}") from exc
            if not isfinite(result):
                raise ValueError(f"candle {name} must be finite")
            return result

        open_price, high, low, close = (number(f) for f in ("open", "high", "low", "close"))
        volume_raw = row.get("volume")
        volume = 0.0 if volume_raw in (None, "") else float(volume_raw)
        timeframe = str(row.get("timeframe", "M15")).upper()

        if min(open_price, high, low, close) <= 0:
            raise ValueError("candle prices must be positive")
        if high < max(open_price, close) or low > min(open_price, close) or low > high:
            raise ValueError(
                f"candle OHLC is inconsistent (o={open_price} h={high} l={low} c={close})"
            )
        if timeframe not in TIMEFRAME_MINUTES:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        return cls(timestamp, open_price, high, low, close, volume, timeframe)

    @property
    def minutes(self) -> int:
        return TIMEFRAME_MINUTES[self.timeframe]

    @property
    def close_time(self) -> datetime:
        return self.timestamp + timedelta(minutes=self.minutes)

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

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "timeframe": self.timeframe,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def to_candles(rows: Sequence[Mapping[str, Any]]) -> list[Candle]:
    return [Candle.from_mapping(row) for row in rows]
