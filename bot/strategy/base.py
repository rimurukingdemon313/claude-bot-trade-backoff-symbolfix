"""Two ways to find a trade, one way to take it.

The system now runs more than one strategy, selectable at runtime. What
must NOT vary with that selection is everything downstream: a strategy
proposes, and the scorer, the risk engine, the AI veto and the executor
dispose, exactly as before. Project rule 2 is unchanged — `risk/engine.py`
remains the only code that approves a trade or decides a size, and a
strategy that computed its own position size would be a second source of
truth, not a feature.

So a strategy is deliberately small: it turns market data into a priced,
structurally complete `SetupCandidate`, or into a reason it found none.
It does not know what an account balance is.

Each strategy also declares a `StrategyProfile`. A scalp that targets
1:1.5 and a swing setup that targets 1:3 are not the same trade, and the
risk engine needs to enforce the right floor for whichever is running —
enforce, note, not merely be told: the profile sets the limit the engine
checks, it never bypasses the check. A profile may never be more
permissive than the build's own floor, which is asserted here rather than
trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from ..config import TradingConfig
from ..errors import ConfigError
from ..marketdata.provider import Series
from ..smc.engine import SmcResult

#: The lowest risk:reward this build will run under any strategy. Mirrors
#: `RewardConfig.min_reward_r` and the clamp in `load_config`; a profile
#: below it is a bug, not a choice.
#:
#: It was 1.5, alongside a fixed $40 profit floor. Both are gone for the
#: same reason (bot/risk/reward.py): a target is the nearest meaningful
#: opposing liquidity, so demanding 1:2 or 1:1.5 of every setup does not
#: make the market offer it - it discards structurally sound trades whose
#: next pool happens to sit closer. 1.2 is the floor; a strategy may
#: still choose higher, and the reversion mode does.
BUILD_MINIMUM_RISK_REWARD = 1.2


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    """What a strategy needs from the risk engine, and what to expect of it.

    `expected_frequency` and `expected_win_rate` are DESCRIPTIONS OF INTENT,
    never predictions and never used in a calculation. They exist so the
    dashboard can say what a mode is for. Project rule 13: the only honest
    statement about win rate is the one the trade history eventually makes.
    """

    key: str
    name: str
    description: str
    min_risk_reward: float
    #: Free text: "several a day", "a few a week".
    expected_frequency: str
    thesis: str

    def __post_init__(self) -> None:
        if self.min_risk_reward < BUILD_MINIMUM_RISK_REWARD:
            raise ConfigError(
                f"strategy {self.key!r} declares a 1:{self.min_risk_reward:g} floor, below the "
                f"build minimum of 1:{BUILD_MINIMUM_RISK_REWARD:g}. A strategy may choose a "
                "higher floor than the build; it may never choose a lower one."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "minRiskReward": self.min_risk_reward,
            "expectedFrequency": self.expected_frequency,
            "thesis": self.thesis,
        }


class Strategy(Protocol):
    """Market data in, a priced candidate or a reason out."""

    profile: StrategyProfile

    def analyze(
        self, symbol: str, series: dict[str, Series], *, now: datetime | None = None
    ) -> SmcResult: ...


#: name -> factory. Populated by `bot.strategy.registry` to keep the
#: protocol module free of imports from its implementations.
BUILDERS: dict[str, Callable[[TradingConfig], Strategy]] = {}
