"""Component versions recorded on every trade, decision, and journal row.

Every meaningful strategy/risk/AI change MUST bump the corresponding
version here. Performance analysis groups results by these strings, so a
silent behaviour change under an unchanged version makes historical
results uninterpretable (see docs/TESTING.md, "experiment control").
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

SYSTEM_VERSION = "6.9.0"
SMC_ENGINE_VERSION = "smc-4.2.0"
SCORING_VERSION = "score-3.2.0"
RISK_ENGINE_VERSION = "risk-3.2.0"
EXECUTION_VERSION = "exec-2.8.0"
AI_PROMPT_VERSION = "ai-prompt-2.2.0"
#: Which strategy produced a trade. Recorded alongside the rest so two
#: modes' results are never averaged together into a meaningless number.
STRATEGY_VERSION = "reversion-2.1.0"


#: Config sections whose values change WHAT gets traded or HOW it is
#: managed. Credentials, URLs, storage and scheduling are deliberately
#: absent: nothing secret is ever hashed here, and a poll interval does
#: not change a decision.
_TUNED_SECTIONS = ("scoring", "risk", "execution", "mtf", "reward")

#: AI fields that gate a trade. The keys and the timeouts do not.
_TUNED_AI_FIELDS = ("enabled", "min_confidence", "allow_trade_without_ai")

#: Set by `load_config()`. "default" until something is overridden.
_TUNING = "default"


def tuning_fingerprint() -> str:
    """Which tuning produced a decision.

    Rule 9 says a behaviour change must be visible in the version stamp,
    because performance analysis groups by it. Several tunables are now
    settable from the environment — deliberately, so an operator can run
    a wider stop or a higher score floor on paper for a fortnight without
    a deploy. That created exactly the hole rule 9 is about: every trade
    from the experiment and every trade from the control recorded the
    same `risk-3.2.0`, so the two could never be told apart afterwards.

    This closes it. A build running stock defaults records "default"; any
    override records a short digest of what was changed, and the change
    itself is logged in full at startup so the digest can be read back.
    """

    return _TUNING


def _fingerprint_of(config: Any) -> tuple[str, dict[str, Any]]:
    """Returns (fingerprint, the settings that differ from the defaults)."""

    changed: dict[str, Any] = {}
    for name in _TUNED_SECTIONS:
        section = getattr(config, name, None)
        if section is None or not dataclasses.is_dataclass(section):
            continue
        default = type(section)()
        for field in dataclasses.fields(section):
            value = getattr(section, field.name)
            if value != getattr(default, field.name, object()):
                changed[f"{name}.{field.name}"] = value

    ai = getattr(config, "ai", None)
    if ai is not None and dataclasses.is_dataclass(ai):
        default_ai = type(ai)()
        for field_name in _TUNED_AI_FIELDS:
            value = getattr(ai, field_name, None)
            if value != getattr(default_ai, field_name, object()):
                changed[f"ai.{field_name}"] = value

    if not changed:
        return "default", {}
    blob = json.dumps(changed, sort_keys=True, default=str)
    return "tuned-" + hashlib.sha256(blob.encode()).hexdigest()[:8], changed


def set_tuning_fingerprint(config: Any) -> tuple[str, dict[str, Any]]:
    """Record the tuning this process is running. Called by `load_config`."""

    global _TUNING
    _TUNING, changed = _fingerprint_of(config)
    return _TUNING, changed


def version_stamp() -> dict[str, str]:
    """The full version tuple stored alongside every persisted decision."""

    return {
        "system": SYSTEM_VERSION,
        "smc": SMC_ENGINE_VERSION,
        "scoring": SCORING_VERSION,
        "risk": RISK_ENGINE_VERSION,
        "execution": EXECUTION_VERSION,
        "ai_prompt": AI_PROMPT_VERSION,
        "tuning": tuning_fingerprint(),
    }
