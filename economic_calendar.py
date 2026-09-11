"""Real high-impact economic-news filter (ForexFactory calendar feed).

This is a genuine news filter, not a volatility proxy: it downloads the
public ForexFactory economic calendar and checks whether a High-impact
event for either currency in a pair falls inside a configurable blackout
window around the current time. The source enforces a strict request rate
(2 requests / 5 minutes), so the feed is cached on disk and refreshed at
most once every CACHE_TTL_SECONDS regardless of how often callers ask.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_PATH = Path(__file__).parent / "economic_calendar_cache.json"
CACHE_TTL_SECONDS = 15 * 60  # refresh at most every 15 minutes

# Minutes before/after a High-impact event during which trading is blocked
# for any pair containing that event's currency.
BLACKOUT_BEFORE_MINUTES = 30
BLACKOUT_AFTER_MINUTES = 30

# Which currency each traded symbol is exposed to (for blackout matching).
SYMBOL_CURRENCIES: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "AUDJPY": ("AUD", "JPY"),
    "AUDCHF": ("AUD", "CHF"),
    "XAUUSD": ("USD", "USD"),  # gold trades mainly on USD-driven news
}


class CalendarError(RuntimeError):
    """Raised when the calendar feed cannot be fetched or parsed."""


def _fetch_remote(timeout: int = 15) -> list[dict[str, Any]]:
    request = Request(
        CALENDAR_URL,
        headers={"User-Agent": "FieldworkPaperTrading/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise CalendarError(f"ForexFactory calendar HTTP error {exc.code}") from exc
    except URLError as exc:
        raise CalendarError(f"ForexFactory calendar network error: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise CalendarError("ForexFactory calendar returned unreadable data") from exc
    if not isinstance(payload, list):
        raise CalendarError("ForexFactory calendar returned an unexpected shape")
    return payload


def _load_cache() -> dict[str, Any] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict) or "fetched_at" not in cached or "events" not in cached:
        return None
    return cached


def _save_cache(events: list[dict[str, Any]]) -> None:
    try:
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "events": events})
        )
    except OSError:
        pass  # cache is a best-effort optimization; failing to write is not fatal


def get_calendar_events(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return this week's calendar events, using a 15-minute on-disk cache.

    Falls back to a stale cache (rather than raising) if the remote feed is
    unreachable, so a transient network hiccup never silently disables the
    news filter by throwing an unhandled error up to the caller.
    """

    cached = _load_cache()
    cache_is_fresh = (
        cached is not None and (time.time() - cached["fetched_at"]) < CACHE_TTL_SECONDS
    )
    if cache_is_fresh and not force_refresh:
        return cached["events"]

    try:
        events = _fetch_remote()
        _save_cache(events)
        return events
    except CalendarError:
        if cached is not None:
            return cached["events"]
        raise


def _parse_event_time(raw_date: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw_date)
    except ValueError:
        return None


def upcoming_high_impact_events(
    currencies: tuple[str, ...],
    *,
    now: datetime | None = None,
    before_minutes: int = BLACKOUT_BEFORE_MINUTES,
    after_minutes: int = BLACKOUT_AFTER_MINUTES,
) -> list[dict[str, Any]]:
    """High-impact events for the given currencies inside the blackout window."""

    reference = now or datetime.now(timezone.utc)
    window_start = reference - timedelta(minutes=after_minutes)
    window_end = reference + timedelta(minutes=before_minutes)

    try:
        events = get_calendar_events()
    except CalendarError:
        # If the feed is fully unavailable (no cache either), fail open with
        # an empty list rather than blocking all trading indefinitely on a
        # third-party outage. The volatility-spike check in risk_engine.py
        # remains as a second line of defense either way.
        return []

    matches: list[dict[str, Any]] = []
    for event in events:
        if event.get("impact") != "High":
            continue
        if event.get("country") not in currencies:
            continue
        event_time = _parse_event_time(str(event.get("date", "")))
        if event_time is None:
            continue
        if window_start <= event_time <= window_end:
            matches.append(event)
    return matches


def news_blackout_for_symbol(
    symbol: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """High-impact events currently in the blackout window for a traded symbol."""

    currencies = SYMBOL_CURRENCIES.get(symbol.upper())
    if currencies is None:
        return []
    return upcoming_high_impact_events(currencies, now=now)


def _main() -> None:
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
    events = news_blackout_for_symbol(symbol)
    print(json.dumps({"symbol": symbol.upper(), "blackout_events": events}, indent=2))


if __name__ == "__main__":
    _main()
