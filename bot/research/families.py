"""The eight strategy families of docs/EXPERIMENT_EDGE_PROGRAM.md.

Each is written from the pre-registration's text, with its two plateau
parameters as constructor arguments and every other number fixed. A rule
reads indicator arrays only at index <= i; the causality test in
tests/test_research_engine.py checks every decision against a copy of
history that ends at i.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ..marketdata.candles import Candle
from .daily import LONG, SHORT
from .data import trading_date
from .engine import (
    Context, Entry, Manage, Position,
    atr, efficiency_ratio, ema, prior_extreme, sma, stdev,
)

_LONDON = ZoneInfo("Europe/London")


def _london_minutes(moment: datetime) -> tuple[int, Any]:
    local = moment.astimezone(_LONDON)
    return local.hour * 60 + local.minute, local.date()


def _ok(*values) -> bool:
    return all(v is not None for v in values)


# -- F1 adaptive trend following -------------------------------------------


@dataclass(frozen=True)
class AdaptiveTrend:
    breakout: int = 20
    trail: float = 3.0
    name: str = "F1_adaptive_trend"
    er_min: float = 0.30

    def prepare(self, bars, ctx):
        closes = [b.close for b in bars]
        return {
            "c": closes,
            "fast": ema(closes, 50),
            "slow": ema(closes, 200),
            "er": efficiency_ratio(closes, 20),
            "atr": atr(bars, 20),
            "hh": prior_extreme([b.high for b in bars], self.breakout, highest=True),
            "ll": prior_extreme([b.low for b in bars], self.breakout, highest=False),
        }

    def entry(self, i, bars, pre, ctx):
        if i < 5:
            return None
        f, s, e, a, a5 = pre["fast"][i], pre["slow"][i], pre["er"][i], pre["atr"][i], pre["atr"][i - 5]
        hh, ll, c = pre["hh"][i], pre["ll"][i], pre["c"][i]
        if not _ok(f, s, e, a, a5, hh, ll) or e < self.er_min or not a > a5:
            return None
        if f > s and c > hh:
            return Entry(LONG, stop_distance=2.5 * a, tag="trend")
        if f < s and c < ll:
            return Entry(SHORT, stop_distance=2.5 * a, tag="trend")
        return None

    def manage(self, i, bars, pre, ctx, pos):
        f, s, a, c = pre["fast"][i], pre["slow"][i], pre["atr"][i], pre["c"][i]
        if not _ok(f, s, a):
            return None
        if (pos.direction == LONG and f < s) or (pos.direction == SHORT and f > s):
            return Manage(exit=True)
        if pos.direction == LONG:
            best = max(pos.state.get("best", c), c)
            pos.state["best"] = best
            return Manage(stop=best - self.trail * a)
        best = min(pos.state.get("best", c), c)
        pos.state["best"] = best
        return Manage(stop=best + self.trail * a)


# -- F3 cross-sectional currency momentum ---------------------------------------

CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD")


def align_by_trading_date(series: dict[str, Sequence[Candle]]) -> dict[str, list[Candle]]:
    """Keep only trading dates every pair has, in order."""

    dated = {p: {trading_date(b.timestamp): b for b in bars} for p, bars in series.items()}
    common = sorted(set.intersection(*(set(d) for d in dated.values())))
    return {p: [dated[p][day] for day in common] for p in series}


def cross_sectional_schedule(aligned: dict[str, list[Candle]], lookback: int) -> list[tuple[str, int] | None]:
    """At each aligned bar: the (pair, direction) to hold, or None.

    Only defined on the first bar of each trading week (a rebalance);
    elsewhere None. Built from closes up to and including that bar.
    """

    pairs = sorted(aligned)
    n = len(next(iter(aligned.values())))
    stamps = aligned[pairs[0]]
    out: list[tuple[str, int] | None] = [None] * n
    for i in range(1, n):
        this_week = trading_date(stamps[i].timestamp).isocalendar()[:2]
        last_week = trading_date(stamps[i - 1].timestamp).isocalendar()[:2]
        if this_week == last_week or i < lookback:
            continue
        strength = {c: [] for c in CURRENCIES}
        for p in pairs:
            move = math.log(aligned[p][i].close / aligned[p][i - lookback].close)
            strength[p[:3]].append(move)
            strength[p[3:6]].append(-move)
        score = {c: sum(v) / len(v) for c, v in strength.items() if v}
        best = max(pairs, key=lambda p: abs(score[p[:3]] - score[p[3:6]]))
        gap = score[best[:3]] - score[best[3:6]]
        if gap != 0:
            out[i] = (best, LONG if gap > 0 else SHORT)
    return out


@dataclass(frozen=True)
class CrossSectionalMomentum:
    """Per-pair view of a portfolio schedule held in ctx.extra."""

    lookback: int = 63
    stop_mult: float = 3.0
    name: str = "F3_xs_momentum"

    def prepare(self, bars, ctx):
        return {"atr": atr(bars, 20), "schedule": ctx.extra["schedule"], "pair": ctx.symbol}

    def entry(self, i, bars, pre, ctx):
        pick, a = pre["schedule"][i], pre["atr"][i]
        if pick is None or a is None or pick[0] != pre["pair"]:
            return None
        return Entry(pick[1], stop_distance=self.stop_mult * a, tag="xs")

    def manage(self, i, bars, pre, ctx, pos):
        pick = pre["schedule"][i]
        if pick is not None and pick != (pre["pair"], pos.direction):
            return Manage(exit=True)
        return None


# -- F4 statistical mean reversion -------------------------------------------------


@dataclass(frozen=True)
class ZScoreReversion:
    z: float = 2.5
    lookback: int = 48
    name: str = "F4_zscore_reversion"
    er_max: float = 0.25

    def prepare(self, bars, ctx):
        closes = [b.close for b in bars]
        mean, sd = sma(closes, self.lookback), stdev(closes, self.lookback)
        z = [None if not _ok(m, s) or s == 0 else (c - m) / s for c, m, s in zip(closes, mean, sd)]
        return {"c": closes, "o": [b.open for b in bars], "mean": mean, "z": z,
                "er": efficiency_ratio(closes, self.lookback), "atr": atr(bars, 24)}

    def entry(self, i, bars, pre, ctx):
        if i < 1:
            return None
        z0, z1, e, a, m = pre["z"][i - 1], pre["z"][i], pre["er"][i], pre["atr"][i], pre["mean"][i]
        if not _ok(z0, z1, e, a, m) or e >= self.er_max:
            return None
        c, o = pre["c"][i], pre["o"][i]
        if z0 >= self.z and c < o and z1 < z0:
            return Entry(SHORT, stop_distance=1.5 * a, target_price=m, max_bars=48, tag="mr")
        if z0 <= -self.z and c > o and z1 > z0:
            return Entry(LONG, stop_distance=1.5 * a, target_price=m, max_bars=48, tag="mr")
        return None

    def manage(self, i, bars, pre, ctx, pos):
        return None


# -- F5 volatility breakout -----------------------------------------------------------


@dataclass(frozen=True)
class VolatilityBreakout:
    ratio: float = 0.75
    lookback: int = 20
    name: str = "F5_volatility_breakout"

    def prepare(self, bars, ctx):
        return {"c": [b.close for b in bars], "a10": atr(bars, 10), "a50": atr(bars, 50),
                "a20": atr(bars, 20),
                "hh": prior_extreme([b.high for b in bars], self.lookback, highest=True),
                "ll": prior_extreme([b.low for b in bars], self.lookback, highest=False)}

    def entry(self, i, bars, pre, ctx):
        if i < 1:
            return None
        p10, p50, n10, a20 = pre["a10"][i - 1], pre["a50"][i - 1], pre["a10"][i], pre["a20"][i]
        hh, ll, c = pre["hh"][i], pre["ll"][i], pre["c"][i]
        if not _ok(p10, p50, n10, a20, hh, ll) or p50 == 0:
            return None
        if not (p10 / p50 < self.ratio and n10 > p10):
            return None
        if c > hh:
            return Entry(LONG, stop_distance=1.5 * a20, target_r=2.0, max_bars=48, tag="breakout")
        if c < ll:
            return Entry(SHORT, stop_distance=1.5 * a20, target_r=2.0, max_bars=48, tag="breakout")
        return None

    def manage(self, i, bars, pre, ctx, pos):
        return None


# -- F6 tick-VWAP mean reversion ---------------------------------------------------------


@dataclass(frozen=True)
class TickVwapReversion:
    deviation: float = 2.0
    er_max: float = 0.30
    name: str = "F6_tick_vwap_reversion"

    def prepare(self, bars, ctx):
        vwap: list[float | None] = [None] * len(bars)
        session: list[Any] = [None] * len(bars)
        start_min: list[int] = [0] * len(bars)
        end_min: list[int] = [0] * len(bars)
        pv = vol = 0.0
        current = None
        for i, b in enumerate(bars):
            minute, day = _london_minutes(b.timestamp)
            start_min[i] = minute
            end_min[i], _ = _london_minutes(b.close_time)
            if minute >= 8 * 60:
                if day != current:
                    current, pv, vol = day, 0.0, 0.0
                weight = b.volume if b.volume > 0 else 1.0
                pv += (b.high + b.low + b.close) / 3.0 * weight
                vol += weight
                vwap[i] = pv / vol
                session[i] = day
        closes = [b.close for b in bars]
        return {"c": closes, "vwap": vwap, "session": session, "start": start_min, "end": end_min,
                "atr": atr(bars, 20), "er": efficiency_ratio(closes, 32)}

    def _dev(self, pre, i):
        v, a = pre["vwap"][i], pre["atr"][i]
        return None if not _ok(v, a) or a == 0 else (pre["c"][i] - v) / a

    def entry(self, i, bars, pre, ctx):
        if i < 1 or not (10 * 60 <= pre["start"][i] < 16 * 60):
            return None
        if pre["session"][i] is None or pre["session"][i] != pre["session"][i - 1]:
            return None
        d0, a, e = self._dev(pre, i - 1), pre["atr"][i], pre["er"][i]
        if not _ok(d0, a, e) or e >= self.er_max or ctx.costs.spread > 0.25 * a:
            return None
        c, c0, v = pre["c"][i], pre["c"][i - 1], pre["vwap"][i]
        if d0 >= self.deviation and c < c0:
            return Entry(SHORT, stop_distance=1.0 * a, target_price=v, tag="vwap")
        if d0 <= -self.deviation and c > c0:
            return Entry(LONG, stop_distance=1.0 * a, target_price=v, tag="vwap")
        return None

    def manage(self, i, bars, pre, ctx, pos):
        if pre["end"][i] >= 17 * 60 or pre["session"][i] != pre["session"][pos.entry_index]:
            return Manage(exit=True)
        return None


# -- F7 regime switching -----------------------------------------------------------------


@dataclass(frozen=True)
class RegimeSwitching:
    trend_er: float = 0.35
    range_er: float = 0.25
    name: str = "F7_regime_switching"

    def _parts(self):
        return (AdaptiveTrend(), ZScoreReversion(er_max=self.range_er), VolatilityBreakout())

    def prepare(self, bars, ctx):
        trend, mr, brk = self._parts()
        closes = [b.close for b in bars]
        return {"trend": trend.prepare(bars, ctx), "mr": mr.prepare(bars, ctx),
                "brk": brk.prepare(bars, ctx), "a10": atr(bars, 10), "a50": atr(bars, 50),
                "er": efficiency_ratio(closes, 48)}

    def regime(self, i, pre) -> str | None:
        if i < 1:
            return None
        a10, a50, e = pre["a10"][i - 1], pre["a50"][i - 1], pre["er"][i - 1]
        if not _ok(a10, a50, e) or a50 == 0:
            return None
        x = a10 / a50
        if x > 1.8:
            return "EXTREME"
        if x < 0.75:
            return "COMPRESSED"
        if e >= self.trend_er:
            return "TRENDING"
        if e < self.range_er:
            return "RANGING"
        return None

    def entry(self, i, bars, pre, ctx):
        trend, mr, brk = self._parts()
        state = self.regime(i, pre)
        if state == "COMPRESSED":
            return brk.entry(i, bars, pre["brk"], ctx)
        if state == "TRENDING":
            return trend.entry(i, bars, pre["trend"], ctx)
        if state == "RANGING":
            return mr.entry(i, bars, pre["mr"], ctx)
        return None

    def manage(self, i, bars, pre, ctx, pos):
        if pos.tag == "trend":
            return AdaptiveTrend().manage(i, bars, pre["trend"], ctx, pos)
        return None


# -- F9 trend pullback ----------------------------------------------------------------------


@dataclass(frozen=True)
class TrendPullback:
    pullback_ema: int = 20
    target_r: float = 2.0
    name: str = "F9_trend_pullback"

    def prepare(self, bars, ctx):
        closes = [b.close for b in bars]
        return {"c": closes, "h": [b.high for b in bars], "l": [b.low for b in bars],
                "fast": ema(closes, 50), "slow": ema(closes, 200),
                "pull": ema(closes, self.pullback_ema), "atr": atr(bars, 14)}

    def entry(self, i, bars, pre, ctx):
        if i < 1:
            return None
        f, s, p0, p1, f0, a = (pre["fast"][i], pre["slow"][i], pre["pull"][i - 1],
                               pre["pull"][i], pre["fast"][i - 1], pre["atr"][i])
        if not _ok(f, s, p0, p1, f0, a):
            return None
        c, c0, h0, l0 = pre["c"][i], pre["c"][i - 1], pre["h"][i - 1], pre["l"][i - 1]
        order = dict(stop_distance=2.0 * a, target_r=self.target_r, max_bars=30, tag="pullback")
        if f > s and l0 <= p0 and c0 > f0 and c > h0 and c > p1:
            return Entry(LONG, **order)
        if f < s and h0 >= p0 and c0 < f0 and c < l0 and c < p1:
            return Entry(SHORT, **order)
        return None

    def manage(self, i, bars, pre, ctx, pos):
        return None


# -- F10 London opening-range breakout -------------------------------------------------------


@dataclass(frozen=True)
class LondonOpeningRange:
    range_bars: int = 4
    target_mult: float = 1.5
    name: str = "F10_london_orb"

    def prepare(self, bars, ctx):
        start = [0] * len(bars)
        end = [0] * len(bars)
        day = [None] * len(bars)
        for i, b in enumerate(bars):
            start[i], day[i] = _london_minutes(b.timestamp)
            end[i], _ = _london_minutes(b.close_time)
        open_at, close_at = 8 * 60, 8 * 60 + 15 * self.range_bars
        # The range for a day is known once its last range bar has closed;
        # only then, and only from bars of that same day, is it used.
        rng: dict[Any, list[float]] = {}
        known_from: dict[Any, int] = {}
        for i in range(len(bars)):
            if open_at <= start[i] < close_at:
                hi_lo = rng.setdefault(day[i], [-math.inf, math.inf, 0])
                hi_lo[0] = max(hi_lo[0], bars[i].high)
                hi_lo[1] = min(hi_lo[1], bars[i].low)
                hi_lo[2] += 1
                if hi_lo[2] == self.range_bars:
                    known_from[day[i]] = i
        return {"c": [b.close for b in bars], "start": start, "end": end, "day": day,
                "range": rng, "known": known_from, "window": (close_at, 12 * 60)}

    def entry(self, i, bars, pre, ctx):
        lo_w, hi_w = pre["window"]
        today = pre["day"][i]
        if not (lo_w <= pre["start"][i] < hi_w):
            return None
        known = pre["known"].get(today)
        if known is None or known >= i:
            return None
        high, low, _ = pre["range"][today]
        width = high - low
        daily = ctx.daily_atr_at(bars[i].close_time)
        if daily is None or width < 5 * ctx.costs.spread or width > 1.0 * daily:
            return None
        # Only the FIRST close of today's window beyond the range may signal.
        for j in range(known + 1, i):
            if pre["day"][j] == today and lo_w <= pre["start"][j] < hi_w:
                if pre["c"][j] > high or pre["c"][j] < low:
                    return None
        c = pre["c"][i]
        if c > high:
            return Entry(LONG, stop_price=low, target_distance=self.target_mult * width, tag="orb")
        if c < low:
            return Entry(SHORT, stop_price=high, target_distance=self.target_mult * width, tag="orb")
        return None

    def manage(self, i, bars, pre, ctx, pos):
        if pre["end"][i] >= 16 * 60 or pre["day"][i] != pre["day"][pos.entry_index]:
            return Manage(exit=True)
        return None


FAMILIES = {
    "F1": (AdaptiveTrend, "D1", ("breakout", "trail")),
    "F3": (CrossSectionalMomentum, "D1", ("lookback", "stop_mult")),
    "F4": (ZScoreReversion, "H1", ("z", "lookback")),
    "F5": (VolatilityBreakout, "H1", ("ratio", "lookback")),
    "F6": (TickVwapReversion, "M15", ("deviation", "er_max")),
    "F7": (RegimeSwitching, "H1", ("trend_er", "range_er")),
    "F9": (TrendPullback, "H4", ("pullback_ema", "target_r")),
    "F10": (LondonOpeningRange, "M15", ("range_bars", "target_mult")),
}
