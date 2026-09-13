"""Persistent kill switch.

Survives restarts because it lives in the database, not in memory. When
active, no new trade may be created for any symbol; existing positions
are still managed (a kill switch must not strand a live position without
a stop).

Auto-trip conditions are evaluated by the risk engine, which calls
`trip()`. Clearing is deliberately manual-only for safety trips: a
system that trips on broker inconsistency and then un-trips itself on
the next scan has no kill switch at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..clock import utc_now
from ..observability import log_event
from ..storage.repositories import StateRepository

KEY = "kill_switch"

#: Reasons the operator may clear from the dashboard. SAFETY-class trips
#: (environment mismatch, corrupted state, broker inconsistency) are not
#: in this set and require an explicit force clear.
CLEARABLE = ("DAILY_LOSS_LIMIT", "MAX_DRAWDOWN", "CONSECUTIVE_LOSSES", "MANUAL")


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool
    reason: str | None
    detail: str | None
    tripped_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "detail": self.detail,
            "trippedAt": self.tripped_at,
        }


class KillSwitch:
    def __init__(self, state: StateRepository) -> None:
        self._state = state

    def read(self) -> KillSwitchState:
        """Read the persisted state.

        If the store cannot be read the switch reports ACTIVE. A system
        that cannot tell whether it has been stopped must behave as if it
        has been — the alternative is trading through an emergency stop
        because the database blinked.
        """

        try:
            raw = self._state.get(KEY) or {}
        except Exception as exc:  # noqa: BLE001 - storage backend specific
            log_event(
                "SAFETY",
                f"kill switch state is unreadable ({exc}); failing closed",
                severity="critical",
            )
            return KillSwitchState(
                active=True,
                reason="STATE_UNREADABLE",
                detail=str(exc)[:300],
                tripped_at=None,
            )
        return KillSwitchState(
            active=bool(raw.get("active")),
            reason=raw.get("reason"),
            detail=raw.get("detail"),
            tripped_at=raw.get("trippedAt"),
        )

    @property
    def active(self) -> bool:
        return self.read().active

    def trip(self, reason: str, detail: str = "") -> KillSwitchState:
        """Idempotent: an already-tripped switch keeps its first reason,
        which is the one that actually stopped trading."""

        current = self.read()
        if current.active:
            return current
        payload = {
            "active": True,
            "reason": reason,
            "detail": detail,
            "trippedAt": utc_now().isoformat(),
        }
        self._state.set(KEY, payload)
        log_event(
            "SAFETY",
            f"KILL SWITCH TRIPPED: {reason}",
            severity="critical",
            reason=reason,
            detail=detail,
        )
        return self.read()

    def clear(self, *, force: bool = False, actor: str = "dashboard") -> KillSwitchState:
        current = self.read()
        if not current.active:
            return current
        if current.reason not in CLEARABLE and not force:
            log_event(
                "SAFETY",
                f"refused to clear kill switch: reason {current.reason} requires a forced clear",
                severity="error",
                reason=current.reason,
            )
            return current
        self._state.set(KEY, {"active": False, "reason": None, "detail": None, "trippedAt": None})
        log_event(
            "SAFETY",
            "kill switch cleared",
            severity="info",
            previous_reason=current.reason,
            forced=force,
            actor=actor,
        )
        return self.read()
