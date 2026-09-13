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
        "Trades continuation after a structural shift: a liquidity sweep, a break of structure "
        "with displacement, and an entry back inside the imbalance it left. Requires H4, H1 and "
        "M15 to agree, so it stands aside often and aims far."
    ),
    min_risk_reward=2.0,
    expected_frequency="a few a week",
    thesis="structure breaks in the direction institutional flow has already committed to",
)


class SmcStrategy:
    profile = PROFILE

    def __init__(self, config: TradingConfig) -> None:
        self.engine = SmcEngine(config)

    def analyze(
        self, symbol: str, series: dict[str, Series], *, now: datetime | None = None
    ) -> SmcResult:
        return self.engine.analyze(symbol, series, now=now)
