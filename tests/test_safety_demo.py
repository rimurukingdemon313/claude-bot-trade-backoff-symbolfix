"""DEMO-only enforcement. If any of these fail, the system must not run."""

from __future__ import annotations

import dataclasses

import pytest

from bot.config import BrokerConfig, load_config
from bot.errors import ConfigError, DemoVerificationError
from bot.broker.tradelocker import decode_token_claims
from bot.safety.demo_guard import require_demo, verify_demo
from bot.safety.kill_switch import KillSwitch


def test_config_refuses_a_live_endpoint():
    with pytest.raises(ConfigError, match="LIVE endpoint"):
        load_config({"TRADELOCKER_URL": "https://live.tradelocker.com/backend-api"})


def test_config_refuses_to_disable_the_demo_requirement(config):
    forced = dataclasses.replace(config, require_demo=False)
    with pytest.raises(ConfigError, match="require_demo cannot be disabled"):
        forced.validate()


@pytest.mark.parametrize(
    "metadata",
    [
        {"accountType": "DEMO"},
        {"type": "demo"},
        {"isDemo": True},
        {"name": "Practice Account"},
    ],
)
def test_positive_demo_signals_pass(config, metadata):
    assert verify_demo(config, metadata, stage="test").verified is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"accountType": "LIVE"},
        {"type": "real"},
        {"isDemo": False},
        {"name": "Live Funded"},
        {},               # no metadata at all
        None,             # broker unreachable
        {"accountType": ""},
    ],
)
def test_anything_short_of_positive_proof_fails(config, metadata):
    verification = verify_demo(config, metadata, stage="test")
    assert verification.verified is False
    assert verification.reason


def test_live_url_beats_a_demo_looking_account(config):
    # Even if the broker claims DEMO, a live URL is disqualifying.
    live = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, base_url="https://live.tradelocker.com/x")
    )
    assert verify_demo(live, {"accountType": "DEMO"}, stage="test").verified is False


def test_unknown_url_is_not_assumed_demo(config):
    unknown = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, base_url="https://api.example.com/v1")
    )
    assert verify_demo(unknown, {"accountType": "DEMO"}, stage="test").verified is False


def test_require_demo_raises_and_names_the_stage(config):
    with pytest.raises(DemoVerificationError, match="before_order_submission"):
        require_demo(config, {"accountType": "LIVE"}, stage="before_order_submission")


# -- signal 2 from the access token ---------------------------------------
#
# GATESFX returns account records with no type field of any kind — the live
# deployment sat blocked on ENVIRONMENT_MISMATCH with a correctly configured
# demo account. The broker still states the environment; it states it in the
# session it signed. These tests pin that second source and, just as
# important, pin that it cannot be used to soften anything.


#: The account record GATESFX actually returns: no type field anywhere.
GATESFX_ACCOUNT = {
    "id": "2475112",
    "name": "2475112",
    "currency": "USD",
    "accNum": "1",
    "accountBalance": "998.34",
    "status": "ACTIVE",
}


def jwt(payload: dict) -> str:
    """A JWT with a real payload and a junk signature.

    The signature is junk on purpose: the guard must read the claims as
    evidence and never as authority.
    """

    import base64
    import json

    def segment(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'HS256'})}.{segment(payload)}.not-a-real-signature"


def test_a_demo_claim_satisfies_signal_two_when_the_account_record_is_silent(config):
    claims = decode_token_claims(jwt({"sub": "2475112", "iss": "https://demo.tradelocker.com"}))
    verification = verify_demo(config, GATESFX_ACCOUNT, stage="test", claims=claims)
    assert verification.verified is True
    assert verification.account_ok is True


def test_a_live_claim_fails_even_when_the_account_record_says_demo(config):
    """A contradiction resolves the safe way, never the convenient one."""

    claims = decode_token_claims(jwt({"iss": "https://live.tradelocker.com"}))
    verification = verify_demo(config, {"accountType": "DEMO"}, stage="test", claims=claims)
    assert verification.verified is False
    assert "LIVE" in (verification.reason or "")


def test_a_live_account_record_fails_even_when_the_token_says_demo(config):
    claims = decode_token_claims(jwt({"iss": "https://demo.tradelocker.com"}))
    verification = verify_demo(config, {"accountType": "LIVE"}, stage="test", claims=claims)
    assert verification.verified is False


def test_a_token_with_no_environment_claim_is_still_a_failure(config):
    """The whole point: absence of evidence is not evidence."""

    claims = decode_token_claims(jwt({"sub": "2475112", "exp": 1893456000}))
    verification = verify_demo(config, GATESFX_ACCOUNT, stage="test", claims=claims)
    assert verification.verified is False


def test_the_failure_names_the_fields_it_actually_saw(config):
    """The old message said only that nothing matched, which cost a full
    deployment cycle to diagnose. It must say what it looked at."""

    claims = decode_token_claims(jwt({"sub": "2475112", "exp": 1893456000}))
    reason = verify_demo(config, GATESFX_ACCOUNT, stage="test", claims=claims).reason or ""
    assert "accNum" in reason and "status" in reason  # account keys
    assert "token fields seen" in reason or "token: nothing returned" in reason


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "only.two", "a.!!!not-base64!!!.c", None],
)
def test_an_unreadable_token_yields_no_claims_rather_than_raising(token):
    """This runs immediately before every order. It may not throw."""

    assert decode_token_claims(token) is None


