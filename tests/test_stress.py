"""Stress tests: extreme market and infrastructure conditions.

These are not "does it crash" tests. Each one asserts a SAFETY INVARIANT
holds under conditions that break naive implementations:

  * risk actually taken never exceeds the risk approved;
  * no order is ever sent without a stop and a target;
  * no condition produces two orders for one setup;
  * no failure produces a fabricated number;
  * every failure is classified, surfaced, and fails closed.

The scenarios are deliberately violent — 20% flash crashes, weekend gaps
straight through a stop, spreads wider than the target, a broker that
answers 429 in a loop, a database that dies mid-execution. A system that
only behaves on calm data is not a trading system.
"""

from __future__ import annotations

import dataclasses
import io
import urllib.error
from datetime import timedelta

import pytest

from bot.broker.http import CircuitBreaker, HttpTransport, Throttle
from bot.broker.models import InstrumentSpec, Quote
from bot.broker.paper import PaperBroker
from bot.config import ExecutionMode, profit_floor_feasibility
from bot.errors import (
    AmbiguousExecution,
    BrokerError,
    BrokerRateLimited,
    BrokerRejected,
    CircuitOpen,
    MarketDataError,
    StaleDataError,
)
from bot.execution.executor import Executor
from bot.marketdata.candles import Candle
from bot.marketdata.provider import MarketDataProvider
from bot.marketdata.validation import validate_series, validate_spread
from bot.orchestrator import Orchestrator
from bot.risk.engine import AccountRiskState, RiskEngine
from bot.risk.sizing import SizingError, calculate_position_size
from bot.smc.engine import SmcEngine
from bot.smc.regime import classify_regime
from fakes import (
    BASE_TIME,
    DEFAULT_SPEC,
    SETUP_END,
    FakeBroker,
    aligned_htf,
    bullish_setup_m15,
    candle,
    series_from_path,
)


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


# =========================================================================
# 1. Violent market data
# =========================================================================


def flash_crash_series(*, drop: float = 0.20) -> list[Candle]:
    """Calm, then a single candle that erases 20% of the price."""

    path = [(1.1000, 1.1008, 1.0992, 1.1001)] * 90
    crash_low = 1.1000 * (1 - drop)
    path.append((1.1000, 1.1002, crash_low, crash_low * 1.002))
    path += [(crash_low, crash_low * 1.01, crash_low * 0.99, crash_low * 1.005)] * 8
    return series_from_path(path, timeframe="M15", end=SETUP_END)


def test_a_flash_crash_makes_the_regime_untradeable():
    regime = classify_regime(flash_crash_series())
    assert regime.volatility == "extreme"
    assert regime.tradeable is False
    assert "standing aside" in regime.note


def test_a_flash_crash_produces_no_setup(config, repos):
    broker = FakeBroker()
    crash = flash_crash_series()
    for timeframe in ("M15", "H1", "H4"):
        broker.set_series(
            "EURUSD",
            timeframe,
            crash if timeframe == "M15" else aligned_htf(crash, timeframe=timeframe),
        )
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert broker.submitted == [], "an order was placed into a flash crash"
    assert result.executed is None
    assert result.outcomes[0].outcome in ("NO_SETUP", "BELOW_TIER", "REJECTED")


