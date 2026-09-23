"""Market-data provider.

Broker-native data is the ONLY source used for decisions. The previous
build analysed Yahoo Finance candles and then submitted the resulting
stop/target prices to TradeLocker — two different price series, so every
level was subtly wrong and could be rejected or filled at a price the
analysis never saw.

A cache exists because one scan needs H1/M15 for several symbols and
the broker is rate limited. It holds each series until the bar that could
CHANGE it actually closes, which is a fact rather than a guess:
`validate_series` removes the forming candle, so a series is closed bars
only, and closed bars do not move. Re-fetching one before its next close
returns different bytes and identical analysis input.

The old policy was a fixed fraction of the bar (0.2), which re-fetched
every series five times per bar and threw four of those away. Across 23
symbols that was the difference between 53 and 30 history requests per
scan, and Cloudflare answered 1015 to the difference.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from ..broker.models import InstrumentSpec
from ..clock import ensure_utc, utc_now
from ..config import TradingConfig
from ..errors import MarketDataError
from ..observability import log_event
from .candles import Candle, TIMEFRAME_MINUTES, to_candles
from .validation import ValidationReport, validate_series

#: Never re-fetch the same series faster than this, however close the next
#: bar is. Several symbols share one scan and would otherwise stampede the
#: same endpoint within a second of each other.
MIN_CACHE_SECONDS = 20.0

#: Brokers publish a closed bar a moment after its close. Expiring exactly
#: on the boundary spends a request to be told what we already knew.
PUBLISH_GRACE_SECONDS = 5.0


#: How many closed bars the LIVE engine sees per timeframe.
#:
#: Named and exported because the backtester must show the engine the
#: same window. It did not: it passed the entire history up to bar i, so
#: on bar 200,000 the SMC engine analysed 200,000 candles while the live
#: bot analyses 400. That is not only O(n^2) — a ten-year run never
#: finished one symbol — it is a different computation. The liquidity
#: map, the dealing range and the swing structure are all built from the
#: window they are given, so a backtest fed the whole history picked
#: targets from pools the live bot cannot see and measured a bot that
#: never traded.
LIVE_LOOKBACK: dict[str, int] = {"M15": 400, "H1": 300}
DEFAULT_LOOKBACK = 300


def live_lookback(timeframe: str) -> int:
    return LIVE_LOOKBACK.get(timeframe.upper(), DEFAULT_LOOKBACK)


@dataclass(frozen=True, slots=True)
class Series:
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    report: ValidationReport

    @property
    def latest(self) -> Candle:
        return self.candles[-1]


class MarketDataProvider:
    def __init__(self, broker: Any, config: TradingConfig) -> None:
        self.broker = broker
        self.config = config
        self._cache: dict[tuple[str, str], tuple[float, Series]] = {}
        self._lock = threading.RLock()

    def _seconds_until_stale(
        self, series: "Series", timeframe: str, now: datetime | None = None
    ) -> float:
        """How long this series is still the whole truth.

        Derived from the data, not from a calendar: the newest CLOSED bar
        plus one bar duration is exactly when the next one closes, so this
        needs no assumption about where session boundaries fall — which
        matters for H1, where "the next whole hour" stops being the answer
        the moment a broker's session opens on the half hour.

        Bounded on both sides. Never longer than one bar, so a broken
        timestamp cannot pin a stale series in memory; never shorter than
        `MIN_CACHE_SECONDS`, so the several symbols sharing one scan do
        not each re-fetch the same series seconds apart.
        """

        bar_seconds = TIMEFRAME_MINUTES[timeframe.upper()] * 60
        if not series.candles:
            return MIN_CACHE_SECONDS
        moment = ensure_utc(now) if now is not None else utc_now()
        next_close = series.candles[-1].close_time + timedelta(seconds=bar_seconds)
        remaining = (next_close - moment).total_seconds() + PUBLISH_GRACE_SECONDS
        return max(MIN_CACHE_SECONDS, min(float(bar_seconds), remaining))

    def series(
        self,
        spec: InstrumentSpec,
        timeframe: str,
        *,
        count: int = 300,
        now: datetime | None = None,
        force: bool = False,
    ) -> Series:
        timeframe = timeframe.upper()
        key = (spec.symbol, timeframe)
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force and time.monotonic() < cached[0]:
                return cached[1]

        raw = self.broker.candles(spec, timeframe, count=count)
        if not raw:
            raise MarketDataError(f"broker returned no {timeframe} candles for {spec.symbol}")
        try:
            parsed = to_candles(raw)
        except ValueError as exc:
            raise MarketDataError(f"{spec.symbol} {timeframe}: malformed candle data — {exc}") from exc

        candles, report = validate_series(
            parsed,
            timeframe=timeframe,
            min_candles=self.config.smc.min_candles,
            now=now,
        )
        series = Series(
            symbol=spec.symbol, timeframe=timeframe, candles=tuple(candles), report=report
        )
        with self._lock:
            # An EXPIRY, not a fetch time: how long the answer stays the
            # answer depends on where in the bar we are, which a fixed TTL
            # measured from the fetch cannot express.
            self._cache[key] = (
                time.monotonic() + self._seconds_until_stale(series, timeframe, now),
                series,
            )
        return series

    def multi_timeframe(
        self,
        spec: InstrumentSpec,
        timeframes: tuple[str, ...] = ("H1", "M15"),
        *,
        now: datetime | None = None,
    ) -> dict[str, Series]:
        """Fetch all timeframes for one symbol, failing as a unit.

        Partial multi-timeframe data is refused: an H1 bias derived from a
        stale series combined with a fresh M15 entry is worse than no
        signal at all.
        """

        result: dict[str, Series] = {}
        for timeframe in timeframes:
            count = live_lookback(timeframe)
            result[timeframe] = self.series(spec, timeframe, count=count, now=now)
        return result

    def invalidate(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == symbol]:
                    self._cache.pop(key, None)

    def health(self) -> dict[str, Any]:
        with self._lock:
            entries = {
                f"{symbol}:{timeframe}": {
                    # The cache stores an EXPIRY now, so report what an
                    # operator can actually act on: how long this series
                    # stays usable, not how long ago it was fetched.
                    "usableForSeconds": round(max(0.0, expires_at - time.monotonic()), 1),
                    "candles": series.report.accepted,
                    "newestClose": series.report.newest_close.isoformat()
                    if series.report.newest_close
                    else None,
                }
                for (symbol, timeframe), (expires_at, series) in self._cache.items()
            }
        return {"cached": entries}
