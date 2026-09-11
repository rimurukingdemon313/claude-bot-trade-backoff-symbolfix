"""Free live market-data adapter for the paper-trading SMC loop."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

# Symbol -> Yahoo Finance ticker. Add pairs here; everything else in the
# system reads from this map, so this is the single source of truth.
SUPPORTED_SYMBOLS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "AUDJPY": "AUDJPY=X",
    "AUDCHF": "AUDCHF=X",
    "XAUUSD": "XAUUSD=X",
}

# Timeframe label -> (Yahoo interval, Yahoo range, minutes-per-candle for
# closed-candle alignment, minimum candles required for a usable analysis).
TIMEFRAME_SPECS: dict[str, dict[str, Any]] = {
    "M15": {"interval": "15m", "range": "5d", "minutes": 15, "min_candles": 7},
    "H1": {"interval": "60m", "range": "1mo", "minutes": 60, "min_candles": 7},
    "H4": {"interval": "60m", "range": "3mo", "minutes": 240, "min_candles": 7},
}


class MarketDataError(RuntimeError):
    """Raised when the live source cannot provide valid candles."""


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _resample_candles(candles: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Aggregate finer candles (e.g. H1) into coarser buckets (e.g. H4).

    Yahoo does not serve a native 4-hour interval for FX, so H4 candles are
    built by grouping consecutive H1 candles into 4-hour UTC-aligned buckets.
    """

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for candle in candles:
        timestamp = datetime.fromisoformat(candle["timestamp"])
        bucket_hour = (timestamp.hour // 4) * 4
        bucket_key = timestamp.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket_key, []).append(candle)

    resampled: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        members = buckets[bucket_start]
        resampled.append(
            {
                "timestamp": bucket_start.isoformat(),
                "timeframe": "H4",
                "open": members[0]["open"],
                "high": max(member["high"] for member in members),
                "low": min(member["low"] for member in members),
                "close": members[-1]["close"],
                "volume": sum(member.get("volume", 0.0) or 0.0 for member in members),
            }
        )
    return resampled


def parse_yahoo_chart_payload(
    payload: Mapping[str, Any],
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    closed_only: bool = False,
) -> list[dict[str, Any]]:
    """Convert Yahoo's chart response into SMC-engine candle mappings."""

    timeframe = timeframe.upper()
    spec = TIMEFRAME_SPECS.get(timeframe)
    if spec is None:
        supported = ", ".join(sorted(TIMEFRAME_SPECS))
        raise MarketDataError(f"unsupported timeframe {timeframe!r}; supported: {supported}")

    chart = payload.get("chart")
    result = chart.get("result") if isinstance(chart, Mapping) else None
    if not isinstance(result, list) or not result or not isinstance(result[0], Mapping):
        error = chart.get("error") if isinstance(chart, Mapping) else None
        raise MarketDataError(f"Yahoo returned no chart data for {symbol}: {error or 'empty result'}")

    chart_result = result[0]
    timestamps = chart_result.get("timestamp")
    indicators = chart_result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, Mapping) else None
    quote = quotes[0] if isinstance(quotes, list) and quotes and isinstance(quotes[0], Mapping) else None
    if not isinstance(timestamps, list) or quote is None:
        raise MarketDataError("Yahoo response is missing timestamps or OHLCV quote data")

    # H4 candles are resampled from raw H1 data, so alignment/labelling below
    # always targets the *fetched* interval (H1), not the requested one.
    fetch_timeframe = "H1" if timeframe == "H4" else timeframe
    fetch_minutes = TIMEFRAME_SPECS[fetch_timeframe]["minutes"]

    candles: list[dict[str, Any]] = []
    fields = ("open", "high", "low", "close")
    for index, timestamp in enumerate(timestamps):
        values = {field: quote.get(field, [None] * len(timestamps))[index] for field in fields}
        numbers = {field: _finite_number(value) for field, value in values.items()}
        if any(value is None for value in numbers.values()):
            continue
        try:
            timestamp_value = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketDataError("Yahoo returned an invalid candle timestamp") from exc
        if closed_only and fetch_minutes < 60 and (
            timestamp_value.minute % fetch_minutes != 0
            or timestamp_value.second != 0
            or timestamp_value.microsecond != 0
        ):
            continue
        volume_values = quote.get("volume", [])
        volume = _finite_number(volume_values[index]) if index < len(volume_values) else None
        candles.append(
            {
                "timestamp": timestamp_value.isoformat(),
                "timeframe": fetch_timeframe,
                "open": numbers["open"],
                "high": numbers["high"],
                "low": numbers["low"],
                "close": numbers["close"],
                # Yahoo reports zero/null volume for some FX feeds; preserve
                # the feed value as zero rather than inventing tick volume.
                "volume": volume if volume is not None else 0.0,
            }
        )

    if timeframe == "H4":
        candles = _resample_candles(candles, minutes=240)

    if len(candles) < spec["min_candles"]:
        raise MarketDataError(
            f"Yahoo returned only {len(candles)} valid {timeframe} candles; "
            f"at least {spec['min_candles']} are required"
        )
    return candles


def fetch_live_candles(
    symbol: str,
    timeframe: str = "M15",
    *,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """Fetch the latest candles for a supported symbol/timeframe from Yahoo Finance."""

    normalized_symbol = symbol.upper()
    ticker = SUPPORTED_SYMBOLS.get(normalized_symbol)
    if ticker is None:
        supported = ", ".join(sorted(SUPPORTED_SYMBOLS))
        raise MarketDataError(f"unsupported symbol {symbol!r}; supported symbols: {supported}")

    normalized_timeframe = timeframe.upper()
    spec = TIMEFRAME_SPECS.get(normalized_timeframe)
    if spec is None:
        supported = ", ".join(sorted(TIMEFRAME_SPECS))
        raise MarketDataError(f"unsupported timeframe {timeframe!r}; supported: {supported}")

    # H4 has no native Yahoo interval for FX, so it is resampled from H1.
    fetch_interval = "60m" if normalized_timeframe == "H4" else spec["interval"]

    query = urlencode(
        {
            "range": spec["range"],
            "interval": fetch_interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
    )
    request = Request(
        f"{YAHOO_CHART_URL}/{ticker}?{query}",
        headers={"User-Agent": "FieldworkPaperTrading/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise MarketDataError(f"Yahoo HTTP error {exc.code} for {normalized_symbol}") from exc
    except URLError as exc:
        raise MarketDataError(f"Yahoo network error for {normalized_symbol}: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise MarketDataError(f"Yahoo returned an unreadable market-data response for {normalized_symbol}") from exc

    return parse_yahoo_chart_payload(
        payload,
        symbol=normalized_symbol,
        timeframe=normalized_timeframe,
        closed_only=True,
    )