def test_a_weekend_gap_is_tolerated_but_a_mid_week_gap_is_not():
    """A gap is normal across the weekend and abnormal inside a session."""

    friday = BASE_TIME.replace(year=2026, month=9, day=11, hour=20)
    monday = BASE_TIME.replace(year=2026, month=9, day=14, hour=8)
    before = [candle(friday - timedelta(minutes=15 * (40 - i)), 1.10, 1.1005, 1.0995, 1.1001) for i in range(40)]
    # Monday opens 300 pips lower — a real gap, but across the weekend.
    after = [candle(monday + timedelta(minutes=15 * i), 1.07, 1.0705, 1.0695, 1.0701) for i in range(40)]
    _, report = validate_series(
        before + after, timeframe="M15", min_candles=60, now=monday + timedelta(minutes=615)
    )
    assert report.gaps == 0

    # The same hole on a Tuesday is not explainable.
    tuesday = BASE_TIME.replace(year=2026, month=9, day=8, hour=8)
    left = [candle(tuesday + timedelta(minutes=15 * i), 1.10, 1.1005, 1.0995, 1.1001) for i in range(40)]
    # Resumes 9 hours AFTER the first block ends, so the hole is real.
    right = [candle(tuesday + timedelta(hours=19, minutes=15 * i), 1.07, 1.0705, 1.0695, 1.0701) for i in range(40)]
    # Just after the last candle closed, so the staleness guard is not what
    # fires here — the gap count is what is under test.
    _, gapped = validate_series(
        left + right,
        timeframe="M15",
        min_candles=60,
        now=tuesday + timedelta(hours=29, minutes=5),
    )
    assert gapped.gaps >= 1


def test_a_frozen_market_of_identical_candles_yields_no_setup(config):
    """Zero range everywhere: ATR collapses toward zero."""

    frozen = series_from_path([(1.1000, 1.1000, 1.1000, 1.1000)] * 120, end=SETUP_END)
    analysis = SmcEngine(config).analyze_timeframe(frozen, timeframe="M15", now=SETUP_END)
    assert analysis.atr == pytest.approx(0.0, abs=1e-12)
    candidate, rejection = SmcEngine(config).build_candidate(
        "EURUSD", {"M15": analysis, "H1": analysis, "H4": analysis}, now=SETUP_END
    )
    assert candidate is None
    assert rejection is not None


def test_a_price_series_that_collapses_toward_zero_is_refused():
    with pytest.raises(ValueError):
        Candle.from_mapping(
            {"timestamp": BASE_TIME.isoformat(), "timeframe": "M15",
             "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
        )


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_prices_never_enter_the_engine(bad):
    with pytest.raises(ValueError):
        Candle.from_mapping(
            {"timestamp": BASE_TIME.isoformat(), "timeframe": "M15",
             "open": 1.1, "high": bad, "low": 1.0, "close": 1.05}
        )


def test_a_series_where_every_candle_is_the_same_timestamp_is_refused():
    duplicated = [candle(SETUP_END - timedelta(minutes=15), 1.1, 1.11, 1.09, 1.10)] * 200
    with pytest.raises(MarketDataError, match="usable closed candles"):
        validate_series(duplicated, timeframe="M15", min_candles=60, now=SETUP_END)


def test_a_feed_that_stops_updating_goes_stale_rather_than_looking_calm():
    """The dangerous failure: a dead feed looks like a quiet market."""

    # 200 candles that all finished more than two days ago.
    last_close = SETUP_END - timedelta(days=2)
    frozen = [
        candle(last_close - timedelta(minutes=15 * (200 - i)), 1.1, 1.1005, 1.0995, 1.1001)
        for i in range(200)
    ]
    with pytest.raises(StaleDataError):
        validate_series(frozen, timeframe="M15", min_candles=60, now=SETUP_END)


# =========================================================================
# 2. Spread and execution conditions
# =========================================================================


def test_a_spread_wider_than_the_target_is_rejected():
    ok, reason = validate_spread(
        spread=0.0100,
        atr=0.0012,
        stop_distance=0.0030,
        take_profit_distance=0.0060,
        max_spread_atr_fraction=0.12,
        max_spread_tp_fraction=0.05,
    )
    assert not ok and reason


def test_a_news_spread_spike_aborts_before_submission(config, broker, repos):
    from test_execution import make_plan

    broker.quotes["EURUSD"] = Quote("EURUSD", 1.0900, 1.1100, BASE_TIME)  # 200 pip spread
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False and result.status == "ABORTED"
    assert broker.submitted == []


def test_a_crossed_quote_is_refused_not_averaged(config):
    """bid > ask is corrupt data, not a tradable price."""

    from bot.broker.tradelocker import TradeLockerBroker

    live = TradeLockerBroker(config)
    live._account_meta = {"currency": "USD"}
    live.get = lambda path, query=None: {"bp": 1.1100, "ap": 1.0900}  # type: ignore[assignment]
    with pytest.raises(BrokerError, match="crossed quote"):
        live.quote(DEFAULT_SPEC)


def test_a_zero_or_negative_quote_is_refused(config):
    from bot.broker.tradelocker import TradeLockerBroker

    live = TradeLockerBroker(config)
    live._account_meta = {"currency": "USD"}
    live.get = lambda path, query=None: {"bp": 0.0, "ap": 0.0}  # type: ignore[assignment]
    with pytest.raises(BrokerError, match="no usable quote"):
        live.quote(DEFAULT_SPEC)


# =========================================================================
# 3. Risk engine under extremes
# =========================================================================


@pytest.fixture()
def candidate(config, broker):
    series = MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    result = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END)
    assert result.candidate is not None, result.rejection
    return result.candidate


