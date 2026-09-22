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
from bot.risk.reward import evaluate_reward
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


# -- the reward objective, in R ------------------------------------------
#
# This replaced a fixed dollar floor. Expected profit at the structural
# target is `risk x R`, and risk is a fixed percentage of equity, so a
# dollar floor was a statement about ACCOUNT SIZE wearing the costume of a
# statement about setup quality: $40 demanded 1:2 on a $5,000 account and
# 1:4 on a $1,000 one, and the market does not know the balance.


def _reward(rr: float, profit: float = 0.0, **overrides):
    from bot.config import RewardConfig

    return evaluate_reward(
        risk_reward=rr, expected_profit=profit, config=RewardConfig(**overrides)
    )


@pytest.mark.parametrize(
    "equity,rr,reward_dollars",
    [
        # A $5,000 account risking 0.5% puts one R at $25.
        (5_000.0, 1.2, 30.0),
        (5_000.0, 1.5, 37.5),
        (5_000.0, 2.0, 50.0),
    ],
)
def test_a_setup_is_judged_on_its_ratio_whatever_the_dollars_come_to(
    equity, rr, reward_dollars
):
    """$30, $37.50 and $50 are all takeable — the ratio is what is checked.

    The dollar column is asserted so the arithmetic is visible: it is what
    one R is worth at this equity times R, and the old $40 floor would
    have refused the first of these while accepting the third, for a
    difference the market had no part in.
    """

    assert equity * 0.005 * rr == pytest.approx(reward_dollars)
    verdict = _reward(rr, reward_dollars)
    assert verdict.meets_objective, verdict.reason
    assert verdict.expected_profit == reward_dollars


def test_below_the_minimum_r_is_refused_however_large_the_account():
    """The mirror image: a big account does not buy a worse ratio."""

    verdict = _reward(1.19, 11_900.0)
    assert not verdict.meets_objective
    assert "below the 1:1.2 minimum" in verdict.reason
    # And the refusal says what is NOT done about it.
    assert "size is not raised" in verdict.reason


def test_the_preferred_bar_is_a_label_and_never_a_second_gate():
    weak = _reward(1.3, 32.5)
    strong = _reward(1.6, 40.0)
    assert weak.meets_objective and not weak.preferred
    assert strong.meets_objective and strong.preferred
    assert "a strong setup" in strong.reason


def test_the_reward_verdict_cannot_change_entry_stop_target_or_size():
    """It is a judgement on numbers computed elsewhere, and nothing more."""

    import dataclasses as dc

    verdict = _reward(2.0, 50.0)
    fields = {f.name for f in dc.fields(verdict)}
    assert fields == {
        "meets_objective",
        "risk_reward",
        "expected_profit",
        "minimum_r",
        "preferred_r",
        "reason",
    }


def test_no_dollar_figure_can_refuse_a_trade_that_clears_the_ratio():
    """The guarantee the user asked for, asserted rather than assumed.

    A $2 reward on a microscopic account still clears 1:2, and a $2,000
    reward still fails 1:1.1. If any dollar threshold survived anywhere in
    this module, one of these two would come out the other way.
    """

    assert _reward(2.0, 2.0).meets_objective
    assert not _reward(1.1, 2_000.0).meets_objective


