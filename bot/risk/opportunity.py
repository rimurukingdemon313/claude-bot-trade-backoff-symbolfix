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
    shortfall: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "meetsObjective": self.meets_objective,
            "expectedProfit": round(self.expected_profit, 2),
            "targetProfit": round(self.target_profit, 2),
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
            True, expected_profit, config.target_profit, 0.0, "profit objective disabled"
        )

    target = config.target_profit
    # A+ setups may clear a slightly lower bar, because the quality of
    # the setup — not the size of the position — is what is being
    # rewarded. The floor is a fraction of the target, never a
    # size increase.
    threshold = target * config.tolerance_fraction if tier == "A+" else target
    shortfall = max(0.0, threshold - expected_profit)

    if expected_profit >= threshold:
        return OpportunityVerdict(
            True,
            expected_profit,
            target,
            0.0,
            f"expected ${expected_profit:.2f} at target meets the ${threshold:.2f} objective",
        )
    return OpportunityVerdict(
        False,
        expected_profit,
        target,
        shortfall,
        (
            f"expected ${expected_profit:.2f} at the structural target is ${shortfall:.2f} short "
            f"of the ${threshold:.2f} objective. Risk is NOT increased to close the gap — "
            "standing aside."
        ),
    )
