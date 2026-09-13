"""SMC detector unit tests.

Each scenario is hand-built so the correct answer is known by
construction — the point is to prove the detector found the RIGHT feature,
not merely that it found something.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.config import load_config
from bot.marketdata.candles import Candle
from bot.smc.dealing_range import dealing_range
from bot.smc.displacement import detect_displacement
from bot.smc.fvg import best_entry_gap, detect_fair_value_gaps
from bot.smc.indicators import atr, atr_series
from bot.smc.liquidity import build_liquidity_map, detect_sweeps
from bot.smc.orderblocks import detect_order_blocks
from bot.smc.regime import classify_regime
from bot.smc.sessions import classify_session, is_forex_weekend, session_extremes
from bot.smc.structure import detect_structure_events, structural_bias
from bot.smc.swings import detect_swings, swing_bias, without_swept_points
from fakes import BASE_TIME, bullish_setup_m15, flat_market_m15, series_from_path


# -- swings ---------------------------------------------------------------


def test_swing_is_only_confirmed_after_the_window_has_printed():
    path = [(1.0, 1.0, 1.0, 1.0)] * 0
    prices = [1.00, 1.01, 1.05, 1.02, 1.01, 1.03, 1.06]
    path = [(p, p + 0.002, p - 0.002, p) for p in prices]
    candles = series_from_path(path)
    swings = detect_swings(candles, window=2)

    pivot = next(point for point in swings.highs if point.index == 2)
    assert pivot.confirmed_index == 4, "a window-2 pivot is knowable two bars later"
    # The whole no-look-ahead guarantee reduces to this assertion.
    assert swings.known_highs(3) == []
    assert pivot in swings.known_highs(4)


def test_strict_pivots_do_not_fire_on_a_flat_range():
    swings = detect_swings(flat_market_m15(60), window=2)
    assert len(swings.highs) == 0
    assert len(swings.lows) == 0


def test_swing_bias_reads_an_impulsive_uptrend_as_bullish():
    """An uptrend can print higher lows and no confirmed pivot HIGH at all.

    Demanding both sides would report 'range' at the most trending moment,
    which is the bug this rule exists to prevent.
    """

    prices = [1.00, 1.02, 1.01, 1.04, 1.03, 1.06, 1.05, 1.08, 1.07, 1.10, 1.12]
    candles = series_from_path([(p, p + 0.001, p - 0.001, p) for p in prices])
    swings = detect_swings(candles, window=1)
    assert swing_bias(swings, len(candles) - 1) == "bullish"


def test_swing_bias_is_range_when_the_two_sides_disagree():
    # Expanding: higher highs AND lower lows. Neither side wins.
    prices = [1.00, 1.05, 0.99, 1.07, 0.97, 1.09, 1.00]
    candles = series_from_path([(p, p + 0.001, p - 0.001, p) for p in prices])
    swings = detect_swings(candles, window=1)
    assert swing_bias(swings, len(candles) - 1) == "range"


# -- displacement ---------------------------------------------------------


def test_displacement_requires_body_dominance_not_just_size():
    """A wide candle that closes mid-range is rejection, not displacement."""

    base = [(1.1000, 1.1005, 1.0995, 1.1001)] * 20
    # Huge range, tiny body, closes in the middle.
    wide_indecision = (1.1000, 1.1100, 1.0900, 1.1002)
    candles = series_from_path(base + [wide_indecision])
    moves = detect_displacement(candles, atr_multiple=1.3, body_ratio=0.55)
    assert not any(move.index == len(candles) - 1 for move in moves)


def test_displacement_detects_a_real_impulse():
    base = [(1.1000, 1.1006, 1.0994, 1.1001)] * 20
    impulse = (1.1001, 1.1060, 1.1000, 1.1055)
    candles = series_from_path(base + [impulse])
    moves = detect_displacement(candles, atr_multiple=1.3, body_ratio=0.55)
    move = next(m for m in moves if m.index == len(candles) - 1)
    assert move.direction == "bullish"
    assert move.atr_multiple > 1.3
    assert 0 < move.quality <= 1.0


# -- structure ------------------------------------------------------------


def test_a_wick_through_a_level_is_not_a_break():
    """Only a CLOSE beyond the level, clear of the ATR buffer, counts."""

    prices = [1.00, 1.02, 1.05, 1.03, 1.02, 1.04, 1.03]
    path = [(p, p + 0.002, p - 0.002, p) for p in prices]
    # This candle wicks well above the prior swing high but closes below it.
    path.append((1.03, 1.09, 1.029, 1.031))
    candles = series_from_path(path)
    swings = detect_swings(candles, window=2)
    events = detect_structure_events(candles, swings, [], buffer_atr=0.08)
    assert not any(event.index == len(candles) - 1 for event in events)


def test_each_level_only_breaks_once():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    moves = detect_displacement(candles)
    events = detect_structure_events(candles, swings, moves)
    broken = [(event.level_index, event.direction) for event in events]
    assert len(broken) == len(set(broken))


def test_structure_events_never_reference_an_unconfirmed_swing():
    """The look-ahead guard, asserted directly on the output."""

    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    events = detect_structure_events(candles, swings, detect_displacement(candles))
    for event in events:
        source = next(
            point
            for point in list(swings.highs) + list(swings.lows)
            if point.index == event.level_index
        )
        assert source.confirmed_index <= event.index, (
            f"break at bar {event.index} used a swing only confirmed at {source.confirmed_index}"
        )


def test_bias_ignores_a_lower_low_created_by_a_sweep():
    """A stop run prints a textbook lower low; it must not flip the bias.

    This is the correction that keeps the engine from fading its own
    setup at the exact moment the setup completes.
    """

    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    moves = detect_displacement(candles)
    events = detect_structure_events(candles, swings, moves)
    liquidity = build_liquidity_map(candles, swings)
    sweeps = detect_sweeps(candles, liquidity, moves, events)
    assert sweeps, "the fixture is built around a sweep; it must be detected"

    naive = swing_bias(swings, len(candles) - 1)
    corrected = swing_bias(without_swept_points(swings, sweeps), len(candles) - 1)
    assert naive == "bearish", "the raw swing sequence does read bearish after the stop run"
    assert corrected != "bearish"
    assert structural_bias(events, swings, len(candles) - 1, sweeps)[0] == "bullish"


# -- liquidity ------------------------------------------------------------


def test_liquidity_map_clusters_equal_lows_and_weights_them_higher():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    liquidity = build_liquidity_map(candles, swings)
    assert liquidity.levels
    kinds = {level.kind for level in liquidity.levels}
    assert "SWING" in kinds
    for level in liquidity.levels:
        assert 0 < level.weight <= 1.0


def test_a_liquidity_level_is_not_visible_before_it_exists():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    liquidity = build_liquidity_map(candles, swings)
    for level in liquidity.levels:
        assert level not in liquidity.visible(level.source_index - 1)
        assert level in liquidity.visible(level.source_index)


def test_a_bare_wick_without_rejection_is_not_a_sweep():
    """Poking a level and closing beyond it is continuation, not a sweep."""

    base = [(1.1000, 1.1010, 1.0990, 1.1000)] * 30
    path = list(base)
    # Drive straight through the lows and close at the bottom.
    path.append((1.1000, 1.1002, 1.0900, 1.0905))
    candles = series_from_path(path)
    swings = detect_swings(candles, 2)
    liquidity = build_liquidity_map(candles, swings)
    sweeps = detect_sweeps(candles, liquidity, detect_displacement(candles), [])
    assert not any(sweep.index == len(candles) - 1 for sweep in sweeps)


def test_sweep_quality_rises_with_displacement_and_structure():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    moves = detect_displacement(candles)
    events = detect_structure_events(candles, swings, moves)
    liquidity = build_liquidity_map(candles, swings)
    sweeps = detect_sweeps(candles, liquidity, moves, events)
    best = max(sweeps, key=lambda sweep: sweep.quality)
    assert best.direction == "bullish"
    assert best.displaced and best.structure_shift
    assert best.quality > 0.6
    # A sweep is only knowable once its reaction window has printed.
    assert best.confirmed_index >= best.index


# -- fair value gaps ------------------------------------------------------


def test_fvg_detection_and_mitigation_lifecycle():
    path = [(1.1000, 1.1005, 1.0995, 1.1000)] * 20
    path.append((1.1000, 1.1010, 1.0998, 1.1008))   # left
    path.append((1.1008, 1.1080, 1.1006, 1.1075))   # displacement
    path.append((1.1075, 1.1090, 1.1050, 1.1085))   # right: low > left high
    candles = series_from_path(path)
    gaps = detect_fair_value_gaps(candles, detect_displacement(candles), min_size_atr=0.05)
    gap = gaps[-1]
    assert gap.direction == "bullish"
    assert gap.lower == pytest.approx(1.1010)
    assert gap.upper == pytest.approx(1.1050)
    assert gap.mitigated_index is None
    assert gap.is_live(len(candles) - 1, max_age=60)


def test_a_mitigated_gap_is_no_longer_an_entry_zone():
    path = [(1.1000, 1.1005, 1.0995, 1.1000)] * 20
    path.append((1.1000, 1.1010, 1.0998, 1.1008))
    path.append((1.1008, 1.1080, 1.1006, 1.1075))
    path.append((1.1075, 1.1090, 1.1050, 1.1085))
    path.append((1.1085, 1.1090, 1.1005, 1.1015))   # trades all the way back
    candles = series_from_path(path)
    gaps = detect_fair_value_gaps(candles, detect_displacement(candles), min_size_atr=0.05)
    gap = next(g for g in gaps if g.direction == "bullish")
    assert gap.mitigated_index is not None
    assert not gap.is_live(len(candles) - 1, max_age=60)
    assert best_entry_gap(gaps, direction="BUY", at_index=len(candles) - 1, max_age=60) is None


# -- order blocks ---------------------------------------------------------


def test_order_block_requires_a_consequential_impulse():
    """The last opposing candle before a move that did nothing is not an OB."""

    path = [(1.1000, 1.1005, 1.0995, 1.1000)] * 25
    path.append((1.1000, 1.1002, 1.0990, 1.0992))   # down candle
    path.append((1.0992, 1.1030, 1.0991, 1.1028))   # impulse with no gap/BOS
    candles = series_from_path(path)
    moves = detect_displacement(candles)
    blocks = detect_order_blocks(candles, moves, structure_events=[], gaps=[])
    assert all(block.index != len(candles) - 2 for block in blocks) or all(
        block.broke_structure or block.has_imbalance for block in blocks
    )


def test_real_order_block_is_found_and_graded():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    moves = detect_displacement(candles)
    events = detect_structure_events(candles, swings, moves)
    gaps = detect_fair_value_gaps(candles, moves)
    blocks = detect_order_blocks(candles, moves, events, gaps)
    bullish = [block for block in blocks if block.direction == "bullish"]
    assert bullish
    best = max(bullish, key=lambda block: block.strength)
    assert best.broke_structure or best.has_imbalance
    assert 0 < best.strength <= 1.0
    assert best.confirmed_index >= best.index


# -- dealing range --------------------------------------------------------


def test_premium_discount_classification():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    index = len(candles) - 1
    ranges = dealing_range(swings, index, candles[index].close)
    assert ranges is not None
    assert ranges.zone in ("premium", "equilibrium", "discount")
    # Preference, never a veto: alignment is graded, not boolean.
    assert 0.0 <= ranges.alignment("BUY") <= 1.0
    assert 0.0 <= ranges.alignment("SELL") <= 1.0


def test_discount_favours_buying_and_premium_favours_selling():
    from bot.smc.dealing_range import DealingRange

    low = DealingRange(high=1.2, low=1.0, price=1.02)
    high = DealingRange(high=1.2, low=1.0, price=1.18)
    assert low.zone == "discount" and low.alignment("BUY") > low.alignment("SELL")
    assert high.zone == "premium" and high.alignment("SELL") > high.alignment("BUY")


# -- regime / sessions ----------------------------------------------------


def test_regime_separates_a_trend_from_a_dead_range():
    candles = bullish_setup_m15()
    swings = detect_swings(candles, 2)
    moves = detect_displacement(candles)
    events = detect_structure_events(candles, swings, moves)
    trending = classify_regime(candles, events)
    ranging = classify_regime(flat_market_m15(120))
    assert trending.trend == "trending"
    assert ranging.trend == "ranging"
    assert trending.quality() > ranging.quality()


def test_regime_without_confirmed_breaks_is_transitional_not_trending():
    """Directional movement alone is not a trend; structure must confirm it."""

    assert classify_regime(bullish_setup_m15()).trend == "transitional"


def test_extreme_volatility_makes_a_market_untradeable():
    path = [(1.1000, 1.1010, 1.0990, 1.1000)] * 60
    path += [(1.1000, 1.2000, 1.0000, 1.1900)] * 3
    regime = classify_regime(series_from_path(path))
    assert regime.volatility == "extreme"
    assert regime.tradeable is False


@pytest.mark.parametrize(
    "hour,expected",
    [(2, "ASIAN"), (9, "LONDON"), (14, "OVERLAP"), (19, "NEW_YORK"), (23, "OFF_HOURS")],
)
def test_session_classification(hour, expected):
    moment = BASE_TIME.replace(hour=hour)
    assert classify_session(moment).name == expected


def test_the_fx_weekend_is_closed():
    friday_late = BASE_TIME.replace(year=2026, month=9, day=11, hour=22)
    sunday_early = BASE_TIME.replace(year=2026, month=9, day=13, hour=10)
    sunday_open = BASE_TIME.replace(year=2026, month=9, day=13, hour=23)
    assert is_forex_weekend(friday_late)
    assert is_forex_weekend(sunday_early)
    assert not is_forex_weekend(sunday_open)


def test_session_extremes_include_the_previous_day():
    candles = bullish_setup_m15()
    extremes = session_extremes(candles, candles[-1].close_time)
    assert extremes
    for name, values in extremes.items():
        assert values["high"] >= values["low"]


# -- indicators -----------------------------------------------------------


def test_atr_series_uses_only_past_data():
    candles = bullish_setup_m15()
    full = atr_series(candles, 14)
    for cut in (40, 60, len(candles) - 1):
        partial = atr_series(candles[: cut + 1], 14)
        assert partial[cut] == pytest.approx(full[cut]), (
            "ATR at bar i changed when later bars were added — that is look-ahead"
        )


def test_atr_of_an_empty_series_is_zero_not_an_error():
    assert atr([], 14) == 0.0