def test_the_risk_engine_refuses_a_thin_setup_without_touching_size(config, candidate):
    """End to end: the ratio is refused and the position is not inflated."""

    thin = dataclasses.replace(candidate, risk_reward=1.05)
    decision = RiskEngine(config).evaluate(
        candidate=thin, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    reasons = " ".join(decision.reasons)
    assert "1:1.05" in reasons or "below the required" in reasons


def test_position_size_is_never_raised_to_reach_a_dollar_figure():
    """Rounding UP to the broker minimum would exceed the approved risk.

    This is the one place where "make the trade bigger" could sneak in as
    a rounding convenience, so it is asserted directly: the sizer declines
    instead, and says why.
    """

    import dataclasses as dc

    chunky = dc.replace(DEFAULT_SPEC, min_lot=5.0, lot_step=1.0)
    with pytest.raises(SizingError) as caught:
        calculate_position_size(
            spec=chunky,
            risk_amount=25.0,      # 0.5% of $5,000
            entry=1.1000,
            stop_loss=1.0900,      # a wide, structural stop
        )
    message = str(caught.value)
    assert "below the broker minimum" in message
    assert "would exceed the approved risk" in message


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


def test_a_setup_priced_exactly_on_the_minimum_is_not_rejected_by_float_error(config, candidate):
    """Measured: 40 rejections in 600 on targets built to land on the floor.

    An R:R is a ratio of two doubles, so a target placed at exactly the
    minimum recomputes as 1.4999999999999998 about as often as
    1.5000000000000555. Without a tolerance, whether a trade is taken came
    down to the last bit of a float — and the reversion strategy builds
    EVERY target on its floor, which turned an intermittent bug into a
    permanent one for that mode.
    """

    on_the_floor = dataclasses.replace(
        candidate, risk_reward=config.risk.min_risk_reward - 1e-12
    )
    decision = RiskEngine(config).evaluate(
        candidate=on_the_floor, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert not any("below the minimum" in reason for reason in decision.reasons), decision.reasons


def test_a_setup_genuinely_below_the_minimum_is_still_rejected(config, candidate):
    """The tolerance is for float noise, not for a real shortfall."""

    short = dataclasses.replace(candidate, risk_reward=config.risk.min_risk_reward - 0.01)
    decision = RiskEngine(config).evaluate(
        candidate=short, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    assert decision.approved is False
    assert any("below the minimum" in reason for reason in decision.reasons)


def test_risk_never_rises_as_the_account_worsens_across_every_combination(config):
    """Rule 2, checked exhaustively rather than at a few chosen points.

    The existing test walks one dimension at a time. This crosses all of
    them — tier, losing streak, drawdown and realised daily loss — because
    the dangerous version of this bug is not "losses raise risk" (nobody
    writes that) but a reduction applied in the wrong order, or a clamp
    that lifts a floor, showing up only where two adverse conditions
    coincide.
    """

    engine = RiskEngine(config)
    healthy = {
        tier: engine.risk_percentage(tier=tier, account=make_account())[0]
        for tier in ("A+", "A", "B")
    }

    checked = 0
    for tier in ("A+", "A", "B"):
        for losses in range(0, 4):
            for drawdown in (0.0, 0.02, 0.05, 0.08):
                for daily in (0.0, -50.0, -150.0, -250.0):
                    account = make_account(
                        equity=10_000.0 * (1 - drawdown),
                        consecutive_losses=losses,
                        daily_realized_pnl=daily,
                    )
                    risk, _ = engine.risk_percentage(tier=tier, account=account)
                    checked += 1
                    assert risk <= healthy[tier] + 1e-12, (
                        f"adverse state raised risk: tier={tier} losses={losses} "
                        f"drawdown={drawdown} daily={daily} -> {risk} > {healthy[tier]}"
                    )
                    assert risk <= config.risk.max_risk_pct + 1e-12, (
                        f"risk escaped the hard cap: {risk} > {config.risk.max_risk_pct}"
                    )
    assert checked == 192, "the sweep stopped covering what it claims to cover"


def test_a_position_whose_risk_is_unknown_blocks_a_new_order(config, candidate):
    """Zero was the most dangerous number available here.

    `Orchestrator._implied_risk` returned 0.0 for an orphan position with
    no stop loss at the broker. But a position with no stop is not a
    zero-risk position — it is the one position in the book whose loss
    has no floor. Both the portfolio cap and the correlation cap are
    sums of these numbers, so calling it zero made room for MORE new
    risk exactly when the account was least measurable. That is risk
    increased by adverse account state, which rule 2 forbids.

    It is now None, and None stands the engine aside (rule 7).
    """

    account = make_account(
        open_positions=[{"symbol": "GBPUSD", "direction": "BUY", "risk_amount": None}]
    )
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=account, spec=DEFAULT_SPEC, now=SETUP_END
    )

    assert decision.approved is False
    joined = " ".join(decision.reasons)
    assert "could not be established" in joined
    assert "GBPUSD" in joined, "the refusal has to name the position an operator must go look at"


def test_a_priced_position_of_the_same_size_does_not_block(config, candidate):
    """The control: it is the UNKNOWN that stands the engine aside.

    Without this, the test above would also pass if the engine simply
    refused whenever anything was open.
    """

    account = make_account(
        open_positions=[{"symbol": "GBPUSD", "direction": "BUY", "risk_amount": 25.0}]
    )
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=account, spec=DEFAULT_SPEC, now=SETUP_END
    )

    assert decision.approved is True, decision.reasons


def test_an_orphan_with_no_stop_is_reported_as_unknown_risk_not_zero(orchestrator, broker):
    """The source of the None, pinned at the orchestrator.

    A position the database has no plan for AND that carries no stop at
    the broker cannot be priced from anything. The row must say so.
    """

    broker.add_position(
        symbol="EURUSD",
        direction="BUY",
        quantity=0.5,
        entry=1.1000,
        stop_loss=None,
        take_profit=None,
    )
    positions = broker.positions()
    account = broker.account_state()

    state = orchestrator._compose_account_state(account, positions, now=SETUP_END)

    rows = [row for row in state.open_positions if row["symbol"] == "EURUSD"]
    assert rows, "the orphan must appear in the book at all"
    assert rows[0]["risk_amount"] is None, "a stopless position is unknown risk, never zero"


def test_the_exposure_report_counts_what_it_could_not_price():
    """Every figure in the report is a sum, so an omission is a lie.

    The engine stands aside before exposure is computed, so this is
    defence in depth rather than a live path — but a total that quietly
    drops a position must still say it dropped one.
    """

    report = analyse_exposure(
        [
            {"symbol": "EURUSD", "direction": "BUY", "risk_amount": 50.0},
            {"symbol": "USDJPY", "direction": "BUY", "risk_amount": None},
        ],
        {"symbol": "GBPUSD", "direction": "BUY", "risk_amount": 25.0},
    )

    assert report.unpriced_positions == 1
    assert report.total_open_risk == pytest.approx(75.0), (
        "the unknown is left out of the sum rather than entered as a zero"
    )
    assert report.as_dict()["unpricedPositions"] == 1


def test_the_session_trade_cap_is_settable_like_every_other_limit():
    """The tighter of the two frequency caps had no override.

    `max_trades_per_day` (6), `max_open_positions` (3), the daily loss
    limit and the drawdown limit are all environment-settable. The
    SESSION cap — 3, and therefore the one that binds first — was a bare
    constant. An operator raising the daily limit would still have hit
    three per session with nothing in the config to explain it.
    """

    from bot.config import load_config

    assert load_config({}).risk.max_trades_per_session == 3
    tuned = load_config({"RISK_MAX_TRADES_PER_SESSION": "12"}).risk
    assert tuned.max_trades_per_session == 12


def test_raising_the_frequency_caps_never_raises_the_money_caps():
    """Frequency and exposure are separate questions.

    Opening the trade count wide is the operator's call. It must not
    quietly widen what the account can LOSE — the daily loss limit and
    the drawdown limit feed the kill switch, and they stay where they
    are whatever the frequency is set to.
    """

    from bot.config import load_config

    wide = load_config(
        {
            "RISK_MAX_TRADES_PER_SESSION": "30",
            "RISK_MAX_TRADES_PER_DAY": "30",
            "RISK_MAX_OPEN_POSITIONS": "10",
        }
    ).risk
    stock = load_config({}).risk

    assert wide.max_trades_per_day == 30 and wide.max_open_positions == 10
    assert wide.max_daily_loss_pct == stock.max_daily_loss_pct
    assert wide.max_drawdown_pct == stock.max_drawdown_pct
    assert wide.base_risk_pct == stock.base_risk_pct
    assert wide.max_risk_pct == stock.max_risk_pct


def test_no_new_trade_inside_the_rollover_blackout(config, candidate):
    """The window that turned a winner into a loser.

    Live: a USDCHF SELL opened 23:49 broker time (20:49 UTC), eleven
    minutes before the daily rollover. At 21:04 UTC the spread was 9.2
    pips against a 5.7-pip stop, and the position closed at 0.82131 — a
    price the market never traded, six pips above the session high. It
    was +5.8 pips on the bid and came out -7.3.

    A voluntary exit can wait for a sane spread. A broker-side STOP
    cannot: it triggers on the ask, so the spread alone can take out a
    position whose mid price never moved. The only defence is not to be
    holding a fresh, tight-stopped position when the rollover arrives.
    """

    import dataclasses
    from datetime import datetime, timezone

    inside = datetime(2026, 9, 22, 20, 49, tzinfo=timezone.utc)
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=inside
    )

    assert decision.approved is False
    assert any("rollover" in reason for reason in decision.reasons), decision.reasons


