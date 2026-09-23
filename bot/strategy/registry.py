"""Every strategy the system can run, and how to build one.

The registry is the only place that knows the full set. Adding a strategy
is adding a module and one line here; nothing in the orchestrator, the
risk engine or the dashboard needs to learn its name.
"""

from __future__ import annotations

from ..config import TradingConfig
from ..errors import ConfigError
from .base import BUILDERS, Strategy, StrategyProfile
from .smc_strategy import SmcStrategy

#: The mode used when nothing has been chosen. SMC is the strategy this
#: system was built around and the conservative default: it trades least.
DEFAULT_STRATEGY = "smc"

BUILDERS.update({"smc": SmcStrategy})

#: `reversion` is NOT here, deliberately (docs/EXPERIMENT_REVERSION.md).
#:
#: It was selectable on the dashboard and could never trade: the scorer
#: treats an FVG or order-block entry zone as critical, and a strategy that
#: fades a sweep at market never has one, so 691 of 691 candidates were
#: vetoed. Measured with that veto bypassed, its signal lost on all twelve
#: instruments: 38,971 trades, -0.069R, t = -14.3. So the one outcome worse
#: than a dead switch was fixing it. It was removed instead, as the
#: pre-registration committed to before the result was known.
#:
#: The module stays in bot/strategy/reversion.py so that experiment remains
#: reproducible; it is simply no longer a mode anyone can select.
PROFILES: dict[str, StrategyProfile] = {
    "smc": SmcStrategy.profile,
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