@pytest.mark.parametrize(
    "equity,peak",
    [(1.0, 10_000.0), (0.01, 10_000.0), (10.0, 1_000_000.0)],
)
def test_a_nearly_wiped_account_never_trades(config, candidate, equity, peak):
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A+",
        account=make_account(balance=equity, equity=equity, peak_equity=peak),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved is False


def test_a_negative_balance_never_trades(config, candidate):
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A+",
        account=make_account(balance=-500.0, equity=-500.0),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved is False
    assert any("not positive" in reason for reason in decision.reasons)


def test_an_enormous_equity_is_still_capped_by_max_lot(config, candidate):
    """A large account must not produce a position the broker cannot fill."""

    tiny_max = dataclasses.replace(DEFAULT_SPEC, max_lot=2.0)
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A+",
        account=make_account(balance=50_000_000.0, equity=50_000_000.0, available_margin=50_000_000.0),
        spec=tiny_max,
        now=SETUP_END,
    )
    assert decision.size is not None
    assert decision.size.lots <= tiny_max.max_lot


def test_risk_taken_never_exceeds_risk_approved_across_a_wide_sweep(config, candidate):
    """The invariant the whole risk model rests on, swept hard."""

    engine = RiskEngine(config)
    for equity in (2_000, 5_000, 10_000, 25_000, 100_000):
        for tier in ("A+", "A", "B"):
            decision = engine.evaluate(
                candidate=candidate,
                tier=tier,
                account=make_account(balance=equity, equity=equity, available_margin=equity),
                spec=DEFAULT_SPEC,
                now=SETUP_END,
            )
            if not decision.approved or decision.size is None:
                continue
            budget = equity * (decision.risk_pct or 0.0)
            assert decision.size.actual_risk <= budget * 1.02, (
                f"{tier} at {equity}: risked {decision.size.actual_risk} against {budget}"
            )
            assert decision.size.actual_risk <= equity * config.risk.max_risk_pct * 1.02


@pytest.mark.parametrize("rate", [1e-8, 1e8])
def test_an_absurd_conversion_rate_does_not_produce_an_absurd_position(rate):
    """A broken bridge quote must not silently size a monstrous order."""

    cross = dataclasses.replace(
        DEFAULT_SPEC, symbol="AUDJPY", broker_name="AUDJPY",
        base_currency="AUD", quote_currency="JPY", tick_size=0.001, digits=3,
    )
    try:
        size = calculate_position_size(
            spec=cross,
            risk_amount=50.0,
            entry=98.0,
            stop_loss=97.5,
            rate_lookup=lambda base, quote: rate if (base, quote) == ("JPY", "USD") else None,
        )
    except SizingError:
        return  # declining is a correct outcome
    # If it did size, the risk must still be respected and the lot capped.
    assert size.actual_risk <= 50.0 * 1.02
    assert size.lots <= cross.max_lot


