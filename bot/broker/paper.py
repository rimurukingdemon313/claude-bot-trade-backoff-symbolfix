"""Paper trading broker: LIVE prices, SIMULATED fills.

This wraps a real broker and splits its surface in two:

  * **reads pass through** — instruments, quotes, candles and the real
    account balance all come from the live TradeLocker DEMO connection, so
    the strategy analyses exactly the prices it would trade;
  * **writes never leave this process** — orders, modifications and closes
    are simulated against those live prices and persisted to the database.

Everything upstream of the fill is untouched: market data validation, the
SMC engine, scoring, the risk engine, the execution intent and its
idempotency guard, position management, reconciliation, the journal. That is
the point. Paper mode is not a separate code path with its own bugs; it is
the real path with the last inch replaced, which is what makes it evidence
about the live path.

Fill modelling is deliberately pessimistic:

  * an entry crosses the spread (buy at ask, sell at bid) AND pays extra
    slippage on top;
  * a stop fills WORSE than the stop price;
  * a target fills exactly at the target, never better;
  * when one candle touched both the stop and the target, the STOP is
    assumed, because intrabar sequence is unknowable and optimism here is
    how a paper run comes to bear no resemblance to a live one;
  * commission is charged per lot.

An optimistic simulator would be worse than no simulator: it would
manufacture confidence in an execution path that has not been tested.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..clock import utc_now
from ..config import TradingConfig
from ..errors import BrokerError, BrokerRejected
from ..observability import log_event
from ..storage.repositories import Repositories
from .models import AccountState, BrokerOrder, BrokerPosition, InstrumentSpec, OrderResult, Quote


class PaperBroker:
    """A broker-shaped object whose writes are simulated.

    Deliberately NOT a subclass of TradeLockerBroker: inheriting would make
    it possible for an unoverridden method to reach the network. Composition
    means the only calls that touch the broker are the ones written here.
    """

    def __init__(
        self,
        live: Any,
        config: TradingConfig,
        repositories: Repositories,
    ) -> None:
        self.live = live
        self.config = config
        self.repos = repositories
        self.paper = repositories.paper
        self._lock = threading.RLock()
        self._initialised = False
        #: Every simulated write, for assertions and for the audit trail.
        self.simulated_writes: list[dict[str, Any]] = []

    # -- identity --------------------------------------------------------

    @property
    def mode(self) -> str:
        return "paper"

    @property
    def account_metadata(self) -> dict[str, Any] | None:
        """The REAL account's metadata.

        Paper mode does not weaken the DEMO guard: verification still runs
        against the live account, so paper-over-a-live-account is refused
        exactly as live-trading-a-live-account would be.
        """

        return self.live.account_metadata

    def ensure_session(self) -> None:
        self.live.ensure_session()
        self._ensure_account()

    def _ensure_account(self) -> dict[str, Any]:
        with self._lock:
            existing = self.paper.account()
            if existing is not None:
                self._initialised = True
                return existing

            configured = self.config.paper.starting_balance
            currency = "USD"
            if configured is None:
                # Adopt the real balance so paper results are scaled to the
                # account actually being validated.
                try:
                    live_account = self.live.account_state()
                    configured = live_account.balance
                    currency = live_account.currency
                except BrokerError as exc:
                    raise BrokerError(
                        "paper mode could not read the real account balance to size itself, "
                        f"and PAPER_STARTING_BALANCE is not set: {exc}"
                    ) from exc
            record = self.paper.ensure_account(float(configured), currency)
            self._initialised = True
            return record

    # -- reads (pass through) --------------------------------------------

    def instrument(self, symbol: str, *, force: bool = False) -> InstrumentSpec:
        return self.live.instrument(symbol, force=force)

    def available_symbols(self) -> list[str]:
        return self.live.available_symbols()

    def quote(self, spec: InstrumentSpec) -> Quote:
        return self.live.quote(spec)

    def candles(self, spec: InstrumentSpec, timeframe: str, *, count: int = 300) -> list[dict[str, Any]]:
        return self.live.candles(spec, timeframe, count=count)

    def order_history(self, limit: int = 200) -> list[BrokerOrder]:
        """Simulated fills, shaped like broker order history.

        The reconciler reads this to settle an ambiguous intent, so it has
        to answer in the same shape the live broker would.
        """

        rows = self.paper.closed_positions(limit=limit)
        return [
            BrokerOrder(
                order_id=f"paper-{row['position_id']}",
                position_id=str(row["position_id"]),
                symbol=str(row["symbol"]),
                instrument_id=0,
                direction="SELL" if row["direction"] == "BUY" else "BUY",
                quantity=float(row["quantity"] or 0.0),
                status="FILLED",
                price=float(row["exit_price"]) if row["exit_price"] is not None else None,
                stop_price=None,
                order_type="market",
                created_at=None,
                raw={"realizedPl": row.get("realized_pnl"), "paper": True},
            )
            for row in rows
        ]

    def orders(self) -> list[BrokerOrder]:
        # Simulated fills are immediate; nothing ever rests as a working order.
        return []

    # -- account ---------------------------------------------------------

    def account_state(self) -> AccountState:
        """Simulated equity, marked to LIVE prices."""

        record = self._ensure_account()
        starting = float(record["starting_balance"])
        realized = float(record["realized_pnl"] or 0.0)
        commission = float(record["commission_paid"] or 0.0)
        balance = starting + realized - commission

        positions = self.positions()
        open_pnl = sum(position.unrealized_pnl for position in positions)
        margin_used = sum(
            abs(position.quantity) * position.entry_price * 0.01 for position in positions
        )
        return AccountState(
            balance=round(balance, 2),
            equity=round(balance + open_pnl, 2),
            margin_used=round(margin_used, 2),
            margin_available=round(max(0.0, balance + open_pnl - margin_used), 2),
            open_pnl=round(open_pnl, 2),
            today_pnl=round(self._today_realized(), 2),
            currency=str(record["currency"]),
            raw={"paper": True, "startingBalance": starting, "commissionPaid": commission},
        )

    def _today_realized(self) -> float:
        from ..clock import trading_day

        today = trading_day(utc_now())
        return sum(
            float(row["realized_pnl"] or 0.0)
            for row in self.paper.closed_positions(limit=500)
            if str(row.get("closed_at") or "").startswith(today)
        )

    # -- positions -------------------------------------------------------

    def positions(self) -> list[BrokerPosition]:
        """Open simulated positions, marked to live prices.

        Reading positions is also when stops and targets are evaluated —
        the management loop polls this, so protection is checked at the same
        cadence a broker's own engine would be consulted.
        """

        self._ensure_account()
        rows = self.paper.open_positions()
        if not rows:
            return []

        result: list[BrokerPosition] = []
        for row in rows:
            closed = self._settle_protection(row)
            if closed:
                continue
            refreshed = self.paper.by_id(str(row["position_id"])) or row
            mark = float(refreshed.get("mark_price") or refreshed["entry_price"])
            result.append(self._to_position(refreshed, mark))
        return result

    def _to_position(self, row: Mapping[str, Any], mark: float) -> BrokerPosition:
        direction = str(row["direction"])
        entry = float(row["entry_price"])
        quantity = float(row["quantity"])
        move = (mark - entry) if direction == "BUY" else (entry - mark)
        pnl = move * float(row["contract_size"]) * float(row.get("conversion_rate") or 1.0) * quantity
        opened = row.get("opened_at")
        return BrokerPosition(
            position_id=str(row["position_id"]),
            symbol=str(row["symbol"]),
            instrument_id=0,
            direction=direction,
            quantity=quantity,
            entry_price=entry,
            stop_loss=float(row["stop_loss"]) if row["stop_loss"] is not None else None,
            take_profit=float(row["take_profit"]) if row["take_profit"] is not None else None,
            unrealized_pnl=round(pnl, 2),
            opened_at=_parse(opened),
            raw={"paper": True, "markPrice": mark},
        )

    # -- protection settlement -------------------------------------------

    def _settle_protection(self, row: Mapping[str, Any]) -> bool:
        """Close the position if its stop or target has been reached.

        Two evidence sources, deliberately:

        1. the current quote — catches a level reached right now;
        2. the high/low of the closed candles since the position opened —
           catches a level reached BETWEEN polls, which a quote-only check
           would miss entirely on a 30-second loop against a 15-minute
           candle.
        """

        symbol = str(row["symbol"])
        direction = str(row["direction"])
        stop = row["stop_loss"]
        target = row["take_profit"]

        try:
            spec = self.live.instrument(symbol)
            quote = self.live.quote(spec)
        except BrokerError as exc:
            # Cannot mark or settle without a price. Leave the position open
            # and say so: silently treating an unreadable price as "no
            # trigger" is how a stop gets missed.
            log_event(
                "PAPER",
                f"cannot price {symbol} to settle paper protection: {exc}",
                severity="warning",
                symbol=symbol,
            )
            return False

        mark = quote.bid if direction == "BUY" else quote.ask
        self.paper.update_mark(str(row["position_id"]), mark)

        high, low = self._range_since(spec, row)
        hit_stop = hit_target = False

        if stop is not None:
            stop = float(stop)
            hit_stop = (mark <= stop or low <= stop) if direction == "BUY" else (mark >= stop or high >= stop)
        if target is not None:
            target = float(target)
            hit_target = (mark >= target or high >= target) if direction == "BUY" else (mark <= target or low <= target)

        if not hit_stop and not hit_target:
            return False

        tick = spec.tick_size or 0.00001
        if hit_stop and (hit_target and self.config.paper.pessimistic_intrabar or not hit_target):
            slip = self.config.paper.stop_slippage_ticks * tick
            exit_price = (stop - slip) if direction == "BUY" else (stop + slip)
            reason = "STOP_AND_TARGET_SAME_WINDOW" if hit_target else "STOP"
        else:
            exit_price = float(target)
            reason = "TARGET"

        self._close(row, exit_price=exit_price, reason=reason, spec=spec)
        return True

    def _range_since(self, spec: InstrumentSpec, row: Mapping[str, Any]) -> tuple[float, float]:
        """High/low of closed M15 candles since the position opened."""

        opened = _parse(row.get("opened_at"))
        if opened is None:
            return (float("-inf"), float("inf"))
        try:
            candles = self.live.candles(spec, "M15", count=32)
        except BrokerError:
            return (float("-inf"), float("inf"))

        highs: list[float] = []
        lows: list[float] = []
        for candle in candles:
            stamp = _parse(candle.get("timestamp"))
            if stamp is None or stamp < opened:
                continue
            highs.append(float(candle["high"]))
            lows.append(float(candle["low"]))
        if not highs:
            return (float("-inf"), float("inf"))
        return (max(highs), min(lows))

    def _close(
        self,
        row: Mapping[str, Any],
        *,
        exit_price: float,
        reason: str,
        spec: InstrumentSpec | None = None,
        quantity: float | None = None,
    ) -> float:
        direction = str(row["direction"])
        entry = float(row["entry_price"])
        size = float(quantity if quantity is not None else row["quantity"])
        contract = float(row["contract_size"])
        conversion = float(row.get("conversion_rate") or 1.0)
        move = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
        gross = move * contract * conversion * size
        commission = self.config.paper.commission_per_lot * size * 0.5  # exit half of the round turn
        net = gross - commission

        remaining = float(row["quantity"]) - size
        position_id = str(row["position_id"])

        if remaining > 1e-9:
            self.paper.reduce_quantity(position_id, round(remaining, 4))
        else:
            self.paper.close_position(
                position_id, exit_price=exit_price, realized_pnl=round(net, 2), exit_reason=reason
            )
        self.paper.apply_realized(round(gross, 2), round(commission, 2))

        log_event(
            "PAPER",
            f"simulated {reason} on {row['symbol']}: {net:+.2f} at {exit_price}",
            symbol=str(row["symbol"]),
            position_id=position_id,
            exit_price=exit_price,
            realized=round(net, 2),
            reason=reason,
        )
        self.simulated_writes.append(
            {"kind": "CLOSE", "positionId": position_id, "exitPrice": exit_price, "reason": reason}
        )
        return net

    # -- writes (simulated) ----------------------------------------------

    def place_market_order(
        self,
        spec: InstrumentSpec,
        *,
        direction: str,
        quantity: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        """Simulate a market fill at the live price, plus adverse slippage.

        The same validation the live path enforces applies here: a
        non-positive quantity, or a missing stop or target, is refused. Paper
        mode must not accept an order the live broker would reject, or it
        would hide a bug instead of finding one.
        """

        if quantity <= 0:
            raise BrokerRejected(f"refusing to simulate a non-positive quantity ({quantity})")
        if not stop_loss or not take_profit or stop_loss <= 0 or take_profit <= 0:
            raise BrokerRejected("refusing to simulate an order without a positive stop and target")
        if direction.upper() not in ("BUY", "SELL"):
            raise BrokerRejected(f"unknown order direction {direction!r}")

        self._ensure_account()
        quote = self.live.quote(spec)
        tick = spec.tick_size or 0.00001
        slip = self.config.paper.entry_slippage_ticks * tick
        # Cross the spread, then pay slippage on top — both against us.
        fill = (quote.ask + slip) if direction.upper() == "BUY" else (quote.bid - slip)
        fill = spec.round_price(fill)

        if direction.upper() == "BUY" and not (stop_loss < fill < take_profit):
            raise BrokerRejected(
                f"simulated fill {fill} landed outside the plan's levels "
                f"(stop {stop_loss}, target {take_profit}) — the market moved away; not filling"
            )
        if direction.upper() == "SELL" and not (take_profit < fill < stop_loss):
            raise BrokerRejected(
                f"simulated fill {fill} landed outside the plan's levels "
                f"(target {take_profit}, stop {stop_loss}) — the market moved away; not filling"
            )

        conversion = self._conversion_rate(spec, fill)
        position_id = f"paper-{uuid.uuid4().hex[:12]}"
        entry_commission = self.config.paper.commission_per_lot * quantity * 0.5

        self.paper.open_position(
            {
                "position_id": position_id,
                "symbol": spec.symbol,
                "direction": direction.upper(),
                "quantity": quantity,
                "entry_price": fill,
                "stop_loss": spec.round_price(stop_loss),
                "take_profit": spec.round_price(take_profit),
                "contract_size": spec.contract_size,
                "conversion_rate": conversion,
                "commission": entry_commission,
            }
        )
        self.paper.apply_realized(0.0, round(entry_commission, 2))

        self.simulated_writes.append(
            {
                "kind": "ORDER",
                "symbol": spec.symbol,
                "direction": direction.upper(),
                "quantity": quantity,
                "fill": fill,
                "spread": round(quote.spread, 6),
            }
        )
        log_event(
            "PAPER",
            f"simulated {direction.upper()} {quantity} {spec.symbol} at {fill} "
            f"(ask {quote.ask}, bid {quote.bid})",
            symbol=spec.symbol,
            position_id=position_id,
            fill=fill,
            slippage=round(abs(fill - (quote.ask if direction.upper() == "BUY" else quote.bid)), 6),
        )
        return OrderResult(order_id=f"paper-order-{position_id}", raw={"paper": True, "fill": fill})

    def _conversion_rate(self, spec: InstrumentSpec, price: float) -> float:
        """Quote-currency → account-currency rate, from the live broker."""

        from ..risk.sizing import conversion_rate

        def lookup(base: str, quote: str) -> float | None:
            try:
                bridge = self.live.instrument(f"{base}{quote}")
                return self.live.quote(bridge).mid
            except BrokerError:
                return None

        try:
            rate, _ = conversion_rate(spec, price, lookup)
            return rate
        except Exception:  # noqa: BLE001 - sizing already refused this upstream
            return 1.0

    def modify_position(
        self, position_id: str, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> dict[str, Any]:
        row = self.paper.by_id(position_id)
        if row is None or row["status"] != "OPEN":
            raise BrokerRejected(f"no open simulated position {position_id}")

        # A live broker rejects protection already through the market. Paper
        # mode must too, or it would accept a stop the real account would
        # refuse and then "fill" it instantly at a flattering price.
        direction = str(row["direction"])
        try:
            spec = self.live.instrument(str(row["symbol"]))
            quote = self.live.quote(spec)
        except BrokerError as exc:
            raise BrokerRejected(
                f"cannot validate protection for {row['symbol']} without a price: {exc}"
            ) from exc
        market = quote.bid if direction == "BUY" else quote.ask

        if stop_loss is not None:
            through = stop_loss >= market if direction == "BUY" else stop_loss <= market
            if through:
                raise BrokerRejected(
                    f"stop {stop_loss} is already through the market ({market}) for a "
                    f"{direction} position — a broker would reject this"
                )
        if take_profit is not None:
            through = take_profit <= market if direction == "BUY" else take_profit >= market
            if through:
                raise BrokerRejected(
                    f"target {take_profit} is already through the market ({market}) for a "
                    f"{direction} position — a broker would reject this"
                )

        self.paper.update_protection(position_id, stop_loss=stop_loss, take_profit=take_profit)
        self.simulated_writes.append(
            {"kind": "MODIFY", "positionId": position_id, "stopLoss": stop_loss, "takeProfit": take_profit}
        )
        log_event(
            "PAPER",
            f"simulated protection change on {position_id}",
            symbol=str(row["symbol"]),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return {"ok": True, "paper": True}

    def close_position(self, position_id: str, quantity: float | None = None) -> dict[str, Any]:
        row = self.paper.by_id(position_id)
        if row is None or row["status"] != "OPEN":
            raise BrokerRejected(f"no open simulated position {position_id}")
        spec = self.live.instrument(str(row["symbol"]))
        quote = self.live.quote(spec)
        direction = str(row["direction"])
        exit_price = quote.bid if direction == "BUY" else quote.ask
        size = None
        if quantity is not None and 0 < quantity < float(row["quantity"]):
            size = quantity
        net = self._close(
            row,
            exit_price=spec.round_price(exit_price),
            reason="MANUAL" if size is None else "PARTIAL",
            spec=spec,
            quantity=size,
        )
        return {"ok": True, "paper": True, "realizedPnl": round(net, 2)}

    # -- health ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        record = self.paper.account()
        live_health = {}
        try:
            live_health = self.live.health()
        except Exception as exc:  # noqa: BLE001 - health must never raise
            live_health = {"connected": False, "error": str(exc)[:200]}
        return {
            **live_health,
            "mode": "paper",
            "paperInitialised": self._initialised,
            "paperStartingBalance": float(record["starting_balance"]) if record else None,
            "paperRealizedPnl": round(float(record["realized_pnl"] or 0.0), 2) if record else None,
            "paperCommissionPaid": round(float(record["commission_paid"] or 0.0), 2) if record else None,
            "paperOpenPositions": len(self.paper.open_positions()),
            "simulatedWrites": len(self.simulated_writes),
        }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        from ..clock import ensure_utc

        return ensure_utc(value)
    try:
        from ..clock import ensure_utc

        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None
