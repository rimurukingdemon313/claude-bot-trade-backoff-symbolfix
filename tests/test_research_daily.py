"""The daily research engine: causality, fills, and R accounting.

Every future candidate strategy is judged by this engine, so the tests
here are about the engine being honest, not about any strategy being good.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from bot.marketdata.candles import Candle
from bot.research.daily import (
    CANDIDATES,
    LONG,
    SHORT,
    Costs,
    Donchian,
    MovingAverageTrend,
    TimeSeriesMomentum,
    simulate,
)

START = datetime(2015, 1, 1, tzinfo=timezone.utc)
FREE = Costs(spread=0.0, slippage=0.0, commission=0.0, swap_atr_fraction=0.0)


def bars_from(rows, start=START):
    """Daily candles from (open, high, low, close) rows."""

    return [
        Candle(start + timedelta(days=i), o, h, lo, c, 0.0, "D1")
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def flat(n, price=1.0, width=0.001):
    return [(price, price + width, price - width, price)] * n


def walk(n, seed):
    """Seeded walk. Used ONLY to test structural properties (causality),
    never to argue that a strategy works — rule 10."""

    rng = random.Random(seed)
    price, rows = 1.2, []
    for _ in range(n):
        drift = rng.gauss(0.0, 0.006)
        close = max(0.5, price * (1 + drift))
        high = max(price, close) * (1 + abs(rng.gauss(0, 0.002)))
        low = min(price, close) * (1 - abs(rng.gauss(0, 0.002)))
        rows.append((price, high, low, close))
        price = close
    return bars_from(rows)


# -- causality ---------------------------------------------------------------


def _dummy_trade(direction):
    from bot.research.daily import Trade

    return Trade(
        symbol="X", strategy="t", direction=direction, signal_index=0,
        entry_index=0, entry_time=START, entry=1.0, stop=0.9, atr=0.01,
    )


@pytest.mark.parametrize("rule", CANDIDATES, ids=lambda r: r.name)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_decision_is_identical_when_the_future_does_not_exist(rule, seed):
    """THE no-look-ahead guarantee.

    For every bar i, the rule's entry and exit decisions are computed twice:
    once with the whole history available, once on a copy truncated at i,
    with indicators rebuilt from that copy. In the second, bar i+1 is not
    hidden or ignored — it does not exist. Any decision that differs used
    the future.

    This replaced a weaker test. That one compared trades finished before
    a cut point, and a deliberately injected one-bar peek passed it on
    every seed while inflating avgR from +0.03 to +0.35, because a one-bar
    leak still sees "tomorrow" in both runs except at the cut itself.
    `test_the_causality_test_catches_a_deliberate_one_bar_leak` below
    keeps this one honest.
    """

    from bot.research.daily import indicators

    bars = walk(700, seed)
    ind = indicators(bars)
    trades = {LONG: _dummy_trade(LONG), SHORT: _dummy_trade(SHORT)}
    for i in range(rule.warmup, len(bars) - 1):
        seen = bars[: i + 1]
        ind_seen = indicators(seen)
        assert rule.entry(bars, i, ind) == rule.entry(seen, i, ind_seen), f"entry at {i} saw the future"
        for direction, trade in trades.items():
            assert rule.exit(bars, i, ind, trade) == rule.exit(seen, i, ind_seen, trade), (
                f"exit at {i} saw the future"
            )


def test_the_causality_test_catches_a_deliberate_one_bar_leak():
    """A test that cannot fail proves nothing. This rule peeks one bar
    ahead; the check above must reject it."""

    from dataclasses import dataclass

    from bot.research.daily import indicators

    @dataclass(frozen=True)
    class Cheater(Donchian):
        name: str = "cheater"

        def entry(self, bars, i, ind):
            base = super().entry(bars, i, ind)
            if base and i + 1 < len(bars):
                up = bars[i + 1].close > bars[i].close
                return base if up == (base == LONG) else 0
            return base

    bars = walk(700, 1)
    ind = indicators(bars)
    rule = Cheater()
    leaked = any(
        rule.entry(bars, i, ind) != rule.entry(bars[: i + 1], i, indicators(bars[: i + 1]))
        for i in range(rule.warmup, len(bars) - 1)
    )
    assert leaked, "the causality check failed to catch a one-bar look-ahead"


@pytest.mark.parametrize("rule", CANDIDATES, ids=lambda r: r.name)
def test_trades_finished_before_a_cut_do_not_change_when_history_is_cut(rule):
    """A coarser check that the SIMULATOR (not only the rules) is causal:
    fills, stops and costs for a finished trade cannot depend on bars
    after it. It would not catch a one-bar peek inside a rule — the test
    above does that."""

    full = walk(1200, 5)
    cut = 800
    long_run = simulate(full, rule, FREE, symbol="X", close_at_end=False)
    short_run = simulate(full[:cut], rule, FREE, symbol="X", close_at_end=False)

    def finished_before(trades, limit):
        return [
            (t.direction, t.entry_index, round(t.entry, 12), t.exit_index, round(t.exit, 12))
            for t in trades
            if t.exit_index is not None and t.exit_index < limit
        ]

    assert finished_before(long_run, cut - 1) == finished_before(short_run, cut - 1)
    assert finished_before(long_run, cut - 1), "the check must actually cover trades"


# -- fills -------------------------------------------------------------------


def breakout_rows():
    """56 quiet bars, then a close far above the 55-bar high."""

    rows = flat(56)
    rows.append((1.0, 1.05, 0.999, 1.04))   # bar 56: breakout CLOSE
    rows.append((1.041, 1.06, 1.035, 1.05))  # bar 57: the fill bar
    rows += [(1.05, 1.06, 1.045, 1.055)] * 5
    return rows


def test_the_fill_is_the_next_open_never_the_signal_close():
    costs = Costs(spread=0.0002, slippage=0.0001, commission=0.0, swap_atr_fraction=0.0)
    trades = simulate(bars_from(breakout_rows()), Donchian(), costs, symbol="X")
    first = trades[0]
    assert first.signal_index == 56
    assert first.entry_index == 57, "entered on the bar that produced the signal"
    # Next open, plus half the spread, plus adverse slippage.
    assert first.entry == pytest.approx(1.041 + 0.0001 + 0.0001)


def test_a_stop_the_bar_gaps_through_fills_at_the_open_not_the_stop():
    rows = breakout_rows()[:58]
    rows.append((0.90, 0.91, 0.89, 0.90))  # opens far below any stop
    trades = simulate(bars_from(rows), Donchian(), FREE, symbol="X", close_at_end=False)
    stopped = [t for t in trades if t.exit_reason == "STOP"]
    assert stopped, "the gap should have stopped the position"
    assert stopped[0].exit == pytest.approx(0.90), "filled at the stop, as if the gap did not happen"
    assert stopped[0].exit < stopped[0].stop


def test_a_short_pays_the_spread_the_other_way():
    rows = flat(56)
    rows.append((1.0, 1.001, 0.95, 0.96))    # breakdown close
    rows.append((0.959, 0.96, 0.95, 0.955))  # fill bar
    rows += [(0.955, 0.956, 0.95, 0.952)] * 3
    costs = Costs(spread=0.0002, slippage=0.0001, commission=0.0, swap_atr_fraction=0.0)
    trade = simulate(bars_from(rows), Donchian(), costs, symbol="X")[0]
    assert trade.direction == SHORT
    assert trade.entry == pytest.approx(0.959 - 0.0001 - 0.0001)


# -- R accounting --------------------------------------------------------------


def test_the_engines_r_matches_arithmetic_on_the_bars():
    """R is the move from the actual entry fill, over the INITIAL stop
    distance — checked against the bars, not against the engine's own
    formula. With no costs, a position still open at the end is closed at
    the last close, so its R is fully determined by three prices."""

    bars = bars_from(breakout_rows())
    trade = simulate(bars, Donchian(), FREE, symbol="X")[0]
    assert trade.exit_reason == "END"
    risk = trade.entry - trade.stop
    assert risk == pytest.approx(Donchian().stop_atr * trade.atr)
    assert trade.r == pytest.approx((bars[-1].close - trade.entry) / risk)
    assert trade.r > 0, "price rose after a long entry; R must be positive"


def test_commission_and_swap_come_off_every_trade_in_r():
    rows = breakout_rows() + [(1.055, 1.06, 1.05, 1.055)] * 10
    base = simulate(bars_from(rows), Donchian(), FREE, symbol="X")[0]
    charged = simulate(
        bars_from(rows),
        Donchian(),
        Costs(spread=0.0, slippage=0.0, commission=0.0003, swap_atr_fraction=0.01),
        symbol="X",
    )[0]
    risk = base.entry - base.stop
    expected = base.r - (0.0003 + 0.01 * base.atr * base.bars_held) / risk
    assert charged.r == pytest.approx(expected)
    assert charged.r < base.r


def test_swap_is_charged_on_shorts_too():
    """Retail brokers mark up both sides; the pre-registration charges both."""

    rows = flat(56)
    rows.append((1.0, 1.001, 0.95, 0.96))
    rows += [(0.959, 0.96, 0.95, 0.955)] * 10
    swap = Costs(spread=0.0, slippage=0.0, commission=0.0, swap_atr_fraction=0.01)
    no_swap = simulate(bars_from(rows), Donchian(), FREE, symbol="X")[0]
    with_swap = simulate(bars_from(rows), Donchian(), swap, symbol="X")[0]
    assert with_swap.direction == SHORT
    assert with_swap.r < no_swap.r


# -- the rules do what the pre-registration says ----------------------------------


def test_a_flat_market_produces_no_trades():
    for rule in CANDIDATES:
        assert simulate(bars_from(flat(400)), rule, FREE, symbol="X") == []


def test_momentum_only_decides_on_the_first_bar_of_a_month():
    bars = walk(700, 7)
    rule = TimeSeriesMomentum()
    for trade in simulate(bars, rule, FREE, symbol="X", close_at_end=False):
        signal_bar = bars[trade.signal_index]
        previous = bars[trade.signal_index - 1]
        assert signal_bar.timestamp.month != previous.timestamp.month


def test_the_average_rule_enters_on_the_cross_not_merely_in_the_state():
    """A 3-ATR stop-out mid-trend must not be followed by an immediate
    re-entry just because SMA(50) is still above SMA(200)."""

    rule = MovingAverageTrend()
    bars = walk(1500, 11)
    from bot.research.daily import indicators

    ind = indicators(bars)
    for trade in simulate(bars, rule, FREE, symbol="X", close_at_end=False):
        i = trade.signal_index
        before = ind.sma50[i - 1] - ind.sma200[i - 1]
        now = ind.sma50[i] - ind.sma200[i]
        assert (before > 0) != (now > 0), "entered without a cross on the signal bar"


# -- the dataset clock ---------------------------------------------------------


def test_server_time_is_converted_to_utc_across_daylight_saving():
    """The files are MetaTrader server time (New York + 7h), not UTC.

    Read as UTC, every intraday bar sat two to three hours off, and every
    session filter, rollover blackout and weekend gate ran on the wrong
    hours without anything breaking. The FX week opening at Sunday 22:00
    UTC in winter and 21:00 in summer is what the server's Monday 00:00
    must become.
    """

    from bot.research.data import server_to_utc

    winter = server_to_utc(datetime(2019, 1, 7, 0, 0))
    summer = server_to_utc(datetime(2019, 7, 8, 0, 0))
    assert (winter.strftime("%a %H:%M"), winter.tzinfo) == ("Sun 22:00", timezone.utc)
    assert summer.strftime("%a %H:%M") == "Sun 21:00"
    # London's 08:00 local open is server 10:00 in both seasons.
    assert server_to_utc(datetime(2019, 11, 11, 10, 0)).hour == 8
    assert server_to_utc(datetime(2019, 3, 18, 10, 0)).hour == 7


def test_a_daily_bar_keeps_its_trading_date_after_conversion():
    """A daily bar opens at the New York close, i.e. the previous UTC
    evening. Month logic must use the trading day, not that UTC date."""

    from bot.research.data import server_to_utc, trading_date

    opened = server_to_utc(datetime(2019, 2, 1, 0, 0))
    assert opened.date().isoformat() == "2019-01-31"
    assert trading_date(opened).isoformat() == "2019-02-01"
