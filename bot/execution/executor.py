"""Order execution. The only place an order is ever created.

The sequence is fixed and each step is persisted before the next begins,
so a crash at ANY point leaves a recoverable record:

    verify DEMO -> persist intent (CREATED)
    -> fresh duplicate check -> spread check -> verify DEMO again
    -> mark SUBMITTED -> send -> mark ACKNOWLEDGED
    -> verify the position exists at the broker -> mark FILLED

Failure handling, in order of danger:

  * a rejection is PERMANENT: mark FAILED, no retry;
  * an ambiguous outcome (transport died after the request left) is
    SAFETY-CRITICAL: mark AMBIGUOUS, never retry, hand it to the
    reconciler which asks the broker what actually happened;
  * anything that happens BEFORE the request leaves is safe to abort.

If the database is unavailable, no order is created at all. A trade whose
intent cannot be persisted is a trade that cannot be recovered after a
restart, which is worse than a missed opportunity (MASTER_MISSION §56).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..broker.models import InstrumentSpec
from ..broker.symbols import same_instrument
from ..config import TradingConfig
from ..errors import (
    AmbiguousExecution,
    BotError,
    BrokerError,
    BrokerRejected,
    DemoVerificationError,
    StorageError,
)
from ..marketdata.validation import validate_spread
from ..observability import log_event, new_event_id
from ..safety.demo_guard import require_demo
from ..storage.repositories import Repositories
from .plan import TradePlan


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    plan: TradePlan
    status: str
    reason: str | None
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    fill_price: float | None = None
    slippage: float | None = None
    #: True when this refusal is about THIS symbol and nothing was sent,
    #: so the scan may safely consider its next-best candidate. False for
    #: anything account-wide (a failed demo check, an unreadable
    #: database) and for anything whose broker outcome is uncertain.
    symbol_specific: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "executionId": self.plan.execution_id,
            "symbol": self.plan.symbol,
            "direction": self.plan.direction,
            "quantity": self.plan.quantity,
            "brokerOrderId": self.broker_order_id,
            "brokerPositionId": self.broker_position_id,
            "fillPrice": self.fill_price,
            "slippage": round(self.slippage, 6) if self.slippage is not None else None,
            "symbolSpecific": self.symbol_specific,
        }


class Executor:
    def __init__(
        self,
        config: TradingConfig,
        broker: Any,
        repositories: Repositories,
        *,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config
        self.broker = broker
        self.repos = repositories
        self._sleep = sleeper

    # -- pre-trade checks ------------------------------------------------

    def _fresh_duplicate_check(self, plan: TradePlan) -> str | None:
        """A LIVE broker query, never a cached snapshot.

        Seconds pass between scanning and submitting (AI call, risk,
        sizing). A concurrent cycle or a manual trigger can open the same
        symbol in that window, and a cached snapshot would not see it.
        """

        # A broker that cannot be read cannot rule out a duplicate, and an
        # unverifiable duplicate is a refusal (project rule 7). Raising
        # instead was worse than it looked: the intent is already persisted
        # by this point, so the exception escaped leaving it CREATED, and
        # the NEXT scan then found that unresolved intent and refused the
        # symbol again. One rate-limited read blocked a pair until a
        # reconcile came round.
        try:
            positions = self.broker.positions()
        except BotError as exc:
            return (
                f"could not read open positions to rule out a duplicate: {exc}. "
                "No order is created when a duplicate cannot be excluded."
            )
        for position in positions:
            # same_instrument, not string equality: on a broker whose names
            # carry a suffix, `EURUSD` and `EURUSD.R` are one instrument, and
            # comparing them as strings is how a second position gets opened
            # on a pair that is already held.
            if same_instrument(position.symbol, plan.symbol):
                return (
                    f"a {position.direction} position on {plan.symbol} "
                    f"(id {position.position_id}) already exists at the broker"
                )
        unresolved = [
            intent
            for intent in self.repos.intents.unresolved()
            if same_instrument(str(intent["symbol"]), plan.symbol)
            and intent["idempotency_key"] != plan.execution_id
        ]
        if unresolved:
            return (
                f"an unresolved execution intent for {plan.symbol} exists "
                f"({unresolved[0]['idempotency_key']}, status {unresolved[0]['status']}) — "
                "it must be reconciled before another order is created"
            )
        return None

    def _spread_check(self, plan: TradePlan, spec: InstrumentSpec, atr: float) -> tuple[bool, str | None, float | None]:
        try:
            quote = self.broker.quote(spec)
        except BrokerError as exc:
            return False, f"could not read a live quote before submitting: {exc}", None
        ok, reason = validate_spread(
            spread=quote.spread,
            atr=atr,
            stop_distance=abs(plan.entry - plan.stop_loss),
            take_profit_distance=abs(plan.take_profit - plan.entry),
            max_spread_atr_fraction=self.config.execution.max_spread_atr_fraction,
            max_spread_tp_fraction=self.config.execution.max_spread_tp_fraction,
        )
        reference = quote.ask if plan.direction == "BUY" else quote.bid
        return ok, reason, reference

    # -- main --------------------------------------------------------------

    def execute(self, plan: TradePlan, spec: InstrumentSpec, *, atr: float) -> ExecutionResult:
        event_id = new_event_id()
        log = lambda message, **fields: log_event(  # noqa: E731 - local shorthand
            "ORDER", message, symbol=plan.symbol, event_id=event_id, **fields
        )

        # 1. Persistence must be healthy BEFORE anything else. No intent,
        #    no order.
        if not self.repos.db.ping():
            return ExecutionResult(
                False, plan, "ABORTED",
                "database is unavailable — refusing to create an order whose execution "
                "state could not survive a restart",
            )

        # 2. DEMO verification #3 (creation).
        try:
            require_demo(
                self.config,
                self.broker.account_metadata,
                stage="before_order_creation",
                claims=getattr(self.broker, "session_claims", None),
            )
        except DemoVerificationError as exc:
            return ExecutionResult(False, plan, "ABORTED", str(exc))

        # 3. Persist the intent. A UNIQUE violation here means this exact
        #    trade is already in flight or done — that is the duplicate
        #    guard working, not an error to route around.
        try:
            self.repos.intents.create(
                idempotency_key=plan.execution_id,
                symbol=plan.symbol,
                direction=plan.direction,
                plan=plan.as_dict(),
            )
        except StorageError as exc:
            # An intent already exists for this exact plan. That is the
            # duplicate guard — unless the previous attempt never reached
            # the broker, in which case the key had become a lifetime ban
            # on the setup rather than a guard against a second order.
            # `reopen` re-arms ONLY from a state that proves nothing was
            # sent, and refuses outright if any order or position id was
            # ever recorded.
            if not self.repos.intents.reopen(plan.execution_id, plan.as_dict()):
                return ExecutionResult(False, plan, "DUPLICATE", str(exc))
            self.repos.events.append(
                plan.execution_id, "INTENT_REARMED", {"previous": str(exc)[:300]}
            )

        self.repos.trades.create_pending(execution_id=plan.execution_id, plan=plan.as_dict())
        self.repos.events.append(plan.execution_id, "INTENT_CREATED", plan.as_dict())

        # 4. Fresh duplicate check against the live broker.
        duplicate = self._fresh_duplicate_check(plan)
        if duplicate:
            self.repos.intents.mark(plan.execution_id, "FAILED", failure_reason=duplicate)
            self.repos.events.append(plan.execution_id, "ABORTED_DUPLICATE", {"reason": duplicate})
            self.repos.trades.mark_aborted(execution_id=plan.execution_id, reason=duplicate)
            return ExecutionResult(False, plan, "ABORTED", duplicate, symbol_specific=True)

        # 5. Execution conditions.
        ok, reason, reference_price = self._spread_check(plan, spec, atr)
        if not ok:
            self.repos.intents.mark(plan.execution_id, "FAILED", failure_reason=reason)
            self.repos.events.append(plan.execution_id, "ABORTED_SPREAD", {"reason": reason})
            # No order left this process, so the row `create_pending`
            # wrote is not a trade. Leaving it PENDING made the setup
            # look "already traded" for the whole re-entry window, so a
            # spread that widened for one minute cost the setup a day.
            self.repos.trades.mark_aborted(execution_id=plan.execution_id, reason=reason or "spread")
            return ExecutionResult(False, plan, "ABORTED", reason, symbol_specific=True)

        # 6. DEMO verification #4 (submission) — immediately before the write.
        try:
            require_demo(
                self.config,
                self.broker.account_metadata,
                stage="before_order_submission",
                claims=getattr(self.broker, "session_claims", None),
            )
        except DemoVerificationError as exc:
            self.repos.intents.mark(plan.execution_id, "FAILED", failure_reason=str(exc))
            self.repos.trades.mark_aborted(execution_id=plan.execution_id, reason=str(exc))
            return ExecutionResult(False, plan, "ABORTED", str(exc))

        # 7. Submit.
        self.repos.intents.mark(plan.execution_id, "SUBMITTED")
        self.repos.events.append(
            plan.execution_id,
            "SUBMITTED",
            {"quantity": plan.quantity, "entry": plan.entry, "referencePrice": reference_price},
        )
        log("submitting market order", quantity=plan.quantity, direction=plan.direction)

        try:
            order = self.broker.place_market_order(
                spec,
                direction=plan.direction,
                quantity=plan.quantity,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
            )
        except AmbiguousExecution as exc:
            # The single most dangerous state in the system. Do NOT retry.
            self.repos.intents.mark(plan.execution_id, "AMBIGUOUS", failure_reason=str(exc))
            self.repos.events.append(plan.execution_id, "AMBIGUOUS", {"reason": str(exc)})
            self.repos.reconciliations.record(
                "AMBIGUOUS_SUBMISSION",
                {"executionId": plan.execution_id, "reason": str(exc)},
                symbol=plan.symbol,
            )
            log("order outcome UNKNOWN — handing to reconciler, no retry", severity="critical")
            return ExecutionResult(False, plan, "AMBIGUOUS", str(exc))
        except BrokerRejected as exc:
            self.repos.intents.mark(plan.execution_id, "FAILED", failure_reason=str(exc))
            self.repos.events.append(plan.execution_id, "REJECTED", {"reason": str(exc)})
            # An explicit rejection is a KNOWN outcome: no position was
            # created. The AmbiguousExecution paths above deliberately do
            # NOT do this — there the row stays PENDING because a
            # position may exist and rule 3 says the only recovery is to
            # ask the broker, never to try again.
            self.repos.trades.mark_aborted(execution_id=plan.execution_id, reason=str(exc))
            log(f"broker rejected the order: {exc}", severity="error")
            return ExecutionResult(False, plan, "REJECTED", str(exc))
        except BotError as exc:
            # Any other broker error after submission is also treated as
            # ambiguous: we cannot prove the order did not land.
            self.repos.intents.mark(plan.execution_id, "AMBIGUOUS", failure_reason=str(exc))
            self.repos.events.append(plan.execution_id, "AMBIGUOUS", {"reason": str(exc)})
            return ExecutionResult(False, plan, "AMBIGUOUS", str(exc))

        self.repos.intents.mark(
            plan.execution_id, "ACKNOWLEDGED", broker_order_id=order.order_id
        )
        self.repos.events.append(
            plan.execution_id, "ACKNOWLEDGED", {"orderId": order.order_id, "raw": order.raw}
        )

        # 8. Verify the position actually exists. An order id is not a fill.
        position = self._verify_position(plan)
        if position is None:
            self.repos.intents.mark(
                plan.execution_id,
                "AMBIGUOUS",
                failure_reason="order acknowledged but no position appeared within the verification window",
            )
            self.repos.reconciliations.record(
                "UNVERIFIED_FILL",
                {"executionId": plan.execution_id, "orderId": order.order_id},
                symbol=plan.symbol,
            )
            return ExecutionResult(
                False,
                plan,
                "UNVERIFIED",
                "order was acknowledged but no matching position could be verified at the broker",
                broker_order_id=order.order_id,
            )

        slippage = None
        if reference_price:
            slippage = (
                position.entry_price - reference_price
                if plan.direction == "BUY"
                else reference_price - position.entry_price
            )

        self.repos.intents.mark(
            plan.execution_id,
            "FILLED",
            broker_order_id=order.order_id,
            broker_position_id=position.position_id,
        )
        self.repos.trades.mark_open(
            plan.execution_id,
            broker_position_id=position.position_id,
            broker_order_id=order.order_id,
            actual_entry=position.entry_price,
            quantity=position.quantity,
            opened_at=position.opened_at.isoformat() if position.opened_at else None,
        )
        self.repos.daily.record_open()
        self.repos.events.append(
            plan.execution_id,
            "FILLED",
            {
                "positionId": position.position_id,
                "entryPrice": position.entry_price,
                "slippage": slippage,
            },
        )

        # 9. Protection verification. A position without a stop is an
        #    unbounded loss; if the broker did not attach one, attach it now.
        self._verify_protection(plan, position)

        log(
            "position opened and verified",
            position_id=position.position_id,
            entry=position.entry_price,
            slippage=slippage,
        )
        return ExecutionResult(
            True,
            plan,
            "FILLED",
            None,
            broker_order_id=order.order_id,
            broker_position_id=position.position_id,
            fill_price=position.entry_price,
            slippage=slippage,
        )

    def _verify_position(self, plan: TradePlan) -> Any:
        """Poll for the resulting position, with bounded attempts."""

        for attempt in range(self.config.execution.order_verify_attempts):
            try:
                positions = self.broker.positions()
            except BotError:
                positions = []
            for position in positions:
                if (
                    same_instrument(position.symbol, plan.symbol)
                    and position.direction == plan.direction
                ):
                    return position
            if attempt < self.config.execution.order_verify_attempts - 1:
                self._sleep(self.config.execution.order_verify_delay_seconds)
        return None

    def _protection_still_missing(self, position_id: str, missing: list[str]) -> list[str]:
        """Re-read the position and report which protections are still absent.

        A broker that cannot be read afterwards is treated as still
        missing: "we could not confirm the stop is there" and "the stop is
        not there" carry the same consequence, and only one of them is
        safe to assume.
        """

        try:
            positions = self.broker.positions()
        except BotError:
            return list(missing)
        current = next((p for p in positions if str(p.position_id) == str(position_id)), None)
        if current is None:
            # The position is gone: it filled, was closed, or never was.
            # Either way there is nothing left to protect, and claiming a
            # repair on a position that no longer exists would be worse.
            return []
        still: list[str] = []
        if "stopLoss" in missing and not current.stop_loss:
            still.append("stopLoss")
        if "takeProfit" in missing and not current.take_profit:
            still.append("takeProfit")
        return still

    def _verify_protection(self, plan: TradePlan, position: Any) -> None:
        """Confirm SL/TP are attached at the broker, and repair if not."""

        missing = []
        if not position.stop_loss:
            missing.append("stopLoss")
        if not position.take_profit:
            missing.append("takeProfit")
        if not missing:
            self.repos.trades.update_protection(
                position.position_id,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
            )
            return

        log_event(
            "ORDER",
            f"broker position {position.position_id} is missing {', '.join(missing)} — repairing",
            severity="warning",
            symbol=plan.symbol,
        )
        try:
            self.broker.modify_position(
                position.position_id,
                stop_loss=plan.stop_loss if "stopLoss" in missing else None,
                take_profit=plan.take_profit if "takeProfit" in missing else None,
            )
            # Read it back. A write that returned without raising is not a
            # write that took effect, and this is the one repair whose
            # silent failure leaves an unbounded loss open while the system
            # records that it is protected. Re-reading is also the ONLY
            # sanctioned recovery for a write whose outcome is in doubt
            # (project rule 3) — the modify is never retried here.
            still_missing = self._protection_still_missing(position.position_id, missing)
            if still_missing:
                raise BrokerError(
                    f"the repair returned successfully but {', '.join(still_missing)} "
                    f"is still absent at the broker"
                )
            self.repos.events.append(
                plan.execution_id, "PROTECTION_REPAIRED", {"missing": missing}
            )
            self.repos.trades.update_protection(
                position.position_id, stop_loss=plan.stop_loss, take_profit=plan.take_profit
            )
        except BotError as exc:
            # An unprotected position is an emergency: record it loudly so
            # the health endpoint and the dashboard both surface it.
            self.repos.reconciliations.record(
                "UNPROTECTED_POSITION",
                {
                    "positionId": position.position_id,
                    "missing": missing,
                    "error": str(exc),
                },
                symbol=plan.symbol,
            )
            log_event(
                "ORDER",
                f"FAILED to attach protection to {position.position_id}: {exc}",
                severity="critical",
                symbol=plan.symbol,
            )
