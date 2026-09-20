"""Backtester integrity.

The headline test is `test_analysis_on_bar_i_cannot_see_bar_i_plus_one`:
if that passes, no detector can leak the future, because the future is
not in the data it was handed.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.backtest.engine import Backtester, BacktestCosts, SimulatedTrade
from bot.backtest.montecarlo import monte_carlo
from bot.backtest.walkforward import build_folds, walk_forward
from bot.marketdata.candles import Candle
from bot.smc.engine import SmcEngine
from fakes import (
    DEFAULT_SPEC,
    SETUP_END,
    aligned_htf,
    bullish_setup_m15,
    series_from_path,
    trending_path,
)


def long_series(count: int = 600):
    """A long, varied series: trend up, range, trend down, range."""

    path: list[tuple[float, float, float, float]] = []
    price = 1.1000
    for direction, length in ((1, count // 4), (0, count // 4), (-1, count // 4), (0, count // 4)):
        if direction == 0:
            for index in range(length):
                drift = 0.0002 * (1 if index % 2 else -1)
                path.append((price, price + 0.0006, price - 0.0006, price + drift))
                price += drift
        else:
            segment = trending_path(
                count=length, start_price=price, step=0.0007, wobble=0.00015, direction=direction
            )
            path.extend(segment)
            price = segment[-1][3]
    return series_from_path(path, timeframe="M15", end=SETUP_END)


def test_analysis_on_bar_i_cannot_see_bar_i_plus_one(config):
    """The structural no-look-ahead proof.

    Re-running the engine on a truncated series must produce the identical
    verdict it produced when the later bars were present. If any detector
    peeked forward, these two answers would differ.
    """

    engine = SmcEngine(config)
    m15 = bullish_setup_m15()

    for cut in (55, 62, 70, 76, len(m15) - 1):
        truncated = engine.analyze_timeframe(m15[: cut + 1], timeframe="M15", now=m15[cut].close_time)
        full = engine.analyze_timeframe(m15, timeframe="M15", now=m15[-1].close_time)

        truncated_events = [
            (event.index, event.event_type, event.direction)
            for event in truncated.structure_events
        ]
        full_events_up_to_cut = [
            (event.index, event.event_type, event.direction)
            for event in full.structure_events
            if event.index <= cut
        ]
        assert truncated_events == full_events_up_to_cut, (
            f"structure events at bar {cut} changed once later bars were added"
        )

        truncated_swings = {point.index for point in truncated.swings.known_highs(cut)}
        full_swings = {point.index for point in full.swings.known_highs(cut)}
        assert truncated_swings == full_swings


def test_a_backtest_runs_and_reports_honest_statistics(config):
    m15 = long_series(600)
    h1 = aligned_htf(m15, timeframe="H1", count=200)
    result = Backtester(config, DEFAULT_SPEC).run(m15, h1, warmup=150, step=1)
    stats = result.statistics()

    assert stats["barsProcessed"] > 0
    assert stats["startingBalance"] == 10_000.0
    assert isinstance(stats["rejections"], dict)
    # With no trades the statistics must be empty, not invented.
    if stats["trades"] == 0:
        assert stats["winRate"] is None
        assert stats["sample"] == "insufficient"


def test_the_backtester_rejects_most_bars_and_records_why(config):
    m15 = long_series(400)
    result = Backtester(config, DEFAULT_SPEC).run(
        m15,
        aligned_htf(m15, timeframe="H1", count=200),
        warmup=150,
    )
    assert result.setups_rejected, "a selective system must record why it stood aside"
    assert sum(result.setups_rejected.values()) > len(result.trades)


def test_a_bar_touching_both_stop_and_target_resolves_as_the_stop(config):
    """Intrabar order is unknowable; assuming the good outcome makes a
    backtest lie."""

    backtester = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0))
    trade = SimulatedTrade(
        symbol="EURUSD", direction="BUY", entry_index=0, entry_time=SETUP_END,
        entry=1.1000, stop_loss=1.0950, take_profit=1.1150, lots=0.1,
        risk_amount=50.0, setup_grade="A", setup_score=70.0,
    )
    bar = Candle(SETUP_END, 1.1000, 1.1200, 1.0900, 1.1100, 0.0, "M15")
    assert backtester._resolve_exit(trade, bar, 1) is True
    assert trade.exit_reason == "STOP_AND_TARGET_SAME_BAR"
    assert trade.pnl is not None and trade.pnl < 0


def test_costs_are_charged_and_make_results_worse(config):
    free = Backtester(
        config, DEFAULT_SPEC, costs=BacktestCosts(spread_points=0, slippage_points=0, commission_per_lot=0)
    )
    real = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts())

    def run(backtester) -> float:
        trade = SimulatedTrade(
            symbol="EURUSD", direction="BUY", entry_index=0, entry_time=SETUP_END,
            entry=1.1000, stop_loss=1.0950, take_profit=1.1150, lots=1.0,
            risk_amount=500.0, setup_grade="A", setup_score=70.0,
        )
        bar = Candle(SETUP_END, 1.1140, 1.1160, 1.1130, 1.1155, 0.0, "M15")
        backtester._resolve_exit(trade, bar, 1)
        return trade.pnl or 0.0

    assert run(real) < run(free), "commission and slippage must reduce the result"


def test_a_stop_fills_worse_than_the_stop_price(config):
    backtester = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=5))
    trade = SimulatedTrade(
        symbol="EURUSD", direction="BUY", entry_index=0, entry_time=SETUP_END,
        entry=1.1000, stop_loss=1.0950, take_profit=1.1150, lots=0.1,
        risk_amount=50.0, setup_grade="A", setup_score=70.0,
    )
    bar = Candle(SETUP_END, 1.0960, 1.0965, 1.0930, 1.0940, 0.0, "M15")
    backtester._resolve_exit(trade, bar, 1)
    assert trade.exit_price < trade.stop_loss


def test_an_open_trade_cannot_be_closed_on_its_own_entry_bar(config):
    backtester = Backtester(config, DEFAULT_SPEC)
    trade = SimulatedTrade(
        symbol="EURUSD", direction="BUY", entry_index=5, entry_time=SETUP_END,
        entry=1.1000, stop_loss=1.0950, take_profit=1.1150, lots=0.1,
        risk_amount=50.0, setup_grade="A", setup_score=70.0,
    )
    bar = Candle(SETUP_END, 1.1000, 1.1200, 1.0900, 1.1100, 0.0, "M15")
    assert backtester._resolve_exit(trade, bar, 5) is False


# -- walk forward ---------------------------------------------------------


def test_fold_boundaries_never_overlap():
    folds = build_folds(3000, folds=3)
    assert len(folds) == 3
    for fold in folds:
        assert fold.train[1] <= fold.validate[0] <= fold.validate[1] <= fold.test[0]
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.test[1] <= later.train[0]


def test_too_little_data_for_walk_forward_is_an_error_not_a_result():
    with pytest.raises(ValueError, match="too few"):
        build_folds(300, folds=3)


def test_walk_forward_reports_out_of_sample_only(config):
    m15 = long_series(900)
    result = walk_forward(
        config,
        DEFAULT_SPEC,
        m15,
        aligned_htf(m15, timeframe="H1", count=250),
        folds=3,
        step=3,
    )
    summary = result.summary()
    assert summary["folds"] == 3
    assert "verdict" in summary
    # A thin sample must be labelled as such rather than claimed as an
    # edge. Either "no out-of-sample data" (no fold produced enough train
    # trades to select parameters) or "inconclusive" is the honest answer.
    if summary["outOfSampleTrades"] < 20:
        assert any(
            phrase in summary["verdict"]
            for phrase in ("inconclusive", "no out-of-sample data")
        ), summary["verdict"]
        assert "edge" not in summary["verdict"] or "supportable" in summary["verdict"]


# -- monte carlo ----------------------------------------------------------


def test_monte_carlo_needs_a_real_sample():
    assert monte_carlo([10.0, -5.0, 3.0]) is None


def test_monte_carlo_describes_dispersion_and_disclaims_prediction():
    pnls = [120.0] * 25 + [-60.0] * 35
    result = monte_carlo(pnls, runs=400, starting_balance=10_000.0)
    assert result is not None
    payload = result.as_dict()
    assert payload["worstMaxDrawdown"] >= payload["medianMaxDrawdown"]
    assert payload["longestLosingStreak"] >= 1
    assert 0.0 <= payload["riskOfRuin"] <= 1.0
    assert "a prediction of future performance" in payload["disclaimer"]


def test_reordering_cannot_produce_a_range_for_the_total():
    """The old p5/median/p95 return trio was one constant under three names.

    Shuffling a fixed multiset of trades cannot change their sum, so
    every "percentile" of the reordered return was the same figure. The
    report printed it three times and a reader could only conclude there
    was a 5th-percentile outcome being measured. There was not. The total
    is now reported once, as the invariant it is.

    This test asserts the arithmetic that made the old fields impossible
    rather than the absence of the fields, so it stays meaningful if the
    shape changes again.
    """

    pnls = [120.0] * 25 + [-60.0] * 35
    result = monte_carlo(pnls, runs=400, starting_balance=10_000.0)
    assert result is not None
    payload = result.as_dict()

    assert payload["totalReturn"] == pytest.approx(sum(pnls))
    assert "p5Return" not in payload and "medianReturn" not in payload


def test_the_bootstrap_is_the_part_that_can_speak_about_the_total():
    """And the honest answer to the question the fake percentiles posed.

    Drawing the same number of trades WITH replacement does vary the
    total, so its spread is real. On a sample this small the interval is
    wide, which is the finding, not a defect.
    """

    pnls = [120.0] * 25 + [-60.0] * 35
    result = monte_carlo(pnls, runs=2000, starting_balance=10_000.0)
    assert result is not None
    payload = result.as_dict()

    assert payload["bootstrapP5Return"] < payload["bootstrapMedianReturn"]
    assert payload["bootstrapMedianReturn"] < payload["bootstrapP95Return"]
    # The sample's own total sits inside the interval its resamples span.
    assert payload["bootstrapP5Return"] <= payload["totalReturn"] <= payload["bootstrapP95Return"]


def test_monte_carlo_is_reproducible_for_a_given_seed():
    pnls = [50.0] * 20 + [-30.0] * 20
    first = monte_carlo(pnls, runs=200, seed=42)
    second = monte_carlo(pnls, runs=200, seed=42)
    assert first.as_dict() == second.as_dict()
