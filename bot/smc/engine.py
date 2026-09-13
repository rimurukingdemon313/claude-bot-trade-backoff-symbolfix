"""Multi-timeframe SMC engine.

Pipeline per MASTER_MISSION §22/§98:

    H4 context -> H1 bias -> M15 structure -> liquidity map -> sweep
    -> displacement -> BOS/CHoCH -> FVG/OB retracement -> premium/discount

The engine's output is a SetupCandidate whose entry, stop and target are
derived DETERMINISTICALLY from market structure. This is the single most
important change from the previous build, where the AI invented the
prices and the system submitted them to the broker verbatim.

Stop placement is structural: behind the sweep extreme or the far edge of
the point of interest, plus an ATR buffer. Target is the next opposing
liquidity pool when one exists, otherwise an R-multiple projection.
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
    htf_bias: str
    h1_bias: str
    m15_bias: str
    alignment: str           # aligned | partial | conflicted
    sweep: LiquiditySweep | None
    structure_event: StructureEvent | None
    displacement: Displacement | None
    point_of_interest: dict[str, Any] | None
    dealing_range: DealingRange | None
    liquidity_target: dict[str, Any] | None
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
            "htfBias": self.htf_bias,
            "h1Bias": self.h1_bias,
            "m15Bias": self.m15_bias,
            "alignment": self.alignment,
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframes": {name: analysis.as_dict() for name, analysis in self.analyses.items()},
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "rejection": self.rejection,
        }


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
        candidate, rejection = self.build_candidate(symbol, analyses, now=now)
        return SmcResult(symbol=symbol, analyses=analyses, candidate=candidate, rejection=rejection)

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

        # --- direction: HTF context leads, M15 must not contradict it ---
        direction, alignment, rejection = self._resolve_direction(h4, h1, m15)
        if direction is None:
            return None, rejection

        wanted = "bullish" if direction == "BUY" else "bearish"

        # --- the trigger: a confirmed sweep, or a displaced structure break ---
        sweep = self._recent_sweep(m15, wanted, index)
        structure_event = self._recent_structure_event(m15, wanted, index)
        if sweep is None and structure_event is None:
            return None, (
                "no M15 trigger: neither a confirmed liquidity sweep nor a displaced "
                f"{wanted} structure break within {self.smc.sweep_max_age_candles} candles"
            )

        reference_index = max(
            sweep.index if sweep else -1, structure_event.index if structure_event else -1
        )
        displacement = self._recent_displacement(m15, wanted, reference_index)

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
            return None, "no live fair value gap or order block to enter from"

        # Entry requires price to actually be AT the point of interest.
        # Without this gate the engine would chase an extended move and
        # anchor its stop at the far side of the impulse leg, producing a
        # stop several ATR wide for no structural reason.
        proximity = m15.atr * 0.35
        if not (poi.lower - proximity <= price <= poi.upper + proximity):
            return None, (
                f"price {price:.5f} has not retraced into the {poi_kind} zone "
                f"[{poi.lower:.5f}, {poi.upper:.5f}] — waiting rather than chasing"
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
            return None, "could not construct a structurally valid stop and target"
        entry, stop_loss, take_profit, target_level = levels

        stop_distance = abs(entry - stop_loss)
        reward_distance = abs(take_profit - entry)
        if stop_distance <= 0:
            return None, "stop distance resolved to zero"

        stop_atr = stop_distance / m15.atr
        if stop_atr < self.config.risk.min_stop_distance_atr:
            return None, (
                f"stop is only {stop_atr:.2f} ATR away — too tight to survive normal noise "
                f"(minimum {self.config.risk.min_stop_distance_atr} ATR)"
            )
        if stop_atr > self.config.risk.max_stop_distance_atr:
            return None, (
                f"stop is {stop_atr:.2f} ATR away — structurally too wide "
                f"(maximum {self.config.risk.max_stop_distance_atr} ATR)"
            )

        risk_reward = reward_distance / stop_distance
        if risk_reward < self.config.risk.min_risk_reward:
            return None, (
                f"structural R:R is 1:{risk_reward:.2f}, below the required "
                f"1:{self.config.risk.min_risk_reward:g}"
            )

        evidence = self._evidence(direction, alignment, sweep, structure_event, displacement, poi_kind, m15)
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
            htf_bias=h4.bias,
            h1_bias=h1.bias,
            m15_bias=m15.bias,
            alignment=alignment,
            sweep=sweep,
            structure_event=structure_event,
            displacement=displacement,
            point_of_interest={"kind": poi_kind, **poi.as_dict()},
            dealing_range=m15.dealing_range,
            liquidity_target=target_level,
            evidence=evidence,
            timestamp=moment,
        )
        return candidate, None

    # -- helpers ---------------------------------------------------------

    def _resolve_direction(
        self, h4: TimeframeAnalysis, h1: TimeframeAnalysis, m15: TimeframeAnalysis
    ) -> tuple[str | None, str, str | None]:
        """HTF context decides direction; M15 may confirm but never oppose.

        A 'range' higher timeframe is permissive (it has no opinion), but
        an explicit opposing H4 bias is a veto — MASTER_MISSION §22 says
        never trade against strong higher-timeframe structure.
        """

        if m15.bias == "range":
            return None, "conflicted", "M15 has no confirmed directional structure"

        direction = "BUY" if m15.bias == "bullish" else "SELL"
        opposing = "bearish" if m15.bias == "bullish" else "bullish"

        if h4.bias == opposing:
            return None, "conflicted", (
                f"M15 is {m15.bias} but H4 is {h4.bias} — refusing to trade against "
                "higher-timeframe structure"
            )
        if h1.bias == opposing:
            return None, "conflicted", f"M15 is {m15.bias} but H1 is {h1.bias} — conflicted context"

        if h4.bias == m15.bias and h1.bias == m15.bias:
            alignment = "aligned"
        else:
            alignment = "partial"
        return direction, alignment, None

    def _recent_sweep(
        self, analysis: TimeframeAnalysis, wanted: str, index: int
    ) -> LiquiditySweep | None:
        candidates = [
            sweep
            for sweep in analysis.sweeps
            if sweep.direction == wanted
            and sweep.confirmed_index <= index
            and (index - sweep.index) <= self.smc.sweep_max_age_candles
        ]
        return max(candidates, key=lambda sweep: (sweep.quality, sweep.index)) if candidates else None

    def _recent_structure_event(
        self, analysis: TimeframeAnalysis, wanted: str, index: int
    ) -> StructureEvent | None:
        candidates = [
            event
            for event in analysis.structure_events
            if event.direction == wanted
            and event.index <= index
            and (index - event.index) <= self.smc.sweep_max_age_candles
            and event.displaced
        ]
        return max(candidates, key=lambda event: event.index) if candidates else None

    def _recent_displacement(
        self, analysis: TimeframeAnalysis, wanted: str, reference_index: int
    ) -> Displacement | None:
        candidates = [
            move
            for move in analysis.displacements
            if move.direction == wanted and abs(move.index - reference_index) <= 6
        ]
        return max(candidates, key=lambda move: move.quality) if candidates else None

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
        alignment: str,
        sweep: LiquiditySweep | None,
        structure_event: StructureEvent | None,
        displacement: Displacement | None,
        poi_kind: str,
        analysis: TimeframeAnalysis,
    ) -> tuple[str, ...]:
        items = [f"{direction} with {alignment} multi-timeframe context"]
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
