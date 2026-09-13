"""DEMO-only enforcement. Fail closed, always.

MASTER_MISSION §4 requires positive DEMO verification at four points:
startup, broker connection, before order creation, and before order
submission. This module is the single implementation of that check; the
executor calls it again immediately before the write even though the
orchestrator already called it, because the cheap duplicate check is
worth far more than the alternative failure mode.

Two independent signals must BOTH hold:

1. The configured API base URL is a demo endpoint and matches no live
   marker. This is *our* side of the claim.
2. The broker itself says so. TradeLocker exposes this in more than one
   place and not every brand populates the same one, so this signal is
   satisfied by positive evidence from EITHER the account record
   (`/auth/jwt/all-accounts`) OR the claims inside the access token the
   broker signed at login. GATESFX, for one, returns account records with
   no type field at all, while the session itself is unambiguous.

That is not a weakening: both sources are the broker's own assertion,
never ours, and a LIVE marker in *any* source fails the check outright
even when another source says demo. What is still refused is the absence
of evidence — if neither source positively identifies the environment,
verification FAILS.

If either signal is missing, unreadable, or ambiguous, verification
FAILS. There is no "assume demo" branch, no env var that skips it, and
no code path that downgrades a failure to a warning.
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

#: Claims in the broker-signed access token that legitimately name the
#: environment. Deliberately an allowlist: scanning every claim in a JWT
#: for the substring "demo" would eventually match an account nickname
#: and turn a user-controlled string into a safety signal.
_CLAIM_FIELDS = (
    "accountType",
    "accType",
    "environment",
    "env",
    "mode",
    "demo",
    "isDemo",
    "iss",
    "issuer",
    "aud",
    "audience",
    "server",
    "host",
    "domain",
)

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


def _classify(
    source: str,
    data: Mapping[str, Any] | None,
    fields: tuple[str, ...],
    *,
    include_name: bool = False,
) -> tuple[bool | None, str]:
    """Read one broker-supplied record.

    Returns `(True, evidence)` when it positively identifies a DEMO
    environment, `(False, evidence)` when it positively identifies a LIVE
    one, and `(None, observed)` when it expresses no opinion — the third
    case carries the field names that *were* present, because "we found
    nothing" is only actionable if it says what it looked at.
    """

    if not data:
        return None, f"{source}: nothing returned"

    candidates: list[tuple[str, Any]] = []
    for field in fields:
        if field in data:
            candidates.append((field, data[field]))
    if include_name:
        for field in ("name", "accountName", "title"):
            if field in data:
                candidates.append((field, data[field]))

    for field, raw in candidates:
        if isinstance(raw, bool):
            # Only genuinely boolean flags are read as booleans; a stray
            # `mode=False` says nothing about the environment.
            if field in ("demo", "isDemo"):
                return bool(raw), f"{source}.{field}={raw}"
            continue
        text = str(raw).strip().lower()
        if not text:
            continue
        if any(token in text for token in _LIVE_TOKENS):
            return False, f"{source}.{field}={raw!r} identifies a LIVE environment"
        if any(token in text for token in _DEMO_TOKENS):
            return True, f"{source}.{field}={raw!r}"

    observed = ", ".join(sorted(str(key) for key in data)) or "no fields"
    return None, f"{source} fields seen: {observed}"


def _broker_signal(
    account: Mapping[str, Any] | None,
    claims: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Signal 2: does the broker itself identify this as DEMO?

    Positive evidence from either the account record or the signed access
    token satisfies it. A LIVE marker in either one fails it outright,
    even when the other says demo — a contradiction is resolved the safe
    way, never the convenient one.
    """

    account_verdict, account_evidence = _classify(
        "account", account, _TYPE_FIELDS, include_name=True
    )
    claims_verdict, claims_evidence = _classify("token", claims, _CLAIM_FIELDS)

    for verdict, evidence in ((account_verdict, account_evidence), (claims_verdict, claims_evidence)):
        if verdict is False:
            return False, evidence
    for verdict, evidence in ((account_verdict, account_evidence), (claims_verdict, claims_evidence)):
        if verdict is True:
            return True, evidence

    return False, (
        "neither the account record nor the access token positively identifies this as a "
        f"DEMO account ({account_evidence}; {claims_evidence})"
    )


def verify_demo(
    config: TradingConfig,
    account: Mapping[str, Any] | None,
    *,
    stage: str,
    claims: Mapping[str, Any] | None = None,
    now_iso: str | None = None,
) -> DemoVerification:
    """Run both signals and return the verdict. Never raises."""

    from ..clock import utc_now

    url_ok, url_evidence = _url_signal(config.broker.base_url)
    account_ok, account_evidence = _broker_signal(account, claims)
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
    claims: Mapping[str, Any] | None = None,
) -> DemoVerification:
    """verify_demo(), but raises DemoVerificationError on failure.

    Every write path calls this. There is deliberately no variant that
    returns a boolean for a caller to ignore.
    """

    verification = verify_demo(config, account, stage=stage, claims=claims)
    if not verification.verified:
        raise DemoVerificationError(
            f"DEMO verification failed at {stage}: {verification.reason}. "
            "No order will be created or submitted.",
            detail=verification.as_dict(),
        )
    return verification
