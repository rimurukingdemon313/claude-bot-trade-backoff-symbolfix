"""The general research engine and the eight families it judges.

These tests are about the engine being honest — causal, pessimistic,
correctly costed — never about any family being good.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from bot.marketdata.candles import Candle
from bot.research.daily import LONG, SHORT, Costs
from bot.research.engine import (
    Context, Entry, Manage, Position, daily_atr_series, prior_extreme, simulate,
)
from bot.research.families import (
    FAMILIES, AdaptiveTrend, LondonOpeningRange, TickVwapReversion,
    VolatilityBreakout, align_by_trading_date, cross_sectional_schedule,
)

FREE = Costs(spread=0.0, slippage=0.0, commission=0.0, swap_atr_fraction=0.0)
STEP = {"M15": 15, "H1": 60, "H4": 240, "D1": 1440}


def _is_weekend(t: datetime) -> bool:
    return (t.weekday() == 4 and t.hour >= 21) or t.weekday() == 5 or (t.weekday() == 6 and t.hour < 22)


def regime_walk(n, seed, timeframe, start=datetime(2019, 3, 3, 22, 0, tzinfo=timezone.utc)):
    """Segments of trend, range, compression and expansion.

    Seeded, and used ONLY to test structural properties — causality and
    mechanics — never to argue that any rule works (rule 10). The regimes
    exist so every family actually fires; a causality check over a rule
    that never signals would prove nothing.
    """

    rng = random.Random(seed)
    step = timedelta(minutes=STEP[timeframe])
    scale = {"M15": 0.0006, "H1": 0.0012, "H4": 0.0024, "D1": 0.006}[timeframe]
    t, price, rows = start, 1.20, []
    segment, left, anchor = "trend", 0, price
    while len(rows) < n:
        if left <= 0:
            segment = rng.choice(["up", "down", "range", "squeeze", "burst"])
            left, anchor = rng.randint(30, 90), price
        left -= 1
        vol = scale * {"squeeze": 0.25, "burst": 2.5}.get(segment, 1.0)
        drift = {"up": 0.35, "down": -0.35}.get(segment, 0.0) * vol
        pull = -0.15 * (price - anchor) if segment == "range" else 0.0
        close = max(0.5, price + drift + pull + rng.gauss(0, vol))
        high = max(price, close) + abs(rng.gauss(0, vol * 0.4))
        low = min(price, close) - abs(rng.gauss(0, vol * 0.4))
        while _is_weekend(t):
            t += step
        rows.append(Candle(t, price, high, low, close, float(rng.randint(50, 500)), timeframe))
        price, t = close, t + step
    return rows


def daily_from(bars):
    from bot.research.data import trading_date

    days: dict = {}
    for b in bars:
        days.setdefault(trading_date(b.timestamp), []).append(b)
    out = []
    for day in sorted(days):
        group = days[day]
        out.append(Candle(group[0].timestamp, group[0].open, max(b.high for b in group),
                          min(b.low for b in group), group[-1].close, 0.0, "D1"))
    return out


def context(bars, costs=FREE, symbol="EURUSD", **extra):
    if bars and bars[0].timeframe == "D1":
        daily = bars
    else:
        # Rules and the engine read the ATR(20) of the last CLOSED daily
        # bar, which needs twenty trading days before the intraday window
        # starts. Without this prefix F10 can never fire on a short test
        # series — found by the coverage guard, which is what it is for.
        first = bars[0]
        width = 0.012 * first.open
        prefix = [
            Candle(first.timestamp - timedelta(days=40 - k), first.open, first.open + width / 2,
                   first.open - width / 2, first.open, 0.0, "D1")
            for k in range(40)
        ]
        daily = prefix + daily_from(bars)
    times, values = daily_atr_series(daily)
    return Context(symbol=symbol, costs=costs, daily_atr_times=times, daily_atr_values=values, extra=extra)


def _probe(direction):
    return Position(symbol="EURUSD", strategy="t", direction=direction, signal_index=0,
                    entry_index=0, entry_time=datetime(2019, 1, 1, tzinfo=timezone.utc),
                    entry=1.2, stop=1.1 if direction == LONG else 1.3,
                    initial_stop=1.1 if direction == LONG else 1.3, target=None,
                    max_bars=None, swap_per_night=0.0, tag="trend")


SIZES = {"D1": 700, "H4": 700, "H1": 700, "M15": 900}
SINGLE = [key for key in FAMILIES if key != "F3"]


# -- causality -------------------------------------------------------------------


@pytest.mark.parametrize("key", SINGLE)
def test_every_decision_is_identical_when_the_future_does_not_exist(key):
    cls, timeframe, _ = FAMILIES[key]
    rule = cls()
    bars = regime_walk(SIZES[timeframe], seed=hash(key) % 1000, timeframe=timeframe)
    ctx = context(bars, costs=Costs(spread=0.00002, slippage=0.0, commission=0.0))
    full = rule.prepare(bars, ctx)
    fired = 0
    for i in range(1, len(bars) - 1):
        seen = bars[: i + 1]
        cut = rule.prepare(seen, ctx)
        a, b = rule.entry(i, bars, full, ctx), rule.entry(i, seen, cut, ctx)
        assert a == b, f"{key}: entry at {i} saw the future"
        fired += a is not None
        for d in (LONG, SHORT):
            ma = rule.manage(i, bars, full, ctx, _probe(d))
            mb = rule.manage(i, seen, cut, ctx, _probe(d))
            assert ma == mb, f"{key}: manage at {i} saw the future"
    assert fired > 0, f"{key} never fired on the test series — the check would be vacuous"


def test_the_cross_sectional_schedule_never_reads_ahead():
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "EURGBP"]
    aligned = align_by_trading_date(
        {p: regime_walk(260, seed=k, timeframe="D1") for k, p in enumerate(pairs)}
    )
    full = cross_sectional_schedule(aligned, 20)
    picks = 0
    for i in range(len(full)):
        cut = cross_sectional_schedule({p: bars[: i + 1] for p, bars in aligned.items()}, 20)
        assert cut[i] == full[i], f"schedule at {i} saw the future"
        picks += full[i] is not None
    assert picks > 5


def test_the_causality_check_catches_a_deliberate_one_bar_leak():
    @dataclass(frozen=True)
    class Leaky(VolatilityBreakout):
        def entry(self, i, bars, pre, ctx):
            base = super().entry(i, bars, pre, ctx)
            if base is not None and i + 1 < len(bars):
                up = bars[i + 1].close > bars[i].close
                return base if up == (base.direction == LONG) else None
            return base

    bars = regime_walk(700, seed=3, timeframe="H1")
    ctx = context(bars)
    rule = Leaky()
    full = rule.prepare(bars, ctx)
    leaked = any(
        rule.entry(i, bars, full, ctx) != rule.entry(i, bars[: i + 1], rule.prepare(bars[: i + 1], ctx), ctx)
        for i in range(1, len(bars) - 1)
    )
    assert leaked


def test_prior_extreme_excludes_the_current_bar():
    values = [1, 5, 2, 9, 3]
    assert prior_extreme(values, 2, highest=True) == [None, None, 5, 5, 9]
    assert prior_extreme(values, 2, highest=False) == [None, None, 1, 2, 2]


# -- mechanics -----------------------------------------------------------------------


@dataclass(frozen=True)
class Scripted:
    """Signals exactly as told, so fills and exits can be checked by hand."""

    orders: dict
    exits: frozenset = frozenset()
    stops: dict = None
    name: str = "scripted"

    def prepare(self, bars, ctx):
        return None

    def entry(self, i, bars, pre, ctx):
        return self.orders.get(i)

    def manage(self, i, bars, pre, ctx, pos):
        if i in self.exits:
            return Manage(exit=True)
        if self.stops and i in self.stops:
            return Manage(stop=self.stops[i])
        return None


def flat_bars(n, price=1.2, width=0.001, timeframe="H1"):
    start = datetime(2019, 3, 4, 0, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=STEP[timeframe])
    return [Candle(start + k * step, price, price + width, price - width, price, 100.0, timeframe)
            for k in range(n)]


def test_an_order_fills_at_the_next_open_with_half_spread_and_slippage():
    bars = flat_bars(60)
    ctx = context(bars, costs=Costs(spread=0.0002, slippage=0.0001, commission=0.0))
    trades = simulate(bars, Scripted({30: Entry(LONG, stop_distance=0.01)}), ctx)
    assert trades[0].entry_index == 31
    assert trades[0].entry == pytest.approx(1.2 + 0.0001 + 0.0001)


def test_a_gap_through_the_stop_fills_at_the_open():
    bars = flat_bars(60)
    bars[33] = Candle(bars[33].timestamp, 1.15, 1.151, 1.149, 1.15, 100.0, "H1")
    trades = simulate(bars, Scripted({30: Entry(LONG, stop_distance=0.01)}), context(bars))
    assert trades[0].exit_reason == "STOP"
    assert trades[0].exit == pytest.approx(1.15)


def test_stop_and_target_in_one_bar_resolve_as_the_stop():
    bars = flat_bars(60)
    bars[33] = Candle(bars[33].timestamp, 1.2, 1.25, 1.15, 1.2, 100.0, "H1")
    order = Entry(LONG, stop_distance=0.01, target_r=2.0)
    trades = simulate(bars, Scripted({30: order}), context(bars))
    assert trades[0].exit_reason == "STOP"


def test_a_target_fills_at_its_price_less_half_the_spread():
    bars = flat_bars(60)
    bars[33] = Candle(bars[33].timestamp, 1.2, 1.25, 1.199, 1.24, 100.0, "H1")
    costs = Costs(spread=0.0002, slippage=0.0, commission=0.0, swap_atr_fraction=0.0)
    trades = simulate(bars, Scripted({30: Entry(LONG, stop_distance=0.01, target_distance=0.02)}),
                      context(bars, costs=costs))
    trade = trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.exit == pytest.approx(trade.target - 0.0001)


def test_a_stop_can_tighten_but_never_loosen():
    bars = flat_bars(80)
    order = {30: Entry(LONG, stop_distance=0.01)}
    tightened = simulate(bars, Scripted(order, stops={35: 1.1995}), context(bars))
    loosened = simulate(bars, Scripted(order, stops={35: 1.10}), context(bars), close_at_end=False)
    assert tightened[0].exit_reason == "STOP", "a tighter stop inside the bar range must trigger"
    assert loosened == [], "a looser stop must be ignored, leaving the original untouched"


def test_a_time_exit_fires_after_max_bars():
    bars = flat_bars(80)
    trades = simulate(bars, Scripted({30: Entry(LONG, stop_distance=0.01, max_bars=5)}), context(bars))
    assert trades[0].exit_reason == "TIME"
    assert trades[0].exit_index == 31 + 5


def test_an_order_whose_target_the_open_already_passed_is_not_taken():
    bars = flat_bars(60)
    order = Entry(LONG, stop_distance=0.01, target_price=1.19)  # below the fill
    assert simulate(bars, Scripted({30: order}), context(bars)) == []


def test_swap_is_charged_per_new_york_close_crossed_on_both_sides():
    bars = flat_bars(24 * 6)  # six days of H1
    costs = Costs(spread=0.0, slippage=0.0, commission=0.0, swap_atr_fraction=0.01)
    for d in (LONG, SHORT):
        trade = simulate(bars, Scripted({30: Entry(d, stop_distance=0.05)}, exits=frozenset({30 + 48})),
                         context(bars, costs=costs))[0]
        assert trade.nights == 2
        assert trade.r < 0, "a flat market held two nights must cost swap in either direction"


# -- the families do what the pre-registration says --------------------------------


def test_the_opening_range_trades_at_most_once_a_day():
    bars = regime_walk(96 * 20, seed=9, timeframe="M15")
    ctx = context(bars, costs=Costs(spread=0.00002, slippage=0.0, commission=0.0))
    from bot.research.data import trading_date

    trades = simulate(bars, LondonOpeningRange(), ctx)
    days = [t.entry_time.astimezone(__import__("zoneinfo").ZoneInfo("Europe/London")).date() for t in trades]
    assert trades, "no trades — the test would be vacuous"
    assert len(days) == len(set(days))


def test_intraday_families_never_hold_through_the_new_york_close():
    bars = regime_walk(96 * 20, seed=4, timeframe="M15")
    ctx = context(bars, costs=Costs(spread=0.00002, slippage=0.0, commission=0.0, swap_atr_fraction=0.01))
    for rule in (LondonOpeningRange(), TickVwapReversion()):
        trades = simulate(bars, rule, ctx, close_at_end=False)
        assert all(t.nights == 0 for t in trades), f"{rule.name} held overnight"


def test_the_plateau_parameters_named_in_the_registry_exist_on_every_family():
    for key, (cls, _, params) in FAMILIES.items():
        rule = cls()
        for name in params:
            assert hasattr(rule, name), f"{key} has no parameter {name}"
