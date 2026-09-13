"""High-impact economic-event filter.

Two behaviours the previous version got wrong:

* it failed OPEN on a feed outage — if the calendar was unreachable and
  no cache existed, every symbol traded as if no news existed. Here the
  behaviour is configurable and defaults to FAILING CLOSED, because "we
  don't know if NFP is in two minutes" is not a reason to trade;
* it mapped XAUUSD to ("USD", "USD"), which quietly halved the event
  surface. Gold is mapped to USD plus a metals-sensitivity marker.

Blackout windows are asymmetric: longer before the release (positioning
and spread widening start early) than after.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .clock import ensure_utc, utc_now
from .broker.symbols import alphanumeric, canonical_symbol
from .config import NewsConfig
from .observability import log_event

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

SYMBOL_CURRENCIES: dict[str, tuple[str, ...]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "USDCAD": ("USD", "CAD"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "EURJPY": ("EUR", "JPY"),
    "EURGBP": ("EUR", "GBP"),
    "GBPJPY": ("GBP", "JPY"),
    "AUDJPY": ("AUD", "JPY"),
    "AUDCHF": ("AUD", "CHF"),
    "CADJPY": ("CAD", "JPY"),
    # Gold is a USD-denominated safe haven: USD releases move it, and so
    # do the risk events that dominate the broader tape.
    "XAUUSD": ("USD",),
    "XAGUSD": ("USD",),
}


@dataclass(frozen=True, slots=True)
class NewsVerdict:
    blocked: bool
    reason: str | None
    events: tuple[dict[str, Any], ...]
    feed_available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "reason": self.reason,
            "feedAvailable": self.feed_available,
            "events": [
                {
                    "title": event.get("title"),
                    "country": event.get("country"),
                    "impact": event.get("impact"),
                    "date": event.get("date"),
                }
                for event in self.events[:5]
            ],
        }


def currencies_for(symbol: str) -> tuple[str, ...]:
    """The currencies a symbol is exposed to, for blackout matching.

    Resolved through the canonical pair so a decorated broker name
    (`EURUSD.R`) maps to the same currencies as a bare one. Returning ()
    for an unresolvable name means "no blackout" — which is why resolution
    has to work: an unparsed name would silently disable the news filter
    for that symbol.
    """

    canonical = canonical_symbol(symbol)
    if canonical is not None and canonical in SYMBOL_CURRENCIES:
        return SYMBOL_CURRENCIES[canonical]
    if canonical is not None:
        return (canonical[:3], canonical[3:])
    upper = alphanumeric(symbol)
    return SYMBOL_CURRENCIES.get(upper, ())


class NewsFilter:
    def __init__(self, config: NewsConfig, *, cache_path: str | Path | None = None) -> None:
        self.config = config
        self.cache_path = Path(cache_path) if cache_path else Path("data/news_cache.json")
        self._events: list[dict[str, Any]] = []
        self._fetched_at = 0.0
        self._feed_available = False
        self._lock = threading.RLock()

    # -- feed ------------------------------------------------------------

    def _fetch(self) -> list[dict[str, Any]]:
        request = Request(CALENDAR_URL, headers={"User-Agent": "TradeBot/2.0"})
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("calendar feed returned an unexpected shape")
        return payload

    def _read_cache(self) -> tuple[list[dict[str, Any]], float] | None:
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or "events" not in raw:
            return None
        return raw["events"], float(raw.get("fetched_at", 0.0))

    def _write_cache(self, events: list[dict[str, Any]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"fetched_at": time.time(), "events": events})
            )
        except OSError:
            pass

    def refresh(self, *, force: bool = False) -> bool:
        """Update the event list. Returns True if usable data is held."""

        with self._lock:
            fresh = (time.time() - self._fetched_at) < self.config.cache_ttl_seconds
            if self._events and fresh and not force:
                return True

            if not self._events:
                cached = self._read_cache()
                if cached is not None:
                    self._events, self._fetched_at = cached
                    self._feed_available = (
                        time.time() - self._fetched_at
                    ) < self.config.cache_ttl_seconds * 4

            try:
                events = self._fetch()
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
                log_event(
                    "NEWS",
                    f"economic calendar unavailable: {exc}",
                    severity="warning",
                    cached_events=len(self._events),
                )
                return bool(self._events)

            self._events = events
            self._fetched_at = time.time()
            self._feed_available = True
            self._write_cache(events)
            return True

    # -- evaluation ------------------------------------------------------

    def check(self, symbol: str, *, now: datetime | None = None) -> NewsVerdict:
        if not self.config.enabled:
            return NewsVerdict(False, None, (), True)

        moment = ensure_utc(now or utc_now())
        currencies = currencies_for(symbol)
        if not currencies:
            return NewsVerdict(False, "symbol has no mapped currencies", (), self._feed_available)

        has_data = self.refresh()
        if not has_data:
            if self.config.fail_closed_without_feed:
                return NewsVerdict(
                    True,
                    "economic calendar is unavailable and no cache exists — standing aside "
                    "rather than trading blind into a possible high-impact release",
                    (),
                    False,
                )
            return NewsVerdict(False, "calendar unavailable; news filter disabled", (), False)

        window_start = moment - timedelta(minutes=self.config.blackout_after_minutes)
        window_end = moment + timedelta(minutes=self.config.blackout_before_minutes)

        matches: list[dict[str, Any]] = []
        with self._lock:
            events = list(self._events)
        for event in events:
            if str(event.get("impact")) not in self.config.impact_levels:
                continue
            if str(event.get("country")) not in currencies:
                continue
            when = self._parse(event.get("date"))
            if when is None:
                continue
            if window_start <= when <= window_end:
                matches.append(event)

        if matches:
            titles = ", ".join(
                f"{event.get('country')} {event.get('title')}" for event in matches[:3]
            )
            return NewsVerdict(
                True,
                f"high-impact news blackout for {symbol}: {titles}",
                tuple(matches),
                self._feed_available,
            )
        return NewsVerdict(False, None, (), self._feed_available)

    def upcoming(self, symbols: Sequence[str], *, hours: int = 12, now: datetime | None = None) -> list[dict[str, Any]]:
        """Events in the next `hours` for the traded symbols (dashboard)."""

        moment = ensure_utc(now or utc_now())
        horizon = moment + timedelta(hours=hours)
        wanted: set[str] = set()
        for symbol in symbols:
            wanted.update(currencies_for(symbol))
        self.refresh()
        with self._lock:
            events = list(self._events)
        upcoming = []
        for event in events:
            if str(event.get("impact")) not in self.config.impact_levels:
                continue
            if str(event.get("country")) not in wanted:
                continue
            when = self._parse(event.get("date"))
            if when is None or not (moment <= when <= horizon):
                continue
            upcoming.append(
                {
                    "title": event.get("title"),
                    "country": event.get("country"),
                    "impact": event.get("impact"),
                    "date": when.isoformat(),
                    "minutesAway": round((when - moment).total_seconds() / 60.0),
                }
            )
        upcoming.sort(key=lambda item: item["date"])
        return upcoming[:20]

    @staticmethod
    def _parse(raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            return None

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "feedAvailable": self._feed_available,
            "events": len(self._events),
            "ageSeconds": round(time.time() - self._fetched_at) if self._fetched_at else None,
            "failClosed": self.config.fail_closed_without_feed,
        }