def test_token_claims_are_scalars_only_and_bounded():
    """Untrusted input: no nested structure, no unbounded blob reaches the guard."""

    claims = decode_token_claims(
        jwt({"iss": "demo", "accounts": [1, 2, 3], "meta": {"a": 1}, "blob": "x" * 5000})
    )
    assert claims == {"iss": "demo"}


def test_a_nickname_containing_demo_cannot_be_used_as_a_token_signal(config):
    """Claims are read from an allowlist, not scanned wholesale.

    A user-chosen account nickname is not the broker's statement about the
    environment, and must never be promoted into one.
    """

    claims = decode_token_claims(jwt({"nickname": "my demo account", "sub": "2475112"}))
    assert verify_demo(config, GATESFX_ACCOUNT, stage="test", claims=claims).verified is False


def test_the_url_signal_still_has_to_pass_on_its_own(config):
    """Signal 2 broadening must not turn two signals into one."""

    claims = decode_token_claims(jwt({"iss": "https://demo.tradelocker.com"}))
    unknown = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, base_url="https://api.example.com/v1")
    )
    assert verify_demo(unknown, GATESFX_ACCOUNT, stage="test", claims=claims).verified is False


def test_the_guard_never_sees_the_token_itself(config):
    """The claims cross the boundary; the credential does not."""

    import inspect

    from bot.safety import demo_guard

    source = inspect.getsource(demo_guard)
    for forbidden in ("accessToken", "_access_token", "Authorization", "Bearer"):
        assert forbidden not in source


def test_kill_switch_survives_a_new_process(repos):
    KillSwitch(repos.state).trip("DAILY_LOSS_LIMIT", "lost 3%")
    # A fresh object reading the same database is what a restart looks like.
    reloaded = KillSwitch(repos.state)
    assert reloaded.active is True
    assert reloaded.read().reason == "DAILY_LOSS_LIMIT"


def test_kill_switch_keeps_its_first_reason(repos):
    switch = KillSwitch(repos.state)
    switch.trip("MAX_DRAWDOWN", "first")
    switch.trip("MANUAL", "second")
    assert switch.read().reason == "MAX_DRAWDOWN"


def test_safety_trips_cannot_be_cleared_without_force(repos):
    switch = KillSwitch(repos.state)
    switch.trip("ENVIRONMENT_MISMATCH", "live detected")
    assert switch.clear().active is True
    assert switch.clear(force=True).active is False


def test_operational_trips_clear_normally(repos):
    switch = KillSwitch(repos.state)
    switch.trip("DAILY_LOSS_LIMIT", "")
    assert switch.clear().active is False


# -- a broker outage is not a misconfiguration ----------------------------
#
# Both stop trading. Only one should need a human with a key afterwards.
# Conflating them latched ENVIRONMENT_MISMATCH through a transient outage
# and kept the bot down long after the broker came back, under a message
# accusing the account of being live when nobody had managed to ask it.


def test_an_unreachable_broker_is_not_reported_as_a_contradiction(config):
    verification = verify_demo(config, None, stage="test", claims=None)
    assert verification.verified is False  # still fails closed
    assert verification.contradicted is False


def test_a_silent_account_record_is_not_a_contradiction(config):
    verification = verify_demo(config, GATESFX_ACCOUNT, stage="test", claims={})
    assert verification.verified is False
    assert verification.contradicted is False


@pytest.mark.parametrize(
    "account", [{"accountType": "LIVE"}, {"type": "real"}, {"isDemo": False}]
)
def test_a_live_broker_answer_is_a_contradiction(config, account):
    verification = verify_demo(config, account, stage="test")
    assert verification.verified is False
    assert verification.contradicted is True


def test_a_non_demo_url_is_a_contradiction_whatever_the_broker_says(config):
    """Our own misconfiguration, and never softened by a retry."""

    unknown = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, base_url="https://api.example.com/v1")
    )
    verification = verify_demo(unknown, {"accountType": "DEMO"}, stage="test")
    assert verification.contradicted is True


def test_a_broker_outage_does_not_latch_the_kill_switch(orchestrator, broker, repos):
    """The bug this exists to prevent: an outage that needs a manual unlock."""

    broker.metadata = None  # broker answered nothing
    broker.claims = None
    orchestrator.startup()

    state = orchestrator.kill_switch.read()
    assert state.active is False, f"latched on an outage: {state.reason}"
    assert orchestrator.startup_complete is False  # and trading is still blocked


def test_a_live_account_does_latch_the_kill_switch(orchestrator, broker):
    broker.metadata = {"accountType": "LIVE"}
    broker.claims = None
    orchestrator.startup()

    state = orchestrator.kill_switch.read()
    assert state.active is True
    assert state.reason == "ENVIRONMENT_MISMATCH"
    # SAFETY class: a plain clear must not release it.
    assert orchestrator.kill_switch.clear().active is True


def test_the_bot_recovers_by_itself_once_the_broker_returns(orchestrator, broker):
    """No human in the loop: the outage clears, the next startup succeeds."""

    broker.metadata = None
    orchestrator.startup()
    assert orchestrator.startup_complete is False

    broker.metadata = {"id": "1", "accNum": "1", "accountType": "DEMO", "currency": "USD"}
    orchestrator.startup()
    assert orchestrator.startup_complete is True
    assert orchestrator.kill_switch.read().active is False
