"""DEMO-only enforcement. If any of these fail, the system must not run."""

from __future__ import annotations

import dataclasses

import pytest

from bot.config import BrokerConfig, load_config
from bot.errors import ConfigError, DemoVerificationError
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