def test_the_blackout_is_a_window_not_a_ban(config, candidate):
    """The control: it must close again.

    A guard that refused every hour would pass the test above while
    quietly stopping the bot trading at all.
    """

    from datetime import datetime, timezone

    clear = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=make_account(), spec=DEFAULT_SPEC, now=clear
    )
    assert not any("rollover" in reason for reason in decision.reasons), decision.reasons


def test_the_rollover_hour_is_configurable_because_brokers_differ():
    """This broker rolls at 21:00 UTC. Another will not."""

    from datetime import datetime, timezone

    from bot.config import load_config
    from bot.smc.sessions import in_rollover_blackout

    shifted = load_config({"SESSION_ROLLOVER_UTC_HOUR": "0"}).sessions
    assert shifted.rollover_utc_hour == 0
    blocked, _ = in_rollover_blackout(
        datetime(2026, 9, 22, 0, 10, tzinfo=timezone.utc), shifted
    )
    assert blocked is True

    off = load_config(
        {"SESSION_ROLLOVER_BLACKOUT_BEFORE": "0", "SESSION_ROLLOVER_BLACKOUT_AFTER": "0"}
    ).sessions
    blocked, _ = in_rollover_blackout(
        datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc), off
    )
    assert blocked is False, "an operator must be able to switch it off entirely"


