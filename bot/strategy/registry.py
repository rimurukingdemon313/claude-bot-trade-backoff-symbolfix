"""Every strategy the system can run, and how to build one.

The registry is the only place that knows the full set. Adding a strategy
is adding a module and one line here; nothing in the orchestrator, the
risk engine or the dashboard needs to learn its name.
"""

from __future__ import annotations

from ..config import TradingConfig
from ..errors import ConfigError
from .base import BUILDERS, Strategy, StrategyProfile
from .reversion import ReversionStrategy
from .smc_strategy import SmcStrategy

#: The mode used when nothing has been chosen. SMC is the strategy this
#: system was built around and the conservative default: it trades least.
DEFAULT_STRATEGY = "smc"

BUILDERS.update({"smc": SmcStrategy, "reversion": ReversionStrategy})

PROFILES: dict[str, StrategyProfile] = {
    "smc": SmcStrategy.profile,
    "reversion": ReversionStrategy.profile,
}


def available() -> list[StrategyProfile]:
    """Every selectable strategy, in a stable order for the dashboard."""

    return [PROFILES[key] for key in sorted(PROFILES)]


def normalise(name: str | None) -> str:
    """Accept a name, or refuse it by name rather than silently defaulting.

    A typo in TRADING_STRATEGY must not quietly run a different strategy
    than the operator asked for — that is exactly the class of silent
    substitution that makes a trade history uninterpretable.
    """

    if name is None or not str(name).strip():
        return DEFAULT_STRATEGY
    key = str(name).strip().lower()
    if key not in BUILDERS:
        raise ConfigError(
            f"unknown strategy {name!r}. Available: {', '.join(sorted(BUILDERS))}"
        )
    return key


def build(name: str | None, config: TradingConfig) -> Strategy:
    return BUILDERS[normalise(name)](config)
