"""One setup, one trade — and a transcript of how it got there.

Two things are pinned here, because they share the same evidence:

* a setup's IDENTITY, so a stop-out cannot be followed fifteen minutes
  later by the identical trade re-derived from the identical candles;
* the per-condition CHAIN, so a refusal says what passed as well as what
  failed.

Every scenario is constructed, so the correct answer is known before the
code runs (project rule 10), and the clock is injected (rule 10 again).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from bot.smc.checks import CHAIN, FAIL, PASS, PENDING, Checklist, summarise
from bot.smc.engine import SmcEngine
from bot.smc.identity import setup_identity
from bot.storage.db import in_memory_database
from bot.storage.repositories import Repositories
from fakes import SETUP_END, DEFAULT_SPEC, bullish_setup_m15, directional_htf

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


# -- identity -------------------------------------------------------------


def _analysed(cfg):
    engine = SmcEngine(cfg)
    m15 = bullish_setup_m15()
    return engine, {
        "H1": engine.analyze_timeframe(
            directional_htf(m15, timeframe="H1", direction=1), timeframe="H1", now=SETUP_END
        ),
        "M15": engine.analyze_timeframe(m15, timeframe="M15", now=SETUP_END),
    }


def test_the_same_chart_produces_the_same_identity_every_scan(config):
    """The whole point: an identity that moved would be a hash, not an id."""

    engine, analyses = _analysed(config)
    first = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    second = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    assert first is not None and second is not None
    assert first.setup_id
    assert first.setup_id == second.setup_id


def test_identity_ignores_everything_that_moves_between_scans(config):
    """Entry, score and account are NOT part of what makes a setup itself.

    If any of them were, the id would change on the next candle and the
    duplicate check would never fire — which is exactly the failure this
    guards against, arriving as a silent no-op instead of a wrong answer.
    """

    engine, analyses = _analysed(config)
    candidate = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    moved = dataclasses.replace(
        candidate, entry=candidate.entry * 1.01, score_floor=99.0, risk_reward=9.9
    )
    assert moved.setup_id == candidate.setup_id


def test_a_different_sweep_candle_is_a_different_setup(config):
    """Tomorrow's sweep of the same level is a new opportunity, not a repeat."""

    engine, analyses = _analysed(config)
    candidate = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    assert candidate.sweep is not None

    later = dataclasses.replace(
        candidate.sweep, timestamp=candidate.sweep.timestamp + timedelta(days=1)
    )
    assert setup_identity(
        symbol="EURUSD",
        timeframe="M15",
        direction=candidate.direction,
        sweep=later,
        structure_event=candidate.structure_event,
    ) != candidate.setup_id


@pytest.mark.parametrize("field,value", [("symbol", "GBPUSD"), ("direction", "SELL")])
def test_identity_separates_symbols_and_directions(config, field, value):
    engine, analyses = _analysed(config)
    candidate = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    base = dict(
        symbol="EURUSD",
        timeframe="M15",
        direction=candidate.direction,
        sweep=candidate.sweep,
        structure_event=candidate.structure_event,
    )
    assert setup_identity(**{**base, field: value}) != candidate.setup_id


def test_an_unnameable_setup_is_allowed_through_rather_than_blocked():
    """A fail-open, and the only one in the system that is correct.

    With no sweep and no structure event there is nothing durable to key
    on. Returning a constant would make every unnamed setup collide with
    every other and silently block all of them; returning "" means the
    duplicate check simply does not apply, and every other gate still runs.
    """

    assert setup_identity(
        symbol="EURUSD", timeframe="M15", direction="BUY", sweep=None, structure_event=None
    ) == ""


# -- the duplicate block --------------------------------------------------


def test_a_setup_already_traded_is_refused(config):
    """Including after a stop-out. The chart has not changed; that is the point."""

    from bot.risk.engine import RiskEngine

    engine, analyses = _analysed(config)
    candidate = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate

    from tests.test_risk import make_account  # type: ignore[import-not-found]

    fresh = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A",
        account=make_account(),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert fresh.approved, fresh.reasons

    repeat = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A",
        account=make_account(traded_setup_ids=frozenset({candidate.setup_id})),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not repeat.approved
    assert any("already been traded" in reason for reason in repeat.reasons)


