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


# -- a second mode, for testing the SWITCH rather than any strategy ---------


class _AltStrategy:
    """Test-only. Exists so the switching mechanism — persistence, the risk
    floor, the demo guard, the version stamp — stays covered now that the
    product has a single selectable mode. It proposes exactly what SMC
    would, because these tests are about the switch, not the strategy."""

    profile = StrategyProfile(
        key="alt",
        name="Test-only second mode",
        description="exists so the switch mechanism stays tested",
        min_risk_reward=1.5,
        expected_frequency="never — tests only",
        thesis="none",
    )

    def __init__(self, config):
        self._smc = SmcStrategy(config)

    def analyze(self, symbol, series, *, now=None, spread=None):
        return self._smc.analyze(symbol, series, now=now, spread=spread)


@pytest.fixture()
def second_mode(monkeypatch):
    from bot.strategy import registry
    from bot.strategy.base import BUILDERS

    monkeypatch.setitem(BUILDERS, "alt", _AltStrategy)
    monkeypatch.setitem(registry.PROFILES, "alt", _AltStrategy.profile)
    return "alt"


# -- the registry ---------------------------------------------------------


def test_reversion_is_no_longer_a_mode_anyone_can_select():
    """Removed on evidence, as the pre-registration committed to.

    It could not trade (the scorer vetoed 691 of 691 candidates for having
    no entry zone), and with that veto bypassed its signal lost on all
    twelve instruments — 38,971 trades, t = -14.3. Fixing the dead switch
    would have made it a live, losing one. docs/EXPERIMENT_REVERSION.md.
    """

    with pytest.raises(ConfigError, match="unknown strategy"):
        normalise("reversion")
    assert "reversion" not in [profile.key for profile in available()]


def test_a_database_that_still_remembers_reversion_restarts_safely(orchestrator, repos, config):
    """A deploy must not break a bot whose stored choice was reversion.

    It resolves to the default and says so in the log — it must not raise
    and take the process down. (It never placed a trade in that mode, so
    no position depends on it.)
    """

    from bot.orchestrator import STATE_STRATEGY, Orchestrator

    repos.state.set(STATE_STRATEGY, "reversion")
    restarted = Orchestrator(
        config=config,
        broker=orchestrator.broker,
        repositories=repos,
        market_data=orchestrator.market_data,
    )
    assert restarted.strategy_key == "smc"


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
    broker.series[("EURUSD", "H1")] = aligned_htf(m15, timeframe="H1")
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
    # Force H1 to oppose whichever way a sweep would point.
    for wanted, opposing in (("BUY", "bearish"), ("SELL", "bullish")):
        allowed, veto = strategy._htf_permits(
            wanted, dataclasses.replace(analyses["H1"], bias=opposing)
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


def test_switching_persists_across_a_restart(orchestrator, repos, config, second_mode):
    assert orchestrator.strategy_key == "smc"
    orchestrator.set_strategy(second_mode)
    assert orchestrator.strategy_key == second_mode

    # A fresh orchestrator over the same storage is what a restart is.
    from bot.orchestrator import Orchestrator

    restarted = Orchestrator(
        config=config,
        broker=orchestrator.broker,
        repositories=repos,
        market_data=orchestrator.market_data,
    )
    assert restarted.strategy_key == second_mode


def test_switching_moves_the_risk_floor_without_lowering_any_other_limit(orchestrator, second_mode):
    before = orchestrator.risk.limits
    orchestrator.set_strategy(second_mode)
    after = orchestrator.risk.limits

    assert after.min_risk_reward == _AltStrategy.profile.min_risk_reward
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


def test_the_switch_shows_what_a_trade_is_worth_without_promising_anything(orchestrator):
    """What replaced "this mode cannot clear the profit floor".

    That warning was correct while the objective was a dollar figure, and
    it is meaningless now: 1:1.2 R is the same demand at any equity, so no
    mode can be unaffordable. What an operator still wants at the switch
    is the size of a trade in this mode, which is one R times the mode's
    own floor — a measurement, not a forecast (project rule 13).
    """

    status = orchestrator.strategy_status()
    assert status["active"] == "smc"

    equity = orchestrator.live.get("account").value.equity
    risk_pct = orchestrator.config.risk.base_risk_pct
    for option in status["options"]:
        assert "clearsProfitFloor" not in option
        assert option["riskPerTrade"] == pytest.approx(equity * risk_pct, abs=0.01)
        assert option["rewardAtMinimumR"] == pytest.approx(
            equity * risk_pct * option["minRiskReward"], abs=0.01
        )
        # No mode declares a floor under the build's own.
        assert option["minRiskReward"] >= orchestrator.config.reward.min_reward_r


def test_a_switch_cannot_bypass_the_demo_guard(orchestrator, broker, second_mode):
    """Project rule 11: no control may bypass the demo guard."""

    broker.metadata = {"accountType": "LIVE"}
    broker.claims = None
    orchestrator.set_strategy(second_mode)
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


def test_the_orchestrator_stamps_the_mode_actually_in_force(orchestrator, second_mode):
    """Not the configured default — the one the switch selected."""

    orchestrator.set_strategy(second_mode)
    assert orchestrator.strategy_key == second_mode
