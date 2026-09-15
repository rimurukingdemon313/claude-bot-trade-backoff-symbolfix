"""The SMC engine, wearing the strategy interface.

Deliberately a thin adapter and nothing else: the engine's behaviour must
not change because a second strategy now exists beside it. Anything this
file did beyond declaring a profile would be a behaviour change smuggled
in under a refactor.
"""

from __future__ import annotations

from datetime import datetime

from ..config import TradingConfig
from ..marketdata.provider import Series
from ..smc.engine import SmcEngine, SmcResult
from .base import StrategyProfile

PROFILE = StrategyProfile(
    key="smc",
    name="Smart Money Concepts",
    description=(
        "Trades a structural shift on M15: a liquidity sweep, a break of structure with "
        "displacement, and an entry back inside the imbalance it left. H4 is macro context "
        "and H1 the primary bias — a setup that fights either is held to a higher score "
        "rather than refused, and a move against the primary bias must have earned the "
        "name reversal to be taken at all."
    ),
    min_risk_reward=2.0,
    expected_frequency="a few a week",
    thesis="high-quality M15 execution, weighted by H1 bias and H4 context",
)


class SmcStrategy:
    profile = PROFILE

    def __init__(self, config: TradingConfig) -> None:
        self.engine = SmcEngine(config)

    def analyze(
        self, symbol: str, series: dict[str, Series], *, now: datetime | None = None
    ) -> SmcResult:
        return self.engine.analyze(symbol, series, now=now)
