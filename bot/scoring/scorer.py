"""Deterministic setup scoring and tiering.

Design constraints from MASTER_MISSION §27/§28:

* the score is a pure function of the candidate — same input, same score,
  always; nothing here reads the clock or the network;
* weights are explicit, sum to 100, and each component is documented with
  WHY it earns that weight rather than being tuned to a backtest;
* the tier thresholds live in config so they can be moved without
  editing scoring logic, and every change bumps SCORING_VERSION;
* "NO TRADE" is a first-class outcome — the scorer returns a tier of
  NO_TRADE for anything below the B threshold, and that is the expected
  result most of the time.

Weight rationale, in descending order of evidential value:
  trigger quality (25)  - the sweep/structure event IS the edge
  context        (18)   - how the setup stands to the primary bias and macro
  displacement   (14)   - proves intent behind the move
  entry zone     (12)   - a fresh, displaced POI beats a stale one
  risk/reward    (12)   - expectancy scales directly with it
  regime         (10)   - the same setup is worth less in a dead range
  location       (6)    - premium/discount preference, deliberately small
  session        (3)    - a tiebreak, not a thesis

Two things the total alone must never be allowed to do:

* carry a CRITICAL failure. A setup with no real trigger, no entry zone,
  or an unacceptable R:R is not a weak trade that seven good components
  can outvote - it is not a trade. Those three are floored individually.
* let a hard classification off. The MTF layer sets `score_floor` per
  setup type, so a reversal against the primary bias has to be a better
  setup than a continuation, measured on this same scale. The floor only
  ever rises above the configured B tier; nothing here can lower a limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ScoringConfig, TradingConfig
from ..smc.engine import SetupCandidate

WEIGHTS = {
    "trigger": 25.0,
    "context": 18.0,
    "displacement": 14.0,
    "entry_zone": 12.0,
    "risk_reward": 12.0,
    "regime": 10.0,
    "location": 6.0,
    "session": 3.0,
}

TIERS = ("A+", "A", "B", "NO_TRADE")

#: Components that cannot be compensated for. A near-zero score in any of
#: these means the setup is missing something structural, and no amount of
#: session quality or premium/discount agreement substitutes for it.
CRITICAL_COMPONENTS = ("trigger", "entry_zone", "risk_reward")

#: How much a setup's classification is worth on the context axis. A
#: continuation with the H1 trend is the reference; everything that
#: fights something scores less here AND carries a higher floor, so the
#: two act together rather than one excusing the other.
CONTEXT_FRACTION = {
    "CONTINUATION": 1.0,
    "RANGE_ROTATION": 0.60,
    "REVERSAL": 0.55,
}


@dataclass(frozen=True, slots=True)
class SetupScore:
    total: float
    tier: str
    components: dict[str, float]
    notes: tuple[str, ...]

    @property
    def tradeable(self) -> bool:
        return self.tier in ("A+", "A", "B")

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 2),
            "tier": self.tier,
            "components": {key: round(value, 2) for key, value in self.components.items()},
            "maxima": dict(WEIGHTS),
            "notes": list(self.notes),
        }


def _trigger_component(candidate: SetupCandidate) -> tuple[float, str]:
    """The sweep (or displaced break) that justifies entering now."""

    sweep = candidate.sweep
    event = candidate.structure_event
    if sweep is not None:
        fraction = sweep.quality
        if event is not None and event.index >= sweep.index:
            # Liquidity taken THEN structure shifted is the textbook
            # sequence and the highest-conviction trigger available.
            fraction = min(1.0, fraction + 0.15)
        note = f"sweep quality {sweep.quality:.2f}" + (
            " followed by a structure shift" if event is not None and event.index >= sweep.index else ""
        )
        return fraction, note
    if event is not None:
        fraction = 0.45 + 0.35 * event.displacement_quality
        return min(1.0, fraction), f"displaced {event.event_type} without a prior sweep"
    return 0.0, "no trigger"


def _context_component(candidate: SetupCandidate) -> tuple[float, str]:
    """How the setup stands to the H1 trend.

    Graded, not boolean. Agreement between H1 and M15 is not the thing
    being measured - a reversal is by definition NOT in agreement, and
    scoring it zero for that would reinstate the rigid filter this
    replaced. What is measured is how much the setup has to fight, and a
    setup that fights more simply has to be better, which is what
    `score_floor` enforces.
    """

    fraction = CONTEXT_FRACTION.get(candidate.setup_type)
    if fraction is None:
        # An unrecognised classification is not scored generously. Rule:
        # unknown is never treated as favourable.
        return 0.0, f"unrecognised setup type {candidate.setup_type!r}"
    detail = f"H1 {candidate.h1_bias} / M15 {candidate.m15_bias}"
    return fraction, f"{candidate.setup_type} ({detail})"


def _displacement_component(candidate: SetupCandidate) -> tuple[float, str]:
    move = candidate.displacement
    if move is None:
        return 0.2, "no displacement near the trigger"
    return move.quality, f"displacement {move.atr_multiple:.1f}x ATR"


def _entry_zone_component(candidate: SetupCandidate) -> tuple[float, str]:
    poi = candidate.point_of_interest or {}
    kind = poi.get("kind")
    if kind == "FVG":
        base = 0.6 + (0.3 if poi.get("displaced") else 0.0)
        base += min(0.1, float(poi.get("sizeAtr") or 0.0) * 0.1)
        return min(1.0, base), "fresh fair value gap" if poi.get("displaced") else "fair value gap"
    if kind == "ORDER_BLOCK":
        strength = float(poi.get("strength") or 0.0)
        return min(1.0, 0.45 + strength * 0.55), f"order block strength {strength:.2f}"
    return 0.0, "no entry zone"


#: What a projected target keeps of its TOTAL score.
#:
#: A structural target is a level the market has a reason to reach. A
#: projection is a distance chosen because it pays for the stop, and the
#: two are not the same evidence even when they produce the same ratio -
#: so they must not score the same.
#:
#: Applied to the total rather than to the R:R component, and the
#: difference matters. `risk_reward` is a CRITICAL component with a floor
#: of its own, and at the minimum ratio it already sits near that floor -
#: so any discount worth the name pushed it under, and a quality penalty
#: silently became a structural veto through an interaction nobody
#: designed. A projection is weaker evidence about the whole setup, which
#: is what a total is for.
PROJECTED_TARGET_FRACTION = 0.85


def _risk_reward_component(candidate: SetupCandidate, minimum: float) -> tuple[float, str]:
    """Scales from the minimum acceptable R:R up to 4R, then saturates.

    Saturation is deliberate: rewarding an 8R target encourages picking
    targets price will never reach. A PROJECTED target is discounted for
    a related reason - the ratio was derived from the stop rather than
    measured off a level, so on that path the number carries less
    information than the same number would from real liquidity.
    """

    ratio = candidate.risk_reward
    if ratio < minimum:
        return 0.0, f"R:R 1:{ratio:.2f} below minimum"
    span = max(0.5, 4.0 - minimum)
    score = min(1.0, (ratio - minimum) / span * 0.7 + 0.3)
    label = (candidate.liquidity_target or {}).get("label")
    return score, f"R:R 1:{ratio:.2f}" + (f" to {label}" if label else "")


def _regime_component(candidate: SetupCandidate) -> tuple[float, str]:
    return candidate.regime.quality(), candidate.regime.note


def _location_component(candidate: SetupCandidate) -> tuple[float, str]:
    dealing = candidate.dealing_range
    if dealing is None:
        # Absence of evidence is not half-evidence.
        #
        # This returned 0.5, which inverted the component: a setup MEASURED
        # to be in a bad location (alignment near 0) scored below one where
        # the location was simply unknown, so the scorer preferred
        # ignorance to a bad reading. Measured at 0% of bars on realistic
        # data, so this is a soundness fix rather than a live one - but a
        # default that rewards missing information is the kind that starts
        # mattering the day the data gets worse (project rule 6).
        return 0.0, "no dealing range established — location scores nothing, not half"
    alignment = dealing.alignment(candidate.direction)
    return alignment, f"price in {dealing.zone} ({dealing.position:.0%} of range)"


def _session_component(candidate: SetupCandidate) -> tuple[float, str]:
    return candidate.session.quality, f"{candidate.session.name} session"


class SetupScorer:
    def __init__(self, config: TradingConfig) -> None:
        self.config = config
        self.scoring: ScoringConfig = config.scoring

    def score(self, candidate: SetupCandidate) -> SetupScore:
        minimum_rr = self.config.risk.min_risk_reward
        parts = {
            "trigger": _trigger_component(candidate),
            "context": _context_component(candidate),
            "displacement": _displacement_component(candidate),
            "entry_zone": _entry_zone_component(candidate),
            "risk_reward": _risk_reward_component(candidate, minimum_rr),
            "regime": _regime_component(candidate),
            "location": _location_component(candidate),
            "session": _session_component(candidate),
        }
        components = {
            name: max(0.0, min(1.0, fraction)) * WEIGHTS[name]
            for name, (fraction, _) in parts.items()
        }
        notes = tuple(f"{name}: {note}" for name, (_, note) in parts.items())
        total = sum(components.values())

        projected = bool((candidate.liquidity_target or {}).get("projected"))
        if projected:
            total *= PROJECTED_TARGET_FRACTION
            notes += (
                "target: projected at the minimum R, not measured off a level — "
                f"total scaled to {PROJECTED_TARGET_FRACTION:.0%}",
            )

        # Hard structural gates. These are not score penalties — a setup
        # missing its trigger or its entry zone is not a weak trade, it is
        # not a trade, and no combination of the other components may vote
        # it back in.
        critical_floor = self.config.mtf.min_critical_component_fraction
        for name in CRITICAL_COMPONENTS:
            fraction = parts[name][0]
            if fraction <= 0.0:
                return SetupScore(
                    total,
                    "NO_TRADE",
                    components,
                    notes + (f"gate: {name} is absent ({parts[name][1]})",),
                )
            if fraction < critical_floor:
                return SetupScore(
                    total,
                    "NO_TRADE",
                    components,
                    notes
                    + (
                        f"gate: {name} scored {fraction:.2f} of 1.00, below the "
                        f"{critical_floor:.2f} a critical component must reach on its own",
                    ),
                )
        if candidate.setup_type not in CONTEXT_FRACTION:
            # Includes the legacy "conflicted" state: a setup whose
            # classification this scorer does not recognise is refused
            # rather than scored on a guess.
            return SetupScore(
                total,
                "NO_TRADE",
                components,
                notes + (f"gate: unclassified setup ({candidate.setup_type})",),
            )
        if not candidate.session.tradeable:
            return SetupScore(
                total, "NO_TRADE", components, notes + ("gate: session liquidity too thin",)
            )

        # The MTF layer's floor for this classification, and the
        # configured minimum tradeable score. Taking the max means every
        # one of them can only ever demand MORE, never less.
        #
        # `min_tradeable_score` was missing from this line, which made it
        # a dead setting: parsed from SCORING_MIN_TRADEABLE, documented
        # in .env.example as "the knob worth knowing about", and read by
        # nothing. An operator raising it to 68 to trade A grades only
        # would have seen the trade count not move, run their fortnight
        # of paper, and concluded that filtering by grade does nothing —
        # having never once filtered by grade. A control that silently
        # does nothing is worse than an absent one, because the operator
        # draws a conclusion from it.
        #
        # It defaults to tier_b, so this is a no-op on a stock build.
        floor = max(self.scoring.tier_b, self.scoring.min_tradeable_score, candidate.score_floor)
        if total < floor:
            return SetupScore(
                total,
                "NO_TRADE",
                components,
                notes
                + (
                    f"gate: {candidate.setup_type} requires a score of {floor:.0f}, "
                    f"scored {total:.1f}",
                ),
            )

        if total >= self.scoring.tier_a_plus:
            tier = "A+"
        elif total >= self.scoring.tier_a:
            tier = "A"
        elif total >= self.scoring.tier_b:
            tier = "B"
        else:
            tier = "NO_TRADE"
        return SetupScore(total, tier, components, notes)


def tier_rank(tier: str) -> int:
    """Higher is better. Used to rank candidates across symbols."""

    return {"A+": 3, "A": 2, "B": 1, "NO_TRADE": 0}.get(tier, 0)
