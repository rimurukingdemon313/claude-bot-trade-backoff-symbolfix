"""Monte Carlo analysis of a trade sequence.

Resamples the ORDER of realised trades to answer "how bad could the
drawdown have been with the same edge and different luck?". It is not a
forecast, and every report this module produces says so (MASTER_MISSION
§65).

Two things it deliberately does NOT do: it does not resample with a
fitted distribution (which would import assumptions the data does not
support), and it never reports a single "expected return" number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

DISCLAIMER = (
    "Monte Carlo reorders historical results; it describes the dispersion "
    "implied by a finite sample and is not a prediction of future performance."
)


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    runs: int
    trades_per_run: int
    median_return: float
    percentile_5_return: float
    percentile_95_return: float
    median_max_drawdown: float
    worst_max_drawdown: float
    percentile_95_drawdown: float
    longest_losing_streak: int
    risk_of_ruin: float
    ruin_threshold: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "tradesPerRun": self.trades_per_run,
            "medianReturn": round(self.median_return, 2),
            "p5Return": round(self.percentile_5_return, 2),
            "p95Return": round(self.percentile_95_return, 2),
            "medianMaxDrawdown": round(self.median_max_drawdown, 2),
            "p95MaxDrawdown": round(self.percentile_95_drawdown, 2),
            "worstMaxDrawdown": round(self.worst_max_drawdown, 2),
            "longestLosingStreak": self.longest_losing_streak,
            "riskOfRuin": round(self.risk_of_ruin, 4),
            "ruinThreshold": self.ruin_threshold,
            "disclaimer": DISCLAIMER,
        }


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[position]


def monte_carlo(
    pnls: Sequence[float],
    *,
    runs: int = 2000,
    starting_balance: float = 10_000.0,
    ruin_fraction: float = 0.5,
    seed: int | None = 7,
) -> MonteCarloResult | None:
    """Shuffle the trade order `runs` times. Returns None for tiny samples."""

    values = [float(value) for value in pnls]
    if len(values) < 10:
        return None

    rng = random.Random(seed)
    ruin_level = starting_balance * ruin_fraction
    returns: list[float] = []
    drawdowns: list[float] = []
    ruined = 0
    longest_streak = 0

    for _ in range(runs):
        order = values[:]
        rng.shuffle(order)
        equity = starting_balance
        peak = equity
        max_drawdown = 0.0
        streak = 0
        hit_ruin = False
        for value in order:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
            streak = streak + 1 if value < 0 else 0
            longest_streak = max(longest_streak, streak)
            if equity <= ruin_level:
                hit_ruin = True
        if hit_ruin:
            ruined += 1
        returns.append(equity - starting_balance)
        drawdowns.append(max_drawdown)

    return MonteCarloResult(
        runs=runs,
        trades_per_run=len(values),
        median_return=_percentile(returns, 0.5),
        percentile_5_return=_percentile(returns, 0.05),
        percentile_95_return=_percentile(returns, 0.95),
        median_max_drawdown=_percentile(drawdowns, 0.5),
        worst_max_drawdown=max(drawdowns),
        percentile_95_drawdown=_percentile(drawdowns, 0.95),
        longest_losing_streak=longest_streak,
        risk_of_ruin=ruined / runs,
        ruin_threshold=ruin_level,
    )
