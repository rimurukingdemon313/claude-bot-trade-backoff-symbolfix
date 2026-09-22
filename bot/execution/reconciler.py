"""Broker/database reconciliation.

TradeLocker is authoritative for what positions exist (MASTER_MISSION
§6). The database is authoritative for WHY they exist — the plan, the
risk decision, the score. Reconciliation makes the two agree without
either silently overwriting the other.

Runs at startup (before any new trade is permitted) and periodically
thereafter (§85), because webhooks do not exist here and a position can
close between scans.

Four disagreements are handled:

  1. an unresolved intent whose outcome is unknown -> ask the broker;
  2. a broker position the database does not know -> adopt as ORPHANED;
  3. a database OPEN trade the broker does not have -> close it out
     using order history for the realized result;
  4. a broker position with no stop loss -> emergency protection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..broker.symbols import same_instrument
from ..clock import utc_now
from ..config import TradingConfig
from ..errors import BotError
from ..observability import log_event
from ..storage.repositories import Repositories


@dataclass
class ReconcileReport:
    checked_positions: int = 0
    adopted_orphans: list[str] = field(default_factory=list)
    #: Positions the broker has closed whose result could not be read.
    #: Kept apart from `closed_stale` because they are the ones whose P/L
    #: never reached the daily counters.
    unmeasured_closes: list[str] = field(default_factory=list)
    closed_stale: list[str] = field(default_factory=list)
    resolved_intents: list[str] = field(default_factory=list)
    unresolved_intents: list[str] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    at: str = ""

    @property
    def clean(self) -> bool:
        return not self.unresolved_intents and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkedPositions": self.checked_positions,
            "adoptedOrphans": self.adopted_orphans,
            "unmeasuredCloses": self.unmeasured_closes,
            "closedStale": self.closed_stale,
            "resolvedIntents": self.resolved_intents,
            "unresolvedIntents": self.unresolved_intents,
            "unprotected": self.unprotected,
            "errors": self.errors,
            "clean": self.clean,
            "at": self.at,
        }


class Reconciler:
    def __init__(self, config: TradingConfig, broker: Any, repositories: Repositories) -> None:
        self.config = config
        self.broker = broker
        self.repos = repositories

    def reconcile(self, *, repair_protection: bool = True) -> ReconcileReport:
        report = ReconcileReport(at=utc_now().isoformat())

        try:
            positions = self.broker.positions()
        except BotError as exc:
            report.errors.append(f"could not read broker positions: {exc}")
            return report
        report.checked_positions = len(positions)
        by_id = {position.position_id: position for position in positions}

        self._resolve_intents(positions, report)
        self._adopt_orphans(positions, report)
        self._close_stale(by_id, report)
        if repair_protection:
            self._repair_protection(positions, report)

        self.repos.reconciliations.record("PERIODIC" if report.clean else "DISCREPANCY", report.as_dict())
        return report

    # -- 1. unresolved intents -------------------------------------------

    def _resolve_intents(self, positions: list[Any], report: ReconcileReport) -> None:
        """Settle every in-flight intent against what the broker actually has.

        This is what makes an ambiguous submission safe: instead of
        retrying blind, we ask the broker whether the order landed.
        """

        for intent in self.repos.intents.unresolved():
            key = str(intent["idempotency_key"])
            symbol = str(intent["symbol"]).upper()
            direction = str(intent["direction"]).upper()

            match = next(
                (
                    position
                    for position in positions
                    if same_instrument(position.symbol, symbol)
                    and position.direction == direction
                ),
                None,
            )
            if match is not None:
                self.repos.intents.mark(
                    key, "FILLED", broker_position_id=match.position_id
                )
                self.repos.trades.mark_open(
                    key,
                    broker_position_id=match.position_id,
                    broker_order_id=intent.get("broker_order_id"),
                    actual_entry=match.entry_price,
                    quantity=match.quantity,
                    opened_at=match.opened_at.isoformat() if match.opened_at else None,
                )
                self.repos.events.append(
                    key, "RESOLVED_FILLED", {"positionId": match.position_id}
                )
                report.resolved_intents.append(key)
                log_event(
                    "RECONCILE",
                    f"ambiguous intent {key} resolved: the order DID fill",
                    symbol=symbol,
                )
                continue

            # No position. Check order history before declaring nothing
            # happened — it may have filled and already closed.
            landed = self._order_history_match(key, intent, symbol)
            if landed:
                self.repos.intents.mark(key, "FILLED", broker_order_id=landed)
                self.repos.events.append(key, "RESOLVED_HISTORICAL", {"orderId": landed})
                report.resolved_intents.append(key)
                continue

            if str(intent["status"]) in ("CREATED",):
                # It never left this process.
                self.repos.intents.mark(key, "ABANDONED", failure_reason="never submitted")
                self.repos.events.append(key, "ABANDONED", {})
                report.resolved_intents.append(key)
                continue

            self.repos.intents.mark(
                key, "ABANDONED", failure_reason="no matching position or order found at the broker"
            )
            self.repos.events.append(key, "RESOLVED_NO_FILL", {})
            report.resolved_intents.append(key)
            log_event(
                "RECONCILE",
                f"ambiguous intent {key} resolved: nothing reached the broker",
                symbol=symbol,
            )

    def _order_history_match(self, key: str, intent: dict[str, Any], symbol: str) -> str | None:
        recorded_order = intent.get("broker_order_id")
        try:
            history = self.broker.order_history(limit=100)
        except BotError:
            return None
        for order in history:
            if recorded_order and order.order_id == str(recorded_order):
                return order.order_id
        return None

    # -- 2. orphaned broker positions ------------------------------------

    def _adopt_orphans(self, positions: list[Any], report: ReconcileReport) -> None:
        for position in positions:
            known = self.repos.trades.by_position_id(position.position_id)
            if known is not None:
                continue
            execution_id = self.repos.trades.adopt_orphan(
                broker_position_id=position.position_id,
                snapshot={
                    "symbol": position.symbol,
                    "direction": position.direction,
                    "entry": position.entry_price,
                    "stop_loss": position.stop_loss,
                    "take_profit": position.take_profit,
                    "quantity": position.quantity,
                    "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                },
            )
            report.adopted_orphans.append(position.position_id)
            self.repos.events.append(
                execution_id, "ADOPTED_ORPHAN", position.as_dict()
            )
            log_event(
                "RECONCILE",
                f"adopted unknown broker position {position.position_id} on {position.symbol}",
                severity="warning",
                symbol=position.symbol,
            )

    # -- 3. positions the broker no longer has ---------------------------

    def _close_stale(self, by_id: dict[str, Any], report: ReconcileReport) -> None:
        for trade in self.repos.trades.open_trades():
            position_id = trade.get("broker_position_id")
            if not position_id:
                continue
            if str(position_id) in by_id:
                continue
            realized, exit_price = self._realized_from_history(str(position_id))
            measured = realized is not None
            closed = self.repos.trades.mark_closed(
                broker_position_id=str(position_id),
                exit_price=exit_price,
                realized_pnl=realized,
                exit_reason="BROKER_CLOSED" if measured else "BROKER_CLOSED_PNL_UNKNOWN",
            )
            if closed is not None and measured:
                self.repos.daily.record_close(float(realized))
            report.closed_stale.append(str(position_id))

            if measured:
                log_event(
                    "RECONCILE",
                    f"position {position_id} is closed at the broker; local state updated "
                    f"(realized {float(realized):.2f})",
                    symbol=str(trade.get("symbol")),
                )
                continue

            # The daily counters are safety controls, so an unmeasured
            # close is left OUT of them rather than entered as a zero.
            # That keeps the streak intact - a close nobody could price
            # must not reset a losing run - and leaves a visible gap.
            report.unmeasured_closes.append(str(position_id))
            self.repos.reconciliations.record(
                "CLOSE_WITHOUT_RESULT",
                {"positionId": str(position_id), "symbol": str(trade.get("symbol"))},
                symbol=str(trade.get("symbol")),
            )
            log_event(
                "RECONCILE",
                f"position {position_id} is closed at the broker but its result could not "
                "be read; daily P/L and the loss streak are UNCHANGED rather than "
                "credited with a zero — the trade is recorded with an unknown result",
                severity="critical",
                symbol=str(trade.get("symbol")),
            )

    def _realized_from_history(self, position_id: str) -> tuple[float | None, float | None]:
        """Derive the realized result from the broker's own order history.

        Returns (pnl, exit_price), and `pnl` is None when the history
        cannot be read or carries no result for this position.

        It used to return 0.0 there, under a docstring saying that a
        fabricated PnL would corrupt the daily loss counter — which is
        exactly what a 0.0 did, because the caller fed it straight into
        that counter. Three things went wrong at once, and all of them
        the wrong way:

        * the real loss vanished from `realized_pnl`, so the daily loss
          limit under-counted and its kill switch tripped late;
        * `consecutive_losses` RESET, because 0.0 is not a loss — so a
          losing streak that should have been reducing risk was cleared
          by a number nobody measured, which is rule 2's "risk may never
          be increased by adverse account state" arriving through the
          back door;
        * the close counted as neither a win nor a loss while still
          incrementing `trades_closed`, quietly diluting the win rate.

        None says "this closed and I do not know for how much", which is
        a gap an operator can see (project rule 6).
        """

        # Closed POSITIONS first — that is where a realized result lives.
        #
        # This used to consult order history alone, matching on
        # `positionId` and `realizedPl`. An ordersHistory row on this
        # backend is an ORDER and carries neither, so the match could
        # never succeed and every closed trade was recorded
        # BROKER_CLOSED_PNL_UNKNOWN — while the broker's own app showed
        # the figures plainly under its "Closed Positions" tab. The
        # honest-gap machinery worked perfectly; it was reporting a gap
        # that only existed because the wrong endpoint was being asked.
        reader = getattr(self.broker, "closed_position_result", None)
        if callable(reader):
            try:
                realized, exit_price = reader(position_id)
            except BotError:
                realized, exit_price = None, None
            if realized is not None:
                return realized, exit_price

        try:
            history = self.broker.order_history(limit=200)
        except BotError:
            return None, None
        pnl = 0.0
        exit_price = None
        found = False
        for order in history:
            if order.position_id != position_id:
                continue
            raw = order.raw or {}
            value = raw.get("realizedPl", raw.get("realizedPnL"))
            if value not in (None, ""):
                try:
                    candidate = float(value)
                except (TypeError, ValueError):
                    continue
                if candidate != 0.0 or not found:
                    pnl = candidate
                    exit_price = order.price
                    found = True
        return (pnl if found else None), exit_price

    # -- 4. unprotected positions ----------------------------------------

    def _repair_protection(self, positions: list[Any], report: ReconcileReport) -> None:
        for position in positions:
            if position.stop_loss:
                continue
            report.unprotected.append(position.position_id)
            trade = self.repos.trades.by_position_id(position.position_id)
            planned_stop = trade.get("stop_loss") if trade else None
            if not planned_stop:
                log_event(
                    "RECONCILE",
                    f"position {position.position_id} has NO stop loss and no recorded plan "
                    "to restore one — flagged for operator attention",
                    severity="critical",
                    symbol=position.symbol,
                )
                continue
            try:
                self.broker.modify_position(position.position_id, stop_loss=float(planned_stop))
                log_event(
                    "RECONCILE",
                    f"restored missing stop loss on {position.position_id}",
                    severity="warning",
                    symbol=position.symbol,
                )
            except BotError as exc:
                report.errors.append(
                    f"could not restore stop loss on {position.position_id}: {exc}"
                )
