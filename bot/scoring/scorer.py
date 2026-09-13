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
  htf alignment  (18)   - trading with context is the largest single filter
  displacement   (14)   - proves intent behind the move
  entry zone     (12)   - a fresh, displaced POI beats a stale one
  risk/reward    (12)   - expectancy scales directly with it
  regime         (10)   - the same setup is worth less in a dead range
  location       (6)    - premium/discount preference, deliberately small
  session        (3)    - a tiebreak, not a thesis
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ScoringConfig, TradingConfig
from ..smc.engine import SetupCandidate

WEIGHTS = {
    "trigger": 25.0,
    "htf_alignment": 18.0,
    "displacement": 14.0,
    "entry_zone": 12.0,
    "risk_reward": 12.0,
    "regime": 10.0,
    "location": 6.0,
    "session": 3.0,
}

TIERS = ("A+", "A", "B", "NO_TRADE")


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


def _alignment_component(candidate: SetupCandidate) -> tuple[float, str]:
    if candidate.alignment == "aligned":
        return 1.0, "H4, H1 and M15 all agree"
    if candidate.alignment == "partial":
        # One higher timeframe is neutral rather than opposed.
        return 0.6, "higher timeframe is neutral, not opposed"
    return 0.0, "timeframes conflict"


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


def _risk_reward_component(candidate: SetupCandidate, minimum: float) -> tuple[float, str]:
    """Scales from the minimum acceptable R:R up to 4R, then saturates.

    Saturation is deliberate: rewarding an 8R target encourages picking
    targets price will never reach.
    """

    ratio = candidate.risk_reward
    if ratio < minimum:
        return 0.0, f"R:R 1:{ratio:.2f} below minimum"
    span = max(0.5, 4.0 - minimum)
    return min(1.0, (ratio - minimum) / span * 0.7 + 0.3), f"R:R 1:{ratio:.2f}"


def _regime_component(candidate: SetupCandidate) -> tuple[float, str]:
    return candidate.regime.quality(), candidate.regime.note


def _location_component(candidate: SetupCandidate) -> tuple[float, str]:
    dealing = candidate.dealing_range
    if dealing is None:
        return 0.5, "no dealing range established"
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
            "htf_alignment": _alignment_component(candidate),
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

        # Hard structural gates. These are not score penalties — a setup
        # missing its trigger or trading into conflict is not a weak
        # trade, it is not a trade.
        if parts["trigger"][0] <= 0.0 or parts["entry_zone"][0] <= 0.0:
            return SetupScore(total, "NO_TRADE", components, notes + ("gate: missing trigger or entry zone",))
        if candidate.alignment == "conflicted":
            return SetupScore(total, "NO_TRADE", components, notes + ("gate: timeframe conflict",))
        if not candidate.session.tradeable:
            return SetupScore(
                total, "NO_TRADE", components, notes + ("gate: session liquidity too thin",)
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
