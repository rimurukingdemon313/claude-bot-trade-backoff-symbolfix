"""News filtering, HTTP resilience, and secret redaction."""

from __future__ import annotations

import dataclasses
import json
import urllib.error
from datetime import timedelta

import pytest

from bot.broker.http import CircuitBreaker, HttpTransport, Throttle
from bot.config import NewsConfig
from bot.errors import AmbiguousExecution, BrokerAuthError, BrokerError, BrokerRateLimited, BrokerRejected, CircuitOpen
from bot.news import NewsFilter, currencies_for
from bot.observability import redact
from fakes import SETUP_END


# -- news -----------------------------------------------------------------


def seeded_filter(events, *, tmp_path, **overrides) -> NewsFilter:
    config = dataclasses.replace(NewsConfig(), **overrides)
    news = NewsFilter(config, cache_path=tmp_path / "cache.json")
    news._events = events
    news._fetched_at = SETUP_END.timestamp()
    news._feed_available = True
    # Refreshing would hit the network; the seeded data stands in for a
    # successful fetch.
    news.refresh = lambda *, force=False: True  # type: ignore[assignment]
    return news


def event(country: str, minutes_away: int, impact: str = "High") -> dict:
    return {
        "title": f"{country} rate decision",
        "country": country,
        "impact": impact,
        "date": (SETUP_END + timedelta(minutes=minutes_away)).isoformat(),
    }


def test_currency_mapping_covers_both_legs():
    assert currencies_for("GBPJPY") == ("GBP", "JPY")
    assert currencies_for("EURNZD") == ("EUR", "NZD")


def test_gold_is_mapped_to_usd_not_to_usd_twice():
    """The previous build mapped XAUUSD to ('USD','USD')."""

    assert currencies_for("XAUUSD") == ("USD",)


def test_an_imminent_high_impact_event_blocks_the_pair(tmp_path):
    news = seeded_filter([event("USD", 10)], tmp_path=tmp_path)
    verdict = news.check("EURUSD", now=SETUP_END)
    assert verdict.blocked is True
    assert "USD rate decision" in verdict.reason


def test_an_unrelated_currency_does_not_block(tmp_path):
    news = seeded_filter([event("JPY", 10)], tmp_path=tmp_path)
    assert news.check("EURUSD", now=SETUP_END).blocked is False


def test_low_impact_events_do_not_block(tmp_path):
    news = seeded_filter([event("USD", 10, impact="Low")], tmp_path=tmp_path)
    assert news.check("EURUSD", now=SETUP_END).blocked is False


def test_an_event_outside_the_window_does_not_block(tmp_path):
    news = seeded_filter([event("USD", 240)], tmp_path=tmp_path)
    assert news.check("EURUSD", now=SETUP_END).blocked is False


def test_the_post_release_window_still_blocks(tmp_path):
    news = seeded_filter([event("USD", -10)], tmp_path=tmp_path)
    assert news.check("EURUSD", now=SETUP_END).blocked is True


def test_an_unavailable_feed_fails_closed_by_default(tmp_path):
    """The previous build traded as if no news existed when the feed was
    down. Not knowing is not the same as nothing being scheduled."""

    news = NewsFilter(NewsConfig(), cache_path=tmp_path / "missing.json")
    news.refresh = lambda *, force=False: False  # type: ignore[assignment]
    verdict = news.check("EURUSD", now=SETUP_END)
    assert verdict.blocked is True
    assert "standing aside" in verdict.reason


def test_fail_open_is_available_but_opt_in(tmp_path):
    news = NewsFilter(
        dataclasses.replace(NewsConfig(), fail_closed_without_feed=False),
        cache_path=tmp_path / "missing.json",
    )
    news.refresh = lambda *, force=False: False  # type: ignore[assignment]
    assert news.check("EURUSD", now=SETUP_END).blocked is False


def test_a_disabled_filter_never_blocks(tmp_path):
    news = NewsFilter(dataclasses.replace(NewsConfig(), enabled=False), cache_path=tmp_path / "c.json")
    assert news.check("EURUSD", now=SETUP_END).blocked is False


def test_upcoming_events_are_listed_for_the_dashboard(tmp_path):
    news = seeded_filter([event("USD", 120), event("EUR", 300)], tmp_path=tmp_path)
    upcoming = news.upcoming(["EURUSD"], hours=12, now=SETUP_END)
    assert len(upcoming) == 2
    assert upcoming[0]["minutesAway"] == 120


