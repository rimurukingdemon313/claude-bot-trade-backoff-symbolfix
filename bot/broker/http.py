"""HTTP transport: bounded retries, jitter, throttling, circuit breaker.

Deliberate asymmetry between reads and writes:

* Reads (GET) retry on 429/5xx/timeout with exponential backoff + full
  jitter, bounded by `max_attempts`.
* Writes (POST/PATCH/DELETE) NEVER retry inside this layer. TradeLocker
  documents no client-supplied idempotency key, so a resent write can
  create a second real order. A write whose outcome is unknown raises
  AmbiguousExecution and the caller must reconcile against broker state.

The circuit breaker exists so a broker outage degrades into "we stop
calling for a minute" instead of "every scan spends its whole budget
timing out".
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import (
    AmbiguousExecution,
    BrokerAuthError,
    BrokerError,
    BrokerRateLimited,
    BrokerRejected,
    CircuitOpen,
)
from ..observability import log_event

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@dataclass
class CircuitBreaker:
    """Closed -> Open -> Half-open, with a cooldown that grows.

    The growth is the important part. A fixed 60-second reset means that
    against a block which lasts longer than a minute — a Cloudflare rate
    limit is the case here — the client probes once a minute, forever.
    Cloudflare extends a rate limit for traffic that keeps arriving during
    it, so a fixed retry does not merely fail to help: it holds the block
    open. The bot was the reason it never cleared.

    Each consecutive open doubles the wait, capped. One success resets
    everything, so a broker that comes back is used immediately.
    """

    failure_threshold: int = 5
    reset_seconds: float = 60.0
    #: Ceiling on the grown cooldown. Fifteen minutes is longer than any
    #: rate limit observed here and short enough that a recovered broker is
    #: picked up within one scan interval.
    max_reset_seconds: float = 900.0
    _failures: int = 0
    _opened_at: float = 0.0
    _half_open: bool = False
    #: How many times the circuit has opened without a success in between.
    _consecutive_opens: int = 0

    @property
    def current_reset_seconds(self) -> float:
        """The wait this open cycle earns: 60s, 120s, 240s, ... capped."""

        grown = self.reset_seconds * (2 ** max(0, self._consecutive_opens - 1))
        return min(grown, self.max_reset_seconds)

    def before_call(self) -> None:
        if self._failures < self.failure_threshold:
            return
        wait = self.current_reset_seconds
        elapsed = time.monotonic() - self._opened_at
        if elapsed < wait:
            raise CircuitOpen(
                f"broker circuit open after {self._failures} consecutive failures; "
                f"retry in {wait - elapsed:.0f}s"
            )
        self._half_open = True

    def record_success(self) -> None:
        self._failures = 0
        self._half_open = False
        # A single success clears the escalation. Backing off is for a
        # broker that is still refusing, never a tax on one that recovered.
        self._consecutive_opens = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures == self.failure_threshold:
            self._opened_at = time.monotonic()
            self._consecutive_opens += 1
        elif self._failures > self.failure_threshold:
            # A probe through the half-open gate failed. That is another
            # open cycle, and it earns the next step of the backoff.
            self._opened_at = time.monotonic()
            self._consecutive_opens += 1
            self._failures = self.failure_threshold
        self._half_open = False

    @property
    def state(self) -> str:
        if self._failures < self.failure_threshold:
            return "closed"
        return "half-open" if self._half_open else "open"


class Throttle:
    """Minimum spacing between outbound requests, plus a shared cooldown.

    TradeLocker sits behind Cloudflare, which rate-limits bursts (HTTP
    429 / error 1015). Spacing requests at the source is cheaper and more
    reliable than absorbing 429s after the fact.

    Two mechanisms, because spacing alone was not enough:

    1. `min_interval` — the floor between any two requests. The scan,
       the position poll and the reconcile run on separate threads and
       share one transport, so this lock is what keeps them from
       interleaving into a burst.

    2. `penalise()` — a cooldown every caller observes. Per-request
       backoff was the original design and it does not work across
       threads: the thread that received the 429 slept while the other
       two carried straight on hammering the same host, which is how a
       single rate limit became a sustained one. A limit is a property of
       the HOST, not of the unlucky request that discovered it.
    """

    def __init__(self, min_interval: float = 0.6, *, sleeper: Any = time.sleep) -> None:
        self.min_interval = min_interval
        # Injectable for the same reason the clock is (project rule 10):
        # a test that really sleeps is a test nobody runs. This class used
        # time.sleep directly while the transport beside it already took a
        # sleeper, so raising the production interval silently added
        # minutes to the suite.
        self._sleep = sleeper
        self._last = 0.0
        self._penalty_until = 0.0
        self._lock = threading.Lock()

    def penalise(self, seconds: float) -> None:
        """Hold every caller back — the host asked us to stop, not this call."""

        with self._lock:
            self._penalty_until = max(self._penalty_until, time.monotonic() + max(0.0, seconds))

    @property
    def cooling_down(self) -> float:
        """Seconds left on the shared cooldown, for health reporting."""

        return max(0.0, self._penalty_until - time.monotonic())

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(self.min_interval - (now - self._last), self._penalty_until - now)
            if delay > 0:
                self._sleep(delay)
            self._last = time.monotonic()


class HttpTransport:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_attempts: int = 4,
        circuit: CircuitBreaker | None = None,
        throttle: Throttle | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.circuit = circuit or CircuitBreaker()
        # A throttle built here shares the transport's sleeper. Otherwise a
        # caller that injected one to keep tests instant still got a
        # throttle sleeping on the real clock — and after a 429 that is a
        # full minute of it.
        self.throttle = throttle or Throttle(sleeper=sleeper)
        self._sleep = sleeper
        self.calls = 0
        self.last_latency_ms: float | None = None

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Exponential backoff with FULL jitter (1s, 2s, 4s base).

        Full jitter (uniform in [0, base]) rather than fixed sleeps keeps
        several workers from re-colliding in lockstep after a shared 429.
        """

        if retry_after is not None:
            return min(retry_after, 30.0)
        base = min(2.0**attempt, 16.0)
        return random.uniform(0.0, base)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """Perform one API call.

        `idempotent` defaults to False for write methods and True for
        reads; a caller can only ever make something LESS retryable, and
        the write path never sets it to True.
        """

        is_write = method.upper() in WRITE_METHODS
        retryable = (not is_write) if idempotent is None else bool(idempotent and not is_write)

        if query:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None

        attempts = self.max_attempts if retryable else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            self.circuit.before_call()
            self.throttle.wait()
            request = urllib.request.Request(url, data=payload, method=method.upper())
            request.add_header("User-Agent", BROWSER_UA)
            request.add_header("Content-Type", "application/json")
            request.add_header("Accept", "application/json")
            request.add_header("Accept-Language", "en-US,en;q=0.9")
            for key, value in (headers or {}).items():
                request.add_header(key, value)

            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                self.calls += 1
                self.last_latency_ms = (time.monotonic() - started) * 1000
                self.circuit.record_success()
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                retry_after_header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None

                if exc.code in (401, 403):
                    self.circuit.record_success()  # an auth error is not an outage
                    raise BrokerAuthError(f"{method} {url} unauthorized ({exc.code}): {detail}")
                if exc.code == 429:
                    # Cloudflare's 1015 does not carry Retry-After. A minute
                    # is the shortest cooldown that reliably clears it, and
                    # guessing lower is how a rate limit becomes permanent.
                    self.throttle.penalise(retry_after if retry_after is not None else 60.0)
                    last_error = BrokerRateLimited(f"{method} {url} rate limited: {detail}")
                elif 500 <= exc.code < 600:
                    last_error = BrokerError(f"{method} {url} server error ({exc.code}): {detail}")
                else:
                    self.circuit.record_success()
                    raise BrokerRejected(f"{method} {url} rejected ({exc.code}): {detail}")

                self.circuit.record_failure()
                if retryable and attempt < attempts - 1:
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                raise last_error
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.circuit.record_failure()
                # A write that never got a response is the dangerous case:
                # the order may or may not have reached the matching engine.
                if is_write:
                    raise AmbiguousExecution(
                        f"{method} {url} outcome unknown (transport failure after the request "
                        f"left this process): {exc}. Not retried — reconcile against broker state."
                    ) from exc
                last_error = BrokerError(f"{method} {url} unreachable: {exc}")
                if retryable and attempt < attempts - 1:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error
            except json.JSONDecodeError as exc:
                self.circuit.record_failure()
                if is_write:
                    raise AmbiguousExecution(
                        f"{method} {url} returned an unparseable body; outcome unknown: {exc}"
                    ) from exc
                last_error = BrokerError(f"{method} {url} returned malformed JSON: {exc}")
                if retryable and attempt < attempts - 1:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error

        raise last_error or BrokerError(f"{method} {url} failed after {attempts} attempts")

    def health(self) -> dict[str, Any]:
        cooling = self.throttle.cooling_down
        return {
            "circuit": self.circuit.state,
            "calls": self.calls,
            "lastLatencyMs": round(self.last_latency_ms, 1) if self.last_latency_ms else None,
            "requestSpacingSeconds": self.throttle.min_interval,
            "circuitBackoffSeconds": (
                round(self.circuit.current_reset_seconds)
                if self.circuit.state != "closed"
                else None
            ),
            # Visible because a rate-limit cooldown looks exactly like a
            # hang from outside: the bot is deliberately silent and the
            # operator has no way to tell that from a broken one.
            "rateLimitedFor": round(cooling, 1) if cooling > 0 else None,
        }
