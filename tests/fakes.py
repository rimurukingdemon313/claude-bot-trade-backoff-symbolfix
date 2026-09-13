"""Test doubles and synthetic market builders.

The synthetic markets here are hand-built so each SMC feature can be
asserted against a scenario whose correct answer is known by
construction. They are NOT random walks: a random series proves nothing
about whether a detector found the right swing.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from bot.broker.models import (
    AccountState,
    BrokerOrder,
    BrokerPosition,
    InstrumentSpec,
    OrderResult,
    Quote,
)
from bot.errors import AmbiguousExecution, BrokerRejected
from bot.marketdata.candles import Candle

#: All synthetic series END here: Thursday 14:00 UTC, inside the
#: London/New York overlap, so the session filter does not reject an
#: otherwise-valid setup and session logic stays a separate test concern.
SETUP_END = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
BASE_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)  # a Wednesday, London open

TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}


def candle(
    timestamp: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    timeframe: str = "M15",
    volume: float = 100.0,
) -> Candle:
    return Candle(timestamp, open_price, high, low, close, volume, timeframe)


def series_from_path(
    path: Sequence[tuple[float, float, float, float]],
    *,
    timeframe: str = "M15",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    """Build candles from explicit (open, high, low, close) tuples.

    Pass `end` to anchor the LAST candle's close time, which is how the
    fixtures land inside a chosen trading session.
    """

    minutes = TIMEFRAME_MINUTES[timeframe]
    if end is not None:
        begin = end - timedelta(minutes=minutes * len(path))
    else:
        begin = start or BASE_TIME
    return [
        candle(begin + timedelta(minutes=minutes * index), *values, timeframe=timeframe)
        for index, values in enumerate(path)
    ]


def trending_path(
    *,
    count: int,
    start_price: float,
    step: float,
    wobble: float,
    direction: int = 1,
) -> list[tuple[float, float, float, float]]:
    """A clean stair-step trend with alternating pullbacks.

    Produces unambiguous higher highs and higher lows (or the inverse),
    which is what the swing detector and bias logic are asserted against.
    """

    path: list[tuple[float, float, float, float]] = []
    price = start_price
    for index in range(count):
        # Every third candle pulls back, creating a pivot low in an uptrend.
        pulls_back = index % 3 == 2
        move = -step * 0.55 if pulls_back else step
        open_price = price
        close = price + move * direction
        high = max(open_price, close) + wobble
        low = min(open_price, close) - wobble
        path.append((open_price, high, low, close))
        price = close
    return path


def bullish_setup_m15(
    *,
    start_price: float = 1.1000,
    step: float = 0.00060,
    wobble: float = 0.00012,
    trend_candles: int = 70,
    end: datetime | None = None,
) -> list[Candle]:
    """A complete textbook long setup, built stage by stage.

    Stages, in order:
      1. a sustained uptrend (bullish swing structure on M15);
      2. a pullback that prints a clear swing low;
      3. consolidation that leaves TWO equal lows (stop liquidity);
      4. a sweep candle: wicks below those equal lows, closes back above;
      5. a displacement candle: large body, closes above the last swing
         high (a BOS) and leaves a three-candle imbalance;
      6. a shallow retracement back toward that imbalance, where the
         entry logic should find price sitting in the gap.
    """

    path = trending_path(
        count=trend_candles, start_price=start_price, step=step, wobble=wobble, direction=1
    )
    price = path[-1][3]

    # 2-3. Pull back and print two equal lows at the same level.
    equal_low = price - step * 4
    path.append((price, price + wobble, equal_low + step * 0.5, equal_low + step * 0.8))
    path.append((equal_low + step * 0.8, equal_low + step * 1.2, equal_low, equal_low + step * 0.6))
    path.append((equal_low + step * 0.6, equal_low + step * 1.6, equal_low + step * 0.4, equal_low + step * 1.4))
    path.append((equal_low + step * 1.4, equal_low + step * 1.8, equal_low + step * 0.02, equal_low + step * 0.9))
    path.append((equal_low + step * 0.9, equal_low + step * 1.3, equal_low + step * 0.5, equal_low + step * 1.1))

    # 4. The sweep: deep wick under the equal lows, strong close back above.
    sweep_open = equal_low + step * 1.1
    sweep_low = equal_low - step * 1.8
    sweep_close = equal_low + step * 1.5
    path.append((sweep_open, sweep_close + wobble, sweep_low, sweep_close))

    # 5. Displacement: a large-bodied candle clearing the prior swing high.
    prior_high = max(row[1] for row in path[-14:])
    disp_open = sweep_close
    disp_close = prior_high + step * 1.2
    path.append((disp_open, disp_close + wobble, disp_open - wobble, disp_close))

    # 6. A second leg up, leaving an imbalance behind the displacement.
    leg_open = disp_close + step * 0.6
    leg_close = leg_open + step * 1.1
    path.append((leg_open, leg_close + wobble, leg_open - wobble * 0.5, leg_close))

    # 7. Retracement back into the gap zone.
    path.append((leg_close, leg_close + wobble, disp_close + step * 0.4, disp_close + step * 1.0))
    path.append((disp_close + step * 1.0, disp_close + step * 1.4, disp_close + step * 0.2, disp_close + step * 0.8))

    return series_from_path(path, timeframe="M15", end=end or SETUP_END)


def aligned_htf(
    m15: Sequence[Candle], *, timeframe: str, count: int = 70
) -> list[Candle]:
    """A higher-timeframe series trending the same way as the M15 setup.

    Anchored to end just before the M15 window so the timestamps are
    coherent and the validator sees a fresh, closed series.
    """

    minutes = TIMEFRAME_MINUTES[timeframe]
    last_close = m15[-1].close_time
    step = (m15[-1].close - m15[0].open) / max(1, count) * 1.2
    if abs(step) < 1e-6:
        step = 0.0004
    path = trending_path(
        count=count,
        start_price=m15[0].open,
        step=abs(step),
        wobble=abs(step) * 0.2,
        direction=1 if step > 0 else -1,
    )
    return series_from_path(path, timeframe=timeframe, end=last_close)


def bearish_setup_m15(**kwargs: Any) -> list[Candle]:
    """The mirror image of bullish_setup_m15, for direction symmetry tests."""

    bullish = bullish_setup_m15(**kwargs)
    pivot = bullish[0].open * 2
    return [
        candle(
            item.timestamp,
            pivot - item.open,
            pivot - item.low,
            pivot - item.high,
            pivot - item.close,
            item.timeframe,
            item.volume,
        )
        for item in bullish
    ]


def flat_market_m15(count: int = 80, price: float = 1.1000) -> list[Candle]:
    """A dead range: no structure, no displacement, nothing to trade."""

    path = []
    for index in range(count):
        drift = 0.00002 * (1 if index % 2 else -1)
        path.append((price, price + 0.00008, price - 0.00008, price + drift))
    return series_from_path(path, timeframe="M15", end=SETUP_END)


DEFAULT_SPEC = InstrumentSpec(
    symbol="EURUSD",
    broker_name="EURUSD",
    tradable_instrument_id=1,
    route_id=10,
    quote_route_id=11,
    contract_size=100_000.0,
    tick_size=0.00001,
    tick_value=None,
    lot_step=0.01,
    min_lot=0.01,
    max_lot=100.0,
    base_currency="EUR",
    quote_currency="USD",
    account_currency="USD",
    digits=5,
)


@dataclass
class FakeBroker:
    """In-memory broker with injectable failures.

    Records every write so tests can assert "exactly one order was
    submitted" — which is the whole point of the idempotency machinery.
    """

    account: AccountState = field(
        default_factory=lambda: AccountState(
            balance=10_000.0,
            equity=10_000.0,
            margin_used=0.0,
            margin_available=10_000.0,
            open_pnl=0.0,
            today_pnl=0.0,
            currency="USD",
            raw={},
        )
    )
    metadata: dict[str, Any] | None = field(
        default_factory=lambda: {"id": "1", "accNum": "1", "accountType": "DEMO", "currency": "USD"}
    )
    #: Broker-signed session claims. Defaults to None so the fake keeps
    #: proving that the account record alone is enough; tests that model a
    #: brand with no account type set this instead.
    claims: dict[str, Any] | None = None
    specs: dict[str, InstrumentSpec] = field(default_factory=lambda: {"EURUSD": DEFAULT_SPEC})
    series: dict[tuple[str, str], list[Candle]] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    _positions: list[BrokerPosition] = field(default_factory=list)
    _orders: list[BrokerOrder] = field(default_factory=list)
    _history: list[BrokerOrder] = field(default_factory=list)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    modifications: list[dict[str, Any]] = field(default_factory=list)
    closures: list[dict[str, Any]] = field(default_factory=list)
    place_order_hook: Callable[[dict[str, Any]], Any] | None = None
    fill_on_submit: bool = True
    next_position_id: int = 5000

    # -- session / metadata ---------------------------------------------

    def ensure_session(self) -> None:
        return None

    @property
    def account_metadata(self) -> dict[str, Any] | None:
        return self.metadata

    @property
    def session_claims(self) -> dict[str, Any] | None:
        return self.claims

    def account_state(self) -> AccountState:
        return self.account

    def health(self) -> dict[str, Any]:
        return {"connected": True, "circuit": "closed", "calls": len(self.submitted)}

    # -- instruments / data ---------------------------------------------

    def instrument(self, symbol: str, *, force: bool = False) -> InstrumentSpec:
        """Resolve by canonical pair, mirroring the real broker.

        Keyed canonically so a suffixed account (`EURUSD.R`) behaves the way
        TradeLockerBroker does after symbol resolution: either form of the
        name finds the instrument, and the spec reports the canonical symbol.
        """

        from bot.broker.symbols import alphanumeric, canonical_symbol

        key = canonical_symbol(symbol) or alphanumeric(symbol)
        if key not in self.specs:
            raise BrokerRejected(f"symbol {symbol!r} not available on this account")
        return self.specs[key]

    def available_symbols(self) -> list[str]:
        return sorted(self.specs)

    def quote(self, spec: InstrumentSpec) -> Quote:
        if spec.symbol in self.quotes:
            return self.quotes[spec.symbol]
        candles = self.series.get((spec.symbol, "M15"))
        price = candles[-1].close if candles else 1.1000
        half = spec.tick_size * 5
        return Quote(spec.symbol, price - half, price + half, BASE_TIME)

    def candles(self, spec: InstrumentSpec, timeframe: str, *, count: int = 300) -> list[dict[str, Any]]:
        data = self.series.get((spec.symbol, timeframe.upper()))
        if not data:
            return []
        return [item.as_dict() for item in data[-count:]]

    def set_series(self, symbol: str, timeframe: str, candles: Iterable[Candle]) -> None:
        self.series[(symbol, timeframe.upper())] = list(candles)

    # -- positions / orders ---------------------------------------------

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def orders(self) -> list[BrokerOrder]:
        return list(self._orders)

    def order_history(self, limit: int = 200) -> list[BrokerOrder]:
        return list(self._history)[:limit]

    def add_position(
        self,
        *,
        symbol: str = "EURUSD",
        direction: str = "BUY",
        quantity: float = 0.1,
        entry: float = 1.1000,
        stop_loss: float | None = 1.0950,
        take_profit: float | None = 1.1150,
        position_id: str | None = None,
        opened_at: datetime | None = None,
    ) -> BrokerPosition:
        self.next_position_id += 1
        position = BrokerPosition(
            position_id=position_id or str(self.next_position_id),
            symbol=symbol,
            instrument_id=1,
            direction=direction,
            quantity=quantity,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            unrealized_pnl=0.0,
            opened_at=opened_at or BASE_TIME,
            raw={},
        )
        self._positions.append(position)
        return position

    def remove_position(self, position_id: str, *, realized_pnl: float = 0.0, exit_price: float | None = None) -> None:
        self._positions = [p for p in self._positions if p.position_id != position_id]
        self._history.append(
            BrokerOrder(
                order_id=f"ord-{position_id}",
                position_id=position_id,
                symbol="EURUSD",
                instrument_id=1,
                direction="SELL",
                quantity=0.1,
                status="FILLED",
                price=exit_price,
                stop_price=None,
                order_type="market",
                created_at=BASE_TIME,
                raw={"realizedPl": realized_pnl},
            )
        )

    # -- writes ----------------------------------------------------------

    def place_market_order(
        self,
        spec: InstrumentSpec,
        *,
        direction: str,
        quantity: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        request = {
            "symbol": spec.symbol,
            "direction": direction,
            "quantity": quantity,
            "stopLoss": stop_loss,
            "takeProfit": take_profit,
        }
        self.submitted.append(request)
        if self.place_order_hook is not None:
            outcome = self.place_order_hook(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        self.next_position_id += 1
        order_id = f"ord-{self.next_position_id}"
        if self.fill_on_submit:
            self._positions.append(
                BrokerPosition(
                    position_id=str(self.next_position_id),
                    symbol=spec.symbol,
                    instrument_id=spec.tradable_instrument_id,
                    direction=direction,
                    quantity=quantity,
                    entry_price=self.quote(spec).ask if direction == "BUY" else self.quote(spec).bid,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    unrealized_pnl=0.0,
                    opened_at=BASE_TIME,
                    raw={},
                )
            )
        return OrderResult(order_id=order_id, raw={"orderId": order_id})

    def modify_position(
        self, position_id: str, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> dict[str, Any]:
        self.modifications.append(
            {"positionId": position_id, "stopLoss": stop_loss, "takeProfit": take_profit}
        )
        updated = []
        for position in self._positions:
            if position.position_id == position_id:
                updated.append(
                    BrokerPosition(
                        position.position_id,
                        position.symbol,
                        position.instrument_id,
                        position.direction,
                        position.quantity,
                        position.entry_price,
                        stop_loss if stop_loss is not None else position.stop_loss,
                        take_profit if take_profit is not None else position.take_profit,
                        position.unrealized_pnl,
                        position.opened_at,
                        position.raw,
                    )
                )
            else:
                updated.append(position)
        self._positions = updated
        return {"ok": True}

    def close_position(self, position_id: str, quantity: float | None = None) -> dict[str, Any]:
        self.closures.append({"positionId": position_id, "quantity": quantity})
        if quantity is None:
            self.remove_position(position_id)
        return {"ok": True}


def suffixed_broker(suffix: str = ".R", symbols: tuple[str, ...] = ("EURUSD",)) -> FakeBroker:
    """A FakeBroker whose instrument names all carry a broker suffix.

    Models the account shape this project was reported against: every pair
    listed as `EURUSD.R`, `XAUUSD.R` and so on. Positions report the
    CANONICAL symbol, exactly as the real broker does after resolution —
    which is the behaviour the duplicate-order check depends on.
    """

    broker = FakeBroker()
    broker.specs = {}
    for symbol in symbols:
        contract = 100.0 if symbol.startswith("XAU") else 100_000.0
        digits = 2 if symbol.startswith("XAU") else (3 if symbol.endswith("JPY") else 5)
        broker.specs[symbol] = dataclasses.replace(
            DEFAULT_SPEC,
            symbol=symbol,
            broker_name=f"{symbol}{suffix}",
            contract_size=contract,
            tick_size=10 ** (-digits),
            digits=digits,
            base_currency=symbol[:3],
            quote_currency=symbol[3:],
        )
    m15 = bullish_setup_m15()
    for symbol in symbols:
        broker.set_series(symbol, "M15", m15)
        broker.set_series(symbol, "H1", aligned_htf(m15, timeframe="H1"))
        broker.set_series(symbol, "H4", aligned_htf(m15, timeframe="H4"))
    return broker


def ambiguous_hook(request: dict[str, Any]) -> Exception:
    return AmbiguousExecution("transport died after the request left this process")


def rejecting_hook(request: dict[str, Any]) -> Exception:
    return BrokerRejected("insufficient margin")