# -- transport ------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def transport(**overrides) -> HttpTransport:
    defaults = dict(timeout=1.0, max_attempts=4, throttle=Throttle(min_interval=0.0, sleeper=lambda _s: None), sleeper=lambda _s: None)
    defaults.update(overrides)
    return HttpTransport(**defaults)


def http_error(code: int) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"detail"))


def test_a_rate_limit_is_never_retried(monkeypatch):
    """Retrying a 429 makes the ban it is trying to ride out longer.

    This asserted the opposite: that a read retries THROUGH a rate limit
    and succeeds on the third attempt. That was a deliberate choice, and
    it was wrong once the shared cooldown existed beside it.

    Three reasons, all of which showed up live as Cloudflare 1015:

    * every retry is another request the host counts, so the retry
      extends the limit it is waiting out;
    * `penalise()` holds the throttle lock for the full cooldown, and the
      retry sleeps inside it — so one rate-limited read froze every
      broker call in the process for minutes, not just its own;
    * four failures from ONE symbol tripped a five-failure circuit, which
      then shed every remaining symbol and doubled its own backoff.

    The cooldown is the correct mechanism and already holds every thread
    back. The next scan retries naturally, after it has expired.
    """

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BrokerRateLimited):
        transport().request("GET", "http://x")
    assert calls["n"] == 1, "a rate limit must cost exactly one request"


def test_a_rate_limit_is_not_counted_as_a_broker_outage(monkeypatch):
    """The circuit breaker is for a broker that is DOWN.

    A rate limit is a healthy broker telling us to slow down. Counting it
    as an outage is how one throttled symbol took the whole scan with it.
    """

    from bot.broker.http import CircuitBreaker

    def always_429(request, timeout=None):
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", always_429)
    circuit = CircuitBreaker(failure_threshold=2, reset_seconds=60.0)
    wire = transport(circuit=circuit)
    for _ in range(5):
        with pytest.raises(BrokerRateLimited):
            wire.request("GET", "http://x")
    assert circuit.state == "closed", "a rate limit must not open the circuit"
    assert wire.throttle.cooling_down > 0, "but it must set the shared cooldown"
    assert wire.rate_limited == 5


def test_retries_are_bounded(monkeypatch):
    """Server errors still retry, and still stop. Only 429 is exempt."""

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise http_error(503)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BrokerError):
        transport(max_attempts=3).request("GET", "http://x")
    assert calls["n"] == 3, "retries must never be unbounded"


def test_a_write_is_never_retried(monkeypatch):
    """The single rule that prevents duplicate orders at the wire level."""

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BrokerRateLimited):
        transport().request("POST", "http://x", body={"qty": 1})
    assert calls["n"] == 1


def test_a_lost_write_response_is_ambiguous_not_a_failure(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousExecution, match="outcome unknown"):
        transport().request("POST", "http://x", body={"qty": 1})


def test_an_auth_error_surfaces_immediately(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: (_ for _ in ()).throw(http_error(401))
    )
    with pytest.raises(BrokerAuthError):
        transport().request("GET", "http://x")


def test_a_client_error_is_a_permanent_rejection(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: (_ for _ in ()).throw(http_error(400))
    )
    with pytest.raises(BrokerRejected):
        transport().request("GET", "http://x")


def test_the_circuit_opens_after_repeated_failures_and_sheds_load(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: (_ for _ in ()).throw(http_error(500))
    )
    client = transport(max_attempts=1, circuit=CircuitBreaker(failure_threshold=2, reset_seconds=60))
    for _ in range(2):
        with pytest.raises(Exception):
            client.request("GET", "http://x")
    assert client.circuit.state == "open"
    with pytest.raises(CircuitOpen):
        client.request("GET", "http://x")


def test_backoff_uses_jitter_so_workers_do_not_re_collide():
    client = transport()
    delays = {client._backoff(2) for _ in range(30)}
    assert len(delays) > 1, "a fixed sleep would resynchronise every client after a shared 429"
    assert all(0.0 <= delay <= 4.0 for delay in delays)


def test_a_retry_after_header_is_respected():
    assert transport()._backoff(0, retry_after=7.0) == 7.0


# -- redaction ------------------------------------------------------------


