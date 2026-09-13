"""The "$50+" profit objective.

MASTER_MISSION §37 is explicit that this is a FILTER, never a mandate.
The system may not reach the target by taking more risk, widening
leverage, tightening the stop, or forcing a trade. So the only lever
available is selection: given the risk the risk engine already approved,
can this setup's structural target realistically produce the objective?
If not, the answer is NO TRADE.

Concretely: expected profit is computed from the ALREADY-SIZED position
(risk % fixed, stop structural, target structural). Nothing in this
module can change entry, stop, target, or size — it only returns a
verdict on numbers computed elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import OpportunityConfig


@dataclass(frozen=True, slots=True)
class OpportunityVerdict:
    meets_objective: bool
    expected_profit: float
    target_profit: float
    required_profit: float
    minimum_profit: float
    shortfall: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "meetsObjective": self.meets_objective,
            "expectedProfit": round(self.expected_profit, 2),
            "targetProfit": round(self.target_profit, 2),
            "requiredProfit": round(self.required_profit, 2),
            "minimumProfit": round(self.minimum_profit, 2),
            "shortfall": round(self.shortfall, 2),
            "reason": self.reason,
        }


def evaluate_opportunity(
    *,
    expected_profit: float,
    config: OpportunityConfig,
    tier: str,
) -> OpportunityVerdict:
    """Judge an already-sized trade against the profit objective."""

    if not config.enabled or config.target_profit <= 0:
        return OpportunityVerdict(
            True,
            expected_profit,
            config.target_profit,
            0.0,
            config.minimum_profit,
            0.0,
            "profit objective disabled",
        )

    target = config.target_profit
    # A+ setups may clear the target at a tolerance, because the quality of
    # the SETUP — never the size of the position — is what earns it. The
    # absolute floor still applies: required_profit() takes the max of the
    # tier's bar and `minimum_profit`.
    required = config.required_profit(tier)
    shortfall = max(0.0, required - expected_profit)
    at_floor = required <= config.minimum_profit + 1e-9

    if expected_profit >= required:
        return OpportunityVerdict(
            True,
            expected_profit,
            target,
            required,
            config.minimum_profit,
            0.0,
            f"expected ${expected_profit:.2f} at the structural target clears the "
            f"${required:.2f} bar for a {tier} setup "
            f"(absolute floor ${config.minimum_profit:.2f})",
        )
    return OpportunityVerdict(
        False,
        expected_profit,
        target,
        required,
        config.minimum_profit,
        shortfall,
        (
            f"expected ${expected_profit:.2f} at the structural target is ${shortfall:.2f} short "
            f"of the ${required:.2f} bar"
            + (" (the absolute profit floor)" if at_floor else f" for a {tier} setup")
            + ". Risk is NOT increased, the stop is NOT tightened, and size is NOT inflated to "
            "close the gap — standing aside."
        ),
    )
