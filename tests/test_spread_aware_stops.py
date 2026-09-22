"""The spread the stop is actually triggered on, and the record of it.

The loss that produced this file: a short was closed at 0.82131 when
nothing in the market traded within 6.1 pips of that price. Measured on
the bid the position was +5.8 pips; it was booked at -7.3. The entire
13.1-pip swing was the spread, and the stop's structural buffer of
0.2 ATR was a fraction of the spread at that moment.

Two things follow, and both are tested here:

  * the stop must be built knowing the spread, because a broker-side stop
    triggers on the bid for a long and the ask for a short, never on the
    candle series the level was read off;
  * the spread must be written down, because every argument about this
    was previously conducted with no stored measurement of it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.clock import utc_now
from bot.config import R_EPSILON
from bot.marketdata.provider import MarketDataProvider
from bot.scoring.scorer import SetupScorer
from bot.smc.engine import SmcEngine
from bot.storage.repositories import SpreadRepository
from fakes import DEFAULT_SPEC, SETUP_END, FakeBroker


@pytest.fixture()
def series(config, broker):
    return MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)


# -- the stop knows about the spread --------------------------------------


def test_a_wider_spread_pushes_the_stop_further_from_entry(config, series):
    """The whole point: the level must survive being reached on the ask."""

    engine = SmcEngine(config)
    unpadded = engine.analyze("EURUSD", series, now=SETUP_END, spread=None).candidate
    padded = engine.analyze("EURUSD", series, now=SETUP_END, spread=0.00020).candidate
    assert unpadded is not None and padded is not None

    bare = abs(unpadded.entry - unpadded.stop_loss)
    widened = abs(padded.entry - padded.stop_loss)
    assert widened > bare, "a known spread must move the stop, or it is decoration"
    # One spread width, exactly — not a fudge factor.
    assert widened == pytest.approx(bare + 0.00020, abs=1e-9)


def test_the_padding_scales_with_the_configured_multiple(config, series):
    import dataclasses

    engine = SmcEngine(config)
    bare = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END, spread=None).candidate
    assert bare is not None
    reference = abs(bare.entry - bare.stop_loss)

    doubled = dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, stop_spread_multiple=2.0)
    )
    candidate = SmcEngine(doubled).analyze(
        "EURUSD", series, now=SETUP_END, spread=0.00010
    ).candidate
    assert candidate is not None
    assert abs(candidate.entry - candidate.stop_loss) == pytest.approx(
        reference + 0.00020, abs=1e-9
    )


def test_turning_the_multiple_off_restores_the_unpadded_stop(config, series):
    import dataclasses

    off = dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, stop_spread_multiple=0.0)
    )
    bare = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END, spread=None).candidate
    candidate = SmcEngine(off).analyze("EURUSD", series, now=SETUP_END, spread=0.00050).candidate
    assert bare is not None and candidate is not None
    assert abs(candidate.entry - candidate.stop_loss) == pytest.approx(
        abs(bare.entry - bare.stop_loss), abs=1e-9
    )


def test_an_unreadable_spread_never_becomes_a_zero(config, series):
    """Rule 6, at the one place it would be most tempting to fake.

    `None` means the quote could not be read. It must build the stop the
    old way and leave the executor's spread gate as the protection — not
    silently assert that the spread was zero, which is the one value that
    is never true.
    """

    engine = SmcEngine(config)
    unknown = engine.analyze("EURUSD", series, now=SETUP_END, spread=None).candidate
    assert unknown is not None
    # A zero spread is indistinguishable from "we padded by nothing",
    # which is the honest behaviour here — the distinction that matters
    # is that no NUMBER was invented to stand in for the missing one.
    explicit_zero = engine.analyze("EURUSD", series, now=SETUP_END, spread=0.0).candidate
    assert explicit_zero is not None
    assert abs(unknown.entry - unknown.stop_loss) == pytest.approx(
        abs(explicit_zero.entry - explicit_zero.stop_loss), abs=1e-9
    )


def test_a_negative_spread_is_ignored_rather_than_shrinking_the_stop(config, series):
    """A broker that reports a crossed book must not be able to make a
    stop TIGHTER. Nothing about a bad quote is a reason to risk more."""

    engine = SmcEngine(config)
    bare = engine.analyze("EURUSD", series, now=SETUP_END, spread=None).candidate
    crossed = engine.analyze("EURUSD", series, now=SETUP_END, spread=-0.00050).candidate
    assert bare is not None and crossed is not None
    assert abs(crossed.entry - crossed.stop_loss) == pytest.approx(
        abs(bare.entry - bare.stop_loss), abs=1e-9
    )


def test_widening_the_stop_does_not_widen_the_money_at_risk(config, series):
    """Rule 2's guarantee, restated for this change.

    A wider stop must cost lots, never dollars. If padding the stop
    increased the risk amount it would be a silent risk increase dressed
    as a safety feature.
    """

    from bot.risk.engine import AccountRiskState, RiskEngine

    engine = SmcEngine(config)
    scorer = SetupScorer(config)
    risk = RiskEngine(config)
    account = AccountRiskState(
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

    amounts = []
    for spread in (None, 0.00020):
        candidate = engine.analyze("EURUSD", series, now=SETUP_END, spread=spread).candidate
        assert candidate is not None
        decision = risk.evaluate(
            candidate=candidate,
            tier=scorer.score(candidate).tier,
            account=account,
            spec=DEFAULT_SPEC,
            now=SETUP_END,
        )
        if not decision.approved:
            pytest.skip(f"risk refused the padded setup: {decision.reasons}")
        amounts.append(decision.risk_amount)

    assert amounts[1] == pytest.approx(amounts[0], rel=0.02), (
        "padding the stop changed the money at risk; it must only change the size"
    )


# -- the epsilon the boundary needs ---------------------------------------


def test_a_setup_exactly_on_the_reward_floor_is_not_refused_by_float_noise(config, series):
    """The bug the padded stop exposed.

    The target is chosen at `minimum_reward + offset` and then has that
    same offset subtracted, so a setup landing on the floor arrives as
    the floor minus one float ulp. Every other R comparison in the system
    already tolerated that; the engine's own gate and the scorer's
    critical component did not, and between them they threw the setup out
    as though it had no target at all.
    """

    engine = SmcEngine(config)
    candidate = engine.analyze("EURUSD", series, now=SETUP_END, spread=0.00010).candidate
    assert candidate is not None, "a setup on the floor must survive the gate"
    assert candidate.risk_reward >= config.risk.min_risk_reward - R_EPSILON

    score = SetupScorer(config).score(candidate)
    assert score.components["risk_reward"] > 0.0, (
        "R:R is a critical component; a boundary setup must not be vetoed outright"
    )


# -- the record -----------------------------------------------------------


def test_a_spread_sample_is_stored_and_read_back(repos):
    moment = utc_now()
    repos.spreads.record(
        symbol="eurusd",
        observed_at=moment,
        bid=1.09995,
        ask=1.10005,
        spread=0.00010,
        session="LONDON",
    )
    summary = repos.spreads.by_symbol()
    assert "EURUSD" in summary, "the symbol is normalised on the way in"
    assert summary["EURUSD"]["n"] == 1
    assert summary["EURUSD"]["median"] == pytest.approx(0.00010)


def test_a_thin_sample_says_so_rather_than_reporting_a_median_as_fact(repos):
    """Rule 6 applied to statistics: two readings are two moments."""

    moment = utc_now()
    for index in range(3):
        repos.spreads.record(
            symbol="EURUSD",
            observed_at=moment + timedelta(minutes=index),
            bid=1.0999,
            ask=1.1001,
            spread=0.0002,
            session="LONDON",
        )
    assert repos.spreads.by_symbol()["EURUSD"]["sample"] == "insufficient"

    for index in range(SpreadRepository.MIN_SAMPLES):
        repos.spreads.record(
            symbol="EURUSD",
            observed_at=moment + timedelta(hours=1, minutes=index),
            bid=1.0999,
            ask=1.1001,
            spread=0.0002,
            session="LONDON",
        )
    assert repos.spreads.by_symbol()["EURUSD"]["sample"] == "sufficient"


def test_samples_are_bucketed_by_the_hour_they_were_taken(repos):
    """This is what makes the rollover blackout an argument from data
    rather than from one screenshot."""

    base = utc_now().replace(hour=13, minute=0, second=0, microsecond=0)
    repos.spreads.record(
        symbol="EURUSD", observed_at=base, bid=1.0999, ask=1.1000, spread=0.00010
    )
    repos.spreads.record(
        symbol="EURUSD",
        observed_at=base.replace(hour=21),
        bid=1.0995,
        ask=1.1005,
        spread=0.00100,
    )
    by_hour = repos.spreads.by_hour("EURUSD")
    assert by_hour[13]["median"] == pytest.approx(0.00010)
    assert by_hour[21]["median"] == pytest.approx(0.00100)
    assert by_hour[21]["median"] > by_hour[13]["median"]


def test_a_scan_records_a_spread_for_every_symbol_it_looks_at(orchestrator):
    """The recorder has to be wired into the live path, not merely exist."""

    orchestrator.startup()
    orchestrator.scan()
    summary = orchestrator.repos.spreads.by_symbol()
    assert summary, "a completed scan stored no spread at all"
    assert all(entry["n"] >= 1 for entry in summary.values())


def test_a_broker_that_cannot_quote_does_not_break_the_scan(orchestrator, broker):
    """And records nothing rather than recording a zero."""

    from bot.errors import BrokerError

    def refuse(spec):
        raise BrokerError("quote endpoint unavailable")

    broker.quote = refuse
    orchestrator.startup()
    result = orchestrator.scan()
    assert result is not None, "a missing quote must not abort the scan"
    assert orchestrator.repos.spreads.by_symbol() == {}, (
        "an unreadable spread must leave no row, not a zero"
    )


# -- the grade floor, named rather than remembered -------------------------


def test_the_grade_floor_can_be_named_instead_of_looked_up(monkeypatch):
    """`SCORING_MIN_TIER=A` must mean the A band, wherever the band sits.

    The numeric `SCORING_MIN_TRADEABLE` already worked. It is also a
    number an operator has to know (A starts at 68) and has to remember
    to change again if the band ever moves. Naming the grade means a band
    and its floor cannot drift apart.
    """

    from bot.config import _scoring_from_env

    monkeypatch.delenv("SCORING_MIN_TIER", raising=False)
    monkeypatch.delenv("SCORING_MIN_TRADEABLE", raising=False)
    stock = _scoring_from_env()
    assert stock.min_tradeable_score == stock.tier_b

    monkeypatch.setenv("SCORING_MIN_TIER", "A")
    assert _scoring_from_env().min_tradeable_score == stock.tier_a

    monkeypatch.setenv("SCORING_MIN_TIER", "a+")
    assert _scoring_from_env().min_tradeable_score == stock.tier_a_plus


def test_the_two_floor_settings_can_only_ever_demand_more(monkeypatch):
    """Neither may be used to talk the other one down."""

    from bot.config import _scoring_from_env

    monkeypatch.setenv("SCORING_MIN_TIER", "B")
    monkeypatch.setenv("SCORING_MIN_TRADEABLE", "75")
    assert _scoring_from_env().min_tradeable_score == 75.0

    monkeypatch.setenv("SCORING_MIN_TIER", "A+")
    monkeypatch.setenv("SCORING_MIN_TRADEABLE", "60")
    scoring = _scoring_from_env()
    assert scoring.min_tradeable_score == scoring.tier_a_plus


def test_an_unrecognised_grade_is_refused_rather_than_ignored(monkeypatch):
    """Silently defaulting to B would mean an operator who typed "A1" ran
    a fortnight of B trades believing they had filtered by grade — the
    exact failure the dead knob this replaces used to produce."""

    from bot.config import _scoring_from_env
    from bot.errors import ConfigError

    monkeypatch.setenv("SCORING_MIN_TIER", "A1")
    with pytest.raises(ConfigError, match="SCORING_MIN_TIER"):
        _scoring_from_env()


# -- an unknown R is not break-even ---------------------------------------


def test_an_unmeasurable_position_reports_no_r_rather_than_zero(config, broker, repos):
    """Rule 6, at the field most likely to reassure someone falsely.

    `0.0` does not read as "unknown". It reads as "exactly break-even",
    which is the calmest number this field can show, displayed precisely
    when nothing can be measured. `_current_price` already refuses to
    invent a price for this reason; the R derived from it was inventing
    one anyway.
    """

    from bot.errors import BrokerError
    from bot.execution.manager import PositionManager

    broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.1, entry=1.1000, stop_loss=1.0950
    )
    position = broker.positions()[0]

    def refuse(spec):
        raise BrokerError("no quote")

    broker.quote = refuse
    rows = PositionManager(config, broker, repos).track([position])
    assert rows[0]["rMultiple"] is None, "an unmeasurable R must be a gap, not a zero"
    assert rows[0]["currentPrice"] is None
    assert rows[0]["priceStatus"]


def test_a_position_with_no_entry_price_is_reported_as_unmeasurable(config, broker, repos):
    """A missing entry price is not an entry price of zero.

    Every number in the row is measured FROM the entry, so a zero does
    not produce a slightly wrong R — it produces an R in the thousands
    and an excursion equal to the whole price of the instrument.
    """

    import dataclasses

    from bot.execution.manager import PositionManager

    broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.1, entry=1.1000, stop_loss=1.0950
    )
    position = dataclasses.replace(broker.positions()[0], entry_price=0.0)

    rows = PositionManager(config, broker, repos).track([position])
    assert rows[0]["rMultiple"] is None
    assert "entry price" in rows[0]["priceStatus"]
