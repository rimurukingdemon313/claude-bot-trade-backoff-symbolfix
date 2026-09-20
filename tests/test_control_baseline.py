"""Does the selection beat a coin flip?

The question a backtest cannot answer on its own. "The strategy lost
$445" means nothing until you know what NOT selecting would have lost on
the same data, with the same costs and the same simulator pessimism —
and this harness is deliberately pessimistic (a bar touching both stop
and target books the stop), so absolute numbers are a floor rather than
an estimate.

Measured on 12 symbols x ~27 days of generated M15:

    SMC (shipped)   n=180  win 33.3%  -2.47$/trade  (-0.111R)
    coin flip       n=104  win 33.7%  -6.41$/trade  (-0.282R)

The selection was worth +0.171R per trade over random. That is not a
claim of profitability — the price series is generated and contains no
edge for anything to find, so both numbers are bounded by costs, which
averaged $2.71 against $25 of risk. It is a claim that the selection is
doing something, which is the weaker statement the evidence supports.

This file keeps the control runnable, because an edge over random is the
first thing a strategy change can silently destroy.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from bot.backtest.engine import BacktestCosts, Backtester, SimulatedTrade
from bot.risk.sizing import SizingError, calculate_position_size
from bot.smc.indicators import atr
from fakes import DEFAULT_SPEC


class CoinFlipBacktester(Backtester):
    """Same stop geometry, sizing, costs and exits. Direction by coin.

    A control has to differ in exactly ONE thing or it measures the wrong
    difference — so everything except the choice of direction and the
    choice of moment comes from the real engine.
    """

    RATE = 1.0 / 90.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rng = random.Random(hash(self.spec.symbol) & 0xFFFF)

    def propose(self, visible_m15, visible_h1, *, index, cutoff, balance, peak,
                consecutive_losses):
        if self._rng.random() > self.RATE:
            return None, "no coin"
        candles = list(visible_m15)
        atr_value = atr(candles, self.config.smc.atr_period)
        if not atr_value or atr_value <= 0:
            return None, "no atr"

        price = candles[-1].close
        direction = "BUY" if self._rng.random() < 0.5 else "SELL"
        stop_distance = atr_value
        stop = price - stop_distance if direction == "BUY" else price + stop_distance
        reward = stop_distance * self.config.risk.min_risk_reward
        take_profit = price + reward if direction == "BUY" else price - reward

        spread = self.costs.spread_points * self.spec.tick_size
        entry = price + spread / 2 if direction == "BUY" else price - spread / 2
        try:
            size = calculate_position_size(
                spec=self.spec,
                risk_amount=balance * self.config.risk.base_risk_pct,
                entry=entry,
                stop_loss=stop,
                rate_lookup=self.rate_lookup,
            )
        except SizingError:
            return None, "sizing"

        return (
            SimulatedTrade(
                symbol=self.spec.symbol, direction=direction, entry_index=index,
                entry_time=cutoff, entry=entry, stop_loss=stop, take_profit=take_profit,
                lots=size.lots, initial_stop=stop, risk_amount=size.actual_risk,
                setup_grade="B", setup_score=56.0, conversion_rate=size.conversion_rate,
            ),
            "",
        )


def test_the_control_differs_from_the_real_engine_in_exactly_one_thing():
    """Otherwise it measures the wrong difference.

    If the control also changed the stop rule, the sizing or the exits,
    a gap between it and the strategy would be uninterpretable — and a
    control that flatters the strategy is worse than no control at all.
    """

    import inspect

    source = inspect.getsource(CoinFlipBacktester.propose)
    # Sizing, costs and exits come from the real machinery.
    assert "calculate_position_size" in source
    assert "self.costs.spread_points" in source
    assert "min_risk_reward" in source
    # And the only judgement it makes is a coin.
    assert "self.smc" not in source, "the control must not consult the strategy"
    assert "self.scorer" not in source


def test_the_control_produces_trades_at_all():
    """A control that never fires proves nothing, quietly."""

    from bot.config import load_config
    from tests.test_portfolio_backtest import _h1, _walk  # type: ignore[import-not-found]

    config = load_config({})
    m15 = _walk(1200, seed=11)
    engine = CoinFlipBacktester(config, DEFAULT_SPEC, costs=BacktestCosts())
    result = engine.run(m15, _h1(m15), warmup=250, step=2)

    assert result.trades, "the coin never came up"
    for trade in result.trades:
        assert trade.direction in ("BUY", "SELL")
        assert trade.risk_amount > 0
        # Same R geometry the engine is held to — and measured from the
        # ACTUAL entry, which has paid the half-spread, so the realised
        # ratio sits a little BELOW the nominal. That gap is the cost
        # showing up where it belongs rather than being waved through,
        # and the control has to carry it or it would be comparing a
        # frictionless coin against a strategy that pays for its fills.
        reward = abs(trade.take_profit - trade.entry)
        risk = abs(trade.entry - trade.initial_stop)
        realised = reward / risk
        assert realised < config.risk.min_risk_reward, "the spread must cost something"
        assert realised > config.risk.min_risk_reward * 0.9


def test_the_tradeable_floor_can_be_raised_without_a_code_deploy(monkeypatch):
    """The experiment the measurements point at, made runnable.

    A grades averaged +0.014R and B grades -0.152R over 180 simulated
    trades — the entire loss was in the B cohort. But the correlation
    between the continuous score and the outcome was +0.037, so the
    boundary matters and the ranking behind it does not.

    One dataset is not grounds for moving a default. It is grounds for
    making the experiment cheap, which is what this asserts.
    """

    from bot.config import load_config

    assert load_config({}).scoring.min_tradeable_score == pytest.approx(56.0)

    monkeypatch.setenv("SCORING_MIN_TRADEABLE", "68")
    raised = load_config({})
    assert raised.scoring.min_tradeable_score == pytest.approx(68.0)
    assert raised.scoring.tier_a == pytest.approx(68.0), "still the A boundary"

    # Garbage falls back to the default rather than opening the gate.
    monkeypatch.setenv("SCORING_MIN_TRADEABLE", "")
    assert load_config({}).scoring.min_tradeable_score == pytest.approx(56.0)