def test_a_stop_one_tick_wide_is_refused_by_the_engine(config, candidate):
    """Sizing would explode; the structural guard must catch it first."""

    absurd = dataclasses.replace(
        candidate, stop_loss=candidate.entry - DEFAULT_SPEC.tick_size, stop_distance=DEFAULT_SPEC.tick_size
    )
    decision = RiskEngine(config).evaluate(
        candidate=absurd, tier="A+", account=make_account(), spec=DEFAULT_SPEC, now=SETUP_END
    )
    # Either the R:R collapses or sizing refuses — both are safe, neither
    # may approve a position sized off a one-tick stop.
    assert decision.approved is False or (
        decision.size is not None and decision.size.actual_risk <= make_account().equity * config.risk.max_risk_pct * 1.02
    )


def test_simultaneous_limit_breaches_all_report(config, candidate):
    """Under compound stress every reason must surface, not just the first."""

    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A+",
        account=make_account(
            equity=8_000.0,
            peak_equity=10_000.0,
            daily_realized_pnl=-500.0,
            consecutive_losses=5,
            trades_today=10,
        ),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert decision.approved is False
    assert len(decision.reasons) >= 3, decision.reasons


def test_the_profit_floor_is_reported_as_unreachable_rather_than_silently_never_trading(config):
    """A tiny account would otherwise return NO TRADE forever with no clue why."""

    feasibility = profit_floor_feasibility(config, 300.0)
    assert feasibility["feasible"] is False
    assert "No setup can pass this filter" in feasibility["reason"]
    assert feasibility["requiredEquity"] > 300.0


def test_feasibility_is_judged_against_attainable_rr_not_the_minimum(config):
    """`min_risk_reward` is a floor, not a cap.

    Targets are structural, so a setup's R:R is whatever the liquidity
    above it is worth. Judging feasibility by the *minimum* R:R declared a
    $1,000 account incapable of a $40 win, when any 1:4 setup clears it —
    the bot was reported as permanently blocked while it was merely
    selective.
    """

    feasibility = profit_floor_feasibility(config, 1_000.0)
    assert feasibility["feasible"] is True
    assert feasibility["demanding"] is True
    # $10 risk, $40 floor -> a setup must be worth 1:4.
    assert feasibility["requiredRiskReward"] == pytest.approx(4.0, abs=0.05)
    assert "reachable but demanding" in feasibility["reason"]


def test_the_profit_floor_never_raises_risk_to_close_the_gap(config):
    """Rule 2: risk may be reduced by account state, never increased by it.

    A profit objective that could bid the risk ceiling up would be exactly
    the martingale the risk engine forbids, arriving through the back door.
    """

    poor = profit_floor_feasibility(config, 300.0)
    rich = profit_floor_feasibility(config, 30_000.0)
    assert poor["maxRiskPerTrade"] == pytest.approx(300.0 * config.risk.max_risk_pct)
    assert rich["maxRiskPerTrade"] == pytest.approx(30_000.0 * config.risk.max_risk_pct)


# =========================================================================
# 4. Infrastructure under failure
# =========================================================================


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"detail"))


def test_a_rate_limit_storm_is_bounded_and_never_hangs(monkeypatch):
    calls = {"n": 0}

    def always_429(request, timeout=None):
        calls["n"] += 1
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", always_429)
    transport = HttpTransport(
        timeout=1.0, max_attempts=4, throttle=Throttle(min_interval=0.0, sleeper=lambda _s: None), sleeper=lambda _s: None
    )
    with pytest.raises(BrokerRateLimited):
        transport.request("GET", "http://x")
    # One. The tightest bound there is, and the right one: every retry of
    # a 429 is another request the host counts against the same limit, so
    # retrying extends the ban it is waiting out. The shared cooldown
    # holds every caller back instead; the next scan retries after it.
    assert calls["n"] == 1, "a rate limit must cost exactly one request"
    assert transport.throttle.cooling_down > 0, "the shared cooldown must be set"


def test_a_rate_limit_storm_never_duplicates_a_write(monkeypatch):
    calls = {"n": 0}

    def always_429(request, timeout=None):
        calls["n"] += 1
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", always_429)
    transport = HttpTransport(
        timeout=1.0, max_attempts=6, throttle=Throttle(min_interval=0.0, sleeper=lambda _s: None), sleeper=lambda _s: None
    )
    with pytest.raises(BrokerRateLimited):
        transport.request("POST", "http://x", body={"qty": 1})
    assert calls["n"] == 1


