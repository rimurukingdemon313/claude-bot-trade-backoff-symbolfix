"""Sweep reversion: the high-frequency mode.

SMC mode is a structure-continuation strategy. It demands H4, H1 and M15
agreement, a break of structure, displacement, and an entry back inside a
fresh imbalance. Every one of those is a filter, and together they are why
it stands aside most days — correctly, but rarely.

This mode trades a different, far more common event with the SAME measured
primitives. Nothing new is invented (project rule 12): the sweeps, the
dealing range, the regime, the ATR and the session engine are the ones
already written and tested for SMC.

The thesis, in one line: price reaches past a level where stops rest, does
not hold there, and returns into the range it came from.

  1. a liquidity level is swept — price trades beyond it;
  2. the candle closes back inside, rejecting the excursion;
  3. the trade is taken AGAINST the sweep, toward the middle of the range;
  4. the stop sits beyond the wick, because the thesis is dead if price
     goes back out there;
  5. the target is a fixed multiple of that risk, so the R:R is known
     before the trade rather than hostage to where the nearest pool sits.

Why it fires more often: no displacement is required, no break of
structure is required, and a ranging M15 — which SMC rejects outright as
"no confirmed directional structure", the single largest cause of NO
TRADE — is the regime this strategy is FOR.

Where it stays disciplined: it will not fade a higher-timeframe trend.
When H4 has a direction, only sweeps that resolve WITH it are taken (a
sell-side sweep in a bullish H4 is a dip, not a top). When H4 is ranging,
either side is allowed. Fading a strong trend is how a high-hit-rate
strategy earns its one catastrophic loss.

Nothing here promises a win rate. A tighter target is hit more often than
a distant one, which is arithmetic; whether that nets out ahead of SMC on
this account is a question only the trade history can answer, and it has
not been asked yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import R_EPSILON, TradingConfig
from ..marketdata.provider import Series
from ..smc.dealing_range import dealing_range
from ..smc.engine import SetupCandidate, SmcEngine, SmcResult, TimeframeAnalysis
from ..smc.liquidity import LiquiditySweep
from ..smc.mtf import NO_TRADE, RANGE_ROTATION, TRADE
from ..smc.sessions import classify_session
from .base import StrategyProfile

PROFILE = StrategyProfile(
    key="reversion",
    name="Sweep reversion",
    description=(
        "Fades a failed run past a liquidity level, back toward the middle of the range. "
        "Trades ranging conditions that the SMC mode rejects outright, so it fires far more "
        "often for a smaller target."
    ),
    # The build's own floor. Deliberately not lower: a scalp is allowed to
    # aim closer than a swing setup, it is not allowed to escape the limit
    # every trade in this system is held to.
    min_risk_reward=1.5,
    expected_frequency="several a day across a wide symbol list",
    thesis="price that cannot hold beyond a swept level returns into its range",
)


@dataclass(frozen=True, slots=True)
class ReversionConfig:
    """Thresholds, kept few on purpose.

    Every parameter is another degree of freedom for a backtest to fit
    against (project rule 12), so there are four and each one answers a
    question that cannot be skipped.
    """

    #: How decisively the sweep candle must close back inside. A 0.5 wick
    #: means half the candle's range was rejected.
    min_rejection_ratio: float = 0.5
    #: How recent the sweep must be, in confirmed bars. Older than this and
    #: the reversion has either happened already or failed.
    max_age_bars: int = 3
    #: Stop buffer beyond the sweep extreme, in ATR.
    stop_buffer_atr: float = 0.35
    #: How close to a range extreme price may be and still be traded
    #: toward it. Buying the top 15% of a range is not a reversion, it is
    #: the thing a reversion fades.
    min_range_position: float = 0.15


class ReversionStrategy:
    """The high-frequency mode. Same output type as the SMC engine."""

    profile = PROFILE

    def __init__(self, config: TradingConfig, params: ReversionConfig | None = None) -> None:
        self.config = config
        self.params = params or ReversionConfig()
        # Reused wholesale: the per-timeframe analysis is identical work,
        # and duplicating it would be a second implementation to keep
        # correct. Only the candidate construction differs.
        self.smc = SmcEngine(config)

    def analyze(
        self, symbol: str, series: dict[str, Series], *, now: datetime | None = None
    ) -> SmcResult:
        analyses = {
            timeframe: self.smc.analyze_timeframe(list(data.candles), timeframe=timeframe, now=now)
            for timeframe, data in series.items()
        }
        candidate, rejection = self.build_candidate(symbol, analyses, now=now)
        return SmcResult(
            symbol=symbol,
            analyses=analyses,
            candidate=candidate,
            rejection=rejection,
            state=TRADE if candidate is not None else NO_TRADE,
        )

    # -- candidate --------------------------------------------------------

    def build_candidate(
        self, symbol: str, analyses: dict[str, TimeframeAnalysis], *, now: datetime | None = None
    ) -> tuple[SetupCandidate | None, str | None]:
        m15 = analyses.get("M15")
        h1 = analyses.get("H1")
        h4 = analyses.get("H4")
        if m15 is None or h1 is None or h4 is None:
            return None, "multi-timeframe analysis incomplete (H4, H1 and M15 are all required)"

        index = m15.last_index
        price = m15.price
        moment = now or m15.candles[-1].close_time
        session = classify_session(moment, self.config.sessions)

        if session.weekend:
            return None, "forex market is closed for the weekend"
        if not m15.regime.tradeable:
            return None, f"M15 regime not tradeable: {m15.regime.note}"
        if m15.atr <= 0:
            return None, "ATR is zero — cannot normalise structure or size a stop"

        sweep = self._recent_sweep(m15, index)
        if sweep is None:
            return None, (
                f"no liquidity sweep confirmed within the last {self.params.max_age_bars} bars "
                f"with at least {self.params.min_rejection_ratio:.0%} rejection"
            )

        # A bullish sweep took SELL-side liquidity (the lows) and points up.
        direction = "BUY" if sweep.direction == "bullish" else "SELL"

        allowed, veto = self._htf_permits(direction, h4, h1)
        if not allowed:
            return None, veto

        # The dealing range is a QUALITY FILTER, not the target. Measuring
        # showed why: after a sweep at the end of a leg, price often sits
        # near the extreme it just made, so the range middle lies BEHIND
        # the trade and there is nothing to aim at. Aiming at a fixed
        # multiple of the risk is always available and is what a scalp
        # wants anyway; the range is used to refuse the bad half of it —
        # buying at the top, selling at the bottom.
        drange = m15.dealing_range or dealing_range(m15.swings, index, price)

        if drange is not None and drange.size > 0:
            position = drange.position
            if direction == "BUY" and position > 1.0 - self.params.min_range_position:
                return None, (
                    f"BUY reversion refused: price is at {position:.0%} of the dealing range, "
                    "too close to the top to be buying"
                )
            if direction == "SELL" and position < self.params.min_range_position:
                return None, (
                    f"SELL reversion refused: price is at {position:.0%} of the dealing range, "
                    "too close to the bottom to be selling"
                )

        levels = self._price_levels(direction, sweep, m15, price, drange)
        if levels is None:
            return None, "the sweep wick sits the wrong side of price — no stop to place"
        entry, stop_loss, take_profit = levels

        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            return None, "stop distance resolved to zero"
        risk_reward = abs(take_profit - entry) / stop_distance
        if risk_reward < self.profile.min_risk_reward - R_EPSILON:
            return None, (
                f"reversion R:R is 1:{risk_reward:.2f}, below the required "
                f"1:{self.profile.min_risk_reward:g}"
            )

        return (
            SetupCandidate(
                symbol=symbol,
                direction=direction,
                entry=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=risk_reward,
                stop_distance=stop_distance,
                atr=m15.atr,
                session=session,
                regime=m15.regime,
                htf_bias=h4.bias,
                h1_bias=h1.bias,
                m15_bias=m15.bias,
                alignment="counter",
                setup_type=RANGE_ROTATION,
                # Named for what it is: this mode fades a stretched move
                # back toward the range, which is a rotation whatever the
                # higher timeframes are doing. Leaving it unclassified
                # would now be refused by the scorer, and rightly - a
                # classification the scorer cannot place is not scored on
                # a guess. `score_floor` stays 0 so the build's B tier
                # remains this mode's bar: naming the setup honestly must
                # not silently retune a mode that was working.
                sweep=sweep,
                structure_event=None,
                displacement=None,
                point_of_interest=None,
                dealing_range=drange,
                liquidity_target={
                    "kind": "risk_multiple",
                    "price": take_profit,
                    "multiple": self.profile.min_risk_reward,
                },
                evidence=(
                    f"{direction} reversion after a {sweep.level.label} sweep",
                    f"rejection {sweep.rejection_ratio:.0%}, quality {sweep.quality:.2f}",
                    f"target is 1:{self.profile.min_risk_reward:g} of the swept-wick stop",
                    f"H4 {h4.bias}, H1 {h1.bias}, M15 {m15.bias}",
                ),
                timestamp=m15.candles[index].close_time,
            ),
            None,
        )

    # -- helpers ----------------------------------------------------------

    def _recent_sweep(self, m15: TimeframeAnalysis, index: int) -> LiquiditySweep | None:
        """The newest sweep this bar is allowed to know about.

        `confirmed_index` is the bar at which the sweep became knowable.
        Filtering on it is what keeps this strategy free of look-ahead
        (project rule 4) — a sweep confirmed by a bar we have not reached
        is not evidence, it is the future.
        """

        usable = [
            sweep
            for sweep in m15.sweeps
            if sweep.confirmed_index <= index
            and index - sweep.confirmed_index <= self.params.max_age_bars
            and sweep.rejection_ratio >= self.params.min_rejection_ratio
        ]
        if not usable:
            return None
        return max(usable, key=lambda s: (s.confirmed_index, s.quality))

    def _htf_permits(
        self, direction: str, h4: TimeframeAnalysis, h1: TimeframeAnalysis
    ) -> tuple[bool, str]:
        """Never fade a higher timeframe that has made up its mind.

        A ranging H4 has no opinion, so either side is available — and that
        is the common case, which is where the frequency comes from. But a
        trending H4 is not something a reversion trade should stand in
        front of: the one loss that does not come back is the one taken
        against the dominant flow.

        This is DELIBERATELY not `bot.smc.mtf`, and it is not a leftover of
        the rigid agreement rule that module replaced. The SMC engine takes
        a turn against the primary bias when the turn has earned the name
        reversal — a swept level of real significance, displacement away
        from it, a CHoCH. This mode has no such trigger to appeal to: it
        fades a stretched move on the strength of the stretch alone, and a
        stretch is not evidence of a turn. So it keeps the stricter rule,
        and it applies only while this strategy is the selected one. Both
        strategies then meet the same risk engine, unchanged (rule 14).
        """

        opposing = "bearish" if direction == "BUY" else "bullish"

        if h4.bias == opposing:
            return False, (
                f"{direction} reversion refused: H4 is {h4.bias} and a reversion trade "
                "may not fade a decided higher timeframe"
            )
        if h4.bias == "range" and h1.bias == opposing:
            return False, (
                f"{direction} reversion refused: H4 has no bias and H1 is {h1.bias} — "
                "nothing supports the reversion"
            )
        return True, ""

    def _price_levels(
        self,
        direction: str,
        sweep: LiquiditySweep,
        m15: TimeframeAnalysis,
        price: float,
        drange: Any,
    ) -> tuple[float, float, float] | None:
        """Entry at the market, stop beyond the wick, target at a fixed R.

        The stop is the honest part: the thesis is "price could not hold
        beyond that level", so the trade is wrong the moment price goes
        back out there, and the stop belongs just past the wick.

        The target is then a multiple of whatever that distance turned out
        to be — which is what makes the R:R known before the trade rather
        than hostage to where the nearest pool happens to sit. When the
        range middle lies FURTHER away than the projection, the projection
        is kept: a scalp taking a tight target more often is the whole
        point of this mode, and reaching for the extra is the other
        strategy's job.
        """

        buffer = m15.atr * self.params.stop_buffer_atr
        swept_candle = m15.candles[sweep.index]
        entry = price
        reach = self.profile.min_risk_reward

        if direction == "BUY":
            stop_loss = swept_candle.low - buffer
            if stop_loss >= entry:
                return None
            take_profit = entry + (entry - stop_loss) * reach
        else:
            stop_loss = swept_candle.high + buffer
            if stop_loss <= entry:
                return None
            take_profit = entry - (stop_loss - entry) * reach

        return entry, stop_loss, take_profit