def test_no_new_trade_in_the_hours_before_the_friday_close(config, candidate):
    """A gap does not respect a stop loss.

    `is_forex_weekend` stops trading at Friday 21:00 UTC, but a position
    opened at 18:00 is held across the whole weekend. The Sunday reopen
    fills at the first available price, which can be far beyond the
    stop — so the trade can lose considerably more than it was sized to
    lose. Rule 2 does not let the system accept that on purpose.

    The entry is blocked rather than the position force-closed later: a
    trade with time to resolve is left to resolve.
    """

    from datetime import datetime, timezone

    friday_evening = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)
    assert friday_evening.weekday() == 4

    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=make_account(),
        spec=DEFAULT_SPEC, now=friday_evening,
    )

    assert decision.approved is False
    assert any("Friday close" in reason for reason in decision.reasons), decision.reasons


def test_friday_morning_still_trades(config, candidate):
    """The control: it is the hours BEFORE the close, not the whole day."""

    from datetime import datetime, timezone

    friday_morning = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)
    decision = RiskEngine(config).evaluate(
        candidate=candidate, tier="A", account=make_account(),
        spec=DEFAULT_SPEC, now=friday_morning,
    )
    assert not any("Friday close" in reason for reason in decision.reasons), decision.reasons


def test_the_weekend_blackout_can_be_switched_off():
    from datetime import datetime, timezone

    from bot.config import load_config
    from bot.smc.sessions import in_weekend_entry_blackout

    off = load_config({"SESSION_WEEKEND_BLACKOUT_HOURS": "0"}).sessions
    blocked, _ = in_weekend_entry_blackout(
        datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc), off
    )
    assert blocked is False