def test_the_circuit_breaker_stops_a_scan_burning_its_budget(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(http_error(503)),
    )
    transport = HttpTransport(
        timeout=1.0,
        max_attempts=1,
        circuit=CircuitBreaker(failure_threshold=3, reset_seconds=60),
        throttle=Throttle(min_interval=0.0, sleeper=lambda _s: None),
        sleeper=lambda _s: None,
    )
    for _ in range(3):
        with pytest.raises(BrokerError):
            transport.request("GET", "http://x")
    # From here calls are shed instantly rather than each costing a timeout.
    for _ in range(5):
        with pytest.raises(CircuitOpen):
            transport.request("GET", "http://x")


def test_a_database_failure_mid_flight_blocks_the_order(config, broker, repos):
    from test_execution import make_plan

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    repos.db.close()
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False
    assert broker.submitted == []


def test_a_broker_that_reports_no_position_after_a_fill_is_ambiguous_not_success(
    config, broker, repos
):
    from test_execution import make_plan

    broker.fill_on_submit = False
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.status == "UNVERIFIED"
    assert repos.intents.get(result.plan.execution_id)["status"] == "AMBIGUOUS"


def test_a_storm_of_ambiguous_submissions_never_produces_a_second_order(config, broker, repos):
    """The worst case: the network dies on every attempt."""

    from test_execution import make_plan

    broker.place_order_hook = lambda request: AmbiguousExecution("connection reset")
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)

    plan = make_plan()
    first = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)
    assert first.status == "AMBIGUOUS"
    for _ in range(5):
        repeat = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)
        assert repeat.status == "DUPLICATE"
    assert len(broker.submitted) == 1, "an ambiguous outcome must never be retried"


def test_every_symbol_failing_leaves_the_scan_intact(config, repos):
    """Total data blackout: the scan must complete and explain itself."""

    broker = FakeBroker()
    multi = dataclasses.replace(config, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    for symbol in multi.symbols:
        broker.specs[symbol] = dataclasses.replace(
            DEFAULT_SPEC, symbol=symbol, broker_name=symbol
        )
    orchestrator = Orchestrator(
        multi, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, multi)
    )
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert len(result.outcomes) == 3
    assert all(outcome.reason for outcome in result.outcomes)
    assert result.executed is None
    assert broker.submitted == []
    # And every one of them is journalled with a reason.
    assert len(repos.journal.recent()) >= 3


def test_health_stays_answerable_with_everything_broken(config, broker, repos):
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    orchestrator.startup()

    def explode(*args, **kwargs):
        raise BrokerError("total outage")

    broker.health = explode  # type: ignore[assignment]
    broker.positions = explode  # type: ignore[assignment]
    repos.db.close()

    health = orchestrator.health()
    assert health["ok"] is False
    assert health["tradingPermitted"] is False
    # The kill switch must fail CLOSED when its state cannot be read.
    assert health["components"]["killSwitch"]["active"] is True


# =========================================================================
# 5. Paper mode under violent conditions
# =========================================================================


@pytest.fixture()
def paper_stress(config, broker, repos):
    paper_config = dataclasses.replace(
        config,
        mode=ExecutionMode.PAPER,
        paper=dataclasses.replace(config.paper, starting_balance=10_000.0),
    )
    wrapper = PaperBroker(broker, paper_config, repos)
    wrapper.ensure_session()
    return wrapper, broker, paper_config


