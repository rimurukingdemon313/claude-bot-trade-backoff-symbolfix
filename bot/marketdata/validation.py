"""Market-data validation — the last line before analysis.

Every rule here exists because its absence is a known way to produce a
wrong trade:

* the forming candle is removed, because deciding on a candle that has
  not closed is the classic repainting bug (a "BOS" that un-happens);
* timestamps must be strictly increasing and de-duplicated, because a
  repeated bar silently doubles the weight of one price;
* prices must be positive and OHLC-consistent;
* the newest closed candle must be recent, because analysing a stale
  series is analysing a market that has already moved;
* gaps are counted; a series riddled with holes is rejected rather than
  quietly interpolated.

Weekend gaps are expected on FX and are not counted as holes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..clock import utc_now
from ..errors import MarketDataError, StaleDataError
from .candles import Candle, TIMEFRAME_MINUTES


@dataclass(frozen=True, slots=True)
class ValidationReport:
    timeframe: str
    accepted: int
    dropped_forming: int
    dropped_duplicate: int
    dropped_invalid: int
    gaps: int
    newest_close: datetime | None
    age_minutes: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "accepted": self.accepted,
            "droppedForming": self.dropped_forming,
            "droppedDuplicate": self.dropped_duplicate,
            "droppedInvalid": self.dropped_invalid,
            "gaps": self.gaps,
            "newestClose": self.newest_close.isoformat() if self.newest_close else None,
            "ageMinutes": round(self.age_minutes, 2) if self.age_minutes is not None else None,
        }


def _is_weekend_gap(previous_close: datetime, next_open: datetime) -> bool:
    """True when the hole spans the FX weekend (Fri ~22:00 - Sun ~22:00)."""

    if next_open - previous_close < timedelta(hours=6):
        return False
    return previous_close.weekday() >= 4 and next_open.weekday() <= 6


def validate_series(
    candles: Sequence[Candle],
    *,
    timeframe: str,
    min_candles: int,
    now: datetime | None = None,
    max_age_multiple: float = 3.0,
    max_gap_ratio: float = 0.05,
) -> tuple[list[Candle], ValidationReport]:
    """Return (closed, clean, ordered candles, report) or raise.

    `max_age_multiple` is in candle intervals: an M15 series is stale once
    the newest CLOSED candle finished more than 45 minutes ago.
    """

    reference = now or utc_now()
    timeframe = timeframe.upper()
    if timeframe not in TIMEFRAME_MINUTES:
        raise MarketDataError(f"unsupported timeframe {timeframe!r}")
    minutes = TIMEFRAME_MINUTES[timeframe]

    dropped_invalid = 0
    dropped_forming = 0
    dropped_duplicate = 0

    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    cleaned: list[Candle] = []
    seen: set[datetime] = set()

    for candle in ordered:
        if candle.timeframe != timeframe:
            dropped_invalid += 1
            continue
        if candle.timestamp in seen:
            dropped_duplicate += 1
            continue
        # A candle whose close time is in the future has not finished
        # forming. Strategy decisions never see it.
        if candle.close_time > reference:
            dropped_forming += 1
            continue
        if candle.timestamp > reference:
            dropped_invalid += 1
            continue
        if candle.range < 0 or min(candle.open, candle.high, candle.low, candle.close) <= 0:
            dropped_invalid += 1
            continue
        seen.add(candle.timestamp)
        cleaned.append(candle)

    if len(cleaned) < min_candles:
        raise MarketDataError(
            f"{timeframe}: only {len(cleaned)} usable closed candles after validation, "
            f"{min_candles} required"
        )

    expected = timedelta(minutes=minutes)
    gaps = 0
    for previous, following in zip(cleaned, cleaned[1:]):
        delta = following.timestamp - previous.timestamp
        if delta > expected * 1.5 and not _is_weekend_gap(previous.close_time, following.timestamp):
            gaps += 1

    if gaps > max(1, int(len(cleaned) * max_gap_ratio)):
        raise MarketDataError(
            f"{timeframe}: {gaps} unexplained gaps across {len(cleaned)} candles "
            "— the series is too incomplete to analyse"
        )

    newest_close = cleaned[-1].close_time
    age_minutes = (reference - newest_close).total_seconds() / 60.0
    if age_minutes > minutes * max_age_multiple:
        raise StaleDataError(
            f"{timeframe}: newest closed candle finished {age_minutes:.0f} minutes ago "
            f"(limit {minutes * max_age_multiple:.0f}m) — refusing to trade stale data"
        )

    report = ValidationReport(
        timeframe=timeframe,
        accepted=len(cleaned),
        dropped_forming=dropped_forming,
        dropped_duplicate=dropped_duplicate,
        dropped_invalid=dropped_invalid,
        gaps=gaps,
        newest_close=newest_close,
        age_minutes=age_minutes,
    )
    return cleaned, report


def validate_spread(
    *,
    spread: float,
    atr: float,
    stop_distance: float,
    take_profit_distance: float,
    max_spread_atr_fraction: float,
    max_spread_tp_fraction: float,
) -> tuple[bool, str | None]:
    """Execution-condition gate (MASTER_MISSION §26).

    Spread is judged in three relative terms, never as an absolute pip
    number, because "2 pips" means something different on EURUSD and gold.
    """

    if spread < 0:
        return False, "negative spread reported by broker"
    if atr > 0 and spread > atr * max_spread_atr_fraction:
        return False, (
            f"spread {spread:.6f} is {spread / atr:.1%} of ATR "
            f"(limit {max_spread_atr_fraction:.0%})"
        )
    if stop_distance > 0 and spread > stop_distance * 0.25:
        return False, (
            f"spread {spread:.6f} is {spread / stop_distance:.1%} of the stop distance (limit 25%)"
        )
    if take_profit_distance > 0 and spread > take_profit_distance * max_spread_tp_fraction:
        return False, (
            f"spread {spread:.6f} is {spread / take_profit_distance:.1%} of the target distance "
            f"(limit {max_spread_tp_fraction:.0%})"
        )
    return True, None
