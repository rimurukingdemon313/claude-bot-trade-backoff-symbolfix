"""Component versions recorded on every trade, decision, and journal row.

Every meaningful strategy/risk/AI change MUST bump the corresponding
version here. Performance analysis groups results by these strings, so a
silent behaviour change under an unchanged version makes historical
results uninterpretable (see docs/TESTING.md, "experiment control").
"""

from __future__ import annotations

SYSTEM_VERSION = "3.9.1"
SMC_ENGINE_VERSION = "smc-2.0.0"
SCORING_VERSION = "score-2.0.0"
RISK_ENGINE_VERSION = "risk-2.3.0"
EXECUTION_VERSION = "exec-2.6.0"
AI_PROMPT_VERSION = "ai-prompt-2.0.0"
#: Which strategy produced a trade. Recorded alongside the rest so two
#: modes' results are never averaged together into a meaningless number.
STRATEGY_VERSION = "reversion-1.0.0"


def version_stamp() -> dict[str, str]:
    """The full version tuple stored alongside every persisted decision."""

    return {
        "system": SYSTEM_VERSION,
        "smc": SMC_ENGINE_VERSION,
        "scoring": SCORING_VERSION,
        "risk": RISK_ENGINE_VERSION,
        "execution": EXECUTION_VERSION,
        "ai_prompt": AI_PROMPT_VERSION,
    }
