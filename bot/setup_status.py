"""Configuration checklist.

Written for the case that actually blocks people: the service is deployed,
something is missing, and the failure surfaces as a 503 or an empty dashboard
with no indication of which variable is absent.

This reports, for every setting the system needs, whether it is present and
what it is for — and **never** its value. Booleans only. A diagnostic that
echoes a password back into a browser is not a diagnostic.

It works before any credentials exist, which is the whole point: it is the
screen you read when nothing else works yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import ExecutionMode, TradingConfig, profit_floor_feasibility

REQUIRED = "required"
RECOMMENDED = "recommended"
OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class Setting:
    name: str
    importance: str
    purpose: str
    #: What the operator should paste, with the secret part left blank.
    example: str = ""

    @property
    def present(self) -> bool:
        value = os.environ.get(self.name)
        return bool(value and value.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "importance": self.importance,
            "purpose": self.purpose,
            "present": self.present,
            "example": self.example,
        }


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "TRADELOCKER_EMAIL", REQUIRED,
        "The email you sign in to TradeLocker with.",
        "TRADELOCKER_EMAIL=your@email.com",
    ),
    Setting(
        "TRADELOCKER_PASSWORD", REQUIRED,
        "Your TradeLocker password.",
        "TRADELOCKER_PASSWORD=",
    ),
    Setting(
        "TRADELOCKER_SERVER", REQUIRED,
        "The server name shown on the TradeLocker login screen (e.g. GATESFX).",
        "TRADELOCKER_SERVER=",
    ),
    Setting(
        "TRADELOCKER_ACC_ID", REQUIRED,
        "The account number shown after the '#' in the account switcher.",
        "TRADELOCKER_ACC_ID=",
    ),
    Setting(
        "TRADELOCKER_URL", RECOMMENDED,
        "The DEMO API endpoint. Defaults to the demo host; a live URL is refused.",
        "TRADELOCKER_URL=https://demo.tradelocker.com/backend-api",
    ),
    Setting(
        "DATABASE_URL", RECOMMENDED,
        "PostgreSQL. WITHOUT THIS the kill switch, daily-loss counter and drawdown "
        "state reset on every redeploy, because the filesystem is ephemeral. "
        "Add a PostgreSQL plugin and this is set for you.",
        "",
    ),
    Setting(
        "DASHBOARD_TOKEN", RECOMMENDED,
        "Protects the pause, kill-switch and manual-scan controls. Any long random string.",
        "DASHBOARD_TOKEN=",
    ),
    Setting(
        "TRADED_SYMBOLS", OPTIONAL,
        "Which pairs to scan. Accepts bare pairs (EURUSD) or your broker's exact "
        "names (EURUSD.R) — both resolve.",
        "TRADED_SYMBOLS=EURUSD,GBPUSD,USDJPY,AUDUSD,USDCHF,XAUUSD",
    ),
    Setting(
        "TRADING_MODE", OPTIONAL,
        "'paper' simulates fills against live prices and sends nothing to the broker "
        "(the default). 'demo_live' places real orders on the DEMO account.",
        "TRADING_MODE=paper",
    ),
    Setting(
        "GEMINI_API_KEY", OPTIONAL,
        "Optional AI review. AI can only VETO a trade, never create or change one. "
        "Leave blank to run fully deterministically.",
        "",
    ),
    Setting(
        "GROQ_API_KEY", OPTIONAL,
        "Optional AI review fallback. Same veto-only role.",
        "",
    ),
)


@dataclass
class SetupReport:
    ready: bool
    missing_required: list[str] = field(default_factory=list)
    missing_recommended: list[str] = field(default_factory=list)
    settings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paste_block: str = ""
    next_step: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "missingRequired": self.missing_required,
            "missingRecommended": self.missing_recommended,
            "settings": self.settings,
            "warnings": self.warnings,
            "pasteBlock": self.paste_block,
            "nextStep": self.next_step,
        }


def build_setup_report(config: TradingConfig, *, equity: float | None = None) -> SetupReport:
    """Assess the deployment's configuration. Values are never included."""

    missing_required = [s.name for s in SETTINGS if s.importance == REQUIRED and not s.present]
    missing_recommended = [
        s.name for s in SETTINGS if s.importance == RECOMMENDED and not s.present
    ]

    warnings: list[str] = []
    if "DATABASE_URL" in missing_recommended:
        warnings.append(
            "No DATABASE_URL: risk state (kill switch, daily loss, drawdown, losing streak) "
            "will reset on every redeploy. Add a PostgreSQL plugin before trading for real."
        )
    if "DASHBOARD_TOKEN" in missing_recommended:
        warnings.append(
            "No DASHBOARD_TOKEN: anyone who can reach this URL can pause the bot or trip "
            "the kill switch."
        )
    # The silent-永-NO-TRADE trap: AI is required, no provider is configured,
    # and trading without AI is not permitted. Every candidate is then
    # rejected at the AI gate and the only trace is a line in the decision
    # journal. Failing closed is correct; failing closed SILENTLY is not.
    ai = config.ai
    if ai.enabled and not (ai.gemini_key or ai.groq_key) and not ai.allow_trade_without_ai:
        warnings.append(
            "AI_ENABLED=true but neither GEMINI_API_KEY nor GROQ_API_KEY is set, and "
            "AI_ALLOW_TRADE_WITHOUT_AI=false. Every setup will be REJECTED at the AI "
            "gate and the bot will never open a trade. Fix by one of: set "
            "AI_ENABLED=false to run fully deterministically (recommended — AI is a "
            "veto only, never a source of trades), set AI_ALLOW_TRADE_WITHOUT_AI=true, "
            "or add an API key."
        )

    if config.mode is ExecutionMode.DEMO_LIVE:
        warnings.append(
            "TRADING_MODE=demo_live: real orders will be placed on the DEMO account. "
            "Set it to 'paper' to simulate against live prices instead."
        )
    if equity is not None:
        feasibility = profit_floor_feasibility(config, equity)
        if not feasibility.get("feasible"):
            warnings.append(feasibility["reason"])

    # Only the settings still missing, so there is nothing to hunt through.
    outstanding = [s for s in SETTINGS if not s.present and s.example]
    paste_block = "\n".join(s.example for s in outstanding)

    if missing_required:
        next_step = (
            f"Set {len(missing_required)} required variable(s): "
            f"{', '.join(missing_required)}. Paste the block below into your host's "
            "environment variables, fill in the blanks, and redeploy."
        )
    elif missing_recommended:
        next_step = (
            "The bot can connect. Before trading for real, add: "
            f"{', '.join(missing_recommended)}. Then run the verification below."
        )
    else:
        next_step = "Configuration is complete. Run the account verification below."

    return SetupReport(
        ready=not missing_required,
        missing_required=missing_required,
        missing_recommended=missing_recommended,
        settings=[s.as_dict() for s in SETTINGS],
        warnings=warnings,
        paste_block=paste_block,
        next_step=next_step,
    )