def test_an_empty_identity_never_matches_the_blocked_set(config):
    """The fail-open, asserted end to end rather than trusted."""

    from bot.risk.engine import RiskEngine
    from tests.test_risk import make_account  # type: ignore[import-not-found]

    engine, analyses = _analysed(config)
    candidate = engine.evaluate("EURUSD", analyses, now=SETUP_END).candidate
    unnamed = dataclasses.replace(candidate, setup_id="")

    decision = RiskEngine(config).evaluate(
        candidate=unnamed,
        tier="A",
        account=make_account(traded_setup_ids=frozenset({""})),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved, decision.reasons


def test_traded_ids_are_read_from_storage_so_a_restart_cannot_forget(config):
    """A redeploy mid-session must not hand back a setup already taken."""

    repos = Repositories(in_memory_database())
    repos.trades.create_pending(
        execution_id="exec-1",
        plan={"symbol": "EURUSD", "direction": "BUY", "setup_id": "abc123"},
    )
    assert repos.trades.traded_setup_ids(hours=24.0, now=NOW + timedelta(minutes=1)) == frozenset(
        {"abc123"}
    )


def test_the_block_expires_with_the_window(config):
    """Long enough that the evidence cannot still be live, and no longer.

    A sweep stops being a usable trigger after `sweep_max_age_candles`
    (5 hours on M15), so a 24h window means a blocked identity can never
    still be tradeable — and after it, a fresh sweep of the same level
    would have its own identity anyway.
    """

    repos = Repositories(in_memory_database())
    repos.trades.create_pending(
        execution_id="exec-1",
        plan={"symbol": "EURUSD", "direction": "BUY", "setup_id": "abc123"},
    )
    window = config.risk.setup_reentry_block_hours
    assert window > 5.0, "the window must outlive the evidence it protects"

    from bot.clock import utc_now

    long_after = utc_now() + timedelta(hours=window + 1)
    assert repos.trades.traded_setup_ids(hours=window, now=long_after) == frozenset()


# -- the per-condition chain ----------------------------------------------


def test_a_complete_setup_records_every_condition(config):
    engine, analyses = _analysed(config)
    result = engine.evaluate("EURUSD", analyses, now=SETUP_END)
    assert result.candidate is not None, result.rejection

    names = [check.name for check in result.checks]
    assert names == list(CHAIN), "the chain must be reported in evaluation order"
    assert all(check.status in (PASS, PENDING) for check in result.checks), summarise(
        result.checks
    )
    # The ones that decide a trade are never PENDING on a candidate.
    decisive = {"entry_zone", "zone_retest", "stop_loss", "take_profit", "risk_reward"}
    for check in result.checks:
        if check.name in decisive:
            assert check.status == PASS
            assert check.detail, f"{check.name} passed without saying why"


def test_a_refusal_says_what_passed_before_it_failed(config):
    """The half an operator cannot infer from a reason string.

    "no live fair value gap or order block" leaves them guessing whether
    the sweep was found and the break confirmed, or whether the whole
    thing fell over at the first hurdle.
    """

    engine, analyses = _analysed(config)
    # Remove every entry zone, leaving the trigger intact.
    stripped = dict(analyses)
    stripped["M15"] = dataclasses.replace(analyses["M15"], gaps=(), order_blocks=())
    result = engine.evaluate("EURUSD", stripped, now=SETUP_END)

    assert result.candidate is None
    by_name = {check.name: check for check in result.checks}
    assert by_name["entry_zone"].status == FAIL
    assert by_name["liquidity_sweep"].status == PASS
    # And everything after the failure is PENDING, not FAIL: the engine
    # never reached those, which is different information.
    assert by_name["zone_retest"].status == PENDING
    assert by_name["risk_reward"].status == PENDING


def test_pending_is_not_a_euphemism_for_fail(config):
    """They call for opposite reactions, so they must not share a bucket."""

    chain = Checklist()
    chain.passed("liquidity_sweep", "swept")
    chain.failed("displacement", "none")
    finished = chain.finish()
    statuses = {check.name: check.status for check in finished}

    assert statuses["liquidity_sweep"] == PASS
    assert statuses["displacement"] == FAIL
    assert statuses["structure_break"] == PENDING
    assert len(finished) == len(CHAIN)


def test_the_chain_is_append_only(config):
    """A later stage re-grading an earlier one stops it being a transcript."""

    chain = Checklist()
    chain.passed("liquidity_sweep")
    with pytest.raises(ValueError, match="append-only"):
        chain.failed("liquidity_sweep")
    with pytest.raises(ValueError, match="not part of the strategy chain"):
        chain.passed("invented_condition")


def test_the_checks_reach_the_journal_and_the_dashboard(config):
    engine, analyses = _analysed(config)
    result = engine.evaluate("EURUSD", analyses, now=SETUP_END)
    payload = result.candidate.as_dict()
    assert [check["name"] for check in payload["checks"]] == list(CHAIN)
    assert payload["setupId"] == result.candidate.setup_id
