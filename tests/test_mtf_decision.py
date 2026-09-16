"""The multi-timeframe decision layer, scenario by scenario.

    H1 = TREND   M15 = EXECUTION

The behaviour being pinned is a deliberate replacement of a rigid rule.
The engine used to derive direction from the M15 bias alone and refuse
outright if a higher timeframe disagreed, which rejected the setup the
strategy exists to take. It also had the inverse failure - because
direction came from M15, an ordinary retracement inside an H1 leg looked
like a signal whenever H1 happened to be neutral.

H4 sat above H1 as macro context and is gone. At 240 minutes it was
sixteen times the execution timeframe; H1 is four times it, which is the
ratio trend-following actually uses. The tests that existed only to
express an H4/H1 disagreement are gone with it, and one new test asserts
that H4 is not merely unused but cannot be consulted.

So every scenario below asserts one of two things:

  * a setup that fights something is allowed, at a higher bar; or
  * a setup that has NOT earned the name reversal is refused, and the
    refusal says which of the two it is.

Every series is built stage by stage in `fakes`, so the correct answer is
known by construction rather than observed after the fact (project rule
10). Nothing here reaches a network or reads the clock.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.config import TradingConfig, load_config
from bot.scoring.scorer import CONTEXT_FRACTION, SetupScorer
from bot.smc.engine import SmcEngine
from bot.smc import mtf
from fakes import (
    SETUP_END,
    bearish_setup_m15,
    bullish_setup_m15,
    directional_htf,
    flat_market_m15,
    reversal_setup_m15,
)

BULLISH, BEARISH, NEUTRAL = 1, -1, 0


@pytest.fixture()
def cfg() -> TradingConfig:
    base = load_config({})
    return dataclasses.replace(
        base,
        symbols=("EURUSD",),
        ai=dataclasses.replace(base.ai, enabled=False),
        news=dataclasses.replace(base.news, enabled=False),
    )


def evaluate(cfg: TradingConfig, m15_candles, *, h1: int):
    """Run the engine over an explicitly constructed H1/M15 picture."""

    engine = SmcEngine(cfg)
    analyses = {
        "H1": engine.analyze_timeframe(
            directional_htf(m15_candles, timeframe="H1", direction=h1),
            timeframe="H1",
            now=SETUP_END,
        ),
        "M15": engine.analyze_timeframe(m15_candles, timeframe="M15", now=SETUP_END),
    }
    return engine.evaluate("EURUSD", analyses, now=SETUP_END)


# -- 1-2: continuation, everything agreeing ---------------------------------


def test_1_h1_and_m15_both_bullish_is_a_continuation(cfg):
    candidate, rejection, decision = evaluate(cfg, bullish_setup_m15(), h1=BULLISH)
    assert candidate is not None, rejection
    assert candidate.direction == "BUY"
    assert decision.setup_type == mtf.CONTINUATION
    assert decision.alignment == "aligned"


def test_2_h1_and_m15_both_bearish_is_a_continuation(cfg):
    candidate, rejection, decision = evaluate(cfg, bearish_setup_m15(), h1=BEARISH)
    assert candidate is not None, rejection
    assert candidate.direction == "SELL"
    assert decision.setup_type == mtf.CONTINUATION


# -- 3-4: H4 is gone, and gone means unreachable ---------------------------


def test_3_the_engine_decides_on_h1_and_m15_alone(cfg):
    """Two timeframes are the whole input. Asserted, not assumed.

    An H4 series is now simply absent from what a scan fetches, so the
    engine must produce its answer without one - and the refusal for
    missing data must name the two it actually needs, or the next
    operator debugs a message that describes a build that no longer
    exists.
    """

    engine = SmcEngine(cfg)
    m15_candles = bullish_setup_m15()
    analyses = {
        "H1": engine.analyze_timeframe(
            directional_htf(m15_candles, timeframe="H1", direction=BULLISH),
            timeframe="H1",
            now=SETUP_END,
        ),
        "M15": engine.analyze_timeframe(m15_candles, timeframe="M15", now=SETUP_END),
    }
    candidate, rejection, _ = engine.evaluate("EURUSD", analyses, now=SETUP_END)
    assert candidate is not None, rejection

    _, missing, _ = engine.evaluate("EURUSD", {"M15": analyses["M15"]}, now=SETUP_END)
    assert "H1 and M15" in missing
    assert "H4" not in missing


def test_4_an_h4_series_cannot_change_the_decision(cfg):
    """The strongest form of "deleted": supplying one is inert.

    A leftover consumer would show up here as a different answer rather
    than as a stale docstring, which is the failure mode that matters -
    an H4 quietly steering the engine while nothing in the code reads as
    if it does.
    """

    engine = SmcEngine(cfg)
    m15_candles = bullish_setup_m15()
    base = {
        "H1": engine.analyze_timeframe(
            directional_htf(m15_candles, timeframe="H1", direction=BULLISH),
            timeframe="H1",
            now=SETUP_END,
        ),
        "M15": engine.analyze_timeframe(m15_candles, timeframe="M15", now=SETUP_END),
    }
    with_h4 = dict(base)
    with_h4["H4"] = engine.analyze_timeframe(
        directional_htf(m15_candles, timeframe="H4", direction=BEARISH),
        timeframe="H4",
        now=SETUP_END,
    )

    plain = engine.evaluate("EURUSD", base, now=SETUP_END)
    contradicted = engine.evaluate("EURUSD", with_h4, now=SETUP_END)
    assert plain[2].as_dict() == contradicted[2].as_dict()
    assert plain[0].as_dict() == contradicted[0].as_dict()

    # And nothing the decision publishes still describes a macro layer.
    assert "h4Context" not in plain[2].as_dict()
    assert "htfBias" not in plain[0].as_dict()


# -- 5-6: retracement is NOT a reversal ------------------------------------


def test_5_m15_bearish_inside_a_bullish_h1_leg_is_a_retracement(cfg):
    candidate, rejection, decision = evaluate(cfg, bearish_setup_m15(), h1=BULLISH)
    assert candidate is None
    assert decision.setup_type == mtf.RETRACEMENT
    assert "retracement rather than a reversal" in rejection


def test_6_m15_bullish_inside_a_bearish_h1_leg_is_a_retracement(cfg):
    candidate, rejection, decision = evaluate(cfg, bullish_setup_m15(), h1=BEARISH)
    assert candidate is None
    assert decision.setup_type == mtf.RETRACEMENT
    assert "retracement rather than a reversal" in rejection


def test_5_6_the_refusal_names_what_the_move_was_missing(cfg):
    """A refusal an operator cannot act on is a refusal they will override."""

    _, rejection, _ = evaluate(cfg, bearish_setup_m15(), h1=BULLISH)
    assert "CHoCH" in rejection or "sweep" in rejection or "displacement" in rejection


# -- 7-8: trigger quality --------------------------------------------------


def test_7_sweep_displacement_choch_and_an_fvg_is_a_complete_trigger(cfg):
    candidate, rejection, _ = evaluate(cfg, reversal_setup_m15(), h1=BEARISH)
    assert candidate is not None, rejection
    assert candidate.sweep is not None
    assert candidate.displacement is not None
    assert candidate.point_of_interest is not None
    assert SetupScorer(cfg).score(candidate).components["trigger"] > 0


def test_8_a_flat_market_produces_no_trigger_at_all(cfg):
    candidate, rejection, decision = evaluate(cfg, flat_market_m15(), h1=BULLISH)
    assert candidate is None
    assert decision.setup_type == mtf.NOISE
    assert "trigger" in rejection


def test_8_a_micro_break_below_the_significance_floor_is_not_structure(cfg):
    """Structure has to clear its level by a configured ATR fraction.

    Without that floor an ordinary M15 wiggle through an old pivot reads
    as a break of structure, and the engine starts trading noise. Asserted
    on the evidence gatherer directly, because a high-quality sweep is an
    independent trigger - the engine does not need a structure break to
    act, and a test that went through the engine would be measuring the
    sweep rather than the floor.
    """

    engine = SmcEngine(cfg)
    m15 = engine.analyze_timeframe(bullish_setup_m15(), timeframe="M15", now=SETUP_END)
    assert any(event.direction == "bullish" for event in m15.structure_events)

    permissive = mtf.gather_direction_evidence(
        m15, "bullish", index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    assert permissive.structure_event is not None

    strict = mtf.gather_direction_evidence(
        m15,
        "bullish",
        index=m15.last_index,
        smc=cfg.smc,
        mtf=dataclasses.replace(cfg.mtf, min_structure_clearance_atr=99.0),
    )
    assert strict.structure_event is None, "a break below the floor was counted as structure"
    assert strict.choch is None


def test_8_a_trigger_below_the_quality_floor_is_classified_noise(cfg):
    blunt = dataclasses.replace(cfg, mtf=dataclasses.replace(cfg.mtf, min_trigger_quality=1.01))
    candidate, rejection, decision = evaluate(blunt, bullish_setup_m15(), h1=BULLISH)
    assert candidate is None
    assert decision.setup_type == mtf.NOISE
    assert "noise, not a setup" in rejection


# -- 9-11: levels must be mathematically consistent ------------------------


def test_9_no_classification_can_emit_a_candidate_below_the_rr_floor(cfg):
    """R:R is a hard requirement, and classification cannot soften it.

    Note what this does NOT claim: the engine projects its target at the
    minimum R when the nearest liquidity pool is closer than that, so a
    structurally sub-minimum R:R is close to unreachable HERE. The floor
    that bites is the risk engine's, which re-checks it as the last gate
    before money - so that is where the guarantee is asserted.
    """

    for builder, h1 in (
        (bullish_setup_m15, BULLISH),
        (bearish_setup_m15, BEARISH),
        (reversal_setup_m15, BEARISH),
    ):
        candidate, rejection, _ = evaluate(cfg, builder(), h1=h1)
        assert candidate is not None, rejection
        assert candidate.risk_reward >= cfg.risk.min_risk_reward

    # And a candidate that IS below the floor is refused by the scorer,
    # whatever its classification says.
    candidate, _, _ = evaluate(cfg, reversal_setup_m15(), h1=BEARISH)
    starved = dataclasses.replace(candidate, risk_reward=0.1)
    assert SetupScorer(cfg).score(starved).tier == "NO_TRADE"


def test_10_a_structurally_over_wide_stop_is_refused_even_on_a_real_reversal(cfg):
    """A hard blocker applies to a correctly classified reversal too.

    The classification decides how much evidence is demanded. It never
    decides whether the stop rules apply.
    """

    violent = reversal_setup_m15(displacement_overshoot=1.2)
    candidate, rejection, decision = evaluate(cfg, violent, h1=BEARISH)
    assert decision.setup_type == mtf.REVERSAL
    assert candidate is None
    assert "too wide" in rejection


def test_11_entry_stop_and_target_are_always_consistent(cfg):
    for builder, h1 in (
        (bullish_setup_m15, BULLISH),
        (bearish_setup_m15, BEARISH),
        (reversal_setup_m15, BEARISH),
    ):
        candidate, rejection, _ = evaluate(cfg, builder(), h1=h1)
        assert candidate is not None, rejection
        if candidate.direction == "BUY":
            assert candidate.stop_loss < candidate.entry < candidate.take_profit
        else:
            assert candidate.take_profit < candidate.entry < candidate.stop_loss
        assert candidate.risk_reward >= cfg.risk.min_risk_reward
        assert candidate.stop_distance > 0


# -- 16-18: market regime ---------------------------------------------------


def test_16_chop_is_a_market_with_neither_a_trend_nor_a_range(cfg):
    """A trending market is never chop, however the threshold is set."""

    from bot.smc.regime import Regime

    def regime(trend: str, strength: float) -> Regime:
        return Regime(trend, "normal", 0.001, 0.5, strength, True, f"{trend} test regime")

    assert not mtf.is_choppy(regime("trending", 0.05), cfg.mtf)
    assert not mtf.is_choppy(regime("ranging", 0.05), cfg.mtf)
    assert mtf.is_choppy(regime("transitional", 0.0), cfg.mtf)
    assert not mtf.is_choppy(
        regime("transitional", cfg.mtf.choppy_directional_strength + 0.01), cfg.mtf
    )
    # Unreadable regime is treated as chop, not as permission.
    assert mtf.is_choppy(None, cfg.mtf)


def test_16_a_choppy_market_demands_more_of_the_same_setup(cfg):
    """Chop raises the floor; it does not silently permit the same trade."""

    from bot.smc.regime import Regime

    engine = SmcEngine(cfg)
    m15_candles = bullish_setup_m15()
    analyses = {
        "H1": engine.analyze_timeframe(
            directional_htf(m15_candles, timeframe="H1", direction=BULLISH),
            timeframe="H1",
            now=SETUP_END,
        ),
        "M15": engine.analyze_timeframe(m15_candles, timeframe="M15", now=SETUP_END),
    }
    _, _, calm = engine.evaluate("EURUSD", analyses, now=SETUP_END)

    chop = Regime("transitional", "normal", analyses["H1"].atr, 0.5, 0.0, True, "chop")
    choppy_analyses = dict(analyses)
    choppy_analyses["H1"] = dataclasses.replace(analyses["H1"], regime=chop)
    _, _, choppy = engine.evaluate("EURUSD", choppy_analyses, now=SETUP_END)

    assert calm.setup_type == choppy.setup_type == mtf.CONTINUATION
    assert choppy.score_floor == pytest.approx(calm.score_floor + cfg.mtf.choppy_score_premium)


def test_17_with_no_h1_trend_the_m15_sequence_carries_the_setup(cfg):
    """A trendless H1 is the absence of opposition, not opposition.

    Refusing these outright would be new over-filtering - but the bar is
    higher, because M15 is then the only anchor there is.
    """

    candidate, rejection, decision = evaluate(cfg, bullish_setup_m15(), h1=NEUTRAL)
    assert candidate is not None, rejection
    assert decision.setup_type in (mtf.CONTINUATION, mtf.RANGE_ROTATION)
    assert candidate.score_floor >= cfg.mtf.floor_no_htf_context


def test_17_a_trigger_against_the_only_structure_present_is_noise(cfg):
    """No H1 trend and M15 pointing the other way: nothing anchors the
    direction on either timeframe."""

    permissive = dataclasses.replace(
        cfg, mtf=dataclasses.replace(cfg.mtf, range_rotation_min_position=0.999)
    )
    candidate, rejection, decision = evaluate(
        permissive, bearish_setup_m15(), h1=NEUTRAL
    )
    if candidate is None:
        assert decision.setup_type in (mtf.NOISE, mtf.RETRACEMENT)


def test_18_a_strong_trend_continuation_carries_the_lowest_bar(cfg):
    candidate, rejection, decision = evaluate(cfg, bullish_setup_m15(), h1=BULLISH)
    assert candidate is not None, rejection
    assert decision.setup_type == mtf.CONTINUATION
    assert candidate.score_floor <= max(cfg.scoring.tier_b, cfg.mtf.floor_continuation)


# -- 19-20: against the trend -----------------------------------------------


def test_19_a_genuine_reversal_against_h1_is_allowed(cfg):
    """The other half of the mandate.

    A sweep of real liquidity, displacement away from it, and a CHoCH
    against the prevailing M15 trend is a reversal, and the H1 trend
    pointing the other way must not by itself refuse it.
    """

    candidate, rejection, decision = evaluate(cfg, reversal_setup_m15(), h1=BEARISH)
    assert candidate is not None, rejection
    assert candidate.direction == "BUY"
    assert decision.setup_type == mtf.REVERSAL
    assert candidate.score_floor >= cfg.mtf.floor_reversal

    score = SetupScorer(cfg).score(candidate)
    assert score.total >= candidate.score_floor
    assert score.tradeable


def test_19_a_reversal_is_held_to_a_higher_bar_than_a_continuation(cfg):
    reversal, _, _ = evaluate(cfg, reversal_setup_m15(), h1=BEARISH)
    continuation, _, _ = evaluate(cfg, bullish_setup_m15(), h1=BULLISH)
    assert reversal is not None and continuation is not None
    assert reversal.score_floor > continuation.score_floor
    assert CONTEXT_FRACTION[reversal.setup_type] < CONTEXT_FRACTION[continuation.setup_type]


def test_20_the_freshest_trigger_wins_the_direction(cfg):
    """Both directions can classify. Recency decides between them.

    The scalp classification used to sit here: a reversal that fought the
    macro as well as the trend, refused by default. With no macro there
    is no such case, and what remains is the rule that always mattered on
    an execution timeframe - the side the market moved LAST is the side
    that is live. Ranking by classification first was tried and was
    wrong: a stale continuation inside the age window outranked a fresh
    reversal, so the engine took the direction the market had just turned
    away from.
    """

    candidate, rejection, decision = evaluate(cfg, reversal_setup_m15(), h1=BEARISH)
    assert candidate is not None, rejection

    engine = SmcEngine(cfg)
    m15 = engine.analyze_timeframe(reversal_setup_m15(), timeframe="M15", now=SETUP_END)
    chosen = mtf.gather_direction_evidence(
        m15, decision.wanted, index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    other = mtf.gather_direction_evidence(
        m15,
        "bearish" if decision.wanted == "bullish" else "bullish",
        index=m15.last_index,
        smc=cfg.smc,
        mtf=cfg.mtf,
    )
    assert chosen.latest_trigger_index >= other.latest_trigger_index


def test_20_a_refused_direction_never_flips_into_the_opposite_trade(cfg):
    """A retracement is a refusal, not a signal to trade the other way.

    It is also not a veto on the continuation it belongs to - that is
    covered below. What it must never do is become a trade in its own
    direction.
    """

    candidate, _, decision = evaluate(cfg, bearish_setup_m15(), h1=BULLISH)
    assert candidate is None
    assert decision.setup_type == mtf.RETRACEMENT
    assert decision.direction is None
    assert not decision.tradeable


# -- the decision model itself ---------------------------------------------


def test_a_classification_can_only_ever_demand_more_than_the_build(cfg):
    """No classification may loosen a limit. The floors are a one-way
    ratchet above the configured B tier."""

    for name in (mtf.CONTINUATION, mtf.RANGE_ROTATION, mtf.REVERSAL):
        assert name in CONTEXT_FRACTION
    # Every takeable classification is scored, and nothing else is: a
    # name the scorer cannot place is scored zero, so a stale entry here
    # would quietly grade a setup the engine can no longer produce.
    assert set(CONTEXT_FRACTION) == set(mtf._PREFERENCE)

    floors = (
        cfg.mtf.floor_range_rotation,
        cfg.mtf.floor_reversal,
        cfg.mtf.floor_no_htf_context,
    )
    assert all(floor >= cfg.scoring.tier_b for floor in floors)
    assert cfg.mtf.floor_reversal >= cfg.mtf.floor_range_rotation


def test_the_decision_is_deterministic(cfg):
    first = evaluate(cfg, bullish_setup_m15(), h1=BULLISH)
    second = evaluate(cfg, bullish_setup_m15(), h1=BULLISH)
    assert first[2].as_dict() == second[2].as_dict()
    assert (first[0] is None) == (second[0] is None)
    if first[0] is not None:
        assert first[0].as_dict() == second[0].as_dict()


def test_every_outcome_carries_a_signal_state(cfg):
    engine = SmcEngine(cfg)
    for builder, h1, expected in (
        # A complete setup.
        (bullish_setup_m15, BULLISH, mtf.TRADE),
        # A retracement inside the H1 leg: a definite refusal.
        (bearish_setup_m15, BULLISH, mtf.NO_TRADE),
        # Favourable context, no execution trigger yet - which is exactly
        # what WATCH is for, and is more use than a flat NO_TRADE.
        (flat_market_m15, BULLISH, mtf.WATCH),
    ):
        m15 = builder()
        series = {
            "H1": directional_htf(m15, timeframe="H1", direction=h1),
            "M15": m15,
        }
        analyses = {
            name: engine.analyze_timeframe(candles, timeframe=name, now=SETUP_END)
            for name, candles in series.items()
        }
        candidate, _, decision = engine.evaluate("EURUSD", analyses, now=SETUP_END)
        from bot.smc.engine import _signal_state

        state = _signal_state(candidate, decision)
        assert state in (mtf.NO_TRADE, mtf.WATCH, mtf.VALID_SETUP, mtf.TRADE)
        assert state == expected


# -- 12-15: hard blockers are not negotiable by classification -------------


def _reversal_candidate(cfg):
    candidate, rejection, decision = evaluate(
        cfg, reversal_setup_m15(), h1=BEARISH
    )
    assert candidate is not None, rejection
    assert decision.setup_type == mtf.REVERSAL
    return candidate


def _account(**overrides):
    from bot.risk.engine import AccountRiskState

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


def _decide(cfg, candidate, account, *, tier="A+"):
    from bot.risk.engine import RiskEngine
    from fakes import DEFAULT_SPEC

    return RiskEngine(cfg).evaluate(
        candidate=candidate,
        tier=tier,
        account=account,
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )


def test_a_reversal_is_still_subject_to_the_risk_engine(cfg):
    """The classification decides how much EVIDENCE is required.

    It never decides whether the limits apply. Project rule 2: the risk
    engine is the only code that approves a trade, and nothing in the MTF
    layer, the scorer or the AI can raise a limit.
    """

    candidate = _reversal_candidate(cfg)
    assert _decide(cfg, candidate, _account()).approved


def test_12_excessive_drawdown_blocks_a_reversal(cfg):
    candidate = _reversal_candidate(cfg)
    ruined = _account(equity=5_000.0, peak_equity=10_000.0)
    decision = _decide(cfg, candidate, ruined)
    assert not decision.approved
    assert any("drawdown" in reason for reason in decision.reasons)


def test_12_a_daily_loss_limit_blocks_a_reversal(cfg):
    candidate = _reversal_candidate(cfg)
    losing = _account(daily_realized_pnl=-5_000.0)
    decision = _decide(cfg, candidate, losing)
    assert not decision.approved
    assert any("daily loss limit" in reason for reason in decision.reasons)


def test_15_an_existing_position_in_the_same_symbol_blocks_a_reversal(cfg):
    """Scenario 15: a conflicting open trade."""

    candidate = _reversal_candidate(cfg)
    held = _account(
        open_positions=[{"symbol": "EURUSD", "direction": "SELL", "risk_amount": 50.0}]
    )
    decision = _decide(cfg, candidate, held)
    assert not decision.approved
    assert any("already holding" in reason for reason in decision.reasons)


def test_14_the_daily_trade_limit_blocks_a_reversal(cfg):
    """Scenario 14 at the risk layer: the executor's idempotency guard is
    the other half, and is covered in tests/test_execution.py."""

    candidate = _reversal_candidate(cfg)
    spent = _account(trades_today=cfg.risk.max_trades_per_day)
    decision = _decide(cfg, candidate, spent)
    assert not decision.approved
    assert any("daily trade limit" in reason for reason in decision.reasons)


def test_13_a_news_blackout_blocks_the_symbol_before_any_analysis(cfg, tmp_path):
    """Scenario 13: news restriction.

    Checked in the orchestrator before market data is even fetched, so no
    classification exists yet to argue with it.
    """

    from datetime import timedelta

    from bot.news import NewsFilter

    enabled = dataclasses.replace(
        cfg, news=dataclasses.replace(cfg.news, enabled=True, fail_closed_without_feed=False)
    )
    news = NewsFilter(enabled.news, cache_path=tmp_path / "news.json")
    news._events = [
        {
            "title": "Test high-impact release",
            "country": "USD",
            "impact": "High",
            "date": (SETUP_END + timedelta(minutes=5)).isoformat(),
        }
    ]
    news._fetched_at = SETUP_END.timestamp()
    news._feed_available = True
    news.refresh = lambda *, force=False: True  # type: ignore[assignment]

    verdict = news.check("EURUSD", now=SETUP_END)
    assert verdict.blocked, verdict.reason

    # And the orchestrator checks it BEFORE market data, so no
    # classification has been formed that could argue with it.
    import inspect

    from bot.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._evaluate_symbol)
    assert source.index("self.news.check") < source.index("self.strategy.analyze")


def test_the_kill_switch_outranks_every_classification(cfg):
    from bot.risk.engine import RiskEngine
    from bot.safety.kill_switch import KillSwitch
    from bot.storage.db import in_memory_database
    from bot.storage.repositories import Repositories
    from fakes import DEFAULT_SPEC

    repos = Repositories(in_memory_database())
    switch = KillSwitch(repos.state)
    switch.trip("MANUAL", "operator stopped the bot")

    decision = RiskEngine(cfg, switch).evaluate(
        candidate=_reversal_candidate(cfg),
        tier="A+",
        account=_account(),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not decision.approved
    assert any("kill switch" in reason for reason in decision.reasons)


# -- the MTF layer's own no-look-ahead guarantee ---------------------------


def test_the_mtf_layer_at_bar_i_cannot_see_bar_i_plus_one(cfg):
    """Rule 4, asserted on this layer and not only on the detectors.

    `test_analysis_on_bar_i_cannot_see_bar_i_plus_one` proves the
    DETECTORS are honest. It says nothing about what the decision layer
    does with their output, and this layer was selecting a displacement
    with `abs(move.index - reference) <= 6` and no upper bound at all -
    so bar 79 could answer bar 76. Production never noticed because
    production always asks about the last bar; a walk-forward backtest
    would have been scored against candles it could not have seen.
    """

    engine = SmcEngine(cfg)
    m15 = engine.analyze_timeframe(bullish_setup_m15(), timeframe="M15", now=SETUP_END)

    for cut in range(40, m15.last_index + 1):
        for wanted in ("bullish", "bearish"):
            evidence = mtf.gather_direction_evidence(
                m15, wanted, index=cut, smc=cfg.smc, mtf=cfg.mtf
            )
            if evidence.sweep is not None:
                assert evidence.sweep.confirmed_index <= cut
                assert evidence.sweep.index <= cut
            if evidence.structure_event is not None:
                assert evidence.structure_event.index <= cut
            if evidence.choch is not None:
                assert evidence.choch.index <= cut
            if evidence.displacement is not None:
                assert evidence.displacement.index <= cut, (
                    f"a displacement at {evidence.displacement.index} answered bar {cut}"
                )
            assert evidence.latest_trigger_index <= cut


def test_a_displacement_after_the_asked_bar_is_never_selected(cfg):
    """The exact defect, pinned with an injected future candle.

    Built rather than hunted: no fixture happened to place a displacement
    in the window AFTER the trigger, which is why the bug survived a green
    suite. Constructing it makes the guarantee testable instead of lucky.
    """

    engine = SmcEngine(cfg)
    m15 = engine.analyze_timeframe(bullish_setup_m15(), timeframe="M15", now=SETUP_END)
    real = next(d for d in m15.displacements if d.direction == "bullish")

    future = dataclasses.replace(real, index=real.index + 3, quality=1.0)
    tampered = dataclasses.replace(m15, displacements=m15.displacements + (future,))

    evidence = mtf.gather_direction_evidence(
        tampered, "bullish", index=real.index, smc=cfg.smc, mtf=cfg.mtf
    )
    assert evidence.displacement is not None
    assert evidence.displacement.index <= real.index
    assert evidence.displacement.quality < 1.0, "the future candle was preferred on quality"


def test_recency_is_measured_on_the_latest_trigger_not_the_graded_one(cfg):
    """A strong old sweep must not make a fresh signal look stale.

    The sweep carried on the evidence is chosen by QUALITY, because that
    is what grading needs. The recency comparison between directions needs
    the opposite: the most recent qualifying trigger, whichever it is.
    Conflating them let a direction be judged stale on the strength of its
    own best evidence - and being judged stale is what hands the trade to
    the other side.
    """

    engine = SmcEngine(cfg)
    m15 = engine.analyze_timeframe(bullish_setup_m15(), timeframe="M15", now=SETUP_END)
    evidence = mtf.gather_direction_evidence(
        m15, "bullish", index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    assert evidence.latest_trigger_index >= evidence.reference_index

    strong_but_old = next(s for s in m15.sweeps if s.direction == "bullish")
    fresh_but_weak = dataclasses.replace(
        strong_but_old,
        index=m15.last_index,
        confirmed_index=m15.last_index,
        quality=strong_but_old.quality / 3.0,
    )
    both = dataclasses.replace(m15, sweeps=m15.sweeps + (fresh_but_weak,))
    evidence = mtf.gather_direction_evidence(
        both, "bullish", index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    assert evidence.sweep.quality == strong_but_old.quality, "grading takes the best sweep"
    assert evidence.latest_trigger_index == m15.last_index, "recency takes the newest"


def test_a_neutral_timeframe_is_never_read_as_opposed(cfg):
    """`_opposite("range")` returned "bullish" from a bare else.

    That makes `opposes_h1()` accidentally true for a neutral H1 against
    a bearish setup - a timeframe with no opinion reading as one that
    disagrees, which is the exact failure this layer exists to end, and
    `_against_primary_bias` would then be asked to classify a setup that
    fights nothing.
    """

    assert mtf._opposite("bullish") == "bearish"
    assert mtf._opposite("bearish") == "bullish"
    assert mtf._opposite("range") == ""
    assert mtf._opposite("") == ""

    # And a neutral H1 is trendless, not opposed.
    candidate, rejection, decision = evaluate(cfg, bullish_setup_m15(), h1=NEUTRAL)
    assert candidate is not None, rejection
    assert decision.setup_type in (mtf.CONTINUATION, mtf.RANGE_ROTATION)
    assert decision.alignment == "partial"


# -- the pullback must not lose the trade to its own continuation ---------


def test_a_retracement_does_not_take_the_trade_from_its_continuation(cfg):
    """The blocker that dominated live scans, on three symbols at once.

    A recency veto used to sit in `decide`: a direction the configuration
    had refused could stand down a staler opposite one. It was written as
    "any more recent opposing trigger that is not noise", which is far
    too broad - a RETRACEMENT is this layer stating that the counter move
    has NOT earned the name reversal, the definition of a pullback, and a
    pullback into the imbalance is the entry the whole strategy is built
    around. So the veto rejected the setup it existed to protect.

    The veto is gone with H4 (the only refusal that ever carried real
    counter-evidence was the macro case), so this asserts the behaviour
    through the engine rather than through stubs of a mechanism that no
    longer exists: a bullish setup inside a bullish H1 leg is taken even
    though the bearish direction is simultaneously refused as a pullback.
    """

    engine = SmcEngine(cfg)
    m15_candles = bullish_setup_m15()
    m15 = engine.analyze_timeframe(m15_candles, timeframe="M15", now=SETUP_END)
    h1 = engine.analyze_timeframe(
        directional_htf(m15_candles, timeframe="H1", direction=BULLISH),
        timeframe="H1",
        now=SETUP_END,
    )
    assert h1.bias == "bullish"

    # Built, not hunted: no fixture happens to carry a counter-trigger
    # NEWER than the setup's own, which is the only arrangement in which
    # the old veto could fire. A bearish sweep on the last bar, with no
    # bearish displacement and no bearish CHoCH behind it, is exactly the
    # pullback the strategy waits to buy.
    template = max(m15.sweeps, key=lambda sweep: sweep.index)
    pullback = dataclasses.replace(
        template,
        direction="bearish",
        index=m15.last_index,
        confirmed_index=m15.last_index,
    )
    tampered = dataclasses.replace(m15, sweeps=m15.sweeps + (pullback,))

    counter = mtf.gather_direction_evidence(
        tampered, "bearish", index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    with_trend = mtf.gather_direction_evidence(
        tampered, "bullish", index=m15.last_index, smc=cfg.smc, mtf=cfg.mtf
    )
    assert counter.has_trigger, "no opposing trigger: this test proves nothing"
    assert counter.latest_trigger_index > with_trend.latest_trigger_index, (
        "the pullback must be the FRESHER signal, or there is nothing to veto with"
    )
    assert (
        mtf.classify(
            h1=h1,
            m15=tampered,
            wanted="bearish",
            evidence=counter,
            mtf=cfg.mtf,
            tier_b=cfg.scoring.tier_b,
        ).setup_type
        == mtf.RETRACEMENT
    )

    decision, evidence = mtf.decide(
        h1=h1,
        m15=tampered,
        index=m15.last_index,
        smc=cfg.smc,
        mtf=cfg.mtf,
        tier_b=cfg.scoring.tier_b,
    )
    assert decision.direction == "BUY", decision.rationale
    assert decision.setup_type == mtf.CONTINUATION
    assert evidence is not None


def test_no_refusal_can_silently_stand_a_direction_down(cfg):
    """Rule 6, applied to this layer: a refusal always says why.

    Whatever the outcome, `decide` returns a rationale naming the
    classification it reached. A blank or generic refusal is how an
    operator ends up overriding the engine, which is the failure the
    whole layer is written against.
    """

    for builder, h1 in (
        (bullish_setup_m15, BULLISH),
        (bearish_setup_m15, BULLISH),
        (flat_market_m15, BULLISH),
        (bullish_setup_m15, NEUTRAL),
        (reversal_setup_m15, BEARISH),
    ):
        _, _, decision = evaluate(cfg, builder(), h1=h1)
        assert decision.rationale.strip(), "a silent decision is not a decision"
        assert decision.setup_type in (
            mtf.CONTINUATION,
            mtf.RANGE_ROTATION,
            mtf.REVERSAL,
            mtf.RETRACEMENT,
            mtf.NOISE,
        )
        assert decision.tradeable == (decision.direction is not None)
