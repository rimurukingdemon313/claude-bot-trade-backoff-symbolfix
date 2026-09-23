"""A general research engine: any timeframe, stops, targets, trails, time exits.

`bot/research/daily.py` produced the recorded trend-following results and
is left exactly as it was, so those stay reproducible. This module extends
the same guarantees to the eight-family program
(docs/EXPERIMENT_EDGE_PROGRAM.md):

1. A decision at bar i sees `bars[:i+1]` and nothing later. Every indicator
   array is causal — value i is built from bars 0..i — and rules read only
   index <= i. `tests/test_research_engine.py` checks every decision of every
   rule against a copy of history truncated at that bar.
2. Orders fill at the OPEN of bar i+1. Time-of-day decisions use the CLOSE
   time of bar i, which is known when bar i closes.
3. Fills are pessimistic: half the spread and slippage on every market fill;
   a stop the bar gaps through fills at the open; stop and target in one bar
   resolve as the stop; a target fills at its price less half the spread,
   never at a better gap.
4. A stop may only tighten. A rule proposing a looser one is ignored.
5. Swap is charged per New York close crossed — the calendar difference of
   trading dates, which is how retail swap accrues including the weekend —
   at 1% of the ATR(20) of the last CLOSED daily bar at entry, both
   directions.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence

from ..marketdata.candles import Candle
from .daily import LONG, SHORT, Costs
from .data import trading_date

# -- causal indicator arrays -------------------------------------------------
# Value i depends only on inputs 0..i. None until enough history exists.


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    current = sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current = alpha * values[i] + (1 - alpha) * current
        out[i] = current
    return out


def true_range(bars: Sequence[Candle]) -> list[float]:
    out = [bars[0].high - bars[0].low] if bars else []
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        b = bars[i]
        out.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    return out


def atr(bars: Sequence[Candle], n: int) -> list[float | None]:
    return sma(true_range(bars), n)


def stdev(values: Sequence[float], n: int) -> list[float | None]:
    """Population standard deviation over the last n values."""

    out: list[float | None] = [None] * len(values)
    s = s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        if i >= n:
            old = values[i - n]
            s -= old
            s2 -= old * old
        if i >= n - 1:
            mean = s / n
            out[i] = math.sqrt(max(0.0, s2 / n - mean * mean))
    return out


def efficiency_ratio(closes: Sequence[float], n: int) -> list[float | None]:
    """Kaufman: net move over n bars / sum of absolute bar-to-bar moves."""

    out: list[float | None] = [None] * len(closes)
    steps = [0.0] + [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    path = 0.0
    for i in range(len(closes)):
        path += steps[i]
        if i > n:
            path -= steps[i - n]
        if i >= n:
            out[i] = abs(closes[i] - closes[i - n]) / path if path > 0 else 0.0
    return out


def prior_extreme(values: Sequence[float], n: int, *, highest: bool) -> list[float | None]:
    """Max (or min) of the n values BEFORE i. Value i itself is excluded."""

    from collections import deque

    out: list[float | None] = [None] * len(values)
    window: deque[int] = deque()
    for i in range(len(values)):
        # window holds indices i-n .. i-1
        if i >= 1:
            j = i - 1
            if highest:
                while window and values[window[-1]] <= values[j]:
                    window.pop()
            else:
                while window and values[window[-1]] >= values[j]:
                    window.pop()
            window.append(j)
        while window and window[0] < i - n:
            window.popleft()
        if i >= n:
            out[i] = values[window[0]]
    return out


# -- orders, positions -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """What a rule asks for at the close of bar i. Priced at the next open."""

    direction: int
    stop_price: float | None = None
    stop_distance: float | None = None
    target_price: float | None = None
    target_r: float | None = None
    target_distance: float | None = None
    max_bars: int | None = None
    tag: str = ""


@dataclass(frozen=True, slots=True)
class Manage:
    exit: bool = False
    stop: float | None = None


@dataclass(slots=True)
class Position:
    symbol: str
    strategy: str
    direction: int
    signal_index: int
    entry_index: int
    entry_time: datetime
    entry: float
    stop: float
    initial_stop: float
    target: float | None
    max_bars: int | None
    swap_per_night: float
    tag: str = ""
    state: dict = field(default_factory=dict)
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit: float | None = None
    exit_reason: str | None = None
    nights: int = 0
    cost_r: float = 0.0
    r: float | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": "BUY" if self.direction == LONG else "SELL",
            "entryTime": self.entry_time.isoformat(),
            "exitTime": self.exit_time.isoformat() if self.exit_time else None,
            "entry": self.entry,
            "initialStop": self.initial_stop,
            "exit": self.exit,
            "exitReason": self.exit_reason,
            "nights": self.nights,
            "costR": self.cost_r,
            "tag": self.tag,
            "r": self.r,
        }


@dataclass
class Context:
    """What a rule may know besides its own bars. All of it causal."""

    symbol: str
    costs: Costs
    daily_atr_times: list[datetime] = field(default_factory=list)
    daily_atr_values: list[float] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def daily_atr_at(self, moment: datetime) -> float | None:
        """ATR(20) of the last daily bar that had CLOSED by `moment`."""

        k = bisect_right(self.daily_atr_times, moment)
        return self.daily_atr_values[k - 1] if k > 0 else None


def daily_atr_series(daily: Sequence[Candle]) -> tuple[list[datetime], list[float]]:
    values = atr(daily, 20)
    times, out = [], []
    for bar, value in zip(daily, values):
        if value is not None:
            times.append(bar.close_time)
            out.append(value)
    return times, out


class Rule(Protocol):
    name: str

    def prepare(self, bars: Sequence[Candle], ctx: Context) -> Any: ...

    def entry(self, i: int, bars: Sequence[Candle], pre: Any, ctx: Context) -> Entry | None: ...

    def manage(
        self, i: int, bars: Sequence[Candle], pre: Any, ctx: Context, pos: Position
    ) -> Manage | None: ...


# -- the simulation ------------------------------------------------------------------


def simulate(
    bars: Sequence[Candle],
    rule: Rule,
    ctx: Context,
    *,
    close_at_end: bool = True,
) -> list[Position]:
    costs = ctx.costs
    half = costs.spread / 2.0
    pre = rule.prepare(bars, ctx)
    closed: list[Position] = []
    pos: Position | None = None
    pending_exit = False
    pending_entry: Entry | None = None
    pending_signal = -1
    pending_stop: float | None = None

    def finish(p: Position, index: int, fill: float, reason: str, when: datetime) -> None:
        p.exit_index, p.exit_time, p.exit, p.exit_reason = index, when, fill, reason
        p.nights = max(0, (trading_date(when) - trading_date(p.entry_time)).days)
        risk = abs(p.entry - p.initial_stop)
        swap = p.swap_per_night * p.nights
        gross = (fill - p.entry) * p.direction
        if risk > 0:
            p.r = (gross - costs.commission - swap) / risk
            # Everything the market did not do to this trade: the spread and
            # slippage at both fills, commission and swap, in R.
            frictions = 2 * (half + costs.slippage) + costs.commission + swap
            p.cost_r = frictions / risk
        closed.append(p)

    for i, bar in enumerate(bars):
        # 1. Orders decided at the previous close, at THIS open.
        if pending_exit and pos is not None:
            finish(pos, i, bar.open - pos.direction * (half + costs.slippage), pos.state.get("why", "SIGNAL"), bar.timestamp)
            pos = None
        pending_exit = False

        if pos is not None and pending_stop is not None:
            tighter = (pending_stop > pos.stop) if pos.direction == LONG else (pending_stop < pos.stop)
            if tighter:
                pos.stop = pending_stop
        pending_stop = None

        if pending_entry is not None and pos is None:
            order = pending_entry
            d = order.direction
            fill = bar.open + d * (half + costs.slippage)
            if order.stop_price is not None:
                stop = order.stop_price
            elif order.stop_distance is not None:
                stop = fill - d * order.stop_distance
            else:
                stop = None
            risk = (fill - stop) * d if stop is not None else 0.0
            target = None
            if order.target_price is not None:
                target = order.target_price
            elif order.target_r is not None and risk > 0:
                target = fill + d * order.target_r * risk
            elif order.target_distance is not None:
                target = fill + d * order.target_distance
            # An order the open has already invalidated is not taken: a stop
            # on the wrong side of the fill, or a target already passed.
            valid = risk > 0 and (target is None or (target - fill) * d > 0)
            daily = ctx.daily_atr_at(bar.timestamp)
            if valid and daily is not None:
                pos = Position(
                    symbol=ctx.symbol, strategy=rule.name, direction=d,
                    signal_index=pending_signal, entry_index=i, entry_time=bar.timestamp,
                    entry=fill, stop=stop, initial_stop=stop, target=target,
                    max_bars=order.max_bars, swap_per_night=costs.swap_atr_fraction * daily,
                    tag=order.tag,
                )
        pending_entry = None

        # 2. Intrabar: stop first, then target.
        if pos is not None:
            d = pos.direction
            stop_hit = bar.low <= pos.stop if d == LONG else bar.high >= pos.stop
            target_hit = pos.target is not None and (
                bar.high >= pos.target if d == LONG else bar.low <= pos.target
            )
            if stop_hit:
                raw = min(bar.open, pos.stop) if d == LONG else max(bar.open, pos.stop)
                finish(pos, i, raw - d * (half + costs.slippage), "STOP", bar.close_time)
                pos = None
            elif target_hit:
                finish(pos, i, pos.target - d * half, "TARGET", bar.close_time)
                pos = None

        # 3. At the CLOSE of bar i: decide, from bars[:i+1] only.
        if i == len(bars) - 1:
            break
        if pos is not None:
            if pos.max_bars is not None and i - pos.entry_index + 1 >= pos.max_bars:
                pending_exit = True
                pos.state["why"] = "TIME"
            else:
                m = rule.manage(i, bars, pre, ctx, pos)
                if m is not None:
                    if m.exit:
                        pending_exit = True
                        pos.state.setdefault("why", "SIGNAL")
                    elif m.stop is not None:
                        pending_stop = m.stop
        if pos is None or pending_exit:
            order = rule.entry(i, bars, pre, ctx)
            if order is not None:
                pending_entry = order
                pending_signal = i

    if close_at_end and pos is not None:
        last = len(bars) - 1
        finish(pos, last, bars[last].close - pos.direction * (half + costs.slippage), "END", bars[last].close_time)
    return closed
