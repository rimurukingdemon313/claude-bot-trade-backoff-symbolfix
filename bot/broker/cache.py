"""What the background threads last saw, for the dashboard to read.

The dashboard used to call the broker on every poll: `account_state()`,
`positions()` with a quote per position, then `build_account_state()`
again for the risk panel — five to seven requests per refresh.

Every one goes through the shared throttle, which holds a lock for the
minimum spacing between requests. A scan across 23 symbols issues dozens
of requests and owns that lock for the better part of a minute. So a
`/snapshot` arriving mid-scan queued behind the whole scan; the Node
proxy gave up at 30 seconds, and the page reported the bot unavailable
while the bot was healthy and busy. The bot then finished the work and
wrote its answer into a socket nobody was holding any more, which is the
BrokenPipeError in the logs: not a fault of its own, the tail of one.

Worse, that polling was itself a rate-limit generator. A dashboard open
on a phone was competing with the trading loop for the same Cloudflare
budget that had already been the subject of two fixes.

So the direction is inverted. The threads that must talk to the broker
anyway — the scan and the position poll — deposit what they read here,
and the dashboard reads only from here. It cannot block, it cannot fail,
and it costs the rate limit nothing. This also makes rule 11 true at the
transport level and not merely by convention: a presentation layer that
cannot issue a broker request cannot slow down or rate-limit trading.

This is not a cache in the "avoid recomputation" sense; it is the read
model. Rule 6 holds in full: a value is served with the time it was read
and an explicit staleness flag, never as though it were current, and a
key that was never filled says so rather than reporting a zero.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..clock import utc_now


@dataclass(frozen=True)
class CachedRead:
    """A value, when it was read, and what has gone wrong since.

    `value` and `error` are deliberately not exclusive: a failed refresh
    keeps the last good value and records the failure beside it. "Here is
    what we last saw, 90 seconds ago, and here is why it has not updated"
    is more useful than an empty panel and still honest, because both
    halves are shown.
    """

    value: Any | None = None
    at: datetime | None = None
    error: str | None = None

    @property
    def present(self) -> bool:
        return self.value is not None and self.at is not None

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        if self.at is None:
            return None
        return max(0.0, ((now or utc_now()) - self.at).total_seconds())


class LiveCache:
    """Thread-safe last-known-good storage, keyed by read name."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, CachedRead] = {}

    def put(self, key: str, value: Any, *, now: datetime | None = None) -> None:
        """Record a successful read, clearing any previous failure."""

        with self._lock:
            self._entries[key] = CachedRead(value=value, at=now or utc_now(), error=None)

    def fail(self, key: str, error: str) -> None:
        """Record a failed refresh WITHOUT discarding the last good value."""

        with self._lock:
            previous = self._entries.get(key, CachedRead())
            self._entries[key] = CachedRead(
                value=previous.value, at=previous.at, error=str(error)
            )

    def get(self, key: str) -> CachedRead:
        with self._lock:
            return self._entries.get(key, CachedRead())
