"""The two-timeframe decision layer.

    H1  = TREND
    M15 = EXECUTION

H4 used to sit above these as macro context. It is gone, deliberately:
at 240 minutes it is SIXTEEN times the execution timeframe, and a bias
that coarse is stale relative to the entries it was judging. H1 is four
times M15 — the ratio trend-following actually uses — so the trend is
now read where it can still be acted on.

What this layer replaced, and still replaces, is a rigid agreement rule:
the engine derived direction from the M15 bias alone and refused if a
higher timeframe disagreed. That threw away the setup the strategy
exists to take, and had the inverse failure too — a plain retracement
inside an H1 leg looked like a signal whenever H1 happened to be neutral.

So one question decides the hard cases: has a move against the trend
EARNED the name reversal, or is it a retracement? Answered from
structure and liquidity, never from how far price has moved.

Five classifications:

  CONTINUATION      with the H1 trend, or with M15 structure when H1 is
                    neutral - the setup this system is built around
  RANGE_ROTATION    H1 is ranging; rotate from a swept range extreme
  REVERSAL          against H1, with sweep + displacement + CHoCH
  RETRACEMENT       against H1 WITHOUT that evidence - never traded
  NOISE             not a setup at all

Each carries a SCORE FLOOR rather than a veto, so fighting the trend
raises the quality required instead of discarding the setup. The floors
are configuration (`MtfConfig`), not literals.

A recency veto used to sit here too: a direction the configuration had
refused could stand down a staler opposite one. It is gone with H4,
because H4 is what made it reachable. The only refusal that carried
counter-evidence was a completed reversal case declined for fighting the
macro as well as the trend, and there is no macro to fight now. Every
remaining refusal is NOISE or RETRACEMENT - and a retracement is this
layer saying the counter move has NOT earned the name reversal, which is
the definition of the pullback the strategy enters on. Keeping a guard
whose only reachable input was the setup it must not block would have
been complexity with nothing behind it (project rule 12); the recency
SORT in `decide` still gives the freshest trigger the direction.

This module decides nothing about money. It returns a direction and the
bar that direction must clear; `risk/engine.py` remains the only code
that approves a trade or sizes one (project rule 2), and nothing here can
raise a limit — a classification may only ever demand MORE evidence than
the baseline, never less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..config import MtfConfig, SmcConfig
from .displacement import Displacement
from .liquidity import LiquiditySweep
from .regime import Regime
from .structure import StructureEvent

if TYPE_CHECKING:  # pragma: no cover - import cycle guard only
    from .engine import TimeframeAnalysis

# -- setup classifications ---------------------------------------------------

CONTINUATION = "CONTINUATION"
RANGE_ROTATION = "RANGE_ROTATION"
REVERSAL = "REVERSAL"
RETRACEMENT = "RETRACEMENT"
NOISE = "NOISE"

#: Ordered best-first. Used to break a tie between two directions that
#: both classified, AFTER recency - see `decide`.
_PREFERENCE = (
    CONTINUATION,
    RANGE_ROTATION,
    REVERSAL,
)

# -- signal states -----------------------------------------------------------

#: How near a displacement must be to the trigger to count as the move
#: that produced it. Wider and it picks up an unrelated impulse; narrower
#: and a displacement one candle late is missed.
DISPLACEMENT_PROXIMITY_CANDLES = 6

#: Nothing here is worth watching.
NO_TRADE = "NO_TRADE"
#: Context is favourable but the execution trigger is incomplete.
WATCH = "WATCH"
#: Structurally valid, but entry conditions are not fully satisfied
#: (price has not reached the zone, or levels do not resolve).
VALID_SETUP = "VALID_SETUP"
#: Everything this layer requires has passed. Scoring, risk and execution
#: still have their own say - TRADE here means "this layer is satisfied".
TRADE = "TRADE"


def _a(bias: str) -> str:
    """"a bullish" / "an unclear" - the refusal text is read by a person.

    Hardcoding "an" printed "an bullish H1 bias" on the dashboard for
    every retracement. Small, and the sort of thing that makes an
    operator trust the rest of the message less.
    """

    return f"an {bias}" if bias[:1].lower() in "aeiou" else f"a {bias}"


def _opposite(bias: str) -> str:
    """The other direction, or "" for a bias that has none.

    Returning "bullish" for "range" - which a bare else did - makes
    `bias == _opposite(wanted)` accidentally TRUE for a neutral timeframe
    against a bearish setup, so a timeframe with no opinion would read as
    opposed. `opposes_h1` calls it exactly that way.
    """

    if bias == "bullish":
        return "bearish"
    if bias == "bearish":
        return "bullish"
    return ""


@dataclass(frozen=True, slots=True)
class MtfDecision:
    """Direction, classification, and the bar the setup must clear."""

    direction: str | None          # BUY | SELL | None
    wanted: str | None             # bullish | bearish | None
    setup_type: str
    state: str
    h1_bias: str
    m15_bias: str
    #: Kept for continuity with stored trades and the dashboard badge.
    #: aligned = H1 and M15 agree; partial = H1 has no opinion;
    #: counter = the trend is genuinely opposed.
    alignment: str
    score_floor: float
    regime_note: str
    rationale: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tradeable(self) -> bool:
        """A direction was chosen AND the classification is permitted.

        Read from `direction`, never re-derived from `setup_type`: a
        classification the configuration disables keeps its honest name
        and must still be untradeable.
        """

        return self.direction is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "setupType": self.setup_type,
            "state": self.state,
            "h1Bias": self.h1_bias,
            "m15Bias": self.m15_bias,
            "alignment": self.alignment,
            "scoreFloor": round(self.score_floor, 2),
            "regime": self.regime_note,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class DirectionEvidence:
    """The M15 execution evidence available in one direction."""

    wanted: str
    sweep: LiquiditySweep | None
    structure_event: StructureEvent | None
    displacement: Displacement | None
    choch: StructureEvent | None
    #: The most recent qualifying trigger in this direction, over all of
    #: them - not just the one selected for grading. Used for the recency
    #: comparison between directions.
    latest_trigger_index: int = -1

    @property
    def has_trigger(self) -> bool:
        return self.sweep is not None or self.structure_event is not None

    @property
    def reference_index(self) -> int:
        """The candle the trigger happened on - how recent this evidence is."""

        return max(
            self.sweep.index if self.sweep else -1,
            self.structure_event.index if self.structure_event else -1,
        )

    @property
    def trigger_quality(self) -> float:
        """0..1 strength of the best trigger present."""

        best = 0.0
        if self.sweep is not None:
            best = max(best, self.sweep.quality)
        if self.structure_event is not None:
            # A displaced break with no sweep is real but second-rate:
            # liquidity was not demonstrably taken first.
            best = max(best, 0.45 + 0.35 * self.structure_event.displacement_quality)
        return min(1.0, best)


def is_choppy(regime: Regime | None, config: MtfConfig) -> bool:
    """A market with no usable direction and no usable range.

    Neither a trend to continue nor boundaries to rotate against, so the
    evidence a setup normally leans on is not there to lean on.
    """

    if regime is None:
        return True
    if regime.trend == "trending":
        return False
    return (
        regime.directional_strength < config.choppy_directional_strength
        and regime.trend != "ranging"
    )


def gather_direction_evidence(
    m15: "TimeframeAnalysis",
    wanted: str,
    *,
    index: int,
    smc: SmcConfig,
    mtf: MtfConfig,
) -> DirectionEvidence:
    """Everything M15 offers in one direction, look-ahead safe.

    A sweep is only visible from its `confirmed_index`; a structure event
    must clear its level by a configured ATR fraction to count at all,
    which is what stops an ordinary M15 wiggle reading as a break.
    """

    sweeps = [
        sweep
        for sweep in m15.sweeps
        if sweep.direction == wanted
        and sweep.confirmed_index <= index
        and (index - sweep.index) <= smc.sweep_max_age_candles
    ]
    sweep = max(sweeps, key=lambda s: (s.quality, s.index)) if sweeps else None

    events = [
        event
        for event in m15.structure_events
        if event.direction == wanted
        and event.index <= index
        and (index - event.index) <= smc.sweep_max_age_candles
        and event.displaced
        and event.clearance_atr >= mtf.min_structure_clearance_atr
    ]
    structure_event = max(events, key=lambda e: e.index) if events else None

    choch = None
    chochs = [event for event in events if event.event_type == "CHoCH"]
    if chochs:
        choch = max(chochs, key=lambda e: e.index)

    reference = max(
        sweep.index if sweep else -1,
        structure_event.index if structure_event else -1,
    )
    moves = [
        move
        for move in m15.displacements
        # `move.index <= index` is the whole no-look-ahead guard here, and
        # it was missing: `abs()` accepts a displacement AFTER the bar
        # being asked about, so bar 79 could answer bar 76. Nothing in
        # production noticed because production always asks about the last
        # bar - a backtest walking forward would have been scored against
        # candles it could not have seen (project rule 4).
        if move.direction == wanted
        and move.index <= index
        and abs(move.index - reference) <= DISPLACEMENT_PROXIMITY_CANDLES
    ]
    displacement = max(moves, key=lambda m: m.quality) if moves else None

    # The most recent thing the market did in this direction, over ALL
    # qualifying triggers rather than the one selected for grading. The
    # sweep above is chosen by QUALITY, so a strong old sweep would
    # otherwise make a fresh signal look stale to the recency veto in
    # `decide` - and being judged stale is what lets the opposite
    # direction take the trade.
    latest = max(
        [s.index for s in sweeps] + [e.index for e in events],
        default=-1,
    )

    return DirectionEvidence(
        wanted=wanted,
        sweep=sweep,
        structure_event=structure_event,
        displacement=displacement,
        choch=choch,
        latest_trigger_index=latest,
    )


def _reversal_shortfalls(
    evidence: DirectionEvidence, config: MtfConfig
) -> list[str]:
    """What a counter-bias move is missing before it is a reversal.

    An empty list means the move has taken significant liquidity, moved
    away from it with intent, and broken structure the other way. Anything
    less is a retracement inside the prevailing leg, however far it has
    travelled - distance is not evidence, and treating it as evidence is
    precisely how a system sells the bottom of a pullback.
    """

    missing: list[str] = []

    sweep = evidence.sweep
    if sweep is None:
        missing.append("no liquidity sweep in the reversal direction")
    else:
        if sweep.level.weight < config.reversal_min_level_weight:
            missing.append(
                f"swept level {sweep.level.label!r} carries weight "
                f"{sweep.level.weight:.2f}, below the {config.reversal_min_level_weight:.2f} "
                "a reversal needs - that is an intraday pivot, not the liquidity a turn runs on"
            )
        if sweep.quality < config.reversal_min_sweep_quality:
            missing.append(
                f"sweep quality {sweep.quality:.2f} below the "
                f"{config.reversal_min_sweep_quality:.2f} required against the primary bias"
            )

    displacement = evidence.displacement
    if displacement is None:
        missing.append("no displacement away from the swept level")
    elif displacement.quality < config.reversal_min_displacement_quality:
        missing.append(
            f"displacement quality {displacement.quality:.2f} below the "
            f"{config.reversal_min_displacement_quality:.2f} required"
        )

    if config.reversal_requires_choch and evidence.choch is None:
        missing.append(
            "no CHoCH against the prevailing M15 trend - a break in the direction the "
            "move was already travelling is continuation of a pullback, not a reversal"
        )
    return missing


@dataclass(frozen=True, slots=True)
class Classification:
    """What one direction is, and what it must clear to be taken.

    A named result rather than a five-tuple: `allowed` in particular has
    to be impossible to confuse with the rest, because it is the field
    that decides whether an order can follow.
    """

    setup_type: str
    score_floor: float
    rationale: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    allowed: bool = False

    @classmethod
    def refused(cls, setup_type: str, rationale: str) -> "Classification":
        """Named, scored at nothing, and not takeable.

        The type is still the honest one - a retracement is a
        retracement, and relabelling it NOISE to make it untradeable
        would hide from the operator what the engine actually saw.
        """

        return cls(setup_type=setup_type, score_floor=0.0, rationale=rationale, allowed=False)


@dataclass(frozen=True, slots=True)
class _Case:
    """The inputs every branch of the classifier shares."""

    h1: "TimeframeAnalysis"
    m15: "TimeframeAnalysis"
    wanted: str
    evidence: DirectionEvidence
    mtf: MtfConfig
    tier_b: float
    #: Extra score demanded because the market is chop. Added to every
    #: floor below, never subtracted from one.
    premium: float
    notes: tuple[str, ...]

    def agrees_h1(self) -> bool:
        return self.h1.bias == self.wanted

    def opposes_h1(self) -> bool:
        return self.h1.bias == _opposite(self.wanted)

    def take(self, setup_type: str, floor: float, rationale: str, *note: str) -> Classification:
        """A takeable classification at `max(tier_b, floor)` plus the chop premium.

        The clamp is the one-way ratchet: a classification may demand more
        evidence than the build's own bar, never less, so no branch here
        can loosen a limit however it is configured.
        """

        return Classification(
            setup_type=setup_type,
            score_floor=max(self.tier_b, floor) + self.premium,
            rationale=rationale,
            evidence=self.notes + tuple(note),
            allowed=True,
        )


def classify(
    *,
    h1: "TimeframeAnalysis",
    m15: "TimeframeAnalysis",
    wanted: str,
    evidence: DirectionEvidence,
    mtf: MtfConfig,
    tier_b: float,
) -> Classification:
    """Classify one direction against the H1 trend.

    A dispatcher over three mutually exclusive cases - with the trend,
    without one, against one - because that is the only question that
    changes what the setup IS. Everything else changes only how much it
    has to prove.
    """

    if evidence.trigger_quality < mtf.min_trigger_quality:
        return Classification.refused(
            NOISE,
            f"M15 trigger quality {evidence.trigger_quality:.2f} is below the "
            f"{mtf.min_trigger_quality:.2f} floor - noise, not a setup",
        )

    choppy = is_choppy(h1.regime, mtf) or is_choppy(m15.regime, mtf)
    premium = mtf.choppy_score_premium if choppy else 0.0
    notes = (
        (f"choppy market ({h1.regime.note}) - {premium:.0f} extra points required",)
        if choppy
        else ()
    )
    case = _Case(
        h1=h1,
        m15=m15,
        wanted=wanted,
        evidence=evidence,
        mtf=mtf,
        tier_b=tier_b,
        premium=premium,
        notes=notes,
    )

    if case.agrees_h1():
        return _with_primary_bias(case)
    if not case.opposes_h1():
        return _without_primary_bias(case)
    return _against_primary_bias(case)


def _with_primary_bias(case: _Case) -> Classification:
    """The M15 trigger runs with the H1 trend. The bread and butter."""

    return case.take(
        CONTINUATION,
        case.mtf.floor_continuation,
        "continuation with the trend",
        f"H1 trend is {case.h1.bias} and the M15 trigger runs with it",
    )


def _without_primary_bias(case: _Case) -> Classification:
    """H1 has no confirmed directional structure.

    No trend to follow, so the M15 sequence carries the setup on its own.
    Two ways that can still work, and one way it cannot.
    """

    mtf, h1, m15 = case.mtf, case.h1, case.m15
    dealing = h1.dealing_range or m15.dealing_range
    position = dealing.position if dealing is not None else 0.5
    # A rotation is only a rotation from the correct END of the range.
    at_extreme = (
        position <= (1.0 - mtf.range_rotation_min_position)
        if case.wanted == "bullish"
        else position >= mtf.range_rotation_min_position
    )
    if dealing is not None and at_extreme and case.evidence.sweep is not None:
        return case.take(
            RANGE_ROTATION,
            mtf.floor_range_rotation,
            "rotation from a swept range extreme",
            f"H1 has no trend; price at {position:.0%} of the dealing range "
            f"with the boundary swept ({case.evidence.sweep.level.label})",
        )

    # Not at a boundary, or no boundary to speak of. A trendless H1 is the
    # ABSENCE of opposition rather than opposition, so the M15 sequence can
    # still stand on its own - provided M15's OWN structure points the same
    # way. A trigger against the only structure present is noise.
    if m15.bias != case.wanted:
        return Classification.refused(
            NOISE,
            f"H1 has no trend and the M15 structure is {m15.bias} rather than "
            f"{case.wanted} - nothing anchors this direction on either timeframe",
        )
    return case.take(
        CONTINUATION,
        mtf.floor_no_htf_context,
        "continuation of M15 structure, H1 trendless",
        "no H1 trend in either direction; M15 structure is the only anchor, so "
        "the setup carries the whole burden of proof",
    )


def _against_primary_bias(case: _Case) -> Classification:
    """Reversal, or retracement. The distinction the layer turns on."""

    mtf, h1 = case.mtf, case.h1
    shortfalls = _reversal_shortfalls(case.evidence, mtf)
    if shortfalls:
        return Classification.refused(
            RETRACEMENT,
            f"M15 is {case.wanted} against {_a(h1.bias)} H1 bias, and this is a retracement "
            "rather than a reversal: " + "; ".join(shortfalls),
        )

    return case.take(
        REVERSAL,
        mtf.floor_reversal,
        "genuine reversal against the trend",
        f"sweep, displacement and CHoCH complete against the {h1.bias} H1 trend",
    )


def _alignment_label(h1_bias: str, wanted: str) -> str:
    """aligned / partial / counter - the badge, not a decision.

    Nothing reads this back: it is recorded on the trade and shown on the
    dashboard so a past decision can be read at a glance.
    """

    if h1_bias == _opposite(wanted):
        return "counter"
    if h1_bias == wanted:
        return "aligned"
    return "partial"


def decide(
    *,
    h1: "TimeframeAnalysis",
    m15: "TimeframeAnalysis",
    index: int,
    smc: SmcConfig,
    mtf: MtfConfig,
    tier_b: float,
) -> tuple[MtfDecision, DirectionEvidence | None]:
    """Choose a direction by classifying BOTH, then taking the better one.

    Deriving direction from one timeframe's bias was the original mistake:
    it made the answer depend on which timeframe was consulted rather than
    on what the market had actually done. Classifying both directions and
    ranking the results removes that - and it is what allows a reversal to
    be found at all, since a reversal is by definition the direction the
    H1 trend does NOT point in.
    """

    results: list[tuple[int, str, MtfDecision, DirectionEvidence]] = []
    rejected: list[MtfDecision] = []

    for wanted in ("bullish", "bearish"):
        evidence = gather_direction_evidence(
            m15, wanted, index=index, smc=smc, mtf=mtf
        )
        direction = "BUY" if wanted == "bullish" else "SELL"
        if not evidence.has_trigger:
            rejected.append(
                MtfDecision(
                    direction=None,
                    wanted=wanted,
                    setup_type=NOISE,
                    state=WATCH if h1.bias == wanted else NO_TRADE,
                    h1_bias=h1.bias,
                    m15_bias=m15.bias,
                    alignment=_alignment_label(h1.bias, wanted),
                    score_floor=0.0,
                    regime_note=m15.regime.note,
                    rationale=(
                        f"no M15 {wanted} trigger: neither a confirmed liquidity sweep nor a "
                        f"displaced structure break within {smc.sweep_max_age_candles} candles"
                    ),
                )
            )
            continue

        result = classify(
            h1=h1, m15=m15, wanted=wanted, evidence=evidence, mtf=mtf, tier_b=tier_b
        )
        decision = MtfDecision(
            direction=direction if result.allowed else None,
            wanted=wanted,
            setup_type=result.setup_type,
            state=VALID_SETUP if result.allowed else NO_TRADE,
            h1_bias=h1.bias,
            m15_bias=m15.bias,
            alignment=_alignment_label(h1.bias, wanted),
            score_floor=result.score_floor,
            regime_note=m15.regime.note,
            rationale=result.rationale,
            evidence=result.evidence,
        )
        if result.allowed:
            results.append(
                (_PREFERENCE.index(result.setup_type), wanted, decision, evidence)
            )
        else:
            rejected.append(decision)

    if results:
        # The MOST RECENT trigger wins the direction, and the
        # classification then sets the bar it has to clear.
        #
        # Ranking by classification first looked tidier and was wrong: a
        # stale continuation trigger still inside the age window
        # outranked a fresh reversal, so the engine would take a signal
        # the market had already moved past and, worse, would do it in
        # the direction the market had just turned away from. On the
        # execution timeframe, recency IS the signal. Quality breaks a
        # tie on the same candle, and the classification preference
        # breaks a tie on both, so the result never depends on loop order.
        results.sort(
            key=lambda row: (
                -row[3].latest_trigger_index,
                -row[3].trigger_quality,
                row[0],
            )
        )
        _, _, decision, evidence = results[0]
        return decision, evidence

    # Nothing tradeable. Report the most informative refusal: a
    # retracement that was correctly identified says more than "no
    # trigger", and a WATCH says more than a flat NO_TRADE.
    rejected.sort(key=lambda d: _refusal_rank(d))
    return rejected[0], None


def _refusal_rank(decision: MtfDecision) -> int:
    """Lower sorts first: the refusal an operator learns most from."""

    if decision.setup_type == RETRACEMENT:
        return 0
    if decision.state == WATCH:
        return 1
    return 2
