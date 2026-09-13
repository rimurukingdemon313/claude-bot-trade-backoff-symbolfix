"""Position management: break-even, partials, trailing, structure exits.

Every feature here is OFF by default except break-even and the structural
time stop, because MASTER_MISSION §44 is explicit that management must be
earned by testing, not added because it sounds sophisticated. Each one is
a pure decision function (`plan_actions`) that returns what SHOULD happen,
which is what makes them testable without a broker; applying them is a
separate step.

The exit rules deliberately do NOT include "close because the last candle
looked scary" (§45). Exits are: the stop, the target, a structural
invalidation confirmed by a close, a risk emergency, or the broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..clock import ensure_utc, utc_now
from ..config import TradingConfig
from ..errors import BotError
from ..observability import log_event
from ..storage.repositories import Repositories


@dataclass(frozen=True, slots=True)
class ManagementAction:
    kind: str  # MOVE_STOP | PARTIAL_CLOSE | CLOSE
    position_id: str
    symbol: str
    reason: str
    stop_loss: float | None = None
    quantity: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "positionId": self.position_id,
            "symbol": self.symbol,
            "reason": self.reason,
            "stopLoss": self.stop_loss,
            "quantity": self.quantity,
        }


def _stop_is_behind_market(direction: str, stop: float, price: float, buffer: float) -> bool:
    """Is this stop still on the protective side of the current price?

    A broker rejects a stop that is already through the market, and a
    simulator that accepts one closes the position instantly at what looks
    like break-even. Either way the move is wrong: if price has retraced
    back to entry after touching 1R, there is no valid break-even stop to
    place and the correct action is to leave the original stop alone.
    """

    if direction == "BUY":
        return stop < price - buffer
    return stop > price + buffer


#: R comparisons use a small tolerance. Price arithmetic in floating point
#: makes an exact 1.0R land at 0.9999999999999556, which would silently
#: skip the break-even move at precisely the level it is meant to fire.
R_EPSILON = 1e-6


def r_multiple(*, direction: str, entry: float, stop: float, price: float) -> float:
    """How many R the position is currently up (or down)."""

    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    move = price - entry if direction == "BUY" else entry - price
    return move / risk


def plan_actions(
    *,
    position: Any,
    trade: dict[str, Any] | None,
    price: float,
    config: TradingConfig,
    structure_invalidated: bool = False,
    now: datetime | None = None,
) -> list[ManagementAction]:
    """Decide what to do with one open position. Pure function."""

    moment = now or utc_now()
    actions: list[ManagementAction] = []
    execution = config.execution

    entry = float(trade.get("actual_entry") or trade.get("planned_entry") or position.entry_price) if trade else position.entry_price
    planned_stop = float(trade["stop_loss"]) if trade and trade.get("stop_loss") else position.stop_loss
    if not entry or not planned_stop:
        return actions

    current_r = r_multiple(
        direction=position.direction, entry=entry, stop=float(planned_stop), price=price
    )

    # --- break-even ---
    if execution.enable_breakeven and current_r >= execution.breakeven_at_r - R_EPSILON:
        risk = abs(entry - float(planned_stop))
        # A touch beyond entry so the position covers its own spread
        # rather than scratching at exactly zero.
        buffer = risk * 0.05
        target_stop = entry + buffer if position.direction == "BUY" else entry - buffer
        already_safe = (
            position.stop_loss is not None
            and (
                (position.direction == "BUY" and position.stop_loss >= entry)
                or (position.direction == "SELL" and position.stop_loss <= entry)
            )
        )
        # Only if the break-even level is still behind the market. Price can
        # retrace to entry between polls, and a stop placed through the
        # market is rejected live and fills instantly in simulation.
        placeable = _stop_is_behind_market(
            position.direction, target_stop, price, risk * 0.02
        )
        if not already_safe and placeable:
            actions.append(
                ManagementAction(
                    "MOVE_STOP",
                    position.position_id,
                    position.symbol,
                    f"reached {current_r:.2f}R — moving stop to break-even",
                    stop_loss=target_stop,
                )
            )

    # --- partial take profit ---
    if (
        execution.enable_partial_tp
        and current_r >= execution.partial_tp_at_r - R_EPSILON
        and not (trade or {}).get("partial_taken")
    ):
        actions.append(
            ManagementAction(
                "PARTIAL_CLOSE",
                position.position_id,
                position.symbol,
                f"reached {current_r:.2f}R — taking {execution.partial_tp_fraction:.0%} off",
                quantity=round(position.quantity * execution.partial_tp_fraction, 2),
            )
        )

    # --- trailing ---
    if execution.enable_trailing and current_r >= execution.trail_after_r - R_EPSILON:
        risk = abs(entry - float(planned_stop))
        locked = current_r - 1.0
        trail_stop = (
            entry + risk * locked if position.direction == "BUY" else entry - risk * locked
        )
        improves = position.stop_loss is None or (
            (position.direction == "BUY" and trail_stop > position.stop_loss)
            or (position.direction == "SELL" and trail_stop < position.stop_loss)
        )
        if improves and _stop_is_behind_market(
            position.direction, trail_stop, price, risk * 0.02
        ):
            actions.append(
                ManagementAction(
                    "MOVE_STOP",
                    position.position_id,
                    position.symbol,
                    f"trailing at {current_r:.2f}R, locking {locked:.2f}R",
                    stop_loss=trail_stop,
                )
            )

    # --- structural invalidation ---
    if execution.enable_structure_exit and structure_invalidated and current_r < 1.0:
        actions.append(
            ManagementAction(
                "CLOSE",
                position.position_id,
                position.symbol,
                "structure invalidated by a confirmed close against the setup before 1R",
            )
        )

    # --- time stop ---
    opened = position.opened_at
    if opened is not None:
        age_hours = (moment - ensure_utc(opened)).total_seconds() / 3600.0
        if age_hours >= execution.max_position_hours and current_r < 0.5:
            actions.append(
                ManagementAction(
                    "CLOSE",
                    position.position_id,
                    position.symbol,
                    f"open {age_hours:.0f}h without reaching 0.5R — the thesis has expired",
                )
            )
    return actions


class PositionManager:
    def __init__(self, config: TradingConfig, broker: Any, repositories: Repositories) -> None:
        self.config = config
        self.broker = broker
        self.repos = repositories

    def track(self, positions: Sequence[Any]) -> list[dict[str, Any]]:
        """Refresh excursions and return dashboard-ready position rows."""

        rows: list[dict[str, Any]] = []
        for position in positions:
            trade = self.repos.trades.by_position_id(position.position_id)
            entry = position.entry_price
            stop = (trade or {}).get("stop_loss") or position.stop_loss
            price = self._current_price(position)
            current_r = (
                r_multiple(direction=position.direction, entry=entry, stop=float(stop), price=price)
                if stop and price
                else 0.0
            )
            if price:
                excursion = (
                    price - entry if position.direction == "BUY" else entry - price
                )
                self.repos.trades.update_excursions(
                    position.position_id, mfe=max(0.0, excursion), mae=min(0.0, excursion)
                )
            rows.append(
                {
                    **position.as_dict(),
                    "currentPrice": price,
                    "rMultiple": round(current_r, 3),
                    "riskAmount": (trade or {}).get("risk_amount"),
                    "setupGrade": (trade or {}).get("setup_grade"),
                    "executionId": (trade or {}).get("execution_id"),
                    "durationMinutes": self._duration_minutes(position),
                    "orphaned": (trade or {}).get("status") == "ORPHANED",
                }
            )
        return rows

    def _current_price(self, position: Any) -> float | None:
        try:
            spec = self.broker.instrument(position.symbol)
            quote = self.broker.quote(spec)
            return quote.bid if position.direction == "BUY" else quote.ask
        except BotError:
            return None

    @staticmethod
    def _duration_minutes(position: Any) -> float | None:
        if position.opened_at is None:
            return None
        return round((utc_now() - ensure_utc(position.opened_at)).total_seconds() / 60.0, 1)

    def apply(self, actions: Sequence[ManagementAction]) -> list[dict[str, Any]]:
        """Execute management actions. Each failure is isolated."""

        applied: list[dict[str, Any]] = []
        for action in actions:
            try:
                if action.kind == "MOVE_STOP" and action.stop_loss is not None:
                    self.broker.modify_position(action.position_id, stop_loss=action.stop_loss)
                    self.repos.trades.update_protection(
                        action.position_id, stop_loss=action.stop_loss, take_profit=None
                    )
                elif action.kind == "PARTIAL_CLOSE" and action.quantity:
                    self.broker.close_position(action.position_id, quantity=action.quantity)
                elif action.kind == "CLOSE":
                    self.broker.close_position(action.position_id)
                else:
                    continue
                trade = self.repos.trades.by_position_id(action.position_id)
                if trade:
                    self.repos.events.append(
                        str(trade["execution_id"]), f"MANAGE_{action.kind}", action.as_dict()
                    )
                log_event("POSITION", action.reason, symbol=action.symbol, action=action.kind)
                applied.append({**action.as_dict(), "ok": True})
            except BotError as exc:
                log_event(
                    "POSITION",
                    f"management action {action.kind} failed on {action.position_id}: {exc}",
                    severity="error",
                    symbol=action.symbol,
                )
                applied.append({**action.as_dict(), "ok": False, "error": str(exc)})
        return applied
