"""Timers for the scan, position-management and reconcile loops.

Overlap protection is layered: this scheduler will not start a job that is
already running, AND `Orchestrator.scan` holds its own non-blocking lock
(MASTER_MISSION §60). Either alone would be enough in the happy path; both
are needed because a manual trigger from the API can race a timer.

Scans are aligned to the candle close plus a small offset, so the M15
series the strategy reads is the one that just finished forming rather
than one that is 14 minutes stale.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .clock import utc_now
from .observability import log_event


@dataclass
class JobState:
    name: str
    running: bool = False
    last_run_at: str | None = None
    last_status: str = "idle"
    last_error: str | None = None
    next_run_at: str | None = None
    runs: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "lastRunAt": self.last_run_at,
            "lastStatus": self.last_status,
            "lastError": self.last_error,
            "nextRunAt": self.next_run_at,
            "runs": self.runs,
            "failures": self.failures,
        }


def next_candle_close(interval_minutes: int, offset_seconds: int, now: datetime | None = None) -> datetime:
    """The next boundary of `interval_minutes`, plus the offset."""

    moment = now or utc_now()
    minutes = (moment.minute // interval_minutes + 1) * interval_minutes
    base = moment.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
    return base + timedelta(seconds=offset_seconds)


class PeriodicJob(threading.Thread):
    def __init__(
        self,
        name: str,
        interval_seconds: float,
        run: Callable[[], Any],
        *,
        aligned_to: int | None = None,
        offset_seconds: int = 0,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__(name=f"job-{name}", daemon=True)
        self.state = JobState(name=name)
        self.interval_seconds = interval_seconds
        self._run = run
        self._aligned_to = aligned_to
        self._offset_seconds = offset_seconds
        self._stop = stop_event or threading.Event()

    def _next_delay(self) -> float:
        if self._aligned_to:
            target = next_candle_close(self._aligned_to, self._offset_seconds)
            self.state.next_run_at = target.isoformat()
            return max(1.0, (target - utc_now()).total_seconds())
        target = utc_now() + timedelta(seconds=self.interval_seconds)
        self.state.next_run_at = target.isoformat()
        return self.interval_seconds

    def run(self) -> None:
        while not self._stop.is_set():
            delay = self._next_delay()
            if self._stop.wait(delay):
                break
            if self.state.running:
                log_event(
                    "SCHEDULER",
                    f"{self.state.name} is still running; skipping this tick",
                    severity="warning",
                )
                continue
            self.state.running = True
            self.state.last_run_at = utc_now().isoformat()
            self.state.last_status = "running"
            try:
                self._run()
                self.state.last_status = "completed"
                self.state.last_error = None
                self.state.runs += 1
            except Exception as exc:  # noqa: BLE001 - a job must never kill its thread
                self.state.last_status = "failed"
                self.state.last_error = str(exc)[:500]
                self.state.failures += 1
                log_event(
                    "SCHEDULER",
                    f"{self.state.name} failed: {exc}",
                    severity="error",
                )
            finally:
                self.state.running = False

    def stop(self) -> None:
        self._stop.set()


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[PeriodicJob] = []
        self._stop = threading.Event()

    def add(
        self,
        name: str,
        interval_seconds: float,
        run: Callable[[], Any],
        *,
        aligned_to: int | None = None,
        offset_seconds: int = 0,
    ) -> PeriodicJob:
        job = PeriodicJob(
            name,
            interval_seconds,
            run,
            aligned_to=aligned_to,
            offset_seconds=offset_seconds,
            stop_event=self._stop,
        )
        self._jobs.append(job)
        return job

    def start(self) -> None:
        for job in self._jobs:
            job.start()
        log_event("SCHEDULER", "started", jobs=[job.state.name for job in self._jobs])

    def stop(self, timeout: float = 10.0) -> None:
        """Graceful stop: signal every job and wait for in-flight work.

        Deliberately does NOT touch broker positions (MASTER_MISSION §62)
        — a deploy must never close a live trade.
        """

        self._stop.set()
        deadline = time.monotonic() + timeout
        for job in self._jobs:
            remaining = max(0.1, deadline - time.monotonic())
            job.join(timeout=remaining)
        log_event("SCHEDULER", "stopped")

    def state(self) -> list[dict[str, Any]]:
        return [job.state.as_dict() for job in self._jobs]
