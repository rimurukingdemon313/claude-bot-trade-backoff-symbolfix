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
from .portfolio import PortfolioBacktester


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


#: The grid searched on TRAIN only.
#:
#: Three points, deliberately. A large grid over a small dataset finds
#: noise, and the defence against overfitting is fewer knobs rather than a
#: better search (MASTER_MISSION §64). These two are also the only
#: parameters an operator realistically reaches for, so a grid over
#: anything else would be measuring a decision nobody makes.
#:
#: The values used to be 2.0 / 2.5 R, left behind when the build floor
#: moved to 1.2: every point in the grid sat above the default, so the
#: search could only ever make the system MORE selective than the shipped
#: configuration and never tested it at its own setting.
DEFAULT_GRID: tuple[dict[str, Any], ...] = (
    {"min_risk_reward": 1.2, "tier_b": 56.0},
    {"min_risk_reward": 1.5, "tier_b": 56.0},
    {"min_risk_reward": 1.2, "tier_b": 64.0},
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
    portfolio: Sequence[Any] | None = None,
    folds: int = 3,
    grid: Iterable[dict[str, Any]] = DEFAULT_GRID,
    costs: BacktestCosts | None = None,
    step: int = 1,
) -> WalkForwardResult:
    """Train, validate, test — over one symbol, or over a portfolio.

    `portfolio` is a sequence of `SymbolData`. When it is given, every
    window runs the multi-symbol simulation instead of the single-symbol
    one, and `m15` is used only for its length, to cut the folds.

    That option exists because this function kept answering "no parameter
    set produced enough trades" and the reason was not short windows: it
    was 22 missing symbols. A train window holding four trades cannot
    select a parameter, so the search returned nothing and the folds were
    reported empty — an honest answer to a question asked at the wrong
    scale.
    """

    result = WalkForwardResult()
    grid = list(grid)

    def measure(tuned: TradingConfig, window: tuple[int, int]) -> Any:
        low, high = window
        if portfolio is None:
            return Backtester(tuned, spec, costs=costs).run(m15[low:high], h1, step=step)
        run = PortfolioBacktester(tuned, costs=costs)
        for data in portfolio:
            sliced = data.m15[low:high]
            if len(sliced) < 60:
                continue
            run.add(data.spec, sliced, data.h1)
        return run.run(warmup=min(250, max(0, (high - low) // 4)), step=step)

    for fold in build_folds(len(m15), folds=folds):
        best_params: dict[str, Any] | None = None
        best_score = float("-inf")
        train_reports: list[dict[str, Any]] = []

        for params in grid:
            tuned = _apply(config, params)
            train = measure(tuned, fold.train)
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
        validate = measure(tuned, fold.validate)
        test = measure(tuned, fold.test)
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
