"""Trading sessions and their liquidity profile.

Used three ways: as a quality input to the scorer, as a hard filter when
liquidity is genuinely poor, and as the source of session high/low
liquidity levels for the liquidity map.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..clock import ensure_utc
from ..config import SessionConfig
from ..marketdata.candles import Candle

SESSION_QUALITY = {
    "OVERLAP": 1.0,     # London/New York - deepest liquidity of the day
    "LONDON": 0.9,
    "NEW_YORK": 0.85,
    "ASIAN": 0.5,
    "OFF_HOURS": 0.2,
}


@dataclass(frozen=True, slots=True)
class SessionState:
    name: str
    quality: float
    tradeable: bool
    hour_utc: int
    weekend: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quality": self.quality,
            "tradeable": self.tradeable,
            "hourUtc": self.hour_utc,
            "weekend": self.weekend,
        }


def is_forex_weekend(moment: datetime) -> bool:
    """FX is closed Friday 21:00 UTC to Sunday 22:00 UTC.

    The Friday edge is deliberately an hour early: the last hour before
    the close has thin liquidity and widening spreads, which is exactly
    when a stop is most likely to be hit by a spread spike rather than by
    price.
    """

    moment = ensure_utc(moment)
    weekday, hour = moment.weekday(), moment.hour
    if weekday == 4 and hour >= 21:
        return True
    if weekday == 5:
        return True
    if weekday == 6 and hour < 22:
        return True
    return False


def classify_session(moment: datetime, config: SessionConfig | None = None) -> SessionState:
    config = config or SessionConfig()
    moment = ensure_utc(moment)
    hour = moment.hour

    if is_forex_weekend(moment):
        return SessionState("WEEKEND", 0.0, False, hour, True)

    in_london = config.london[0] <= hour < config.london[1]
    in_ny = config.new_york[0] <= hour < config.new_york[1]
    in_asian = config.asian[0] <= hour < config.asian[1]

    if in_london and in_ny:
        name = "OVERLAP"
    elif in_london:
        name = "LONDON"
    elif in_ny:
        name = "NEW_YORK"
    elif in_asian:
        name = "ASIAN"
    else:
        name = "OFF_HOURS"

    quality = SESSION_QUALITY[name]
    tradeable = name in config.tradeable_sessions or not config.block_low_liquidity
    return SessionState(name, quality, tradeable, hour, False)


def session_window(moment: datetime, name: str, config: SessionConfig | None = None) -> tuple[datetime, datetime]:
    """The [start, end) UTC bounds of `name` on the day of `moment`."""

    config = config or SessionConfig()
    moment = ensure_utc(moment)
    bounds = {"ASIAN": config.asian, "LONDON": config.london, "NEW_YORK": config.new_york}[name]
    start = moment.replace(hour=bounds[0], minute=0, second=0, microsecond=0)
    end = moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=bounds[1])
    return start, end


def session_extremes(
    candles: Sequence[Candle], moment: datetime, config: SessionConfig | None = None
) -> dict[str, dict[str, float]]:
    """High/low of each completed session today plus the previous day.

    These are the levels retail stops cluster behind, which is exactly
    what makes them liquidity targets.
    """

    config = config or SessionConfig()
    moment = ensure_utc(moment)
    today = moment.date()
    yesterday = today - timedelta(days=1)

    result: dict[str, dict[str, float]] = {}

    previous_day = [c for c in candles if c.timestamp.date() == yesterday]
    if previous_day:
        result["PREVIOUS_DAY"] = {
            "high": max(c.high for c in previous_day),
            "low": min(c.low for c in previous_day),
        }

    for name in ("ASIAN", "LONDON", "NEW_YORK"):
        start, end = session_window(moment, name, config)
        members = [c for c in candles if start <= c.timestamp < min(end, moment)]
        if len(members) >= 2:
            result[name] = {
                "high": max(c.high for c in members),
                "low": min(c.low for c in members),
            }
    return result
