"""A daily-bar engine for stop-based strategies. No look-ahead by construction.

The rules this engine enforces are the whole point of it, so they are
stated once here and nowhere weakened below:

1. A signal is computed from the CLOSED bar i and nothing later. Every
   indicator value at i is built from bars[:i+1]; the Donchian channel
   deliberately excludes bar i itself.
2. It is filled at the OPEN of bar i+1. There is no "enter at the close
   that generated the signal" — that price was already gone when the bar
   closed.
3. Fills are pessimistic. Every fill pays half the spread and adverse
   slippage. A stop the bar gaps through fills at the OPEN, not at the
   stop. A stop and an exit signal on the same bar resolve as the stop,
   because the stop is checked intrabar before the close is known.
4. Costs are in R. Commission and a swap charge (a fraction of ATR per
   bar held, both directions) come off every trade.

R is measured against the INITIAL stop distance from the actual entry
fill, so a trade's R cannot be improved by where the signal "wanted" to
enter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from ..marketdata.candles import Candle

LONG, SHORT = 1, -1


@dataclass(frozen=True, slots=True)
class Costs:
    """All in PRICE units of the instrument."""

    spread: float
    slippage: float
    commission: float
    swap_atr_fraction: float = 0.01

    def variant(self, *, spread_multiple: float = 1.0, swap: bool = True) -> "Costs":
        return Costs(
            spread=self.spread * spread_multiple,
            slippage=self.slippage,
            commission=self.commission,
            swap_atr_fraction=self.swap_atr_fraction if swap else 0.0,
        )


def costs_for(symbol: str) -> Costs:
    """The pre-registered cost table (EXPERIMENT_TREND_FOLLOWING.md)."""

    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return Costs(spread=0.30, slippage=0.10, commission=0.07)
    pip = 0.01 if symbol.endswith("JPY") else 0.0001
    return Costs(spread=0.8 * pip, slippage=0.3 * pip, commission=0.7 * pip)


@dataclass(slots=True)
class Trade:
    symbol: str
    strategy: str
    direction: int
    signal_index: int
    entry_index: int
    entry_time: datetime
    entry: float
    stop: float
    atr: float
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit: float | None = None
    exit_reason: str | None = None
    bars_held: int = 0
    r: float | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": "BUY" if self.direction == LONG else "SELL",
            "entryTime": self.entry_time.isoformat(),
            "exitTime": self.exit_time.isoformat() if self.exit_time else None,
            "entry": self.entry,
            "stop": self.stop,
            "exit": self.exit,
            "exitReason": self.exit_reason,
            "barsHeld": self.bars_held,
            "r": self.r,
        }


# -- indicators: every value at i uses bars[:i+1] only -------------------


@dataclass(frozen=True, slots=True)
class Indicators:
    atr: list[float | None]
    sma50: list[float | None]
    sma200: list[float | None]


def _true_ranges(bars: Sequence[Candle]) -> list[float]:
    ranges = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        bar = bars[i]
        ranges.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    return ranges


def _rolling_mean(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= window:
            running -= values[i - window]
        if i >= window - 1:
            out[i] = running / window
    return out


def indicators(bars: Sequence[Candle]) -> Indicators:
    closes = [bar.close for bar in bars]
    return Indicators(
        atr=_rolling_mean(_true_ranges(bars), 20),
        sma50=_rolling_mean(closes, 50),
        sma200=_rolling_mean(closes, 200),
    )


def prior_high(bars: Sequence[Candle], i: int, n: int) -> float | None:
    """Highest high of the n bars BEFORE i. Bar i is excluded on purpose."""

    if i < n:
        return None
    return max(bar.high for bar in bars[i - n : i])


def prior_low(bars: Sequence[Candle], i: int, n: int) -> float | None:
    if i < n:
        return None
    return min(bar.low for bar in bars[i - n : i])


# -- strategies -------------------------------------------------------------


class Rule(Protocol):
    name: str
    warmup: int
    stop_atr: float

    def entry(self, bars: Sequence[Candle], i: int, ind: Indicators) -> int: ...

    def exit(self, bars: Sequence[Candle], i: int, ind: Indicators, trade: Trade) -> bool: ...


@dataclass(frozen=True)
class Donchian:
    """C1 — Turtle System 2: 55-bar breakout in, 20-bar channel out, 2 ATR stop."""

    name: str = "C1_donchian_55_20"
    warmup: int = 56
    stop_atr: float = 2.0
    entry_n: int = 55
    exit_n: int = 20

    def entry(self, bars, i, ind) -> int:
        high, low = prior_high(bars, i, self.entry_n), prior_low(bars, i, self.entry_n)
        if high is None or low is None:
            return 0
        if bars[i].close > high:
            return LONG
        if bars[i].close < low:
            return SHORT
        return 0

    def exit(self, bars, i, ind, trade) -> bool:
        if trade.direction == LONG:
            low = prior_low(bars, i, self.exit_n)
            return low is not None and bars[i].close < low
        high = prior_high(bars, i, self.exit_n)
        return high is not None and bars[i].close > high


@dataclass(frozen=True)
class TimeSeriesMomentum:
    """C2 — sign of the 252-bar return, re-evaluated at each new month, 3 ATR stop.

    The month boundary is the FIRST bar of a new month (see the
    pre-registration's clarification): recognising the last bar of a month
    needs the next bar's date.
    """

    name: str = "C2_tsmom_252"
    warmup: int = 253
    stop_atr: float = 3.0
    lookback: int = 252

    def _boundary(self, bars, i) -> bool:
        return i > 0 and bars[i].timestamp.month != bars[i - 1].timestamp.month

    def _sign(self, bars, i) -> int:
        if i < self.lookback:
            return 0
        change = bars[i].close / bars[i - self.lookback].close - 1.0
        return LONG if change > 0 else SHORT if change < 0 else 0

    def entry(self, bars, i, ind) -> int:
        return self._sign(bars, i) if self._boundary(bars, i) else 0

    def exit(self, bars, i, ind, trade) -> bool:
        if not self._boundary(bars, i):
            return False
        sign = self._sign(bars, i)
        return sign != 0 and sign != trade.direction


@dataclass(frozen=True)
class MovingAverageTrend:
    """C3 — enter on the SMA(50)/SMA(200) cross, exit on the opposite cross, 3 ATR stop."""

    name: str = "C3_sma_50_200"
    warmup: int = 201
    stop_atr: float = 3.0

    def _state(self, ind, i) -> int:
        fast, slow = ind.sma50[i], ind.sma200[i]
        if fast is None or slow is None or fast == slow:
            return 0
        return LONG if fast > slow else SHORT

    def entry(self, bars, i, ind) -> int:
        now, before = self._state(ind, i), self._state(ind, i - 1)
        return now if now != 0 and before != 0 and now != before else 0

    def exit(self, bars, i, ind, trade) -> bool:
        return self._state(ind, i) == -trade.direction


CANDIDATES: tuple[Rule, ...] = (Donchian(), TimeSeriesMomentum(), MovingAverageTrend())


# -- the simulation -----------------------------------------------------------


def simulate(
    bars: Sequence[Candle],
    rule: Rule,
    costs: Costs,
    *,
    symbol: str,
    close_at_end: bool = True,
) -> list[Trade]:
    """One instrument, one rule, one position at a time."""

    ind = indicators(bars)
    half = costs.spread / 2.0
    trades: list[Trade] = []
    position: Trade | None = None
    pending_exit = False
    pending_entry = 0
    pending_signal_index = -1

    def close(trade: Trade, index: int, fill: float, reason: str) -> None:
        trade.exit_index = index
        trade.exit_time = bars[index].timestamp
        trade.exit = fill
        trade.exit_reason = reason
        trade.bars_held = index - trade.entry_index
        risk = abs(trade.entry - trade.stop)
        swap = costs.swap_atr_fraction * trade.atr * trade.bars_held
        gross = (fill - trade.entry) * trade.direction
        trade.r = (gross - costs.commission - swap) / risk if risk > 0 else None
        trades.append(trade)

    for i, bar in enumerate(bars):
        # 1. Orders decided at the previous close execute at THIS open.
        if pending_exit and position is not None:
            fill = bar.open - position.direction * (half + costs.slippage)
            close(position, i, fill, "SIGNAL")
            position = None
        pending_exit = False

        if pending_entry and position is None:
            atr = ind.atr[pending_signal_index]
            if atr and atr > 0:
                fill = bar.open + pending_entry * (half + costs.slippage)
                position = Trade(
                    symbol=symbol,
                    strategy=rule.name,
                    direction=pending_entry,
                    signal_index=pending_signal_index,
                    entry_index=i,
                    entry_time=bar.timestamp,
                    entry=fill,
                    stop=fill - pending_entry * rule.stop_atr * atr,
                    atr=atr,
                )
        pending_entry = 0

        # 2. The stop is live from the entry bar on, checked intrabar.
        if position is not None:
            if position.direction == LONG and bar.low <= position.stop:
                raw = min(bar.open, position.stop)  # a gap through fills at the open
                close(position, i, raw - half - costs.slippage, "STOP")
                position = None
            elif position.direction == SHORT and bar.high >= position.stop:
                raw = max(bar.open, position.stop)
                close(position, i, raw + half + costs.slippage, "STOP")
                position = None

        # 3. At the CLOSE of bar i, decide — from bars[:i+1] only.
        if i < rule.warmup or i == len(bars) - 1:
            continue
        if position is not None and rule.exit(bars, i, ind, position):
            pending_exit = True
        if position is None or pending_exit:
            signal = rule.entry(bars, i, ind)
            if signal:
                pending_entry = signal
                pending_signal_index = i

    if close_at_end and position is not None:
        last = len(bars) - 1
        fill = bars[last].close - position.direction * (half + costs.slippage)
        close(position, last, fill, "END")
    return trades


# -- statistics ---------------------------------------------------------------


def summarise(rs: Sequence[float]) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "win": None, "avgR": None, "sumR": None, "t": None, "pfR": None}
    mean = sum(rs) / n
    t = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        if var > 0:
            t = mean / (math.sqrt(var) / math.sqrt(n))
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    return {
        "n": n,
        "win": sum(1 for r in rs if r > 0) / n,
        "avgR": mean,
        "sumR": sum(rs),
        "t": t,
        "pfR": gains / losses if losses > 0 else None,
    }


def max_drawdown_r(trades: Sequence[Trade]) -> float:
    """Deepest peak-to-trough of cumulative R, trades in exit order."""

    ordered = sorted((t for t in trades if t.r is not None), key=lambda t: t.exit_time)
    peak = cumulative = worst = 0.0
    for trade in ordered:
        cumulative += trade.r
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst
