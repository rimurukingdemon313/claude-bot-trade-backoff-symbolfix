"""News filtering, HTTP resilience, and secret redaction."""

from __future__ import annotations

import dataclasses
import json
import urllib.error
from datetime import timedelta

import pytest

from bot.broker.http import CircuitBreaker, HttpTransport, Throttle
from bot.config import NewsConfig
from bot.errors import AmbiguousExecution, BrokerAuthError, BrokerRateLimited, BrokerRejected, CircuitOpen
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


def test_a_read_retries_on_rate_limiting_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_error(429)
        return FakeResponse({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert transport().request("GET", "http://x")["ok"] is True
    assert calls["n"] == 3


def test_retries_are_bounded(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BrokerRateLimited):
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
