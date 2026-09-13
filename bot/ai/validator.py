"""Hallucination defence: cross-check every AI output against the engine.

The rule this module encodes (MASTER_MISSION §29/§31) is that AI is a
VETO, never a source of instructions:

* it can reject a setup the deterministic pipeline liked;
* it can never create a trade the pipeline did not produce;
* it can never change direction, entry, stop, target, size, or risk;
* proposed prices are compared only as a sanity signal and then discarded.

So the worst a maximally hallucinating model can do to this system is
cause it to skip trades. That is an acceptable failure mode; the reverse
is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AIConfig
from ..observability import log_event
from ..smc.engine import SetupCandidate
from .schema import AIDecision


@dataclass(frozen=True, slots=True)
class AIValidation:
    approved: bool
    reasons: tuple[str, ...]
    decision: AIDecision | None
    #: True when the model tried to do something outside its authority.
    contract_breach: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reasons": list(self.reasons),
            "decision": self.decision.as_dict() if self.decision else None,
            "contractBreach": self.contract_breach,
        }


def validate_ai_decision(
    decision: AIDecision, candidate: SetupCandidate, config: AIConfig
) -> AIValidation:
    reasons: list[str] = []
    breach = False

    if not decision.wants_trade:
        return AIValidation(
            False,
            (f"AI vetoed the setup: {decision.reason[:300]}",),
            decision,
        )

    if decision.direction != candidate.direction:
        # Direct contradiction of the deterministic engine.
        return AIValidation(
            False,
            (
                f"AI proposed {decision.direction} while the SMC engine produced "
                f"{candidate.direction} — contradiction resolves to NO TRADE",
            ),
            decision,
            contract_breach=True,
        )

    if decision.confidence < config.min_confidence:
        return AIValidation(
            False,
            (
                f"AI confidence {decision.confidence}% is below the required "
                f"{config.min_confidence}%",
            ),
            decision,
        )

    # Proposed levels are informational ONLY. A model that returns levels
    # far from the structural ones is flagged, because it suggests the
    # model did not understand the setup it just approved.
    tolerance = max(candidate.stop_distance * 1.5, candidate.atr * 2.0)
    for label, proposed, actual in (
        ("entry", decision.proposed_entry, candidate.entry),
        ("stop", decision.proposed_stop, candidate.stop_loss),
        ("target", decision.proposed_target, candidate.take_profit),
    ):
        if proposed is None:
            continue
        if proposed <= 0:
            reasons.append(f"AI proposed a non-positive {label} ({proposed}) — ignored")
            breach = True
            continue
        if abs(proposed - actual) > tolerance:
            reasons.append(
                f"AI's proposed {label} {proposed:.5f} differs materially from the structural "
                f"{actual:.5f}; the structural level is used and the divergence is recorded"
            )
            breach = True

    # A model proposing an inverted stop/target does not understand the
    # trade, so its approval is not evidence of anything.
    if decision.proposed_stop and decision.proposed_target:
        if candidate.direction == "BUY" and not (
            decision.proposed_stop < decision.proposed_target
        ):
            return AIValidation(
                False,
                ("AI proposed a stop above its own target on a long — rejecting its approval",),
                decision,
                contract_breach=True,
            )
        if candidate.direction == "SELL" and not (
            decision.proposed_target < decision.proposed_stop
        ):
            return AIValidation(
                False,
                ("AI proposed a target above its own stop on a short — rejecting its approval",),
                decision,
                contract_breach=True,
            )

    if breach:
        log_event(
            "AI",
            "AI output diverged from deterministic levels; structural levels retained",
            severity="warning",
            symbol=candidate.symbol,
            reasons=reasons,
        )

    reasons.insert(
        0,
        f"AI confirmed {candidate.direction} at {decision.confidence}% confidence: "
        f"{decision.reason[:300]}",
    )
    return AIValidation(True, tuple(reasons), decision, contract_breach=breach)
