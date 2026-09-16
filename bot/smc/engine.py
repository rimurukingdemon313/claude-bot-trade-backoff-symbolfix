"""Two-timeframe SMC engine.

    H1 trend -> M15 execution
      -> liquidity map -> sweep -> displacement -> BOS/CHoCH
      -> FVG/OB retracement -> premium/discount -> priced candidate

Direction and the bar it must clear come from `bot.smc.mtf`, which
classifies the setup against the H1 trend instead of requiring the
timeframes to agree. Read that module for why: in short, unanimity is not
what makes a setup good, and demanding it threw away the ordinary case of
a pullback entry inside a trend.

This engine then does the part that unanimity was never a substitute for:
finding a real M15 trigger, an entry zone price has actually reached, and
levels that are structurally consistent.

The output is a SetupCandidate whose entry, stop and target are derived
DETERMINISTICALLY from market structure. That remains the single most
important property here — an earlier build had the AI invent the prices
and submitted them to the broker verbatim.

Stop placement is structural: behind the sweep extreme or the far edge of
the point of interest, plus an ATR buffer. Target is the next opposing
liquidity pool when one exists, otherwise an R-multiple projection.

Nothing in this file approves a trade or sizes one. `risk/engine.py` is
the only code that does (project rule 2), and a classification can demand
more evidence than the build's floor but never less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..config import SmcConfig, TradingConfig
from ..marketdata.candles import Candle
from ..marketdata.provider import Series
from .dealing_range import DealingRange, dealing_range
from .displacement import Displacement, detect_displacement
from .fvg import FairValueGap, best_entry_gap, detect_fair_value_gaps
from .indicators import atr
from .liquidity import LiquidityMap, LiquiditySweep, build_liquidity_map, detect_sweeps
from .mtf import (
    NO_TRADE,
    TRADE,
    VALID_SETUP,
    MtfDecision,
    decide as decide_mtf,
)
from .orderblocks import OrderBlock, best_entry_block, detect_order_blocks
from .regime import Regime, classify_regime
from .sessions import SessionState, classify_session
from .structure import StructureEvent, detect_structure_events, structural_bias
from .swings import SwingSet, detect_swings


@dataclass(frozen=True, slots=True)
class TimeframeAnalysis:
    timeframe: str
    candles: tuple[Candle, ...]
    swings: SwingSet
    structure_events: tuple[StructureEvent, ...]
    displacements: tuple[Displacement, ...]
    gaps: tuple[FairValueGap, ...]
    order_blocks: tuple[OrderBlock, ...]
    liquidity: LiquidityMap
    sweeps: tuple[LiquiditySweep, ...]
    regime: Regime
    dealing_range: DealingRange | None
    bias: str
    bias_rationale: str
    atr: float

    @property
    def last_index(self) -> int:
        return len(self.candles) - 1

    @property
    def price(self) -> float:
        return self.candles[-1].close

    def as_dict(self, detail: int = 4) -> dict[str, Any]:
        index = self.last_index
        return {
            "timeframe": self.timeframe,
            "bias": self.bias,
            "rationale": self.bias_rationale,
            "price": self.price,
            "atr": self.atr,
            "candles": len(self.candles),
            "regime": self.regime.as_dict(),
            "dealingRange": self.dealing_range.as_dict() if self.dealing_range else None,
            "structure": [event.as_dict() for event in self.structure_events[-detail:]],
            "sweeps": [sweep.as_dict() for sweep in self.sweeps[-detail:]],
            "fairValueGaps": [
                gap.as_dict() for gap in self.gaps if gap.is_live(index, 60)
            ][-detail:],
            "orderBlocks": [
                block.as_dict() for block in self.order_blocks if block.is_live(index, 60)
            ][-detail:],
            "liquidity": self.liquidity.as_dict(index),
        }


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    """A structurally complete, priced trade idea — before risk sizing."""

    symbol: str
    direction: str           # BUY | SELL
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    stop_distance: float
    atr: float
    session: SessionState
    regime: Regime
    h1_bias: str
    m15_bias: str
    alignment: str           # aligned | partial | counter
    sweep: LiquiditySweep | None
    structure_event: StructureEvent | None
    displacement: Displacement | None
    point_of_interest: dict[str, Any] | None
    dealing_range: DealingRange | None
    liquidity_target: dict[str, Any] | None
    #: How this setup relates to the H1 trend: CONTINUATION,
    #: RANGE_ROTATION or REVERSAL. Recorded on every trade, because
    #: averaging a reversal's results with a continuation's describes
    #: neither.
    setup_type: str = "CONTINUATION"
    #: Minimum total score this classification must reach. Set by the MTF
    #: layer and only ever ABOVE the configured B tier - a classification
    #: can demand more evidence, never less.
    score_floor: float = 0.0
    evidence: tuple[str, ...] = field(default_factory=tuple)
    timestamp: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry": self.entry,
            "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit,
            "riskReward": round(self.risk_reward, 3),
            "stopDistance": self.stop_distance,
            "atr": self.atr,
            "session": self.session.as_dict(),
            "regime": self.regime.as_dict(),
            "h1Bias": self.h1_bias,
            "m15Bias": self.m15_bias,
            "alignment": self.alignment,
            "setupType": self.setup_type,
            "scoreFloor": round(self.score_floor, 2),
            "sweep": self.sweep.as_dict() if self.sweep else None,
            "structureEvent": self.structure_event.as_dict() if self.structure_event else None,
            "displacement": self.displacement.as_dict() if self.displacement else None,
            "pointOfInterest": self.point_of_interest,
            "dealingRange": self.dealing_range.as_dict() if self.dealing_range else None,
            "liquidityTarget": self.liquidity_target,
            "evidence": list(self.evidence),
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class SmcResult:
    symbol: str
    analyses: dict[str, TimeframeAnalysis]
    candidate: SetupCandidate | None
    rejection: str | None
    #: NO_TRADE | WATCH | VALID_SETUP | TRADE. Distinguishing "nothing is
    #: happening here" from "context is right, the trigger has not fired"
    #: is the difference between a screen an operator can read and a wall
    #: of identical refusals. A candidate is still the ONLY thing that can
    #: become an order: a state is a label, never a permission.
    state: str = "NO_TRADE"
    mtf: MtfDecision | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframes": {name: analysis.as_dict() for name, analysis in self.analyses.items()},
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "rejection": self.rejection,
            "state": self.state,
            "mtf": self.mtf.as_dict() if self.mtf else None,
        }


def _signal_state(candidate: SetupCandidate | None, decision: MtfDecision | None) -> str:
    """Label the result so an operator can tell the refusals apart.

    A label, never a permission: only a candidate can become an order, and
    every gate downstream still runs. Rule 8 stands - NO TRADE is the
    expected answer, and WATCH exists so the expected answer is legible
    rather than a wall of identical lines.
    """

    if candidate is not None:
        return TRADE
    if decision is None:
        return NO_TRADE
    if decision.tradeable:
        # Classified and directional, but entry conditions were not met.
        return VALID_SETUP
    return decision.state


class SmcEngine:
    def __init__(self, config: TradingConfig) -> None:
        self.config = config
        self.smc: SmcConfig = config.smc

    # -- per timeframe ---------------------------------------------------

    def analyze_timeframe(self, candles: Sequence[Candle], *, timeframe: str, now: datetime | None = None) -> TimeframeAnalysis:
        window = self.smc.swing_window if timeframe == "M15" else self.smc.htf_swing_window
        swings = detect_swings(candles, window)
        displacements = detect_displacement(
            candles,
            atr_period=self.smc.atr_period,
            atr_multiple=self.smc.displacement_atr_multiple,
            body_ratio=self.smc.displacement_body_ratio,
        )
        structure_events = detect_structure_events(
            candles,
            swings,
            displacements,
            atr_period=self.smc.atr_period,
            buffer_atr=self.smc.bos_atr_buffer,
        )
        gaps = detect_fair_value_gaps(
            candles,
            displacements,
            atr_period=self.smc.atr_period,
            min_size_atr=self.smc.fvg_min_atr_fraction,
        )
        order_blocks = detect_order_blocks(
            candles, displacements, structure_events, gaps, atr_period=self.smc.atr_period
        )
        liquidity = build_liquidity_map(
            candles,
            swings,
            atr_period=self.smc.atr_period,
            equal_tolerance_atr=self.smc.equal_level_atr_fraction,
            now=now,
        )
        sweeps = detect_sweeps(
            candles,
            liquidity,
            displacements,
            structure_events,
            atr_period=self.smc.atr_period,
            reaction_candles=self.smc.sweep_reaction_candles,
        )
        regime = classify_regime(candles, structure_events, atr_period=self.smc.atr_period)
        last_index = len(candles) - 1
        bias, rationale = structural_bias(structure_events, swings, last_index, sweeps)
        return TimeframeAnalysis(
            timeframe=timeframe,
            candles=tuple(candles),
            swings=swings,
            structure_events=tuple(structure_events),
            displacements=tuple(displacements),
            gaps=tuple(gaps),
            order_blocks=tuple(order_blocks),
            liquidity=liquidity,
            sweeps=tuple(sweeps),
            regime=regime,
            dealing_range=dealing_range(swings, last_index, candles[-1].close),
            bias=bias,
            bias_rationale=rationale,
            atr=atr(candles, self.smc.atr_period),
        )

    # -- multi timeframe -------------------------------------------------

    def analyze(
        self, symbol: str, series: dict[str, Series], *, now: datetime | None = None
    ) -> SmcResult:
        analyses = {
            timeframe: self.analyze_timeframe(
                list(data.candles), timeframe=timeframe, now=now
            )
            for timeframe, data in series.items()
        }
        candidate, rejection, decision = self.evaluate(symbol, analyses, now=now)
        return SmcResult(
            symbol=symbol,
            analyses=analyses,
            candidate=candidate,
            rejection=rejection,
            state=_signal_state(candidate, decision),
            mtf=decision,
        )

    def build_candidate(
        self, symbol: str, analyses: dict[str, TimeframeAnalysis], *, now: datetime | None = None
    ) -> tuple[SetupCandidate | None, str | None]:
        """Backwards-compatible view of `evaluate` for callers that only
        need the candidate and the reason there isn't one."""

        candidate, rejection, _ = self.evaluate(symbol, analyses, now=now)
        return candidate, rejection

    def evaluate(
        self, symbol: str, analyses: dict[str, TimeframeAnalysis], *, now: datetime | None = None
    ) -> tuple[SetupCandidate | None, str | None, MtfDecision | None]:
        m15 = analyses.get("M15")
        h1 = analyses.get("H1")
        if m15 is None or h1 is None:
            return None, "multi-timeframe analysis incomplete (H1 and M15 are both required)", None

        index = m15.last_index
        price = m15.price
        moment = now or m15.candles[-1].close_time
        session = classify_session(moment, self.config.sessions)

        if session.weekend:
            return None, "forex market is closed for the weekend", None
        if not m15.regime.tradeable:
            return None, f"M15 regime not tradeable: {m15.regime.note}", None
        if m15.atr <= 0:
            return None, "ATR is zero — cannot normalise structure or size a stop", None

        # --- H1 trend, M15 execution: classified, not unanimous ---
        decision, evidence = decide_mtf(
            h1=h1,
            m15=m15,
            index=index,
            smc=self.smc,
            mtf=self.config.mtf,
            tier_b=self.config.scoring.tier_b,
        )
        if decision.direction is None or evidence is None:
            return None, decision.rationale, decision

        direction = decision.direction
        alignment = decision.alignment
        wanted = decision.wanted or ("bullish" if direction == "BUY" else "bearish")
        sweep = evidence.sweep
        structure_event = evidence.structure_event
        displacement = evidence.displacement
        reference_index = max(
            sweep.index if sweep else -1, structure_event.index if structure_event else -1
        )

        # --- the entry zone: an unmitigated FVG or order block ---
        gap = best_entry_gap(
            m15.gaps,
            direction=direction,
            at_index=index,
            max_age=self.smc.fvg_max_age_candles,
            reference_index=reference_index,
        )
        block = best_entry_block(
            m15.order_blocks,
            direction=direction,
            at_index=index,
            max_age=self.smc.ob_max_age_candles,
            reference_index=reference_index,
        )
        poi, poi_kind = self._choose_poi(gap, block)
        if poi is None:
            return None, "no live fair value gap or order block to enter from", decision

        # Entry requires price to actually be AT the point of interest.
        # Without this gate the engine would chase an extended move and
        # anchor its stop at the far side of the impulse leg, producing a
        # stop several ATR wide for no structural reason.
        proximity = m15.atr * 0.35
        if not (poi.lower - proximity <= price <= poi.upper + proximity):
            # Structurally valid, entry conditions not yet met: this is a
            # VALID_SETUP to watch, not a refusal of the idea.
            return (
                None,
                (
                    f"price {price:.5f} has not retraced into the {poi_kind} zone "
                    f"[{poi.lower:.5f}, {poi.upper:.5f}] — waiting rather than chasing"
                ),
                decision,
            )

        # --- deterministic levels ---
        levels = self._build_levels(
            direction=direction,
            price=price,
            atr_value=m15.atr,
            sweep=sweep,
            poi=poi,
            analysis=m15,
            index=index,
        )
        if levels is None:
            return None, "could not construct a structurally valid stop and target", decision
        entry, stop_loss, take_profit, target_level = levels

        stop_distance = abs(entry - stop_loss)
        reward_distance = abs(take_profit - entry)
        if stop_distance <= 0:
            return None, "stop distance resolved to zero", decision

        stop_atr = stop_distance / m15.atr
        if stop_atr < self.config.risk.min_stop_distance_atr:
            return (
                None,
                (
                    f"stop is only {stop_atr:.2f} ATR away — too tight to survive normal noise "
                    f"(minimum {self.config.risk.min_stop_distance_atr} ATR)"
                ),
                decision,
            )
        if stop_atr > self.config.risk.max_stop_distance_atr:
            return (
                None,
                (
                    f"stop is {stop_atr:.2f} ATR away — structurally too wide "
                    f"(maximum {self.config.risk.max_stop_distance_atr} ATR)"
                ),
                decision,
            )

        risk_reward = reward_distance / stop_distance
        if risk_reward < self.config.risk.min_risk_reward:
            return (
                None,
                (
                    f"structural R:R is 1:{risk_reward:.2f}, below the required "
                    f"1:{self.config.risk.min_risk_reward:g}"
                ),
                decision,
            )

        evidence_notes = self._evidence(
            direction, decision, sweep, structure_event, displacement, poi_kind, m15
        )
        candidate = SetupCandidate(
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
            h1_bias=h1.bias,
            m15_bias=m15.bias,
            alignment=alignment,
            sweep=sweep,
            structure_event=structure_event,
            displacement=displacement,
            point_of_interest={"kind": poi_kind, **poi.as_dict()},
            dealing_range=m15.dealing_range,
            liquidity_target=target_level,
            setup_type=decision.setup_type,
            score_floor=decision.score_floor,
            evidence=evidence_notes,
            timestamp=moment,
        )
        return candidate, None, decision

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _choose_poi(gap: FairValueGap | None, block: OrderBlock | None) -> tuple[Any, str]:
        """Prefer a displacement-born gap; fall back to a strong order block."""

        if gap is not None and block is not None:
            if gap.displaced and gap.size_atr >= 0.2:
                return gap, "FVG"
            if block.strength >= 0.6:
                return block, "ORDER_BLOCK"
            return gap, "FVG"
        if gap is not None:
            return gap, "FVG"
        if block is not None:
            return block, "ORDER_BLOCK"
        return None, ""

    def _build_levels(
        self,
        *,
        direction: str,
        price: float,
        atr_value: float,
        sweep: LiquiditySweep | None,
        poi: Any,
        analysis: TimeframeAnalysis,
        index: int,
    ) -> tuple[float, float, float, dict[str, Any] | None] | None:
        """Entry/stop/target from structure. No model, no guesswork.

        Entry is the CURRENT price: this system submits market orders, so
        pretending to enter at a limit price the book never offered would
        make every downstream R calculation a fiction.
        """

        buffer = atr_value * 0.2
        entry = price
        # The sweep extreme is the ultimate invalidation, but it is only
        # the RELEVANT one when the sweep happened at or after the point of
        # interest. Entering a retracement into a POI created by the
        # post-sweep impulse should risk the POI, not the whole leg.
        sweep_is_invalidation = (
            sweep is not None and sweep.index >= getattr(poi, "index", -1)
        )

        # Invalidation: below the sweep's extreme and the POI's far edge
        # for a long, above both for a short.
        if direction == "BUY":
            # The stop risks the ENTRY ZONE, not the whole impulse leg.
            # The protected swing low is the level that invalidates the
            # directional read, but anchoring the stop there makes it as
            # wide as the entire leg and destroys the R:R the setup was
            # selected for. That level is still respected — as the
            # structure-exit trigger in bot.execution.manager — just not as
            # the initial stop.
            anchors = [poi.lower]
            if sweep_is_invalidation and sweep is not None:
                anchors.append(analysis.candles[sweep.index].low)
            stop_loss = min(anchors) - buffer
            if stop_loss >= entry:
                return None
        else:
            anchors = [poi.upper]
            if sweep_is_invalidation and sweep is not None:
                anchors.append(analysis.candles[sweep.index].high)
            stop_loss = max(anchors) + buffer
            if stop_loss <= entry:
                return None

        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            return None

        # Target: the next opposing liquidity pool, if it is far enough to
        # be worth the risk; otherwise a clean R-multiple projection.
        target = analysis.liquidity.nearest_target(entry, direction, index)
        target_level: dict[str, Any] | None = None
        minimum_reward = stop_distance * self.config.risk.min_risk_reward

        if target is not None:
            # Stop just short of the pool: the fill happens on the way in,
            # not at the exact level where everyone else's orders sit.
            offset = atr_value * 0.1
            candidate_tp = target.price - offset if direction == "BUY" else target.price + offset
            reward = abs(candidate_tp - entry)
            if reward >= minimum_reward:
                target_level = target.as_dict()
                take_profit = candidate_tp
            else:
                take_profit = (
                    entry + minimum_reward if direction == "BUY" else entry - minimum_reward
                )
        else:
            take_profit = entry + minimum_reward if direction == "BUY" else entry - minimum_reward

        if direction == "BUY" and not (stop_loss < entry < take_profit):
            return None
        if direction == "SELL" and not (take_profit < entry < stop_loss):
            return None
        return entry, stop_loss, take_profit, target_level

    @staticmethod
    def _evidence(
        direction: str,
        decision: MtfDecision,
        sweep: LiquiditySweep | None,
        structure_event: StructureEvent | None,
        displacement: Displacement | None,
        poi_kind: str,
        analysis: TimeframeAnalysis,
    ) -> tuple[str, ...]:
        items = [
            f"{direction} classified {decision.setup_type} "
            f"(H1 {decision.h1_bias} / M15 {decision.m15_bias})",
            decision.rationale,
        ]
        items.extend(decision.evidence)
        if decision.score_floor > 0:
            items.append(f"requires a score of at least {decision.score_floor:.0f}")
        if sweep is not None:
            items.append(
                f"{sweep.level.label} swept (quality {sweep.quality:.2f}, "
                f"rejection {sweep.rejection_ratio:.0%})"
            )
        if structure_event is not None:
            items.append(
                f"{structure_event.event_type} {structure_event.direction} "
                f"clearing {structure_event.clearance_atr:.2f} ATR"
            )
        if displacement is not None:
            items.append(
                f"displacement {displacement.atr_multiple:.1f}x ATR "
                f"(quality {displacement.quality:.2f})"
            )
        items.append(f"entry zone: {poi_kind}")
        if analysis.dealing_range is not None:
            items.append(f"price in {analysis.dealing_range.zone} of the dealing range")
        items.append(f"regime: {analysis.regime.note}")
        return tuple(items)
