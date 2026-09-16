"""Walk-forward evaluation.

Optimising over a whole dataset and reporting the result is how a curve
fit gets mistaken for an edge. This splits the series into consecutive
train/validate/test folds; parameters may only be chosen on train,
confirmed on validation, and are then applied ONCE to a test window whose
result is the only number allowed to be quoted (MASTER_MISSION §64).

The parameter grid is intentionally tiny. A large grid over a small
dataset finds noise, and the defence against overfitting is fewer knobs,
not a better search.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from ..config import TradingConfig
from ..marketdata.candles import Candle
from .engine import Backtester, BacktestCosts


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train: tuple[int, int]
    validate: tuple[int, int]
    test: tuple[int, int]


def build_folds(total: int, *, folds: int = 3, train_fraction: float = 0.5, validate_fraction: float = 0.25) -> list[Fold]:
    """Rolling, non-overlapping test windows across the series."""

    if folds < 1:
        raise ValueError("at least one fold is required")
    window = total // folds
    if window < 200:
        raise ValueError(
            f"{total} bars across {folds} folds leaves {window} bars per fold — too few "
            "to evaluate anything; use a longer series or fewer folds"
        )
    result: list[Fold] = []
    for index in range(folds):
        start = index * window
        end = start + window
        train_end = start + int(window * train_fraction)
        validate_end = train_end + int(window * validate_fraction)
        result.append(
            Fold(
                index=index,
                train=(start, train_end),
                validate=(train_end, validate_end),
                test=(validate_end, end),
            )
        )
    return result


DEFAULT_GRID: tuple[dict[str, Any], ...] = (
    {"min_risk_reward": 2.0, "tier_b": 56.0},
    {"min_risk_reward": 2.5, "tier_b": 56.0},
    {"min_risk_reward": 2.0, "tier_b": 64.0},
)


def _apply(config: TradingConfig, params: dict[str, Any]) -> TradingConfig:
    risk = replace(config.risk, min_risk_reward=params.get("min_risk_reward", config.risk.min_risk_reward))
    scoring = replace(config.scoring, tier_b=params.get("tier_b", config.scoring.tier_b))
    scoring = replace(scoring, min_tradeable_score=scoring.tier_b)
    return replace(config, risk=risk, scoring=scoring)


@dataclass
class WalkForwardResult:
    folds: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        tests = [fold["test"] for fold in self.folds if fold.get("test")]
        trades = sum(item.get("trades", 0) for item in tests)
        pnl = sum(item.get("totalPnl", 0.0) or 0.0 for item in tests)
        expectancies = [item["expectancy"] for item in tests if item.get("expectancy") is not None]
        return {
            "folds": len(self.folds),
            "outOfSampleTrades": trades,
            "outOfSamplePnl": round(pnl, 2),
            "outOfSampleExpectancy": round(sum(expectancies) / len(expectancies), 2)
            if expectancies
            else None,
            "consistentFolds": sum(1 for item in tests if (item.get("totalPnl") or 0) > 0),
            "verdict": _verdict(tests),
            "detail": self.folds,
        }


def _verdict(tests: Sequence[dict[str, Any]]) -> str:
    if not tests:
        return "no out-of-sample data"
    total_trades = sum(item.get("trades", 0) for item in tests)
    if total_trades < 20:
        return (
            f"inconclusive: only {total_trades} out-of-sample trades. "
            "No claim about edge is supportable at this sample size."
        )
    positive = sum(1 for item in tests if (item.get("totalPnl") or 0) > 0)
    if positive == len(tests):
        return "positive in every out-of-sample fold (still not a guarantee of future results)"
    if positive == 0:
        return "negative in every out-of-sample fold"
    return f"mixed: positive in {positive} of {len(tests)} out-of-sample folds"


def walk_forward(
    config: TradingConfig,
    spec: Any,
    m15: Sequence[Candle],
    h1: Sequence[Candle],
    *,
    folds: int = 3,
    grid: Iterable[dict[str, Any]] = DEFAULT_GRID,
    costs: BacktestCosts | None = None,
    step: int = 1,
) -> WalkForwardResult:
    result = WalkForwardResult()
    grid = list(grid)

    for fold in build_folds(len(m15), folds=folds):
        best_params: dict[str, Any] | None = None
        best_score = float("-inf")
        train_reports: list[dict[str, Any]] = []

        for params in grid:
            tuned = _apply(config, params)
            train = Backtester(tuned, spec, costs=costs).run(
                m15[fold.train[0] : fold.train[1]], h1, step=step
            )
            stats = train.statistics()
            train_reports.append({"params": params, **stats})
            # Select on expectancy, not on total profit: total profit
            # rewards whichever setting simply traded more.
            score = stats.get("expectancy") or float("-inf")
            if stats.get("trades", 0) < 5:
                score = float("-inf")
            if score > best_score:
                best_score = score
                best_params = params

        if best_params is None:
            result.folds.append(
                {"fold": fold.index, "selected": None, "note": "no parameter set produced enough trades"}
            )
            continue

        tuned = _apply(config, best_params)
        validate = Backtester(tuned, spec, costs=costs).run(
            m15[fold.validate[0] : fold.validate[1]], h1, step=step
        )
        test = Backtester(tuned, spec, costs=costs).run(
            m15[fold.test[0] : fold.test[1]], h1, step=step
        )
        result.folds.append(
            {
                "fold": fold.index,
                "selected": best_params,
                "train": train_reports,
                "validate": validate.statistics(),
                "test": test.statistics(),
            }
        )
    return result
