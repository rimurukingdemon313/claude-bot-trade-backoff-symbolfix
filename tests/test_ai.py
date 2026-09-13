"""AI contract and hallucination defence.

The property under test: a maximally misbehaving model can only ever
cause the system to SKIP trades. It can never create one, change a
direction, move a level, or alter risk.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.ai.schema import AIDecision, extract_json_object, parse_decision
from bot.ai.validator import validate_ai_decision
from bot.errors import AIContractViolation
from bot.marketdata.provider import MarketDataProvider
from bot.smc.engine import SmcEngine
from fakes import DEFAULT_SPEC, SETUP_END


@pytest.fixture()
def candidate(config, broker):
    series = MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    result = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END)
    assert result.candidate is not None
    return result.candidate


def decision(**overrides) -> AIDecision:
    defaults = dict(
        decision="TRADE",
        direction="BUY",
        confidence=85,
        setup_grade="A",
        reason="liquidity swept and structure shifted",
        invalidations=(),
        proposed_entry=None,
        proposed_stop=None,
        proposed_target=None,
        provider="Gemini",
        model="test",
    )
    defaults.update(overrides)
    return AIDecision(**defaults)


# -- parsing --------------------------------------------------------------


def test_json_wrapped_in_prose_and_fences_is_extracted():
    raw = 'Here is my answer:\n```json\n{"decision":"TRADE","confidence":80,"reason":"ok"}\n```\nDone.'
    assert extract_json_object(raw)["decision"] == "TRADE"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "no json here", "{not valid json}", "[1,2,3]"],
)
def test_unparseable_output_raises_rather_than_being_guessed_at(raw):
    with pytest.raises(AIContractViolation):
        extract_json_object(raw)


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"confidence": 80, "reason": "x"}, "missing required field 'decision'"),
        ({"decision": "TRADE", "reason": "x"}, "missing required field 'confidence'"),
        ({"decision": "TRADE", "confidence": 80}, "missing required field 'reason'"),
        ({"decision": "MAYBE", "confidence": 80, "reason": "x"}, "unknown decision"),
        ({"decision": "TRADE", "confidence": "high", "reason": "x"}, "not numeric"),
        ({"decision": "TRADE", "confidence": 140, "reason": "x"}, "outside 0-100"),
        ({"decision": "TRADE", "confidence": 80, "reason": "  "}, "reason is empty"),
        ({"decision": "TRADE", "confidence": 80, "reason": "x"}, "no valid direction"),
    ],
)
def test_every_contract_violation_is_rejected(payload, message):
    with pytest.raises(AIContractViolation, match=message):
        parse_decision(payload, provider="Gemini", model="test")


def test_a_direction_in_place_of_a_decision_is_accepted_as_a_format_tolerance():
    parsed = parse_decision(
        {"decision": "BUY", "confidence": 80, "reason": "aligned"}, provider="Groq", model="test"
    )
    assert parsed.decision == "TRADE" and parsed.direction == "BUY"


# -- validation -----------------------------------------------------------


def test_a_matching_approval_passes(config, candidate):
    result = validate_ai_decision(decision(direction=candidate.direction), candidate, config.ai)
    assert result.approved is True


def test_an_ai_veto_is_honoured(config, candidate):
    result = validate_ai_decision(
        decision(decision="NO_TRADE", direction=None), candidate, config.ai
    )
    assert result.approved is False
    assert "vetoed" in result.reasons[0]


def test_a_contradicting_direction_resolves_to_no_trade(config, candidate):
    opposite = "SELL" if candidate.direction == "BUY" else "BUY"
    result = validate_ai_decision(decision(direction=opposite), candidate, config.ai)
    assert result.approved is False
    assert result.contract_breach is True
    assert "contradiction resolves to NO TRADE" in result.reasons[0]


def test_low_confidence_is_rejected(config, candidate):
    result = validate_ai_decision(
        decision(direction=candidate.direction, confidence=40), candidate, config.ai
    )
    assert result.approved is False
    assert "below the required" in result.reasons[0]


def test_ai_proposed_levels_never_replace_the_structural_ones(config, candidate):
    """The single most important guarantee in this module."""

    result = validate_ai_decision(
        decision(
            direction=candidate.direction,
            proposed_entry=candidate.entry * 1.5,
            proposed_stop=candidate.stop_loss * 0.5,
            proposed_target=candidate.take_profit * 2,
        ),
        candidate,
        config.ai,
    )
    # The trade may still proceed, but on the ENGINE's numbers, and the
    # divergence is recorded.
    assert result.contract_breach is True
    assert any("structural level is used" in reason for reason in result.reasons)
    assert candidate.entry != result.decision.proposed_entry


def test_a_nonsensical_stop_target_pair_invalidates_the_approval(config, candidate):
    result = validate_ai_decision(
        decision(
            direction="BUY",
            proposed_stop=candidate.take_profit,      # stop above target on a long
            proposed_target=candidate.stop_loss,
        ),
        candidate,
        config.ai,
    )
    assert result.approved is False
    assert result.contract_breach is True


def test_a_non_positive_proposed_level_is_flagged(config, candidate):
    result = validate_ai_decision(
        decision(direction=candidate.direction, proposed_entry=-1.0), candidate, config.ai
    )
    assert result.contract_breach is True


# -- orchestrator-level policy -------------------------------------------


def test_ai_is_not_called_for_weak_setups(config, orchestrator):
    """Cost and hallucination surface are both controlled structurally:
    a below-tier setup never reaches a provider."""

    from bot.scoring.scorer import SetupScore

    ai_config = dataclasses.replace(config.ai, enabled=True, min_tier_for_ai="A")
    orchestrator.config = dataclasses.replace(config, ai=ai_config)
    calls = []
    orchestrator.ai.review = lambda *args: calls.append(args)  # type: ignore[assignment]

    weak = SetupScore(total=50.0, tier="B", components={}, notes=())
    assert orchestrator._ai_review(object(), weak) is None
    assert calls == []


def test_all_providers_failing_means_no_trade_by_default(config, orchestrator):
    from bot.errors import AIError
    from bot.scoring.scorer import SetupScore

    orchestrator.config = dataclasses.replace(
        config, ai=dataclasses.replace(config.ai, enabled=True, gemini_key="k")
    )
    orchestrator.ai.config = orchestrator.config.ai

    def explode(*_args):
        raise AIError("every provider failed")

    orchestrator.ai.review = explode  # type: ignore[assignment]
    score = SetupScore(total=75.0, tier="A", components={}, notes=())
    result = orchestrator._ai_review(object(), score)
    assert result is not None and result.approved is False
    assert "AI validation unavailable" in result.reasons[0]
