"""One clock for the whole system.

Every module takes `now` as an injectable parameter and defaults to
`utc_now()`. Tests pin time explicitly; nothing calls datetime.now()
directly, which is what makes session/news/limit logic testable.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; convert an aware one to UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_epoch_seconds(value: datetime) -> float:
    return ensure_utc(value).timestamp()


def from_epoch(value: float) -> datetime:
    """Accept epoch seconds or milliseconds (brokers mix both)."""

    seconds = value / 1000.0 if abs(value) > 10**11 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def trading_day(value: datetime) -> str:
    """The UTC calendar day key used for daily loss / trade-count limits."""

    return ensure_utc(value).strftime("%Y-%m-%d")