def test_access_tokens_are_redacted_from_logs():
    raw = '{"accessToken": "eyJhbGciOi.secret.value", "accNum": 3}'
    assert "eyJhbGciOi" not in redact(raw)
    assert "redacted" in redact(raw)


def test_configured_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret_value")
    assert "gsk_supersecret_value" not in redact("key is gsk_supersecret_value")


# -- a rate limit belongs to the host, not to the unlucky request ---------


def test_a_429_holds_back_every_caller_not_just_the_one_that_saw_it():
    """The live failure this prevents.

    GATESFX answered Cloudflare error 1015 — "you are being rate-limited
    by the website owner's configuration" — and the startup sequence could
    not read account state at all. Per-request backoff was the whole
    design, and it cannot work: the scan, the position poll and the
    reconcile run on separate threads over one transport, so the thread
    that caught the 429 slept while the other two carried on hammering the
    same host. That is how one rate limit becomes a sustained one.
    """

    slept: list[float] = []
    throttle = Throttle(min_interval=0.0, sleeper=slept.append)
    assert throttle.cooling_down == 0.0

    throttle.penalise(60.0)

    assert throttle.cooling_down > 55.0
    throttle.wait()
    assert slept and slept[-1] > 55.0, "a caller that never saw the 429 must still be held"


def test_retry_after_is_preferred_over_the_default_cooldown():
    throttle = Throttle(min_interval=0.0, sleeper=lambda _s: None)
    throttle.penalise(5.0)
    assert 4.0 < throttle.cooling_down <= 5.0


def test_the_cooldown_only_ever_extends():
    """A later, shorter penalty must not shorten a longer one already set."""

    throttle = Throttle(min_interval=0.0, sleeper=lambda _s: None)
    throttle.penalise(60.0)
    throttle.penalise(1.0)
    assert throttle.cooling_down > 55.0


def test_a_rate_limited_response_sets_the_shared_cooldown(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = transport(max_attempts=1)
    with pytest.raises(BrokerRateLimited):
        client.request("GET", "http://x")
    assert client.throttle.cooling_down > 55.0, (
        "the 429 must pause the whole transport, not only this call"
    )


def test_a_transport_built_without_a_throttle_shares_its_sleeper():
    """Otherwise an injected sleeper is silently bypassed by the pacing."""

    slept: list[float] = []
    client = HttpTransport(timeout=1.0, sleeper=slept.append)
    client.throttle.penalise(30.0)
    client.throttle.wait()
    assert slept, "the throttle slept on the real clock instead of the injected one"


# -- a fixed retry keeps a rate limit alive --------------------------------


def test_the_cooldown_grows_while_the_broker_keeps_refusing():
    """A fixed 60s reset probes once a minute, forever.

    Cloudflare extends a rate limit for traffic that keeps arriving during
    it, so a fixed retry does not merely fail to help — it holds the block
    open. The bot was the reason it never cleared.
    """

    from bot.broker.http import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, reset_seconds=60.0)
    for _ in range(5):
        breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.current_reset_seconds == pytest.approx(60.0)

    for expected in (120.0, 240.0, 480.0):
        breaker.record_failure()  # a probe through half-open failed again
        assert breaker.current_reset_seconds == pytest.approx(expected)


def test_the_cooldown_is_capped():
    """Long enough to outlast any observed block, short enough that a
    recovered broker is picked up within one scan interval."""

    from bot.broker.http import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60.0, max_reset_seconds=900.0)
    for _ in range(20):
        breaker.record_failure()
    assert breaker.current_reset_seconds == pytest.approx(900.0)


def test_one_success_clears_the_escalation_entirely():
    """Backing off is for a broker still refusing, never a tax on one that
    recovered."""

    from bot.broker.http import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60.0)
    for _ in range(6):
        breaker.record_failure()
    assert breaker.current_reset_seconds > 60.0

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.current_reset_seconds == pytest.approx(60.0)
    breaker.before_call()  # must not raise


def test_the_health_report_shows_the_wait_that_is_actually_in_force():
    """A fifteen-minute silence is alarming unless it says it is deliberate."""

    client = transport()
    assert client.health()["circuitBackoffSeconds"] is None
    for _ in range(5):
        client.circuit.record_failure()
    assert client.health()["circuitBackoffSeconds"] == 60
