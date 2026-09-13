"""DEMO-only enforcement. Fail closed, always.

MASTER_MISSION §4 requires positive DEMO verification at four points:
startup, broker connection, before order creation, and before order
submission. This module is the single implementation of that check; the
executor calls it again immediately before the write even though the
orchestrator already called it, because the cheap duplicate check is
worth far more than the alternative failure mode.

Two independent signals must BOTH hold:

1. The configured API base URL is a demo endpoint and matches no live
   marker.
2. The broker's own account metadata self-identifies as demo.

If either is missing, unreadable, or ambiguous, verification FAILS. There
is no "assume demo" branch, no env var that skips it, and no code path
that downgrades a failure to a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import DEMO_URL_MARKERS, LIVE_URL_MARKERS, TradingConfig
from ..errors import DemoVerificationError
from ..observability import log_event

#: Field names TradeLocker (and comparable brokers) use to expose account
#: type. All are checked; any one that positively says DEMO satisfies
#: signal 2, and any one that says LIVE/REAL fails it outright.
_TYPE_FIELDS = ("accountType", "type", "accType", "environment", "mode", "demo", "isDemo")

_DEMO_TOKENS = ("demo", "practice", "paper", "sandbox", "test")
_LIVE_TOKENS = ("live", "real", "production", "prod")


@dataclass(frozen=True, slots=True)
class DemoVerification:
    verified: bool
    url_ok: bool
    account_ok: bool
    checked_at: str
    evidence: tuple[str, ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "urlOk": self.url_ok,
            "accountOk": self.account_ok,
            "checkedAt": self.checked_at,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


def _url_signal(base_url: str) -> tuple[bool, str]:
    url = base_url.lower()
    for marker in LIVE_URL_MARKERS:
        if marker in url:
            return False, f"base URL contains LIVE marker {marker!r}"
    for marker in DEMO_URL_MARKERS:
        if marker in url:
            return True, f"base URL contains DEMO marker {marker!r}"
    return False, "base URL matches no known DEMO endpoint"


def _account_signal(account: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Read the broker's own account-type metadata.

    Returns (ok, evidence). An account whose metadata cannot be read at
    all returns False: "we could not check" is not "it is fine".
    """

    if not account:
        return False, "broker returned no account metadata to verify"

    for field in _TYPE_FIELDS:
        if field not in account:
            continue
        raw = account[field]
        if isinstance(raw, bool):
            if field in ("demo", "isDemo"):
                return bool(raw), f"{field}={raw}"
            continue
        text = str(raw).strip().lower()
        if not text:
            continue
        if any(token in text for token in _LIVE_TOKENS):
            return False, f"{field}={raw!r} identifies a LIVE account"
        if any(token in text for token in _DEMO_TOKENS):
            return True, f"{field}={raw!r}"

    # Last resort: some brokers only mark it in the account name.
    name = str(account.get("name", "")).lower()
    if any(token in name for token in _LIVE_TOKENS):
        return False, f"account name {account.get('name')!r} identifies a LIVE account"
    if any(token in name for token in _DEMO_TOKENS):
        return True, f"account name {account.get('name')!r}"

    return False, "no account field positively identifies this as a DEMO account"


def verify_demo(
    config: TradingConfig,
    account: Mapping[str, Any] | None,
    *,
    stage: str,
    now_iso: str | None = None,
) -> DemoVerification:
    """Run both signals and return the verdict. Never raises."""

    from ..clock import utc_now

    url_ok, url_evidence = _url_signal(config.broker.base_url)
    account_ok, account_evidence = _account_signal(account)
    verified = bool(url_ok and account_ok and config.require_demo)

    reason = None
    if not verified:
        parts = []
        if not url_ok:
            parts.append(url_evidence)
        if not account_ok:
            parts.append(account_evidence)
        reason = "; ".join(parts) or "demo verification failed"

    verification = DemoVerification(
        verified=verified,
        url_ok=url_ok,
        account_ok=account_ok,
        checked_at=(now_iso or utc_now().isoformat()),
        evidence=(url_evidence, account_evidence),
        reason=reason,
    )
    log_event(
        "SAFETY",
        f"DEMO verification at {stage}: {'PASS' if verified else 'FAIL'}",
        severity="info" if verified else "critical",
        stage_checked=stage,
        **verification.as_dict(),
    )
    return verification


def require_demo(
    config: TradingConfig,
    account: Mapping[str, Any] | None,
    *,
    stage: str,
) -> DemoVerification:
    """verify_demo(), but raises DemoVerificationError on failure.

    Every write path calls this. There is deliberately no variant that
    returns a boolean for a caller to ignore.
    """

    verification = verify_demo(config, account, stage=stage)
    if not verification.verified:
        raise DemoVerificationError(
            f"DEMO verification failed at {stage}: {verification.reason}. "
            "No order will be created or submitted.",
            detail=verification.as_dict(),
        )
    return verification
