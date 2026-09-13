"""Risk engine and position sizing.

Sizing is the single highest-consequence calculation in the system: the
previous build's formula was wrong by two orders of magnitude on any pair
whose quote currency was not the account currency. These tests assert the
money, not the shape of the output.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from bot.broker.models import InstrumentSpec
from bot.risk.correlation import analyse_exposure, correlation_score, currency_exposure
from bot.risk.engine import AccountRiskState, RiskEngine
from bot.risk.opportunity import evaluate_opportunity
from bot.risk.sizing import SizingError, calculate_position_size, conversion_rate, expected_profit
from bot.safety.kill_switch import KillSwitch
from fakes import DEFAULT_SPEC, SETUP_END


def spec(**overrides) -> InstrumentSpec:
    return dataclasses.replace(DEFAULT_SPEC, **overrides)


USDJPY = spec(
    symbol="USDJPY", broker_name="USDJPY", tick_size=0.001, digits=3,
    base_currency="USD", quote_currency="JPY",
)
AUDJPY = spec(
    symbol="AUDJPY", broker_name="AUDJPY", tick_size=0.001, digits=3,
    base_currency="AUD", quote_currency="JPY",
)
XAUUSD = spec(
    symbol="XAUUSD", broker_name="XAUUSD", contract_size=100.0, tick_size=0.01, digits=2,
    base_currency="XAU", quote_currency="USD", max_lot=50.0,
)


# -- sizing ---------------------------------------------------------------


def test_quote_equals_account_currency_is_exact():
    size = calculate_position_size(spec=DEFAULT_SPEC, risk_amount=100.0, entry=1.1000, stop_loss=1.0950)
    # 50 pips on EURUSD: 0.005 * 100,000 = $500/lot, so $100 of risk = 0.2 lots.
    assert size.lots == pytest.approx(0.2)
    assert size.actual_risk == pytest.approx(100.0, abs=0.01)


def test_base_equals_account_currency_inverts_the_instrument_price():
    """USDJPY on a USD account. The old build was ~150x wrong here."""

    size = calculate_position_size(spec=USDJPY, risk_amount=100.0, entry=150.00, stop_loss=149.50)
    # 0.50 JPY * 100,000 = 50,000 JPY/lot = $333.33/lot at 150. $100 -> 0.30 lots.
    assert size.lots == pytest.approx(0.30)
    assert size.actual_risk == pytest.approx(100.0, abs=0.5)
    assert size.conversion_rate == pytest.approx(1 / 150.0)

    broken_legacy_formula = round((100.0 / 0.5) / 100_000, 2)
    assert broken_legacy_formula == 0.0, "the replaced formula sized this at zero lots"


def test_a_cross_pair_refuses_to_size_without_a_conversion_source():
    with pytest.raises(SizingError, match="no conversion-rate source"):
        calculate_position_size(spec=AUDJPY, risk_amount=100.0, entry=98.0, stop_loss=97.5)


def test_a_cross_pair_refuses_to_size_when_the_broker_cannot_price_the_bridge():
    """An unavailable rate must fail loudly, never default to 1.0."""

    with pytest.raises(SizingError, match="no broker price available"):
        calculate_position_size(
            spec=AUDJPY,
            risk_amount=100.0,
            entry=98.0,
            stop_loss=97.5,
            rate_lookup=lambda base, quote: None,
        )


def test_a_cross_pair_sizes_correctly_with_a_broker_rate():
    size = calculate_position_size(
        spec=AUDJPY,
        risk_amount=100.0,
        entry=98.0,
        stop_loss=97.5,
        rate_lookup=lambda base, quote: 150.0 if (base, quote) == ("USD", "JPY") else None,
    )
    assert size.actual_risk == pytest.approx(100.0, abs=0.5)


def test_an_inverted_conversion_quote_is_also_accepted():
    rate, note = conversion_rate(
        AUDJPY, 98.0, lambda base, quote: 0.00667 if (base, quote) == ("JPY", "USD") else None
    )
    assert rate == pytest.approx(0.00667)
    assert "JPYUSD" in note.replace(" ", "")


def test_metals_use_their_own_contract_size():
    size = calculate_position_size(spec=XAUUSD, risk_amount=100.0, entry=2400.0, stop_loss=2390.0)
    # $10 move * 100 oz = $1000/lot -> 0.1 lots for $100 of risk.
    assert size.lots == pytest.approx(0.10)
    assert expected_profit(
        spec=XAUUSD, lots=size.lots, entry=2400.0, take_profit=2430.0, conversion=1.0
    ) == pytest.approx(300.0)


def test_lots_round_down_so_risk_is_never_exceeded():
    size = calculate_position_size(spec=DEFAULT_SPEC, risk_amount=100.0, entry=1.1000, stop_loss=1.0963)
    assert size.actual_risk <= 100.0
    assert size.lots == pytest.approx(0.27)


def test_a_position_below_the_broker_minimum_is_declined_not_rounded_up():
    """Rounding up to the minimum lot would silently exceed approved risk."""

    with pytest.raises(SizingError, match="below the broker minimum"):
        calculate_position_size(spec=DEFAULT_SPEC, risk_amount=1.0, entry=1.1000, stop_loss=1.0500)


def test_zero_stop_distance_is_refused():
    with pytest.raises(SizingError, match="stop distance is zero"):
        calculate_position_size(spec=DEFAULT_SPEC, risk_amount=100.0, entry=1.1, stop_loss=1.1)


def test_margin_ceiling_blocks_an_oversized_commitment():
    with pytest.raises(SizingError, match="exceeds half of the available"):
        calculate_position_size(
            spec=DEFAULT_SPEC,
            risk_amount=500.0,
            entry=1.1000,
            stop_loss=1.0999,
            leverage=30.0,
            available_margin=100.0,
        )


# -- correlation ----------------------------------------------------------


def test_pairs_sharing_a_leg_in_the_same_direction_are_correlated():
    assert correlation_score("EURUSD", "BUY", "GBPUSD", "BUY") > 0.4


def test_opposing_dollar_exposure_is_negatively_correlated():
    assert correlation_score("EURUSD", "BUY", "USDCHF", "BUY") < 0


def test_the_same_pair_in_opposite_directions_is_a_perfect_hedge():
    assert correlation_score("EURUSD", "BUY", "EURUSD", "SELL") == pytest.approx(-1.0)


def test_gold_carries_inverse_dollar_exposure():
    assert currency_exposure("XAUUSD", "BUY")["USD"] < 0


def test_exposure_aggregates_correlated_risk():
    report = analyse_exposure(
        [{"symbol": "EURUSD", "direction": "BUY", "risk_amount": 50.0}],
        {"symbol": "GBPUSD", "direction": "BUY", "risk_amount": 50.0},
        correlation_threshold=0.45,
    )
    assert report.correlated_risk > 50.0, "a second correlated position must add to the total"
    assert report.per_currency["USD"] == pytest.approx(-100.0)


# -- risk engine ----------------------------------------------------------


def make_account(**overrides) -> AccountRiskState:
    defaults = dict(
        balance=10_000.0,
        equity=10_000.0,
        available_margin=10_000.0,
        peak_equity=10_000.0,
        daily_realized_pnl=0.0,
        open_pnl=0.0,
        trades_today=0,
        trades_this_session=0,
        consecutive_losses=0,
        open_positions=[],
    )
    defaults.update(overrides)
    return AccountRiskState(**defaults)


@pytest.fixture()
def candidate(config, broker, repos):
    from bot.marketdata.provider import MarketDataProvider
    from bot.smc.engine import SmcEngine

    provider = MarketDataProvider(broker, config)
    series = provider.multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    result = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END)
    assert result.candidate is not None, result.rejection
    return result.candidate


def test_a_valid_setup_is_approved_and_sized(config, candidate):
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is True
    assert decision.size is not None and decision.size.lots > 0
    assert decision.risk_pct is not None and decision.risk_pct <= config.risk.max_risk_pct


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"balance": 0.0, "equity": 0.0}, "not positive"),
        ({"daily_realized_pnl": -400.0}, "daily loss limit"),
        ({"equity": 8_500.0, "peak_equity": 10_000.0}, "maximum drawdown"),
        ({"consecutive_losses": 4}, "consecutive losses"),
        ({"trades_today": 6}, "daily trade limit"),
        ({"trades_this_session": 3}, "session trade limit"),
        (
            {"open_positions": [{"symbol": "EURUSD", "direction": "BUY", "risk_amount": 50}]},
            "already holding a position",
        ),
    ],
)
def test_hard_limits_each_block_a_trade(config, candidate, overrides, expected):
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A+",
        account=make_account(**overrides),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved is False
    assert any(expected in reason for reason in decision.reasons), decision.reasons


def test_maximum_open_positions_is_enforced(config, candidate):
    positions = [
        {"symbol": symbol, "direction": "BUY", "risk_amount": 10.0}
        for symbol in ("AUDUSD", "USDCHF", "XAUUSD")
    ]
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A",
        account=make_account(open_positions=positions),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved is False
    assert any("maximum open positions" in reason for reason in decision.reasons)


def test_the_kill_switch_blocks_everything(config, candidate, repos):
    switch = KillSwitch(repos.state)
    switch.trip("MANUAL", "operator stopped trading")
    decision = RiskEngine(config, switch).evaluate(
        candidate=candidate, tier="A+", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    assert any("kill switch" in reason for reason in decision.reasons)


def test_cooldown_after_a_loss(config, candidate):
    account = make_account(last_loss_at=SETUP_END - timedelta(minutes=5))
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=account, spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    assert any("cooling down after a loss" in reason for reason in decision.reasons)


def test_correlated_exposure_cap_blocks_stacking(config, candidate):
    account = make_account(
        open_positions=[{"symbol": "GBPUSD", "direction": "BUY", "risk_amount": 200.0}]
    )
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A+", account=account, spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    assert any("correlated exposure" in reason for reason in decision.reasons)


# -- dynamic risk ---------------------------------------------------------


def test_risk_scales_with_setup_quality_but_never_past_the_ceiling(config):
    engine = RiskEngine(config)
    account = make_account()
    b_risk, _ = engine.risk_percentage(tier="B", account=account)
    a_risk, _ = engine.risk_percentage(tier="A", account=account)
    plus_risk, _ = engine.risk_percentage(tier="A+", account=account)
    assert b_risk < a_risk < plus_risk
    assert plus_risk <= config.risk.max_risk_pct


def test_risk_only_ever_decreases_after_losses(config):
    """The anti-martingale invariant, asserted directly."""

    engine = RiskEngine(config)
    flat = engine.risk_percentage(tier="A", account=make_account())[0]
    for streak in (2, 3):
        reduced = engine.risk_percentage(
            tier="A", account=make_account(consecutive_losses=streak)
        )[0]
        assert reduced < flat, f"risk rose after {streak} losses — that is martingale behaviour"
        flat = reduced


def test_drawdown_reduces_risk(config):
    engine = RiskEngine(config)
    healthy = engine.risk_percentage(tier="A", account=make_account())[0]
    drawn = engine.risk_percentage(
        tier="A", account=make_account(equity=9_300.0, peak_equity=10_000.0)
    )[0]
    assert drawn < healthy


def test_risk_is_clamped_even_with_absurd_configuration(config):
    engine = RiskEngine(dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, base_risk_pct=0.02, max_risk_pct=0.02)
    ))
    risk, notes = engine.risk_percentage(tier="A+", account=make_account())
    assert risk <= 0.02
    assert any("clamped" in note for note in notes)


# -- profit objective -----------------------------------------------------


def test_profit_objective_rejects_a_trade_that_cannot_reach_the_target(config, candidate):
    """The objective filters; it never inflates risk to reach the number."""

    tiny = dataclasses.replace(
        config, opportunity=dataclasses.replace(config.opportunity, target_profit=100_000.0)
    )
    decision = RiskEngine(tiny).evaluate(
        candidate=candidate, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    assert "short of the" in " ".join(decision.reasons)
    assert "Risk is NOT increased" in " ".join(decision.reasons)


def test_a_plus_setups_get_a_tolerance_but_never_a_size_increase():
    from bot.config import OpportunityConfig

    config = OpportunityConfig(target_profit=50.0, tolerance_fraction=0.8)
    assert evaluate_opportunity(expected_profit=42.0, config=config, tier="A+").meets_objective
    assert not evaluate_opportunity(expected_profit=42.0, config=config, tier="A").meets_objective
    assert not evaluate_opportunity(expected_profit=39.0, config=config, tier="A+").meets_objective


# -- auto kill switch -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"daily_realized_pnl": -400.0}, "DAILY_LOSS_LIMIT"),
        ({"equity": 8_500.0}, "MAX_DRAWDOWN"),
        ({"consecutive_losses": 4}, "CONSECUTIVE_LOSSES"),
        ({}, None),
    ],
)
def test_auto_kill_switch_conditions(config, overrides, expected):
    assert RiskEngine(config).evaluate_kill_switch(make_account(**overrides)) == expected


def test_open_losses_count_toward_the_daily_limit(config):
    """Otherwise the limit is trivially evaded by not closing a loser."""

    account = make_account(daily_realized_pnl=-150.0, open_pnl=-200.0)
    assert account.daily_pnl == pytest.approx(-350.0)
    assert RiskEngine(config).evaluate_kill_switch(account) == "DAILY_LOSS_LIMIT"
