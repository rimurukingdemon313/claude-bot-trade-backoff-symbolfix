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
from ..config import R_EPSILON, TradingConfig
from ..errors import AmbiguousExecution, BotError
from ..observability import log_event
from ..smc.sessions import is_forex_weekend
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

    def track(
        self, positions: Sequence[Any], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Refresh excursions and return dashboard-ready position rows."""

        moment = ensure_utc(now) if now is not None else utc_now()
        rows: list[dict[str, Any]] = []
        for position in positions:
            trade = self.repos.trades.by_position_id(position.position_id)
            entry = position.entry_price
            stop = (trade or {}).get("stop_loss") or position.stop_loss
            price, price_status = self._current_price(position, moment)
            # An entry price the broker did not report is not an entry
            # price of zero. Every number below is measured FROM it, so a
            # zero here does not produce a slightly wrong R — it produces
            # an R of several thousand and an MFE equal to the whole
            # price of the instrument.
            measurable = entry is not None and entry > 0
            if not measurable:
                price_status = "broker reported no entry price for this position"
            # Rule 6, at the place it was still being broken. `0.0` is
            # not "unknown", it is "exactly break-even" — the single most
            # reassuring value this field can take, displayed at the
            # moment we can measure nothing. `_current_price` two
            # functions down refuses to invent a price for precisely this
            # reason; the R computed from it was inventing one anyway.
            current_r: float | None = None
            if measurable and stop and price:
                current_r = r_multiple(
                    direction=position.direction,
                    entry=entry,
                    stop=float(stop),
                    price=price,
                )
            if measurable and price:
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
                    "priceStatus": price_status,
                    "rMultiple": round(current_r, 3) if current_r is not None else None,
                    "riskAmount": (trade or {}).get("risk_amount"),
                    "setupGrade": (trade or {}).get("setup_grade"),
                    "executionId": (trade or {}).get("execution_id"),
                    "durationMinutes": self._duration_minutes(position, moment),
                    "orphaned": (trade or {}).get("status") == "ORPHANED",
                    # A position with no row of ours is one we did not open
                    # (or one from an earlier build). Without this it looks
                    # identical to a tracked position whose fields failed to
                    # load, and the operator cannot tell which.
                    "tracked": trade is not None,
                }
            )
        return rows

    def _current_price(self, position: Any, moment: datetime) -> tuple[float | None, str]:
        """Current price, and *why* when there isn't one.

        Rule 6 forbids inventing a price, so a missing one shows as a gap —
        but a gap with no explanation is its own problem. Over a weekend
        every open position reads "—" and looks broken, when the honest
        answer is that FX is shut. Distinguishing that from a broker that
        cannot be reached is the difference between "wait" and "act".
        """

        try:
            spec = self.broker.instrument(position.symbol)
            quote = self.broker.quote(spec)
            price = quote.bid if position.direction == "BUY" else quote.ask
        except BotError as exc:
            if is_forex_weekend(moment):
                return None, "market closed for the weekend"
            return None, f"no quote: {exc}"[:200]
        if price is None:
            if is_forex_weekend(moment):
                return None, "market closed for the weekend"
            return None, "broker returned no price"
        return price, "live"

    @staticmethod
    def _duration_minutes(position: Any, moment: datetime) -> float | None:
        if position.opened_at is None:
            return None
        return round((moment - ensure_utc(position.opened_at)).total_seconds() / 60.0, 1)

    #: A voluntary exit is deferred while the spread alone would cost
    #: this share of the position's own risk.
    #:
    #: A live structural exit fired while the spread was 10.5 pips on a
    #: 5.7-pip stop — 184% — and the market close filled 1.6 pips BEYOND
    #: the stop it was meant to improve on. $24.98 at the stop became
    #: $32.00, so a feature that exists to protect the account cost more
    #: than doing nothing at all.
    #:
    #: Deferring is safe precisely because the stop loss is still sitting
    #: at the broker. The worst case of waiting is the loss the trade was
    #: already sized for; the worst case of closing into a spread wider
    #: than the stop is a LARGER loss than the design permits. Half the
    #: risk in spread alone is absurd for an exit nobody is forcing.
    MAX_EXIT_SPREAD_FRACTION_OF_RISK = 0.5

    def _exit_is_affordable(self, action: ManagementAction) -> tuple[bool, str | None]:
        """Is the spread sane enough to close this position voluntarily?"""

        trade = self.repos.trades.by_position_id(action.position_id)
        if not trade:
            return True, None
        entry = trade.get("actual_entry") or trade.get("planned_entry")
        stop = trade.get("stop_loss")
        if not entry or not stop:
            return True, None
        risk_distance = abs(float(entry) - float(stop))
        if risk_distance <= 0:
            return True, None
        try:
            spec = self.broker.instrument(str(trade.get("symbol") or action.symbol))
            spread = self.broker.quote(spec).spread
        except BotError:
            # No quote, no judgement. The stop still protects the
            # position, so deferring is the conservative answer.
            return False, "could not read a quote to price this exit"
        if spread > risk_distance * self.MAX_EXIT_SPREAD_FRACTION_OF_RISK:
            return False, (
                f"spread {spread:.5f} is {spread / risk_distance:.0%} of this trade's "
                "risk — closing into it would cost more than the stop it replaces; "
                "waiting for a normal spread, the stop is still in place"
            )
        return True, None

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
                    # Persist BEFORE anything else can poll again. The
                    # guard in `plan_actions` reads this flag, and while
                    # it went unwritten the partial fired on every poll
                    # and halved the position each time.
                    self.repos.trades.mark_partial_taken(action.position_id)
                elif action.kind == "CLOSE":
                    # A voluntary exit, never a stop. The stop lives at
                    # the broker and is untouched by this.
                    affordable, why = self._exit_is_affordable(action)
                    if not affordable:
                        log_event(
                            "POSITION",
                            f"deferring {action.reason}: {why}",
                            symbol=action.symbol,
                            severity="warning",
                        )
                        applied.append({**action.as_dict(), "ok": False, "deferred": why})
                        continue
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
            except AmbiguousExecution as exc:
                # The write may have landed. Rule 3 says the only
                # recovery is to ask the broker — never to try again.
                #
                # Nothing here retried in code, and that hid the problem:
                # this method is called on EVERY poll, so a partial close
                # whose outcome was unknown simply came back thirty
                # seconds later and closed another slice. The guard that
                # prevents that is `mark_partial_taken`, and it sat AFTER
                # the broker call, so an ambiguous outcome skipped it.
                # That is the same descending stack of slices that cost a
                # live position — 0.17, 0.09, 0.04, 0.03, 0.01, 0.01 —
                # reached by a different route than the missing column.
                #
                # So the flag is written on the ambiguous path too. It is
                # the safe asymmetry: recording a partial that did not
                # happen costs one runner left open a little larger than
                # intended, and the reconciler corrects it from broker
                # state. NOT recording one that did happen slices the
                # position again, every poll, until nothing is left.
                if action.kind == "PARTIAL_CLOSE":
                    self.repos.trades.mark_partial_taken(action.position_id)
                trade = self.repos.trades.by_position_id(action.position_id)
                if trade:
                    self.repos.events.append(
                        str(trade["execution_id"]),
                        f"MANAGE_{action.kind}_AMBIGUOUS",
                        {**action.as_dict(), "reason": str(exc)},
                    )
                self.repos.reconciliations.record(
                    "AMBIGUOUS_MANAGEMENT",
                    {
                        "positionId": action.position_id,
                        "action": action.kind,
                        "reason": str(exc),
                    },
                    symbol=action.symbol,
                )
                log_event(
                    "POSITION",
                    f"{action.kind} on {action.position_id} outcome UNKNOWN — not repeated, "
                    f"handed to the reconciler: {exc}",
                    severity="critical",
                    symbol=action.symbol,
                )
                applied.append({**action.as_dict(), "ok": False, "ambiguous": str(exc)})
            except BotError as exc:
                log_event(
                    "POSITION",
                    f"management action {action.kind} failed on {action.position_id}: {exc}",
                    severity="error",
                    symbol=action.symbol,
                )
                applied.append({**action.as_dict(), "ok": False, "error": str(exc)})
        return applied
