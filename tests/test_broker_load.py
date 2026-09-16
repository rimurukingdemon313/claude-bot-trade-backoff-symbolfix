"""How many requests a scan actually costs.

Cloudflare answered 1015 to this account repeatedly, and every previous
fix addressed the SYMPTOM: spacing requests further apart, backing off
longer, opening a circuit sooner. None of them reduced the number of
requests, which is the only thing the rate limit counts.

These tests measure that number. They are the only kind that can fail
when a future change quietly reintroduces the load.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from bot.broker.http import CircuitBreaker, HttpTransport, Throttle
from bot.errors import BrokerRateLimited
from bot.marketdata.candles import TIMEFRAME_MINUTES
from bot.marketdata.provider import (
    MIN_CACHE_SECONDS,
    PUBLISH_GRACE_SECONDS,
    MarketDataProvider,
)
from fakes import DEFAULT_SPEC, SETUP_END, FakeBroker, aligned_htf, bullish_setup_m15


@pytest.fixture()
def counting_broker(broker: FakeBroker) -> FakeBroker:
    """The fake, with every candle request counted."""

    broker.candle_calls = []  # type: ignore[attr-defined]
    original = broker.candles

    def counted(spec, timeframe, *, count=300):
        broker.candle_calls.append(timeframe)  # type: ignore[attr-defined]
        return original(spec, timeframe, count=count)

    broker.candles = counted  # type: ignore[assignment]
    return broker


def test_a_closed_series_is_not_refetched_before_its_next_close(config, counting_broker):
    """The whole saving, stated as the fact it rests on.

    `validate_series` removes the forming candle, so a series is closed
    bars only - and closed bars do not move. Re-fetching one before its
    next close returns different bytes and identical analysis input.

    The old policy re-fetched at a fixed fraction of the bar (0.2), which
    is five times per bar, four of them for nothing.
    """

    provider = MarketDataProvider(counting_broker, config)
    for _ in range(5):
        provider.multi_timeframe(DEFAULT_SPEC, now=SETUP_END)

    calls = counting_broker.candle_calls  # type: ignore[attr-defined]
    assert calls.count("H4") == 1, "H4 was re-fetched inside one 4-hour bar"
    assert calls.count("H1") == 1, "H1 was re-fetched inside one 1-hour bar"
    assert calls.count("M15") == 1


def test_the_cache_expires_when_the_next_bar_actually_closes(config, counting_broker):
    """Not a fraction of the bar - the bar."""

    provider = MarketDataProvider(counting_broker, config)
    for timeframe in ("M15", "H1", "H4"):
        series = provider.series(DEFAULT_SPEC, timeframe, now=SETUP_END)
        bar = TIMEFRAME_MINUTES[timeframe] * 60
        newest_close = series.candles[-1].close_time

        # Just after the fetch: usable until this bar's successor closes.
        usable = provider._seconds_until_stale(series, timeframe, now=SETUP_END)
        expected = (newest_close + timedelta(seconds=bar) - SETUP_END).total_seconds()
        assert usable == pytest.approx(
            max(MIN_CACHE_SECONDS, min(float(bar), expected + PUBLISH_GRACE_SECONDS))
        )

        # Past that close, it is stale whatever the clock says elsewhere.
        after = newest_close + timedelta(seconds=bar * 2)
        assert provider._seconds_until_stale(series, timeframe, now=after) == MIN_CACHE_SECONDS


def test_the_cache_is_never_held_longer_than_one_bar(config, counting_broker):
    """A broken timestamp must not pin a stale series in memory."""

    provider = MarketDataProvider(counting_broker, config)
    series = provider.series(DEFAULT_SPEC, "H1", now=SETUP_END)
    far_future = dataclasses.replace(
        series,
        candles=series.candles[:-1]
        + (
            dataclasses.replace(
                series.candles[-1],
                timestamp=series.candles[-1].timestamp + timedelta(days=30),
            ),
        ),
    )
    bar = TIMEFRAME_MINUTES["H1"] * 60
    assert provider._seconds_until_stale(far_future, "H1", now=SETUP_END) <= bar


def test_a_scan_costs_fewer_history_requests_than_the_old_policy(config, counting_broker):
    """The measurement that matters, across a realistic run.

    Sixteen scans at the configured interval is one H4 bar. Under the old
    fixed-fraction TTL every timeframe was re-fetched five times per bar;
    aligned to the close, each is fetched once per bar it belongs to.
    """

    provider = MarketDataProvider(counting_broker, config)
    scan_interval = timedelta(minutes=config.scheduler.scan_interval_minutes)
    moment = SETUP_END
    for _ in range(16):
        provider.multi_timeframe(DEFAULT_SPEC, now=moment)
        moment += scan_interval

    calls = counting_broker.candle_calls  # type: ignore[attr-defined]
    # The fake serves a fixed series, so its newest close never advances:
    # every timeframe should settle into its floor of re-fetches rather
    # than one per scan.
    assert len(calls) < 16 * 3, "the cache saved nothing across sixteen scans"
    assert calls.count("H4") <= calls.count("M15")


# -- the rate limit itself -------------------------------------------------


def _transport(**overrides) -> HttpTransport:
    defaults = dict(
        timeout=1.0,
        max_attempts=4,
        throttle=Throttle(min_interval=0.0, sleeper=lambda _s: None),
        sleeper=lambda _s: None,
    )
    defaults.update(overrides)
    return HttpTransport(**defaults)


def _http_error(code: int):
    import io
    import urllib.error

    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"detail"))


def test_one_rate_limited_read_costs_exactly_one_request(monkeypatch):
    """It used to cost four, and four failures trip a five-failure circuit.

    That is how a single throttled symbol took the rest of the scan with
    it: USDJPY hit 1015, retried three more times into a host that was
    already refusing it, opened the circuit, and AUDUSD, USDCHF and
    XAUUSD were shed without ever reaching the network.
    """

    calls = {"n": 0}

    def always_429(request, timeout=None):
        calls["n"] += 1
        raise _http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", always_429)
    circuit = CircuitBreaker(failure_threshold=5, reset_seconds=60.0)
    wire = _transport(circuit=circuit)

    for _ in range(4):
        with pytest.raises(BrokerRateLimited):
            wire.request("GET", "http://x")

    assert calls["n"] == 4, "four reads must cost four requests, not sixteen"
    assert circuit.state == "closed", "a rate limit is not an outage"
    assert wire.rate_limited == 4
    assert wire.throttle.cooling_down > 0


def test_a_server_error_still_retries(monkeypatch):
    """Only the rate limit is exempt. A 5xx is a real transient fault."""

    calls = {"n": 0}

    def always_503(request, timeout=None):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr("urllib.request.urlopen", always_503)
    circuit = CircuitBreaker(failure_threshold=99, reset_seconds=60.0)
    from bot.errors import BrokerError

    with pytest.raises(BrokerError):
        _transport(max_attempts=3, circuit=circuit).request("GET", "http://x")
    assert calls["n"] == 3


# -- the background poll ---------------------------------------------------


def _orchestrator(config, broker, repos):
    from bot.orchestrator import Orchestrator

    return Orchestrator(
        config,
        broker=broker,
        repositories=repos,
        market_data=MarketDataProvider(broker, config),
    )


def test_an_empty_account_is_not_polled_every_thirty_seconds(config, broker, repos):
    """The largest single source of broker traffic in the system.

    Polling positions every 30s with none open was 2.7x the scan load -
    360 requests an hour spent asking about positions that did not exist,
    against a rate limit the scans needed. While flat the poll backs off.
    """

    orchestrator = _orchestrator(config, broker, repos)
    calls = {"n": 0}
    original = broker.positions

    def counted():
        calls["n"] += 1
        return original()

    broker.positions = counted  # type: ignore[assignment]

    for _ in range(8):
        orchestrator.manage_positions(now=SETUP_END)

    assert calls["n"] == 1, "an empty account was re-read on every tick"
    result = orchestrator.manage_positions(now=SETUP_END)
    assert result["ok"] is True
    assert "no open positions" in result["skipped"]


def test_a_held_position_is_polled_at_the_fast_cadence(config, broker, repos):
    """The back-off is for an EMPTY account and nothing else.

    A position that exists has a stop to move and a structure to check,
    and it gets the full 30-second cadence.
    """

    orchestrator = _orchestrator(config, broker, repos)
    broker.add_position(symbol="EURUSD", direction="BUY", quantity=0.1, entry=1.1000)

    calls = {"n": 0}
    original = broker.positions

    def counted():
        calls["n"] += 1
        return original()

    broker.positions = counted  # type: ignore[assignment]

    for _ in range(5):
        orchestrator.manage_positions(now=SETUP_END)
    assert calls["n"] == 5, "a held position must be polled on every tick"
    assert not orchestrator._can_skip_position_poll()


def test_opening_a_trade_restores_the_fast_cadence_immediately(config, broker, repos):
    """Nothing has to remember to say "a trade happened".

    The count is recorded in the one place that reads positions, so the
    read the executor already triggers restores fast polling by itself.
    """

    orchestrator = _orchestrator(config, broker, repos)
    orchestrator.manage_positions(now=SETUP_END)
    assert orchestrator._can_skip_position_poll(), "flat: the poll should back off"

    broker.add_position(  # a fill appears on the account
        symbol="EURUSD", direction="BUY", quantity=0.1, entry=1.1000
    )
    orchestrator.refresh_live_positions(now=SETUP_END)
    assert orchestrator._positions_held == 1
    assert not orchestrator._can_skip_position_poll(), "a held position must be polled"


def test_the_backoff_can_never_hide_a_position_from_the_reconciler(config, broker, repos):
    """The safety argument, asserted rather than reasoned about.

    Skipping is only ever safe because the reconciler reads the broker on
    its own timer and does not consult this cadence at all.
    """

    orchestrator = _orchestrator(config, broker, repos)
    orchestrator.manage_positions(now=SETUP_END)
    assert orchestrator._can_skip_position_poll()

    broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.1, entry=1.1000
    )
    report = orchestrator.reconciler.reconcile()
    assert report.checked_positions == 1, "the reconciler must see it regardless"


# -- finding a limit nobody knows -----------------------------------------


class FakeHost:
    """A host with a SECRET burst limit, on a clock the test controls.

    The point of the control loop is that the limit is unknown and
    unmeasurable without tripping it. A test that told the throttle the
    answer would be testing nothing, so this never does: the limit lives
    here and the throttle only ever sees 429s.
    """

    def __init__(self, *, requests_per_minute: float) -> None:
        self.window = 60.0
        self.allowed = requests_per_minute
        self.now = 0.0
        self.times: list[float] = []
        self.refusals = 0
        self.served = 0

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    def request(self) -> bool:
        """True if served, False if the host refused (429)."""

        self.times = [t for t in self.times if t > self.now - self.window]
        if len(self.times) >= self.allowed:
            self.refusals += 1
            return False
        self.times.append(self.now)
        self.served += 1
        return True


def _drive(throttle: Throttle, host: FakeHost, *, requests: int) -> None:
    """Run `requests` through the throttle against the host."""

    for _ in range(requests):
        throttle.wait()
        host.now += 0.01  # the request itself takes a moment
        if host.request():
            throttle.record_success()
        else:
            throttle.penalise(60.0)


def test_the_throttle_finds_a_limit_it_was_never_told(monkeypatch):
    """The whole point, stated as a measurement.

    Configured for 100 requests/minute against a host that allows 40. A
    fixed interval cannot solve this - it is either too slow forever or
    refused forever, and picking between them is what every previous
    attempt on this failure did.
    """

    host = FakeHost(requests_per_minute=40)
    throttle = Throttle(min_interval=0.6, sleeper=host.sleep)
    monkeypatch.setattr("bot.broker.http.time.monotonic", lambda: host.now)

    assert 60 / throttle.interval > host.allowed, "must start too fast, or nothing is learned"

    _drive(throttle, host, requests=400)

    learned_rate = 60 / throttle.interval
    assert learned_rate <= host.allowed * 1.2, (
        f"settled at {learned_rate:.0f}/min against a limit of {host.allowed}/min"
    )
    assert throttle.interval <= throttle.max_interval


def test_it_stops_being_refused_once_it_has_learned(monkeypatch):
    """Convergence is only worth anything if the refusals stop."""

    host = FakeHost(requests_per_minute=40)
    throttle = Throttle(min_interval=0.6, sleeper=host.sleep)
    monkeypatch.setattr("bot.broker.http.time.monotonic", lambda: host.now)

    _drive(throttle, host, requests=200)
    early = host.refusals
    _drive(throttle, host, requests=200)
    late = host.refusals - early

    assert late < early, f"still being refused after learning: {early} then {late}"
    assert late <= 1, f"{late} refusals in the second half — it has not settled"


def test_a_generous_host_is_not_slowed_down_forever(monkeypatch):
    """One bad minute must not cost the rest of the day.

    The decrease is multiplicative and the recovery additive, so a host
    that turns out to be generous is paid back for gradually rather than
    being punished permanently.
    """

    host = FakeHost(requests_per_minute=10_000)
    throttle = Throttle(min_interval=0.6, sleeper=host.sleep)
    monkeypatch.setattr("bot.broker.http.time.monotonic", lambda: host.now)

    throttle.penalise(60.0)
    widened = throttle.interval
    assert widened > 0.6, "a refusal must widen the spacing"

    _drive(throttle, host, requests=1000)
    assert throttle.interval < widened, "sustained success must narrow it back"
    assert throttle.interval >= throttle.min_interval, "never below the configured floor"


def test_the_spacing_never_exceeds_its_ceiling(monkeypatch):
    """A host refusing everything must not stall the bot indefinitely."""

    host = FakeHost(requests_per_minute=0)
    throttle = Throttle(min_interval=0.6, max_interval=3.0, sleeper=host.sleep)
    monkeypatch.setattr("bot.broker.http.time.monotonic", lambda: host.now)

    for _ in range(50):
        throttle.penalise(60.0)
    assert throttle.interval == 3.0


def test_health_reports_the_spacing_actually_in_force(monkeypatch):
    """Rule 6 applies to the bot's own settings.

    /health reported the configured floor while the loop ran wider. That
    is the same class of untruth that sent three diagnoses of this
    failure in the wrong direction.
    """

    wire = _transport(throttle=Throttle(min_interval=0.6, sleeper=lambda _s: None))
    wire.throttle.penalise(60.0)
    health = wire.health()
    assert health["requestSpacingSeconds"] > 0.6
    assert health["requestSpacingFloorSeconds"] == 0.6


def test_what_one_run_learns_survives_the_next_restart(config):
    """Railway restarts the process on every deploy.

    Finding the limit costs a handful of refusals, and without this the
    bot bought the same answer again on every push — several times a day
    during active work.
    """

    from bot.service import BotService

    throttle = Throttle(min_interval=0.6, sleeper=lambda _s: None)
    for _ in range(3):
        throttle.penalise(60.0)
    learned = throttle.interval
    assert learned > 0.6

    service = object.__new__(BotService)
    service.repos = repos_for(config)
    service.live_broker = _broker_with(throttle)
    service._persist_request_spacing()

    fresh = Throttle(min_interval=0.6, sleeper=lambda _s: None)
    assert fresh.interval == 0.6
    service.live_broker = _broker_with(fresh)
    service._restore_request_spacing()
    assert fresh.interval == pytest.approx(learned, abs=0.01)


def test_a_stored_spacing_is_never_trusted_past_the_configured_band(config):
    """A value from a different configuration must not widen the floor."""

    throttle = Throttle(min_interval=0.6, max_interval=3.0, sleeper=lambda _s: None)
    throttle.restore(0.01)
    assert throttle.interval == 0.6, "a stored value must not go below the floor"
    throttle.restore(999.0)
    assert throttle.interval == 3.0, "nor above the ceiling"
    throttle.restore("not a number")  # type: ignore[arg-type]
    assert throttle.interval == 3.0, "nor corrupt it"
    throttle.restore(-5.0)
    assert throttle.interval == 3.0


def repos_for(config):
    from bot.storage.db import in_memory_database
    from bot.storage.repositories import Repositories

    return Repositories(in_memory_database())


def _broker_with(throttle):
    class _Transport:
        def __init__(self, t):
            self.throttle = t

    class _Broker:
        def __init__(self, t):
            self.transport = _Transport(t)

    return _Broker(throttle)
