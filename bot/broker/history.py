"""Broker candle history: endpoint discovery, decoding, and paging.

Why this module exists as its own unit: TradeLocker deployments do not all
expose price history the same way. The path, the time unit of the `from`/`to`
bounds, and the envelope key of the returned bars all vary between brokers
running on TradeLocker's backend. The previous version hard-coded one guess,
so a broker that used any other shape produced "no candles" and the bot
silently never traded.

Instead of guessing, this probes a small matrix of known-good shapes once
per process, remembers the one that worked, and reuses it. Discovery is
logged so the working shape is visible in the deployment's own logs, and
`bot.doctor` prints it explicitly.

Decoding is deliberately permissive about field NAMES and strict about
field VALUES: a bar missing a price or carrying a non-finite one is dropped,
never defaulted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..clock import from_epoch, utc_now
from ..errors import BrokerError, BrokerRejected
from ..observability import log_event

#: Timeframe -> the resolution string TradeLocker expects.
RESOLUTIONS = {"M1": "1m", "M5": "5m", "M15": "15m", "H1": "1H", "H4": "4H", "D1": "1D"}

#: Some deployments want uppercase minute codes ("15M") instead of "15m".
ALT_RESOLUTIONS = {"M1": "1M", "M5": "5M", "M15": "15M", "H1": "1H", "H4": "4H", "D1": "1D"}

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}

#: Keys the bar array can arrive under.
BAR_KEYS = ("barDetails", "bars", "candles", "history", "d", "data")


@dataclass(frozen=True, slots=True)
class HistoryStrategy:
    """One concrete way of asking a TradeLocker backend for candles."""

    name: str
    path: str
    time_unit: str          # "ms" | "s"
    resolution_map: str     # "lower" | "upper"
    instrument_param: str
    route_param: str

    def resolution_for(self, timeframe: str) -> str:
        table = RESOLUTIONS if self.resolution_map == "lower" else ALT_RESOLUTIONS
        resolution = table.get(timeframe.upper())
        if resolution is None:
            raise BrokerError(f"unsupported timeframe {timeframe!r}")
        return resolution

    def bounds(self, start: datetime, end: datetime) -> tuple[int, int]:
        scale = 1000 if self.time_unit == "ms" else 1
        return int(start.timestamp() * scale), int(end.timestamp() * scale)


#: Ordered most-likely-first. The first shape is TradeLocker's documented
#: one; the rest cover variants seen on broker deployments of the same
#: backend. Probing stops at the first that returns usable bars.
STRATEGIES: tuple[HistoryStrategy, ...] = (
    HistoryStrategy("documented", "/trade/history", "ms", "lower", "tradableInstrumentId", "routeId"),
    HistoryStrategy("documented-upper", "/trade/history", "ms", "upper", "tradableInstrumentId", "routeId"),
    HistoryStrategy("seconds-bounds", "/trade/history", "s", "lower", "tradableInstrumentId", "routeId"),
    HistoryStrategy("quotes-history", "/trade/quotes/history", "ms", "lower", "tradableInstrumentId", "routeId"),
    HistoryStrategy("bars", "/trade/bars", "ms", "lower", "tradableInstrumentId", "routeId"),
    HistoryStrategy("instrument-id", "/trade/history", "ms", "lower", "instrumentId", "routeId"),
)


def _num(value: Any) -> float | None:
    """Strict numeric parse: None for anything not a finite number."""

    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _extract_bars(payload: Any) -> list[Any]:
    """Find the bar array wherever the deployment put it."""

    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for key in BAR_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
        # One extra level of nesting, e.g. {"d": {"barDetails": [...]}}.
        if isinstance(value, Mapping):
            for inner in BAR_KEYS:
                nested = value.get(inner)
                if isinstance(nested, list) and nested:
                    return nested
    return []


def decode_bars(payload: Any, timeframe: str) -> list[dict[str, Any]]:
    """Turn a raw history response into candle dicts.

    Accepts both object bars (`{"t":…,"o":…}`) and positional array bars
    (`[t, o, h, l, c, v]`). Anything whose OHLC cannot be read as finite
    positive numbers is dropped rather than defaulted — a fabricated price
    would flow straight into a stop-loss calculation.
    """

    timeframe = timeframe.upper()
    rows: list[dict[str, Any]] = []

    for bar in _extract_bars(payload):
        if isinstance(bar, Mapping):
            raw_time = _first(bar, "t", "time", "timestamp", "date", "ts")
            values = [
                _num(_first(bar, "o", "open", "openPrice")),
                _num(_first(bar, "h", "high", "highPrice")),
                _num(_first(bar, "l", "low", "lowPrice")),
                _num(_first(bar, "c", "close", "closePrice")),
            ]
            volume = _num(_first(bar, "v", "volume", "tickVolume")) or 0.0
        elif isinstance(bar, (list, tuple)) and len(bar) >= 5:
            raw_time = bar[0]
            values = [_num(bar[1]), _num(bar[2]), _num(bar[3]), _num(bar[4])]
            volume = (_num(bar[5]) if len(bar) > 5 else 0.0) or 0.0
        else:
            continue

        if raw_time is None or any(value is None for value in values):
            continue
        open_price, high, low, close = values  # type: ignore[misc]
        if min(open_price, high, low, close) <= 0:
            continue
        # Clamp rather than discard a bar whose wick is inconsistent by a
        # rounding tick: brokers occasionally report high slightly below a
        # close. A genuinely broken bar still fails Candle.from_mapping.
        high = max(high, open_price, close)
        low = min(low, open_price, close)

        stamp = _num(raw_time)
        if stamp is None:
            continue
        rows.append(
            {
                "timestamp": from_epoch(stamp).isoformat(),
                "timeframe": timeframe,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    rows.sort(key=lambda row: row["timestamp"])
    # De-duplicate on timestamp, keeping the last occurrence: a paged fetch
    # overlaps at the boundaries by design.
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped[row["timestamp"]] = row
    return [deduped[key] for key in sorted(deduped)]


class HistoryFetcher:
    """Fetches candles, discovering the working endpoint shape once."""

    def __init__(self, request: Callable[[str, Mapping[str, Any]], Any]) -> None:
        self._request = request
        self._strategy: HistoryStrategy | None = None
        self._lock = threading.RLock()
        self.attempts: list[dict[str, Any]] = []

    @property
    def strategy(self) -> HistoryStrategy | None:
        return self._strategy

    def reset(self) -> None:
        with self._lock:
            self._strategy = None

    def _span(self, timeframe: str, count: int) -> timedelta:
        """Wall-clock window to request for `count` candles.

        Over-fetched deliberately: FX closes at weekends and holidays, so
        elapsed time is a poor proxy for candle count. Under-fetching here
        is how a series silently arrives too short to analyse.
        """

        minutes = TIMEFRAME_MINUTES[timeframe.upper()]
        return timedelta(minutes=minutes * count * 2.4)

    def fetch(
        self,
        *,
        instrument_id: int,
        route_id: int,
        timeframe: str,
        count: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        end = now or utc_now()
        start = end - self._span(timeframe, count)

        with self._lock:
            known = self._strategy

        if known is not None:
            rows = self._try(known, instrument_id, route_id, timeframe, start, end)
            if rows:
                return rows[-count:]
            # The learned shape stopped working (broker upgrade, or the
            # instrument genuinely has no history). Re-discover once rather
            # than failing permanently.
            log_event(
                "DATA",
                f"history strategy {known.name!r} returned nothing; re-probing",
                severity="warning",
                timeframe=timeframe,
            )
            self.reset()

        return self._discover(instrument_id, route_id, timeframe, start, end, count)

    def _discover(
        self,
        instrument_id: int,
        route_id: int,
        timeframe: str,
        start: datetime,
        end: datetime,
        count: int,
    ) -> list[dict[str, Any]]:
        self.attempts = []
        errors: list[str] = []

        for strategy in STRATEGIES:
            try:
                rows = self._try(strategy, instrument_id, route_id, timeframe, start, end)
            except BrokerRejected as exc:
                # A 4xx means this shape is wrong for this deployment, which
                # is exactly what probing is for. Keep going.
                self.attempts.append({"strategy": strategy.name, "ok": False, "error": str(exc)[:160]})
                errors.append(f"{strategy.name}: {exc}")
                continue
            except BrokerError as exc:
                # A transport or 5xx failure is NOT evidence about the shape.
                # Abort discovery so a broker outage is not misread as "no
                # endpoint works" and cached as such.
                raise BrokerError(
                    f"history probe aborted on a transport failure ({exc}); "
                    "the endpoint shape is still unknown"
                ) from exc

            self.attempts.append({"strategy": strategy.name, "ok": bool(rows), "bars": len(rows)})
            if rows:
                with self._lock:
                    self._strategy = strategy
                log_event(
                    "DATA",
                    f"history endpoint discovered: {strategy.name}",
                    path=strategy.path,
                    time_unit=strategy.time_unit,
                    resolution=strategy.resolution_for(timeframe),
                    bars=len(rows),
                    timeframe=timeframe,
                )
                return rows[-count:]

        raise BrokerError(
            "no known TradeLocker history endpoint shape returned candles for "
            f"instrument {instrument_id} / {timeframe}. Tried: "
            + "; ".join(attempt["strategy"] for attempt in self.attempts)
            + (f". Errors: {' | '.join(errors[:3])}" if errors else "")
        )

    def _try(
        self,
        strategy: HistoryStrategy,
        instrument_id: int,
        route_id: int,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        from_bound, to_bound = strategy.bounds(start, end)
        payload = self._request(
            strategy.path,
            {
                strategy.instrument_param: instrument_id,
                strategy.route_param: route_id,
                "resolution": strategy.resolution_for(timeframe),
                "from": from_bound,
                "to": to_bound,
            },
        )
        return decode_bars(payload, timeframe)

    def describe(self) -> dict[str, Any]:
        strategy = self._strategy
        return {
            "discovered": strategy.name if strategy else None,
            "path": strategy.path if strategy else None,
            "timeUnit": strategy.time_unit if strategy else None,
            "resolutionCase": strategy.resolution_map if strategy else None,
            "probeAttempts": list(self.attempts),
        }
