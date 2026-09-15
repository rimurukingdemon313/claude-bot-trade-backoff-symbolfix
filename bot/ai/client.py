"""AI provider client with a compact, structured prompt.

Cost control (MASTER_MISSION §32) is structural, not advisory: this
client is only ever CALLED for candidates that already passed structure,
scoring, session, news, spread and risk pre-checks. Weak setups never
reach a model, so token spend scales with genuine opportunities rather
than with scan frequency.

The prompt carries a compact summary, never raw candle history — sending
300 OHLC rows per symbol per scan is both expensive and useless to a
model that cannot verify them.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import AIConfig
from ..errors import AIContractViolation, AIError
from ..observability import log_event
from ..scoring.scorer import SetupScore
from ..smc.engine import SetupCandidate
from ..version import AI_PROMPT_VERSION
from .schema import AIDecision, extract_json_object, parse_decision

SYSTEM_CONTRACT = (
    "You are a risk-averse SMC trade reviewer. You are NOT the trader and NOT the risk "
    "manager. A deterministic engine has already selected the direction and computed the "
    "entry, stop and target from market structure; you cannot change them, cannot change "
    "position size, and cannot change risk. Your ONLY job is to say whether this specific "
    "setup should be taken. When evidence is thin, contradictory, or you are unsure, answer "
    "NO_TRADE — skipping a trade costs nothing, taking a bad one costs money. "
    "Reply with ONE JSON object and nothing else."
)

SCHEMA_HINT = (
    '{"decision":"TRADE|NO_TRADE","direction":"BUY|SELL","confidence":0-100,'
    '"setup_grade":"A+|A|B|C","reason":"concise evidence-based justification",'
    '"invalidations":["what would prove this wrong"]}'
)


@dataclass(frozen=True, slots=True)
class AIAttempt:
    provider: str
    model: str
    ok: bool
    error: str | None
    latency_ms: float


def build_prompt(candidate: SetupCandidate, score: SetupScore) -> str:
    """A compact, factual brief. No candle dumps, no leading language."""

    payload = {
        "symbol": candidate.symbol,
        "proposedDirection": candidate.direction,
        "context": {
            "h4Bias": candidate.htf_bias,
            "h1Bias": candidate.h1_bias,
            "m15Bias": candidate.m15_bias,
            "alignment": candidate.alignment,
            "setupType": candidate.setup_type,
            "session": candidate.session.name,
            "regime": candidate.regime.as_dict(),
        },
        "trigger": {
            "sweep": candidate.sweep.as_dict() if candidate.sweep else None,
            "structureEvent": candidate.structure_event.as_dict()
            if candidate.structure_event
            else None,
            "displacement": candidate.displacement.as_dict() if candidate.displacement else None,
        },
        "entryZone": candidate.point_of_interest,
        "dealingRange": candidate.dealing_range.as_dict() if candidate.dealing_range else None,
        "levels": {
            "entry": candidate.entry,
            "stopLoss": candidate.stop_loss,
            "takeProfit": candidate.take_profit,
            "riskReward": round(candidate.risk_reward, 2),
            "stopDistanceAtr": round(candidate.stop_distance / candidate.atr, 2)
            if candidate.atr
            else None,
        },
        "liquidityTarget": candidate.liquidity_target,
        "deterministicScore": score.as_dict(),
        "evidence": list(candidate.evidence),
    }
    return "\n".join(
        [
            SYSTEM_CONTRACT,
            f"Prompt version: {AI_PROMPT_VERSION}",
            f"Required JSON shape: {SCHEMA_HINT}",
            "Setup under review:",
            json.dumps(payload, separators=(",", ":"), default=str),
        ]
    )


class AIClient:
    """Gemini primary, Groq models as fallbacks. Each stage is bounded."""

    def __init__(self, config: AIConfig) -> None:
        self.config = config
        self.attempts: list[AIAttempt] = []
        self.calls = 0

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.config.gemini_key or self.config.groq_key)

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise AIError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIError(f"transport failure: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AIError(f"provider returned malformed JSON: {exc}") from exc

    def _ask_gemini(self, prompt: str) -> AIDecision:
        if not self.config.gemini_key:
            raise AIError("GEMINI_API_KEY is not configured")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
                "maxOutputTokens": 1024,
            },
        }
        result = self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.config.gemini_model}:generateContent",
            body,
            {"x-goog-api-key": self.config.gemini_key},
        )
        parts = (result.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        return parse_decision(
            extract_json_object(text), provider="Gemini", model=self.config.gemini_model
        )

    def _ask_groq(self, prompt: str, model: str) -> AIDecision:
        if not self.config.groq_key:
            raise AIError("GROQ_API_KEY is not configured")
        result = self._post(
            "https://api.groq.com/openai/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {self.config.groq_key}"},
        )
        text = ((result.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return parse_decision(extract_json_object(text), provider="Groq", model=model)

    def review(self, candidate: SetupCandidate, score: SetupScore) -> AIDecision:
        """Ask the chain in order. Raises AIError only if ALL stages fail.

        A contract violation from one provider is treated as a failure of
        that provider and falls through to the next — but if every
        provider violates the contract, the error propagates and the
        caller resolves it to NO TRADE.
        """

        prompt = build_prompt(candidate, score)
        self.attempts = []
        stages = []
        if self.config.gemini_key:
            stages.append(("Gemini", self.config.gemini_model, lambda: self._ask_gemini(prompt)))
        if self.config.groq_key:
            stages.append(
                (
                    "Groq",
                    self.config.groq_primary_model,
                    lambda: self._ask_groq(prompt, self.config.groq_primary_model),
                )
            )
            stages.append(
                (
                    "Groq",
                    self.config.groq_fallback_model,
                    lambda: self._ask_groq(prompt, self.config.groq_fallback_model),
                )
            )
        if not stages:
            raise AIError("no AI provider is configured")

        last_error: Exception | None = None
        for provider, model, call in stages:
            started = time.monotonic()
            try:
                decision = call()
                latency = (time.monotonic() - started) * 1000
                self.calls += 1
                self.attempts.append(AIAttempt(provider, model, True, None, latency))
                log_event(
                    "AI",
                    f"{provider}/{model} returned {decision.decision}",
                    symbol=candidate.symbol,
                    confidence=decision.confidence,
                    latency_ms=round(latency, 1),
                )
                return decision
            except (AIError, AIContractViolation) as exc:
                latency = (time.monotonic() - started) * 1000
                self.attempts.append(AIAttempt(provider, model, False, str(exc)[:200], latency))
                last_error = exc
                log_event(
                    "AI",
                    f"{provider}/{model} failed: {exc}",
                    severity="warning",
                    symbol=candidate.symbol,
                )
        raise AIError(f"every AI provider failed; last error: {last_error}")

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "available": self.available,
            "calls": self.calls,
            "providers": {
                "gemini": bool(self.config.gemini_key),
                "groq": bool(self.config.groq_key),
            },
            "lastAttempts": [
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "ok": attempt.ok,
                    "error": attempt.error,
                    "latencyMs": round(attempt.latency_ms, 1),
                }
                for attempt in self.attempts
            ],
        }
