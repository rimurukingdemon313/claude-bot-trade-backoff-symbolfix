"""The reward objective, measured in R.

This replaced a fixed dollar floor — "expected profit must clear $50" —
and the reason is arithmetic rather than preference. Expected profit at
the structural target is

    risk x R

and risk is 0.5% of equity, so a dollar floor is really a statement about
ACCOUNT SIZE wearing the costume of a statement about setup quality. On a
$5,000 account, 0.5% is $25, and a $50 floor silently demands 1:2 on
every trade; on a $1,000 account the same floor demands 1:4 and the bot
stands aside for weeks while every refusal looks individually correct.
The market does not know the account balance, so the same setup was
graded differently on two accounts. That is not a filter, it is a bug
with a plausible face.

R is the honest unit: it is the same number on every account, it is what
the structure actually offers, and it cannot be improved by taking more
risk. Which matters, because the three ways to inflate a dollar figure —
size up, tighten the stop, move the target — are each forbidden
elsewhere and would each be invisible here:

* size comes from `sizing.calculate_position_size`, which rounds DOWN and
  declines rather than round up to a broker minimum;
* the stop comes from structure in `smc/engine.py`;
* the target is the nearest meaningful opposing liquidity, also
  structural.

So this module judges a ratio that is already fixed by the time it sees
it, and can only ever say yes or no. Expected profit is still computed
and still reported — an operator wants to know what a trade is worth —
but it is INFORMATION, never a gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import R_EPSILON, RewardConfig


@dataclass(frozen=True, slots=True)
class RewardVerdict:
    meets_objective: bool
    risk_reward: float
    #: What the position is worth at its structural target, in account
    #: currency. Reported, never used as a threshold.
    expected_profit: float
    minimum_r: float
    preferred_r: float
    reason: str

    @property
    def preferred(self) -> bool:
        """Clears the stronger bar. A label, not a permission."""

        return self.meets_objective and self.risk_reward >= self.preferred_r

    def as_dict(self) -> dict[str, Any]:
        return {
            "meetsObjective": self.meets_objective,
            "riskReward": round(self.risk_reward, 3),
            "expectedProfit": round(self.expected_profit, 2),
            "minimumR": self.minimum_r,
            "preferredR": self.preferred_r,
            "preferred": self.preferred,
            "reason": self.reason,
        }


def evaluate_reward(
    *,
    risk_reward: float,
    expected_profit: float,
    config: RewardConfig,
) -> RewardVerdict:
    """Judge an already-priced, already-sized trade on its R alone."""

    minimum = config.min_reward_r
    preferred = config.preferred_reward_r

    if not config.enabled:
        return RewardVerdict(
            True,
            risk_reward,
            expected_profit,
            minimum,
            preferred,
            "reward objective disabled",
        )

    # Consistent with every other R comparison in the system.
    if risk_reward < minimum - R_EPSILON:
        return RewardVerdict(
            False,
            risk_reward,
            expected_profit,
            minimum,
            preferred,
            (
                f"structural reward is 1:{risk_reward:.2f}, below the 1:{minimum:g} minimum. "
                "The target is the nearest meaningful opposing liquidity and the stop is "
                "structural, so neither is moved to close the gap, and size is not raised — "
                "standing aside."
            ),
        )

    strength = "a strong setup" if risk_reward >= preferred else "above the minimum"
    return RewardVerdict(
        True,
        risk_reward,
        expected_profit,
        minimum,
        preferred,
        (
            f"structural reward is 1:{risk_reward:.2f} ({strength}; minimum 1:{minimum:g}, "
            f"preferred 1:{preferred:g}), worth ${expected_profit:.2f} at the target"
        ),
    )
