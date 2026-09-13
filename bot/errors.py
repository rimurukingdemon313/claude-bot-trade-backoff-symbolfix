"""Error taxonomy.

Every failure in this system is classified so the orchestrator can react
deterministically instead of treating "the network blipped" and "we may
have just placed a duplicate order" the same way.

Severity contract:
  TRANSIENT       - retry is safe and bounded.
  PERMANENT       - retrying cannot help; skip this unit of work.
  SAFETY_CRITICAL - trading must stop until a human or a reconcile clears it.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    SAFETY_CRITICAL = "SAFETY_CRITICAL"


class Category(str, Enum):
    BROKER = "BROKER"
    DATA = "DATA"
    AI = "AI"
    DATABASE = "DATABASE"
    EXECUTION = "EXECUTION"
    CONFIG = "CONFIG"
    SAFETY = "SAFETY"


class BotError(RuntimeError):
    """Base class carrying the classification every handler branches on."""

    severity: Severity = Severity.PERMANENT
    category: Category = Category.EXECUTION

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        return {
            "error": str(self),
            "severity": self.severity.value,
            "category": self.category.value,
            "type": type(self).__name__,
        }


class ConfigError(BotError):
    severity = Severity.SAFETY_CRITICAL
    category = Category.CONFIG


class DemoVerificationError(BotError):
    """The account could not be POSITIVELY verified as a DEMO account.

    This is always fatal for trading. There is no fallback path: a system
    that cannot prove it is on demo must never send an order.
    """

    severity = Severity.SAFETY_CRITICAL
    category = Category.SAFETY


class BrokerError(BotError):
    severity = Severity.TRANSIENT
    category = Category.BROKER


class BrokerAuthError(BrokerError):
    severity = Severity.PERMANENT


class BrokerRateLimited(BrokerError):
    severity = Severity.TRANSIENT


class BrokerRejected(BrokerError):
    """The broker understood the request and refused it."""

    severity = Severity.PERMANENT


class CircuitOpen(BrokerError):
    """The broker circuit breaker is open; calls are being shed."""

    severity = Severity.TRANSIENT


class AmbiguousExecution(BotError):
    """A write left this process but its outcome is unknown.

    NEVER auto-retry on this. The only safe recovery is to query the
    broker for the resulting order/position state (see
    bot.execution.reconciler.resolve_ambiguous_intent).
    """

    severity = Severity.SAFETY_CRITICAL
    category = Category.EXECUTION


class MarketDataError(BotError):
    severity = Severity.TRANSIENT
    category = Category.DATA


class StaleDataError(MarketDataError):
    severity = Severity.PERMANENT


class AIError(BotError):
    severity = Severity.TRANSIENT
    category = Category.AI


class AIContractViolation(AIError):
    """The model returned something that is not a valid decision.

    Always resolves to NO TRADE; never to a repaired/guessed decision.
    """

    severity = Severity.PERMANENT


class StorageError(BotError):
    severity = Severity.SAFETY_CRITICAL
    category = Category.DATABASE
