"""Market-data validation: the guard against trading on bad or future data."""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.errors import MarketDataError, StaleDataError
from bot.marketdata.candles import Candle
from bot.marketdata.provider import MarketDataProvider
from bot.marketdata.validation import validate_series, validate_spread
from fakes import BASE_TIME, DEFAULT_SPEC, SETUP_END, bullish_setup_m15, candle


def m15_run(count: int, *, end=SETUP_END, price: float = 1.1000) -> list[Candle]:
    return [
        candle(
            end - timedelta(minutes=15 * (count - index)),
            price,
            price + 0.0005,
            price - 0.0005,
            price + 0.0001,
        )
        for index in range(count)
    ]


def test_the_forming_candle_is_removed():
    """Deciding on an unclosed candle is the classic repainting bug."""

    candles = m15_run(80)
    forming = candle(SETUP_END, 1.1, 1.2, 1.0, 1.15)  # closes in the future
    clean, report = validate_series(
        [*candles, forming], timeframe="M15", min_candles=60, now=SETUP_END + timedelta(minutes=3)
    )
    assert report.dropped_forming == 1
    assert forming not in clean


def test_duplicate_candles_are_dropped():
    candles = m15_run(80)
    clean, report = validate_series(
        [*candles, candles[10]], timeframe="M15", min_candles=60, now=SETUP_END
    )
    assert report.dropped_duplicate == 1
    assert len(clean) == 80


def test_candles_are_returned_in_chronological_order():
    candles = m15_run(80)
    shuffled = list(reversed(candles))
    clean, _ = validate_series(shuffled, timeframe="M15", min_candles=60, now=SETUP_END)
    assert [item.timestamp for item in clean] == sorted(item.timestamp for item in clean)


def test_stale_data_is_refused():
    candles = m15_run(80, end=SETUP_END - timedelta(hours=4))
    with pytest.raises(StaleDataError, match="refusing to trade stale data"):
        validate_series(candles, timeframe="M15", min_candles=60, now=SETUP_END)


def test_too_few_candles_is_refused():
    with pytest.raises(MarketDataError, match="usable closed candles"):
        validate_series(m15_run(20), timeframe="M15", min_candles=60, now=SETUP_END)


def test_a_series_full_of_holes_is_refused():
    candles = [c for index, c in enumerate(m15_run(160)) if index % 3 != 0]
    with pytest.raises(MarketDataError, match="unexplained gaps"):
        validate_series(candles, timeframe="M15", min_candles=60, now=SETUP_END)


def test_the_weekend_gap_is_not_counted_as_a_hole():
    friday = BASE_TIME.replace(year=2026, month=9, day=11, hour=20, minute=0)
    monday = BASE_TIME.replace(year=2026, month=9, day=14, hour=8, minute=0)
    before = [
        candle(friday - timedelta(minutes=15 * (40 - i)), 1.1, 1.1005, 1.0995, 1.1001)
        for i in range(40)
    ]
    after = [
        candle(monday + timedelta(minutes=15 * i), 1.1, 1.1005, 1.0995, 1.1001) for i in range(40)
    ]
    _, report = validate_series(
        before + after, timeframe="M15", min_candles=60, now=monday + timedelta(minutes=615)
    )
    assert report.gaps == 0


@pytest.mark.parametrize(
    "row",
    [
        {"open": 1.1, "high": 1.09, "low": 1.08, "close": 1.095},   # high below close
        {"open": 1.1, "high": 1.12, "low": 1.11, "close": 1.105},   # low above open
        {"open": -1.0, "high": 1.0, "low": -2.0, "close": 0.5},     # negative price
        {"open": 1.1, "high": float("nan"), "low": 1.0, "close": 1.05},
    ],
)
def test_malformed_ohlc_is_rejected_at_the_type_boundary(row):
    with pytest.raises(ValueError):
        Candle.from_mapping({"timestamp": BASE_TIME.isoformat(), "timeframe": "M15", **row})


def test_close_time_is_open_time_plus_the_interval():
    item = candle(BASE_TIME, 1.1, 1.11, 1.09, 1.10, "H4")
    assert item.close_time == BASE_TIME + timedelta(hours=4)


# -- spread ---------------------------------------------------------------


def test_a_normal_spread_passes():
    ok, reason = validate_spread(
        spread=0.00008,
        atr=0.0012,
        stop_distance=0.0030,
        take_profit_distance=0.0060,
        max_spread_atr_fraction=0.12,
        max_spread_tp_fraction=0.05,
    )
    assert ok and reason is None


def test_a_wide_spread_is_rejected_relative_to_volatility():
    ok, reason = validate_spread(
        spread=0.0010,
        atr=0.0012,
        stop_distance=0.0030,
        take_profit_distance=0.0060,
        max_spread_atr_fraction=0.12,
        max_spread_tp_fraction=0.05,
    )
    assert not ok and "of ATR" in reason


def test_a_spread_that_eats_the_stop_is_rejected_even_in_high_volatility():
    """Judged in three relative terms, so a wide-ATR market cannot excuse a
    spread that is a quarter of the risk being taken."""

    ok, reason = validate_spread(
        spread=0.0009,
        atr=0.020,                 # ATR is huge, so the ATR test passes
        stop_distance=0.0030,
        take_profit_distance=0.0060,
        max_spread_atr_fraction=0.12,
        max_spread_tp_fraction=0.05,
    )
    assert not ok and "of the stop distance" in reason


def test_a_negative_spread_is_rejected():
    ok, reason = validate_spread(
        spread=-0.0001, atr=0.001, stop_distance=0.003, take_profit_distance=0.006,
        max_spread_atr_fraction=0.12, max_spread_tp_fraction=0.05,
    )
    assert not ok and "negative spread" in reason


# -- provider -------------------------------------------------------------


def test_provider_validates_and_caches(config, broker):
    provider = MarketDataProvider(broker, config)
    first = provider.series(DEFAULT_SPEC, "M15", now=SETUP_END)
    assert first.report.accepted >= config.smc.min_candles
    broker.set_series("EURUSD", "M15", [])  # cache must serve this
    second = provider.series(DEFAULT_SPEC, "M15", now=SETUP_END)
    assert second is first
    provider.invalidate("EURUSD")
    with pytest.raises(MarketDataError, match="no M15 candles"):
        provider.series(DEFAULT_SPEC, "M15", now=SETUP_END)


def test_multi_timeframe_fails_as_a_unit(config, broker):
    """Partial context is worse than none: a fresh M15 read against a
    stale H1 bias would look like a valid setup."""

    provider = MarketDataProvider(broker, config)
    broker.set_series("EURUSD", "H1", [])
    with pytest.raises(MarketDataError):
        provider.multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
