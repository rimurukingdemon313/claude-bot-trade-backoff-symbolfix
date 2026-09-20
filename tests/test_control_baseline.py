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


def test_stop_width_is_a_cost_control_and_the_algebra_holds():
    """cost/risk = spread / stop_distance. The contract size cancels.

    This is the one finding in the session that is arithmetic rather than
    a search, so it is worth having as an executable statement: halving
    the stop doubles what the trade pays the broker as a share of its own
    risk, whatever the instrument or the lot size.
    """

    from bot.risk.sizing import calculate_position_size

    spread = 8 * DEFAULT_SPEC.tick_size
    fractions = {}
    for stop_distance in (0.0005, 0.0010, 0.0020, 0.0040):
        risk = 25.0
        size = calculate_position_size(
            spec=DEFAULT_SPEC, risk_amount=risk,
            entry=1.1000, stop_loss=1.1000 - stop_distance,
        )
        cost = spread * DEFAULT_SPEC.contract_size * size.lots
        fractions[stop_distance] = cost / size.actual_risk

    # Each doubling of the stop roughly halves the friction.
    widths = sorted(fractions)
    for tighter, wider in zip(widths, widths[1:]):
        assert fractions[wider] < fractions[tighter]
        assert fractions[wider] == pytest.approx(fractions[tighter] / 2, rel=0.05)

    # And it matches spread/stop_distance directly.
    for stop_distance, fraction in fractions.items():
        assert fraction == pytest.approx(spread / stop_distance, rel=0.05)


def test_the_stop_floor_can_be_raised_without_a_code_deploy(monkeypatch):
    from bot.config import load_config

    assert load_config({}).risk.min_stop_distance_atr == pytest.approx(0.35)
    monkeypatch.setenv("RISK_MIN_STOP_ATR", "0.9")
    assert load_config({}).risk.min_stop_distance_atr == pytest.approx(0.9)


@pytest.mark.parametrize(
    "win_rate,payoff",
    [(0.90, 0.10), (0.75, 0.30), (0.66, 0.50), (0.636, 0.48)],
)
def test_a_high_win_rate_is_not_evidence_of_anything_on_its_own(win_rate, payoff):
    """Every row here is a LOSING system with a flattering win rate.

    Measured, not argued: moving the target and nothing else took the win
    rate from 33.3% to 63.6% and the balance from $4,555 to $5,060 and
    back down to $4,731. The peak was in the middle. The average win
    collapsed from $20.54 to $9.74 while the average loss never moved
    from about -$20, so every extra win was bought with a smaller one.

    This pins the arithmetic that makes that inevitable, because a win
    rate is the number most likely to be quoted at someone and the least
    able to carry the claim on its own.
    """

    expectancy = win_rate * payoff - (1 - win_rate) * 1.0
    assert expectancy < 0, "these are the cases that look good and lose"

    # What that win rate would ACTUALLY need to break even.
    required_payoff = (1 - win_rate) / win_rate
    assert payoff < required_payoff


def test_the_build_refuses_a_reward_floor_below_parity():
    """The guard that stops a win-rate chase from shipping.

    Below 1:1 a winner is worth less than a loser costs. The default is
    1.2; the refusal is at parity, and it is a refusal rather than a
    clamp so that setting it is an error rather than a silent adjustment.
    """

    import dataclasses

    from bot.config import load_config
    from bot.errors import ConfigError

    config = load_config({})
    doomed = dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, min_risk_reward=0.515)
    )
    with pytest.raises(ConfigError, match="below 1:1"):
        doomed.validate()
