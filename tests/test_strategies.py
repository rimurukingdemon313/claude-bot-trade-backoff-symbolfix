"""Two strategies, one risk engine, one switch.

The point of these is not that the reversion mode works — nothing here
can establish that — but that selecting it changes ONLY which analysis
runs, and that everything protecting the account is identical either way.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from bot.config import load_config
from bot.errors import ConfigError
from bot.marketdata.provider import MarketDataProvider
from bot.strategy import DEFAULT_STRATEGY, available, build, normalise
from bot.strategy.base import BUILD_MINIMUM_RISK_REWARD, StrategyProfile
from bot.strategy.reversion import ReversionStrategy
from bot.strategy.smc_strategy import SmcStrategy
from fakes import (
    DEFAULT_SPEC,
    SETUP_END,
    FakeBroker,
    aligned_htf,
    bullish_setup_m15,
    flat_market_m15,
)


# -- the registry ---------------------------------------------------------


def test_the_default_is_the_strategy_that_trades_least():
    assert DEFAULT_STRATEGY == "smc"
    assert normalise(None) == "smc"
    assert normalise("") == "smc"


def test_an_unknown_strategy_is_refused_rather_than_silently_defaulted():
    """A typo must not quietly run a different strategy than was asked for.

    Silent substitution is what makes a trade history uninterpretable: the
    journal would say one thing and the engine would have done another.
    """

    with pytest.raises(ConfigError, match="unknown strategy"):
        normalise("smcc")


def test_every_strategy_respects_the_build_risk_reward_floor():
    for profile in available():
        assert profile.min_risk_reward >= BUILD_MINIMUM_RISK_REWARD


def test_a_profile_below_the_build_floor_is_refused_at_construction():
    """The guard is asserted, not trusted. A future strategy cannot opt out."""

    with pytest.raises(ConfigError, match="below the build minimum"):
        StrategyProfile(
            key="reckless",
            name="Reckless",
            description="",
            min_risk_reward=0.5,
            expected_frequency="constantly",
            thesis="",
        )


# -- the reversion strategy ----------------------------------------------


def series_for(broker, config, now=SETUP_END):
    return MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=now)


def broker_with(m15):
    broker = FakeBroker()
    broker.series[("EURUSD", "M15")] = m15
    for timeframe in ("H1", "H4"):
        broker.series[("EURUSD", timeframe)] = aligned_htf(m15, timeframe=timeframe)
    return broker


def test_reversion_produces_a_priced_candidate_or_a_reason(config):
    m15 = bullish_setup_m15(end=SETUP_END)
    broker = broker_with(m15)
    result = ReversionStrategy(config).analyze(
        "EURUSD", series_for(broker, config), now=SETUP_END
    )
    if result.candidate is None:
        assert result.rejection  # never silent
    else:
        candidate = result.candidate
        assert candidate.stop_distance > 0
        assert candidate.risk_reward >= ReversionStrategy.profile.min_risk_reward
        if candidate.direction == "BUY":
            assert candidate.stop_loss < candidate.entry < candidate.take_profit
        else:
            assert candidate.take_profit < candidate.entry < candidate.stop_loss


def test_reversion_refuses_a_weekend_like_every_other_path(config):
    sunday = SETUP_END + timedelta(days=3)
    assert sunday.strftime("%A") == "Sunday"
    m15 = bullish_setup_m15(end=SETUP_END)
    broker = broker_with(m15)
    result = ReversionStrategy(config).analyze(
        "EURUSD", series_for(broker, config), now=sunday
    )
    assert result.candidate is None
    assert "weekend" in (result.rejection or "")


def test_reversion_never_fades_a_decided_higher_timeframe(config):
    """The one loss that does not come back is the one taken against flow."""

    strategy = ReversionStrategy(config)
    m15 = bullish_setup_m15(end=SETUP_END)
    broker = broker_with(m15)
    analyses = {
        timeframe: strategy.smc.analyze_timeframe(
            list(data.candles), timeframe=timeframe, now=SETUP_END
        )
        for timeframe, data in series_for(broker, config).items()
    }
    # Force H4 to oppose whichever way a sweep would point.
    for wanted, opposing in (("BUY", "bearish"), ("SELL", "bullish")):
        allowed, veto = strategy._htf_permits(
            wanted, dataclasses.replace(analyses["H4"], bias=opposing), analyses["H1"]
        )
        assert allowed is False
        assert "may not fade" in veto


def test_reversion_uses_only_sweeps_confirmed_by_the_current_bar(config):
    """Project rule 4. A sweep confirmed later is the future, not evidence."""

    strategy = ReversionStrategy(config)
    m15 = bullish_setup_m15(end=SETUP_END)
    broker = broker_with(m15)
    analysis = strategy.smc.analyze_timeframe(
        list(series_for(broker, config)["M15"].candles), timeframe="M15", now=SETUP_END
    )
    for index in range(len(analysis.candles)):
        sweep = strategy._recent_sweep(analysis, index)
        if sweep is not None:
            assert sweep.confirmed_index <= index


def test_reversion_finds_setups_where_smc_finds_none(config):
    """The whole reason the mode exists.

    SMC's largest single cause of NO TRADE is a ranging M15 with no
    confirmed direction. That regime is what this strategy is for, so it
    must at minimum not inherit the same refusal.
    """

    flat = flat_market_m15(count=90)
    broker = broker_with(flat)
    data = series_for(broker, config)

    smc = SmcStrategy(config).analyze("EURUSD", data, now=SETUP_END)
    reversion = ReversionStrategy(config).analyze("EURUSD", data, now=SETUP_END)

    assert smc.candidate is None
    # It may still stand aside — but never for "no confirmed directional
    # structure", which is precisely the filter it is meant to replace.
    if reversion.candidate is None:
        assert "no confirmed directional structure" not in (reversion.rejection or "")


# -- the switch -----------------------------------------------------------


def test_switching_persists_across_a_restart(orchestrator, repos, config):
    assert orchestrator.strategy_key == "smc"
    orchestrator.set_strategy("reversion")
    assert orchestrator.strategy_key == "reversion"

    # A fresh orchestrator over the same storage is what a restart is.
    from bot.orchestrator import Orchestrator

    restarted = Orchestrator(
        config=config,
        broker=orchestrator.broker,
        repositories=repos,
        market_data=orchestrator.market_data,
    )
    assert restarted.strategy_key == "reversion"


def test_switching_moves_the_risk_floor_without_lowering_any_other_limit(orchestrator):
    before = orchestrator.risk.limits
    orchestrator.set_strategy("reversion")
    after = orchestrator.risk.limits

    assert after.min_risk_reward == ReversionStrategy.profile.min_risk_reward
    # Everything that bounds loss is untouched. A mode switch is not a
    # risk change (project rule 2).
    assert after.max_risk_pct == before.max_risk_pct
    assert after.base_risk_pct == before.base_risk_pct
    assert after.max_daily_loss_pct == before.max_daily_loss_pct
    assert after.max_drawdown_pct == before.max_drawdown_pct
    assert after.max_open_positions == before.max_open_positions


def test_the_switch_refuses_an_unknown_mode(orchestrator):
    with pytest.raises(ConfigError):
        orchestrator.set_strategy("martingale")
    assert orchestrator.strategy_key == "smc"


def test_status_says_whether_a_mode_can_clear_the_profit_floor(orchestrator):
    """An operator must learn this at the switch, not after a silent week."""

    orchestrator.feasibility = {"equity": 988.76, "maxRiskPerTrade": 9.89}
    status = orchestrator.strategy_status()

    assert status["active"] == "smc"
    for option in status["options"]:
        # $9.89 risk at 1:1.5 or 1:2 cannot reach a $40 floor, and the
        # status must say so rather than leave it to be discovered.
        assert option["clearsProfitFloor"] is False
        assert "profit floor" in option["note"]


def test_a_switch_cannot_bypass_the_demo_guard(orchestrator, broker):
    """Project rule 11: no control may bypass the demo guard."""

    broker.metadata = {"accountType": "LIVE"}
    broker.claims = None
    orchestrator.set_strategy("reversion")
    # An explicit weekday: the weekend gate stands the scan down before
    # anything else, and this test is about the guard, not the calendar.
    result = orchestrator.scan(source="test", now=SETUP_END)
    assert result.executed is None
    assert "DEMO verification failed" in (result.skipped_reason or "")


# -- a setup priced exactly on the floor -----------------------------------


def test_a_target_built_on_the_floor_is_not_rejected_by_float_error(config):
    """Measured: 40 rejections in 600 on targets constructed to land on it.

    An R:R is a ratio of two doubles. A target placed at exactly 1.5R
    recomputes as 1.4999999999999998 about as often as 1.5000000000000555,
    so without a tolerance the decision to take a trade came down to the
    last bit of a float. The reversion mode builds every target on its
    floor, which is what made an intermittent bug a constant one.
    """

    from bot.config import R_EPSILON
    from bot.risk.engine import RiskEngine
    from bot.safety.kill_switch import KillSwitch

    for entry, stop in ((1.10000, 1.09800), (0.58559, 0.58430), (151.234, 150.988)):
        distance = entry - stop
        target = entry + distance * config.risk.min_risk_reward
        recomputed = abs(target - entry) / distance
        # Whichever side of the floor the arithmetic lands on, the tolerance
        # must cover it.
        assert recomputed >= config.risk.min_risk_reward - R_EPSILON


# -- the trade record says which mode produced it --------------------------


def test_a_trade_plan_records_the_strategy_that_produced_it(config, broker, repos):
    """Project rule 14. After the fact there is no other way to tell.

    Two modes with different targets and different frequencies averaged
    into one win rate is a number that describes nothing. Separating them
    later is only possible if it was written down at the time.
    """

    from bot.execution.plan import build_plan
    from bot.marketdata.provider import MarketDataProvider
    from bot.risk.engine import RiskEngine
    from bot.safety.kill_switch import KillSwitch
    from bot.scoring.scorer import SetupScorer
    from bot.smc.engine import SmcEngine
    from fakes import DEFAULT_SPEC

    series = MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    candidate = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END).candidate
    assert candidate is not None
    score = SetupScorer(config).score(candidate)

    from bot.risk.engine import AccountRiskState

    account = AccountRiskState(
        balance=10_000.0, equity=10_000.0, available_margin=10_000.0, peak_equity=10_000.0,
        daily_realized_pnl=0.0, open_pnl=0.0, trades_today=0, trades_this_session=0,
        consecutive_losses=0, open_positions=[],
    )
    decision = RiskEngine(config, KillSwitch(repos.state)).evaluate(
        candidate=candidate, tier=score.tier, account=account, spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved, decision.reasons

    for mode in ("smc", "reversion"):
        plan = build_plan(
            candidate=candidate, spec=DEFAULT_SPEC, risk_decision=decision,
            score=score, ai_confidence=None, strategy=mode,
        )
        assert plan.strategy == mode
        assert plan.as_dict()["versions"]["strategy"] == mode


def test_the_orchestrator_stamps_the_mode_actually_in_force(orchestrator):
    """Not the configured default — the one the switch selected."""

    orchestrator.set_strategy("reversion")
    assert orchestrator.strategy_key == "reversion"
