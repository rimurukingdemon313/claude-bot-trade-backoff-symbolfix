"""Broker-facing value objects.

These exist so nothing downstream ever touches a raw broker payload.
Instrument specifications in particular are load-bearing for position
sizing: the old code assumed "1 lot = 100,000 units" for every FX pair
and "$10 per pip", which is wrong for any pair whose quote currency is
not the account currency (see bot/risk/sizing.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Everything needed to size and price an order correctly."""

    symbol: str
    broker_name: str
    tradable_instrument_id: int
    route_id: int
    quote_route_id: int | None
    contract_size: float
    tick_size: float
    tick_value: float | None
    lot_step: float
    min_lot: float
    max_lot: float
    base_currency: str | None
    quote_currency: str | None
    account_currency: str
    digits: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def point(self) -> float:
        """Smallest meaningful price increment (used for spread maths)."""

        return self.tick_size if self.tick_size > 0 else 10 ** (-self.digits)

    def round_lot(self, lots: float) -> float:
        """Snap to the broker's lot step, rounding DOWN.

        Rounding down matters: rounding up would silently exceed the risk
        budget the risk engine just approved.
        """

        if self.lot_step <= 0:
            return round(lots, 2)
        steps = int(lots / self.lot_step + 1e-9)
        return round(steps * self.lot_step, 8)

    def round_price(self, price: float) -> float:
        if self.tick_size > 0:
            return round(round(price / self.tick_size) * self.tick_size, self.digits + 2)
        return round(price, self.digits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "brokerName": self.broker_name,
            "tradableInstrumentId": self.tradable_instrument_id,
            "routeId": self.route_id,
            "contractSize": self.contract_size,
            "tickSize": self.tick_size,
            "tickValue": self.tick_value,
            "lotStep": self.lot_step,
            "minLot": self.min_lot,
            "maxLot": self.max_lot,
            "baseCurrency": self.base_currency,
            "quoteCurrency": self.quote_currency,
            "accountCurrency": self.account_currency,
            "digits": self.digits,
        }


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0


@dataclass(frozen=True, slots=True)
class AccountState:
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    open_pnl: float
    today_pnl: float
    currency: str
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "marginUsed": round(self.margin_used, 2),
            "marginAvailable": round(self.margin_available, 2),
            "openPnl": round(self.open_pnl, 2),
            "todayPnl": round(self.today_pnl, 2),
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    position_id: str
    symbol: str
    instrument_id: int
    direction: str  # BUY | SELL
    quantity: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    unrealized_pnl: float
    opened_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "positionId": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "entryPrice": self.entry_price,
            "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit,
            "unrealizedPnl": round(self.unrealized_pnl, 2),
            "openedAt": self.opened_at.isoformat() if self.opened_at else None,
        }


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    order_id: str
    position_id: str | None
    symbol: str
    instrument_id: int
    direction: str
    quantity: float
    status: str
    price: float | None
    stop_price: float | None
    order_type: str
    created_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)
