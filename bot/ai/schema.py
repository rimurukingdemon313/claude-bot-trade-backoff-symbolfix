"""Strict schema for AI output.

The contract is enforced by construction, not by prompting. Anything that
is not exactly a valid decision object resolves to NO TRADE — invalid
JSON, a missing field, a wrong type, an out-of-range confidence, an
unknown decision value (MASTER_MISSION §30).

Note what the schema does NOT do: it never repairs, coerces, or
"best-guesses" a malformed response. A model that returns 87% confidence
as the string "high" is a model that failed the contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import AIContractViolation

REQUIRED_FIELDS = ("decision", "confidence", "reason")
ALLOWED_DECISIONS = ("TRADE", "NO_TRADE")
ALLOWED_GRADES = ("A+", "A", "B", "C", "NO_TRADE", "")


@dataclass(frozen=True, slots=True)
class AIDecision:
    decision: str            # TRADE | NO_TRADE
    direction: str | None    # BUY | SELL | None
    confidence: int          # 0..100
    setup_grade: str
    reason: str
    invalidations: tuple[str, ...]
    proposed_entry: float | None
    proposed_stop: float | None
    proposed_target: float | None
    provider: str | None = None
    model: str | None = None

    @property
    def wants_trade(self) -> bool:
        return self.decision == "TRADE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "direction": self.direction,
            "confidence": self.confidence,
            "setupGrade": self.setup_grade,
            "reason": self.reason,
            "invalidations": list(self.invalidations),
            "provider": self.provider,
            "model": self.model,
        }


def extract_json_object(raw: str) -> dict[str, Any]:
    """Pull the decision object out of a model response.

    Models wrap JSON in prose or fences even when told not to. Extracting
    the first balanced object is tolerated; anything beyond that (repairing
    trailing commas, inferring missing braces) is not — a response we have
    to guess at is a response we do not trust.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise AIContractViolation("model returned an empty response")

    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    depth = 0
    start = -1
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise AIContractViolation(f"model response is not valid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise AIContractViolation("model returned JSON that is not an object")
                return parsed
    raise AIContractViolation("no JSON object found in the model response")


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def parse_decision(payload: dict[str, Any], *, provider: str, model: str) -> AIDecision:
    """Validate a parsed object into an AIDecision, or raise."""

    for field in REQUIRED_FIELDS:
        if field not in payload:
            raise AIContractViolation(f"model response is missing required field {field!r}")

    raw_decision = str(payload.get("decision", "")).strip().upper().replace(" ", "_")
    if raw_decision in ("BUY", "SELL"):
        # Some models answer with the direction instead of TRADE/NO_TRADE.
        # Accepting that is a format tolerance, not a semantic guess: the
        # direction is still cross-checked against the engine below.
        direction: str | None = raw_decision
        decision = "TRADE"
    elif raw_decision in ALLOWED_DECISIONS:
        decision = raw_decision
        raw_direction = str(payload.get("direction", "")).strip().upper()
        direction = raw_direction if raw_direction in ("BUY", "SELL") else None
    else:
        raise AIContractViolation(f"unknown decision value {payload.get('decision')!r}")

    confidence_raw = payload.get("confidence")
    try:
        confidence = int(round(float(confidence_raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AIContractViolation(f"confidence {confidence_raw!r} is not numeric") from exc
    if not 0 <= confidence <= 100:
        raise AIContractViolation(f"confidence {confidence} is outside 0-100")

    grade = str(payload.get("setup_grade", payload.get("setupGrade", ""))).strip().upper()
    grade = grade.replace("NO TRADE", "NO_TRADE")
    if grade not in ALLOWED_GRADES:
        grade = ""

    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise AIContractViolation("reason is empty")

    # Checked last so a malformed field reports its own specific problem
    # rather than being masked by the missing-direction message.
    if decision == "TRADE" and direction is None:
        raise AIContractViolation("decision is TRADE but no valid direction was provided")

    raw_invalidations = payload.get("invalidations", [])
    if isinstance(raw_invalidations, str):
        invalidations = (raw_invalidations,)
    elif isinstance(raw_invalidations, list):
        invalidations = tuple(str(item) for item in raw_invalidations[:8])
    else:
        invalidations = ()

    return AIDecision(
        decision=decision,
        direction=direction,
        confidence=confidence,
        setup_grade=grade,
        reason=reason[:1200],
        invalidations=invalidations,
        proposed_entry=_number_or_none(payload.get("entry")),
        proposed_stop=_number_or_none(payload.get("sl", payload.get("stop_loss"))),
        proposed_target=_number_or_none(payload.get("tp", payload.get("take_profit"))),
        provider=provider,
        model=model,
    )