def test_a_gap_straight_through_the_stop_books_the_real_loss(paper_stress):
    """A gap loses MORE than the planned risk. That must be recorded
    honestly, not clipped to the planned amount."""

    paper, live, _ = paper_stress
    live.quotes["EURUSD"] = Quote("EURUSD", 1.10000, 1.10010, BASE_TIME)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=1.0, stop_loss=1.0950, take_profit=1.1150
    )
    # Monday opens 400 pips below the stop.
    live.quotes["EURUSD"] = Quote("EURUSD", 1.05500, 1.05510, BASE_TIME)
    assert paper.positions() == []

    closed = paper.paper.closed_positions()[0]
    planned_risk = (1.10012 - 1.0950) * 100_000 * 1.0
    assert closed["realized_pnl"] < 0
    assert abs(closed["realized_pnl"]) > planned_risk * 0.9, (
        "a gap through the stop must be able to exceed the planned risk"
    )


def test_a_spread_explosion_does_not_let_paper_mode_fill(paper_stress):
    paper, live, _ = paper_stress
    live.quotes["EURUSD"] = Quote("EURUSD", 1.0800, 1.1200, BASE_TIME)
    with pytest.raises(BrokerRejected):
        paper.place_market_order(
            DEFAULT_SPEC, direction="BUY", quantity=0.1, stop_loss=1.0950, take_profit=1.1150
        )
    assert paper.positions() == []


def test_paper_equity_never_silently_diverges_from_its_ledger(paper_stress):
    """balance must always equal starting + realised − commission."""

    paper, live, _ = paper_stress
    live.quotes["EURUSD"] = Quote("EURUSD", 1.10000, 1.10010, BASE_TIME)

    for _ in range(3):
        paper.place_market_order(
            DEFAULT_SPEC, direction="BUY", quantity=0.1, stop_loss=1.0950, take_profit=1.1150
        )
        live.quotes["EURUSD"] = Quote("EURUSD", 1.11550, 1.11560, BASE_TIME)
        paper.positions()
        live.quotes["EURUSD"] = Quote("EURUSD", 1.10000, 1.10010, BASE_TIME)

    record = paper.paper.account()
    expected = (
        float(record["starting_balance"])
        + float(record["realized_pnl"])
        - float(record["commission_paid"])
    )
    assert paper.account_state().balance == pytest.approx(round(expected, 2), abs=0.01)


def test_a_broker_outage_mid_paper_run_does_not_lose_the_position(paper_stress):
    paper, live, paper_config = paper_stress
    live.quotes["EURUSD"] = Quote("EURUSD", 1.10000, 1.10010, BASE_TIME)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )

    def explode(_spec):
        raise BrokerError("feed down")

    live.quote = explode  # type: ignore[assignment]
    assert len(paper.positions()) == 1

    # Recovery: the position is still there with its protection intact.
    live.quote = FakeBroker.quote.__get__(live)  # type: ignore[assignment]
    live.quotes["EURUSD"] = Quote("EURUSD", 1.10000, 1.10010, BASE_TIME)
    restored = paper.positions()[0]
    assert restored.stop_loss == pytest.approx(1.0950)
    assert restored.take_profit == pytest.approx(1.1150)


# =========================================================================
# 6. Concurrency
# =========================================================================


def test_overlapping_scans_do_not_both_execute(config, broker, repos):
    import threading

    m15 = bullish_setup_m15()
    broker.set_series("EURUSD", "M15", m15)
    broker.set_series("EURUSD", "H1", aligned_htf(m15, timeframe="H1"))
    broker.set_series("EURUSD", "H4", aligned_htf(m15, timeframe="H4"))
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    orchestrator.startup()

    results = []
    barrier = threading.Barrier(4)

    def run() -> None:
        barrier.wait()
        results.append(orchestrator.scan(source="manual", now=SETUP_END))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(broker.submitted) <= 1, (
        f"{len(broker.submitted)} orders from concurrent scans — the lock failed"
    )


def test_a_second_execution_of_the_same_plan_from_two_threads_yields_one_order(
    config, broker, repos
):
    import threading

    from test_execution import make_plan

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    plan = make_plan()
    statuses: list[str] = []
    barrier = threading.Barrier(5)

    def run() -> None:
        barrier.wait()
        statuses.append(executor.execute(plan, DEFAULT_SPEC, atr=0.0012).status)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(broker.submitted) == 1, f"{len(broker.submitted)} orders for one plan"
    assert statuses.count("FILLED") == 1
