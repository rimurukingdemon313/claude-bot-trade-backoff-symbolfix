"""Setup scoring, tiering, and position management decisions."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from bot.execution.manager import (
    ManagementAction,
    PositionManager,
    plan_actions,
    r_multiple,
)
from bot.marketdata.provider import MarketDataProvider
from bot.scoring.scorer import WEIGHTS, SetupScorer, tier_rank
from bot.smc.engine import SmcEngine
from fakes import DEFAULT_SPEC, SETUP_END, FakeBroker


@pytest.fixture()
def candidate(config, broker):
    series = MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    result = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END)
    assert result.candidate is not None, result.rejection
    return result.candidate


# -- scoring --------------------------------------------------------------


def test_the_weights_are_a_documented_hundred_point_scale():
    assert sum(WEIGHTS.values()) == pytest.approx(100.0)


def test_scoring_is_deterministic(config, candidate):
    scorer = SetupScorer(config)
    assert scorer.score(candidate).total == scorer.score(candidate).total


def test_a_complete_setup_scores_into_a_tradeable_tier(config, candidate):
    score = SetupScorer(config).score(candidate)
    assert score.tier in ("A+", "A", "B")
    assert score.tradeable
    assert score.components["trigger"] > 0


def test_no_component_can_exceed_its_documented_maximum(config, candidate):
    score = SetupScorer(config).score(candidate)
    for name, value in score.components.items():
        assert value <= WEIGHTS[name] + 1e-9


def test_an_unclassified_setup_is_gated_to_no_trade_regardless_of_score(config, candidate):
    """The gate moved from "timeframes disagree" to "this is unclassified".

    Disagreement is no longer disqualifying - a reversal disagrees with
    the primary bias by definition, and refusing it outright was the
    over-filtering the MTF layer replaced. What IS disqualifying is a
    setup the scorer cannot place: an unrecognised classification is
    never scored on a guess, and the legacy "conflicted" state lands here.
    """

    for setup_type in ("conflicted", "", "SOMETHING_NEW"):
        unclassified = dataclasses.replace(candidate, setup_type=setup_type)
        score = SetupScorer(config).score(unclassified)
        assert score.tier == "NO_TRADE"
        assert any("unclassified setup" in note for note in score.notes)


def test_a_critical_component_cannot_be_outvoted_by_the_others(config, candidate):
    """Seven good components must not carry one structural failure.

    A near-absent trigger, entry zone or R:R is not a weak trade that a
    strong session and a favourable regime can compensate for.
    """

    from bot.smc.liquidity import LiquidityLevel, LiquiditySweep

    floor = config.mtf.min_critical_component_fraction
    assert candidate.sweep is not None
    barely = dataclasses.replace(
        candidate,
        structure_event=None,
        sweep=dataclasses.replace(candidate.sweep, quality=floor / 2.0),
    )
    score = SetupScorer(config).score(barely)
    assert score.tier == "NO_TRADE"
    assert any("critical component" in note for note in score.notes)

    # The same setup with a real trigger is tradeable, so the gate is
    # discriminating rather than simply rejecting everything.
    assert SetupScorer(config).score(candidate).tradeable


def test_a_setup_without_a_trigger_is_gated_to_no_trade(config, candidate):
    triggerless = dataclasses.replace(candidate, sweep=None, structure_event=None)
    assert SetupScorer(config).score(triggerless).tier == "NO_TRADE"


def test_an_untradeable_session_is_gated_to_no_trade(config, candidate):
    from bot.smc.sessions import SessionState

    asian = dataclasses.replace(
        candidate, session=SessionState("ASIAN", 0.5, False, 3, False)
    )
    score = SetupScorer(config).score(asian)
    assert score.tier == "NO_TRADE"
    assert any("session liquidity" in note for note in score.notes)


def test_tier_thresholds_are_ordered(config, candidate):
    scorer = SetupScorer(config)
    base = scorer.score(candidate).total
    assert config.scoring.tier_b <= config.scoring.tier_a <= config.scoring.tier_a_plus
    assert base > 0


def test_tier_ranking_orders_candidates():
    assert tier_rank("A+") > tier_rank("A") > tier_rank("B") > tier_rank("NO_TRADE")


def test_context_is_graded_by_what_the_setup_has_to_fight(config, candidate):
    """Continuation > rotation > reversal.

    Graded rather than boolean: a reversal is not scored zero for
    disagreeing with the H1 trend (that was the rigid filter), it is
    scored lower and then held to a higher floor. Both mechanisms point
    the same way, so neither excuses the other.
    """

    scorer = SetupScorer(config)
    ordered = [
        "CONTINUATION",
        "RANGE_ROTATION",
        "REVERSAL",
    ]
    totals = [
        scorer.score(dataclasses.replace(candidate, setup_type=name, score_floor=0.0)).total
        for name in ordered
    ]
    assert totals == sorted(totals, reverse=True)
    assert totals[0] > totals[-1]


def test_a_higher_floor_refuses_a_setup_the_same_score_would_otherwise_pass(
    config, candidate
):
    """The classification floor is what makes a reversal cost more."""

    scorer = SetupScorer(config)
    passing = scorer.score(dataclasses.replace(candidate, score_floor=0.0))
    assert passing.tradeable

    demanding = scorer.score(
        dataclasses.replace(candidate, score_floor=passing.total + 5.0)
    )
    assert demanding.tier == "NO_TRADE"
    assert any("requires a score of" in note for note in demanding.notes)

    # A floor BELOW the configured B tier cannot loosen anything: the
    # scorer takes the higher of the two, so a classification can only
    # ever demand more evidence than the build does.
    loosened = scorer.score(
        dataclasses.replace(candidate, score_floor=-100.0, risk_reward=2.0)
    )
    assert loosened.tier != "NO_TRADE" or loosened.total < config.scoring.tier_b


def test_better_risk_reward_scores_higher(config, candidate):
    scorer = SetupScorer(config)
    modest = scorer.score(dataclasses.replace(candidate, risk_reward=2.0))
    strong = scorer.score(dataclasses.replace(candidate, risk_reward=3.5))
    assert strong.total > modest.total


def test_risk_reward_saturates_so_fantasy_targets_are_not_rewarded(config, candidate):
    scorer = SetupScorer(config)
    good = scorer.score(dataclasses.replace(candidate, risk_reward=4.0))
    absurd = scorer.score(dataclasses.replace(candidate, risk_reward=40.0))
    assert absurd.components["risk_reward"] == pytest.approx(good.components["risk_reward"])


# -- position management --------------------------------------------------


def position(**overrides):
    broker = FakeBroker()
    defaults = dict(symbol="EURUSD", direction="BUY", entry=1.1000, stop_loss=1.0950, take_profit=1.1150)
    defaults.update(overrides)
    return broker.add_position(**defaults)


TRADE = {"actual_entry": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1150}


def test_r_multiple_maths():
    assert r_multiple(direction="BUY", entry=1.10, stop=1.09, price=1.12) == pytest.approx(2.0)
    assert r_multiple(direction="SELL", entry=1.10, stop=1.11, price=1.08) == pytest.approx(2.0)
    assert r_multiple(direction="BUY", entry=1.10, stop=1.10, price=1.12) == 0.0


def test_break_even_moves_the_stop_once_one_r_is_reached(config):
    actions = plan_actions(
        position=position(), trade=TRADE, price=1.1050, config=config, now=SETUP_END
    )
    move = next(action for action in actions if action.kind == "MOVE_STOP")
    assert move.stop_loss > TRADE["actual_entry"], "break-even must clear the entry, not sit on it"


def test_break_even_does_not_fire_before_one_r(config):
    actions = plan_actions(
        position=position(), trade=TRADE, price=1.1020, config=config, now=SETUP_END
    )
    assert not [action for action in actions if action.kind == "MOVE_STOP"]


def test_break_even_is_not_reapplied_once_the_stop_is_already_safe(config):
    safe = position(stop_loss=1.1005)
    actions = plan_actions(position=safe, trade=TRADE, price=1.1080, config=config, now=SETUP_END)
    assert not [action for action in actions if action.kind == "MOVE_STOP"]


def test_trailing_is_off_by_default_and_works_when_enabled(config):
    off = plan_actions(position=position(), trade=TRADE, price=1.1120, config=config, now=SETUP_END)
    assert all(action.reason.startswith("reached") for action in off if action.kind == "MOVE_STOP")

    trailing = dataclasses.replace(
        config, execution=dataclasses.replace(config.execution, enable_trailing=True)
    )
    on = plan_actions(
        position=position(stop_loss=1.1001), trade=TRADE, price=1.1120, config=trailing, now=SETUP_END
    )
    assert any("trailing" in action.reason for action in on)


def test_partial_take_profit_is_on_by_default_and_fires_at_the_tested_level(config):
    """This default MOVED, on evidence, and the move is the assertion.

    It was off, as project rule 12 requires until "evidence justifies
    them". docs/EXPERIMENT_WIN_RATE.md is that evidence: a selection
    rule committed before the results were read, one candidate promoted
    out of ten, and a held-out seed set that chose nothing, on which the
    win rate went 25.4% -> 44.5% and expectancy -0.248R -> -0.128R.

    The trigger is 0.75R, which is what was tested — not the 1.5R this
    used to default to and which measured no better than no partial at
    all.
    """

    assert config.execution.enable_partial_tp is True
    assert config.execution.partial_tp_at_r == pytest.approx(0.75)

    actions = plan_actions(position=position(), trade=TRADE, price=1.1090, config=config, now=SETUP_END)
    partial = next(action for action in actions if action.kind == "PARTIAL_CLOSE")
    assert partial.quantity == pytest.approx(0.05), "half of the position"


def test_a_partial_never_fires_before_its_configured_r(config):
    """The control on the test above: it is the LEVEL that triggers it.

    Without this, "partials are on" would pass even if the trigger were
    ignored and every position were half-closed on arrival.
    """

    early = plan_actions(
        position=position(), trade=TRADE, price=1.1010, config=config, now=SETUP_END
    )
    assert not [action for action in early if action.kind == "PARTIAL_CLOSE"]


def test_partial_take_profit_can_still_be_turned_off(config):
    """A default that moved on synthetic evidence must stay reversible."""

    off = dataclasses.replace(
        config, execution=dataclasses.replace(config.execution, enable_partial_tp=False)
    )
    actions = plan_actions(
        position=position(), trade=TRADE, price=1.1090, config=off, now=SETUP_END
    )
    assert not [action for action in actions if action.kind == "PARTIAL_CLOSE"]


def test_partial_take_profit_when_explicitly_enabled(config):
    enabled = dataclasses.replace(
        config, execution=dataclasses.replace(config.execution, enable_partial_tp=True)
    )
    actions = plan_actions(
        position=position(), trade=TRADE, price=1.1090, config=enabled, now=SETUP_END
    )
    partial = next(action for action in actions if action.kind == "PARTIAL_CLOSE")
    assert partial.quantity == pytest.approx(0.05)


def test_structure_invalidation_closes_an_underwater_trade(config):
    actions = plan_actions(
        position=position(),
        trade=TRADE,
        price=1.0945,
        config=config,
        structure_invalidated=True,
        now=SETUP_END,
    )
    assert any(action.kind == "CLOSE" for action in actions)


def test_structure_invalidation_does_not_close_a_winning_trade(config):
    """Exits are deterministic; a profitable position is not panic-closed
    because one candle looked wrong."""

    actions = plan_actions(
        position=position(),
        trade=TRADE,
        price=1.1100,
        config=config,
        structure_invalidated=True,
        now=SETUP_END,
    )
    assert not [action for action in actions if action.kind == "CLOSE"]


def test_a_stale_position_is_timed_out(config):
    stale = position(opened_at=SETUP_END - timedelta(hours=60))
    actions = plan_actions(
        position=stale, trade=TRADE, price=1.1005, config=config, now=SETUP_END
    )
    close = next(action for action in actions if action.kind == "CLOSE")
    assert "thesis has expired" in close.reason


def test_no_management_without_a_recorded_stop(config):
    assert plan_actions(
        position=position(stop_loss=None), trade=None, price=1.11, config=config, now=SETUP_END
    ) == []


def test_break_even_is_skipped_when_price_retraced_back_to_entry(config):
    """Found by the paper simulator.

    Price can touch 1R and come back to entry between polls. The break-even
    level is then already through the market: a broker would reject it, and a
    simulator that accepted it would close the position instantly at a
    flattering price. The correct action is to leave the original stop alone.
    """

    actions = plan_actions(
        position=position(),
        trade=TRADE,
        price=1.10001,      # touched 1R earlier, now back at entry
        config=config,
        now=SETUP_END,
    )
    assert not [action for action in actions if action.kind == "MOVE_STOP"]


def test_break_even_still_fires_when_price_holds_above_the_level(config):
    actions = plan_actions(
        position=position(), trade=TRADE, price=1.1060, config=config, now=SETUP_END
    )
    move = next(action for action in actions if action.kind == "MOVE_STOP")
    assert move.stop_loss < 1.1060, "the stop must sit behind the market"
    assert move.stop_loss > TRADE["actual_entry"]


def test_trailing_never_proposes_a_stop_through_the_market(config):
    trailing = dataclasses.replace(
        config, execution=dataclasses.replace(config.execution, enable_trailing=True)
    )
    actions = plan_actions(
        position=position(stop_loss=1.1001),
        trade=TRADE,
        price=1.1101,       # 2.02R, so the trail level lands right at the market
        config=trailing,
        now=SETUP_END,
    )
    for action in actions:
        if action.kind == "MOVE_STOP" and action.stop_loss is not None:
            assert action.stop_loss < 1.1101


# -- why a position has no live price -------------------------------------
#
# Over a weekend every open position reads "—" for the current price. That
# is correct (rule 6: never invent a value) but it is indistinguishable
# from a dead broker feed, and an operator watching a healthy bot on a
# Sunday reasonably concludes it is broken. The gap has to say why.


def open_position(**overrides):
    from datetime import timezone

    from bot.broker.models import BrokerPosition

    defaults = dict(
        position_id="p1",
        symbol="AUDCHF",
        instrument_id=1,
        direction="BUY",
        quantity=0.17,
        entry_price=0.58559,
        stop_loss=0.58430,
        take_profit=0.58775,
        unrealized_pnl=-9.57,
        opened_at=datetime(2026, 9, 11, 23, 0, tzinfo=timezone.utc),
    )
    return BrokerPosition(**{**defaults, **overrides})


class MuteBroker(FakeBroker):
    """A broker that cannot price anything — a shut market, or a dead feed.

    The two look identical from here, which is exactly the ambiguity the
    status string has to resolve.
    """

    def quote(self, spec):
        from bot.errors import BrokerError

        raise BrokerError("instrument is not quoting")


def test_a_weekend_gap_is_labelled_as_a_closed_market(config, repos):
    from datetime import timezone

    sunday = datetime(2026, 9, 13, 14, 4, tzinfo=timezone.utc)
    assert sunday.strftime("%A") == "Sunday"

    manager = PositionManager(config, MuteBroker(), repos)
    row = manager.track([open_position()], now=sunday)[0]

    assert row["currentPrice"] is None  # still never invented
    assert row["priceStatus"] == "market closed for the weekend"


def test_a_weekday_gap_is_labelled_as_a_broker_failure(config, repos):
    from datetime import timezone

    wednesday = datetime(2026, 9, 9, 14, 4, tzinfo=timezone.utc)
    assert wednesday.strftime("%A") == "Wednesday"

    manager = PositionManager(config, MuteBroker(), repos)
    row = manager.track([open_position(symbol="EURUSD")], now=wednesday)[0]

    assert row["currentPrice"] is None
    assert "not quoting" in row["priceStatus"]
    assert "weekend" not in row["priceStatus"]


def test_a_priced_position_says_so(config, repos, broker):
    from datetime import timezone

    wednesday = datetime(2026, 9, 9, 14, 4, tzinfo=timezone.utc)
    row = PositionManager(config, broker, repos).track(
        [open_position(symbol="EURUSD")], now=wednesday
    )[0]

    assert row["currentPrice"] is not None
    assert row["priceStatus"] == "live"


def test_a_position_we_did_not_open_is_marked_untracked(config, repos, broker):
    from datetime import timezone

    wednesday = datetime(2026, 9, 9, 14, 4, tzinfo=timezone.utc)
    row = PositionManager(config, broker, repos).track(
        [open_position(symbol="EURUSD")], now=wednesday
    )[0]

    # No trade row of ours exists for it, so its blank risk/grade fields are
    # explained rather than looking like data that failed to load.
    assert row["tracked"] is False
    assert row["riskAmount"] is None


def test_tracking_reads_the_injected_clock_not_the_wall(config, repos, broker):
    """Rule 10: pin the clock. Duration must follow `now`, not real time."""

    from datetime import timezone

    opened = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    rows = PositionManager(config, broker, repos).track(
        [open_position(symbol="EURUSD", opened_at=opened)],
        now=datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc),
    )
    assert rows[0]["durationMinutes"] == pytest.approx(120.0)


def test_a_projected_target_scores_lower_than_a_measured_one(config, candidate):
    """Same ratio, different evidence, different score.

    A structural target is a level the market has a reason to reach. A
    projection is a distance chosen because it pays for the stop. The
    ratio can be identical and the evidence is not, so the score must not
    be either.
    """

    from bot.scoring.scorer import PROJECTED_TARGET_FRACTION

    scorer = SetupScorer(config)
    measured = dataclasses.replace(
        candidate,
        liquidity_target={"kind": "liquidity", "projected": False, "price": candidate.take_profit,
                          "label": "London high"},
    )
    projected = dataclasses.replace(
        candidate,
        liquidity_target={"kind": "projection", "projected": True, "price": candidate.take_profit,
                          "label": "1.2R projection"},
    )

    high = scorer.score(measured)
    low = scorer.score(projected)
    assert low.total == pytest.approx(high.total * PROJECTED_TARGET_FRACTION)
    assert any("projected at the minimum R" in note for note in low.notes)
    assert not any("projected" in note for note in high.notes)


def test_the_projection_penalty_is_not_a_structural_veto_in_disguise(config, candidate):
    """It scales the total, deliberately, and not the R:R component.

    `risk_reward` is a CRITICAL component with a floor of its own, and at
    the minimum ratio it already sits near that floor — so a discount
    applied there pushed it under, and a quality penalty became a
    structural refusal through an interaction nobody designed. This pins
    the separation: the component is untouched, only the total moves.
    """

    scorer = SetupScorer(config)
    at_floor = dataclasses.replace(
        candidate,
        risk_reward=config.risk.min_risk_reward,
        liquidity_target={"kind": "projection", "projected": True, "price": candidate.take_profit,
                          "label": "1.2R projection"},
    )
    measured = dataclasses.replace(
        at_floor,
        liquidity_target={"kind": "liquidity", "projected": False, "price": candidate.take_profit,
                          "label": "London high"},
    )
    projected_score = scorer.score(at_floor)
    measured_score = scorer.score(measured)

    assert projected_score.components["risk_reward"] == pytest.approx(
        measured_score.components["risk_reward"]
    )
    # The tier may legitimately drop a grade — that is the penalty doing
    # its job. What it must not do is reach NO_TRADE through the critical
    # gate, because that is a veto wearing a score's clothes.
    assert measured_score.tradeable
    assert projected_score.tradeable
    assert not any("gate:" in note for note in projected_score.notes)


# -- absence must never outscore a measurement ----------------------------


def test_no_component_rewards_missing_information(config, candidate):
    """The inversion `_location_component` had, swept across all eight.

    It returned 0.5 for a missing dealing range, so a setup MEASURED to
    be in a bad location scored below one where the location was unknown
    — the scorer preferred ignorance to a bad reading. That is worth
    checking everywhere rather than once, because it is invisible until
    the data gets worse and then it is systematic.
    """

    from bot.scoring.scorer import (
        _context_component,
        _displacement_component,
        _entry_zone_component,
        _location_component,
        _risk_reward_component,
        _trigger_component,
    )

    stripped = dataclasses.replace(
        candidate,
        sweep=None,
        structure_event=None,
        displacement=None,
        point_of_interest=None,
        dealing_range=None,
        setup_type="SOMETHING_THIS_BUILD_DOES_NOT_KNOW",
    )

    # Every component that can be handed nothing scores at or below the
    # weakest real reading it could ever produce.
    assert _trigger_component(stripped)[0] == 0.0
    assert _context_component(stripped)[0] == 0.0
    assert _entry_zone_component(stripped)[0] == 0.0
    assert _location_component(stripped)[0] == 0.0
    assert _risk_reward_component(
        dataclasses.replace(stripped, risk_reward=0.1), config.risk.min_risk_reward
    )[0] == 0.0


def test_the_no_displacement_score_sits_below_every_real_displacement(config):
    """`_displacement_component` returns 0.2 for absence, and that is only
    safe because the detector cannot emit anything weaker.

    At the exact thresholds it requires — `displacement_atr_multiple` of
    ATR and `displacement_body_ratio` of body — the quality formula floors
    at 0.275. The margin is 0.075 and it is undocumented, so retuning
    either threshold or any weight in that formula could silently invert
    this component the way location was inverted.
    """

    smc = config.smc
    floor = (
        0.45 * min(smc.displacement_atr_multiple / (smc.displacement_atr_multiple * 2.0), 1.0)
        + 0.35 * 0.0
        + 0.05
    )
    assert floor == pytest.approx(0.275, abs=1e-9)

    from bot.scoring.scorer import _displacement_component

    absent = _displacement_component(
        dataclasses.replace(_stub_candidate(config), displacement=None)
    )[0]
    assert absent < floor, (
        f"absence scores {absent} while the weakest real displacement scores "
        f"{floor} — absence must not outscore a measurement"
    )


def _stub_candidate(config):
    """A candidate with only the fields the component under test reads."""

    import bot.smc.engine as engine_module

    return engine_module.SetupCandidate(
        symbol="EURUSD", direction="BUY", entry=1.1, stop_loss=1.09, take_profit=1.12,
        risk_reward=2.0, stop_distance=0.01, atr=0.001,
        session=_any_session(), regime=_any_regime(),
        h1_bias="bullish", m15_bias="bullish", alignment="aligned",
        sweep=None, structure_event=None, displacement=None,
        point_of_interest=None, dealing_range=None, liquidity_target=None,
    )


def _any_session():
    from bot.smc.sessions import SessionState

    return SessionState("LONDON", True, 0.9, False, "test")


def _any_regime():
    from bot.smc.regime import Regime

    return Regime("trending", "normal", 0.001, 0.5, 0.6, True, "test regime")


def test_the_minimum_tradeable_score_actually_refuses_setups_below_it(config, candidate):
    """A control that silently does nothing is worse than no control.

    `SCORING_MIN_TRADEABLE` was parsed from the environment, carried on
    `ScoringConfig`, and documented in .env.example as "the knob worth
    knowing about" — and read by no production code at all. The scorer's
    floor was `max(tier_b, candidate.score_floor)` and never consulted
    it.

    An operator raising it to 68 to trade only A grades would have seen
    the trade count not move, run their fortnight of paper anyway, and
    concluded that filtering by grade changes nothing — having never
    once filtered by grade. The experiment the documentation recommends
    could not be performed.

    Found because a backtest sweep returned byte-identical results for
    the tuned and the untuned configuration.
    """

    passing = SetupScorer(config).score(candidate)
    assert passing.tradeable, "the fixture must be tradeable at the stock floor"

    strict = dataclasses.replace(
        config,
        scoring=dataclasses.replace(
            config.scoring, min_tradeable_score=passing.total + 5.0
        ),
    )
    verdict = SetupScorer(strict).score(candidate)

    assert verdict.tier == "NO_TRADE"
    assert verdict.tradeable is False
    assert any("requires a score of" in note for note in verdict.notes), verdict.notes


def test_the_minimum_tradeable_score_can_only_ever_demand_more(config, candidate):
    """It is a floor, never a discount.

    Setting it below the B tier must not let a sub-B setup through: the
    scorer takes the max of every floor, so no one setting can lower a
    limit another established.
    """

    reference = SetupScorer(config).score(candidate)
    lowered = dataclasses.replace(
        config, scoring=dataclasses.replace(config.scoring, min_tradeable_score=1.0)
    )
    assert SetupScorer(lowered).score(candidate).tier == reference.tier
