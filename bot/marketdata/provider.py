"""Market-data provider.

Broker-native data is the ONLY source used for decisions. The previous
build analysed Yahoo Finance candles and then submitted the resulting
stop/target prices to TradeLocker — two different price series, so every
level was subtly wrong and could be rejected or filled at a price the
analysis never saw.

A short TTL cache exists because one scan needs H4/H1/M15 for several
symbols and the broker is rate limited; the TTL is always shorter than
the timeframe it serves, so a cache hit can never hide a new closed
candle for long.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from ..broker.models import InstrumentSpec
from ..config import TradingConfig
from ..errors import MarketDataError
from ..observability import log_event
from .candles import Candle, TIMEFRAME_MINUTES, to_candles
from .validation import ValidationReport, validate_series


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

    def _ttl(self, timeframe: str) -> float:
        """Cache for a fraction of one candle: never long enough to miss a close."""

        return max(20.0, TIMEFRAME_MINUTES[timeframe.upper()] * 60 * 0.2)

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
            if cached and not force and (time.monotonic() - cached[0]) < self._ttl(timeframe):
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
            self._cache[key] = (time.monotonic(), series)
        return series

    def multi_timeframe(
        self,
        spec: InstrumentSpec,
        timeframes: tuple[str, ...] = ("H4", "H1", "M15"),
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
            count = 400 if timeframe == "M15" else 300
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
                    "ageSeconds": round(time.monotonic() - stamp, 1),
                    "candles": series.report.accepted,
                    "newestClose": series.report.newest_close.isoformat()
                    if series.report.newest_close
                    else None,
                }
                for (symbol, timeframe), (stamp, series) in self._cache.items()
            }
        return {"cached": entries}
