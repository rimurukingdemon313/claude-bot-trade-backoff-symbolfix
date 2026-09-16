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
