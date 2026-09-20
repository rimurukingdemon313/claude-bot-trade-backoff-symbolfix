"""Monte Carlo analysis of a trade sequence.

Resamples the ORDER of realised trades to answer "how bad could the
drawdown have been with the same edge and different luck?". It is not a
forecast, and every report this module produces says so (MASTER_MISSION
§65).

Two things it deliberately does NOT do: it does not resample with a
fitted distribution (which would import assumptions the data does not
support), and it never reports a single "expected return" number.

Two separate questions live here, and mixing them up produced a
number that looked like evidence and was not:

* **reordering** (shuffle the same trades) answers "how bad could the
  DRAWDOWN have been with the same edge and different luck?". It cannot
  say anything about the total return, because a sum does not care about
  order — every shuffle ends on exactly the same figure. This module
  used to report `medianReturn`, `p5Return` and `p95Return` from those
  runs, three names for one constant, which reads as a 5th-percentile
  outcome and is nothing of the kind.
* **bootstrapping** (draw the same number of trades WITH replacement)
  answers "what range of totals is consistent with a sample this small?".
  That is a real distribution, and it is the one a reader was being
  shown a fake of.

So the total is reported once, as the invariant it is, and the
percentiles come from the bootstrap and are named for it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

DISCLAIMER = (
    "Monte Carlo reorders historical results to describe drawdown dispersion, "
    "and resamples them with replacement to describe how wide the total is "
    "given this sample size. Neither is a prediction of future performance."
)


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    runs: int
    trades_per_run: int
    #: The sample's own total. Identical in every reordering by
    #: construction — reported once so nobody reads it as a percentile.
    total_return: float
    #: From resampling WITH replacement, which is the only part of this
    #: module that can speak about the spread of the total.
    bootstrap_p5_return: float
    bootstrap_median_return: float
    bootstrap_p95_return: float
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
            "totalReturn": round(self.total_return, 2),
            "bootstrapP5Return": round(self.bootstrap_p5_return, 2),
            "bootstrapMedianReturn": round(self.bootstrap_median_return, 2),
            "bootstrapP95Return": round(self.bootstrap_p95_return, 2),
            "medianMaxDrawdown": round(self.median_max_drawdown, 2),
            "p95MaxDrawdown": round(self.percentile_95_drawdown, 2),
            "worstMaxDrawdown": round(self.worst_max_drawdown, 2),
            #: The WORST streak seen across every reordering, not a
            #: typical one. It answers "what could this edge have thrown
            #: at me", which is the question worth asking about a streak.
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
    # `runs` reaches here from a CLI flag. Zero produced no samples at all
    # and then crashed on max() of an empty sequence — a traceback where
    # the honest answer is "this asked for nothing, so there is nothing to
    # report" (project rule 6: an unanswerable question returns None with
    # a reason, it does not blow up).
    if runs < 1:
        return None

    rng = random.Random(seed)
    ruin_level = starting_balance * ruin_fraction
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
        drawdowns.append(max_drawdown)

    # The bootstrap: the same number of trades drawn WITH replacement, so
    # the total genuinely varies and its spread means something. This is
    # the empirical distribution of the sample, not a fitted one.
    bootstrap_totals: list[float] = []
    for _ in range(runs):
        bootstrap_totals.append(sum(rng.choice(values) for _ in range(len(values))))

    return MonteCarloResult(
        runs=runs,
        trades_per_run=len(values),
        total_return=sum(values),
        bootstrap_p5_return=_percentile(bootstrap_totals, 0.05),
        bootstrap_median_return=_percentile(bootstrap_totals, 0.5),
        bootstrap_p95_return=_percentile(bootstrap_totals, 0.95),
        median_max_drawdown=_percentile(drawdowns, 0.5),
        worst_max_drawdown=max(drawdowns),
        percentile_95_drawdown=_percentile(drawdowns, 0.95),
        longest_losing_streak=longest_streak,
        risk_of_ruin=ruined / runs,
        ruin_threshold=ruin_level,
    )
