"""End-to-end pipeline, crash recovery, and the health contract.

These are the integration tests: market data -> SMC -> score -> risk ->
AI -> execution -> database -> dashboard, plus the restart and failure
scenarios the system is supposed to survive.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.api import DashboardApi
from bot.marketdata.provider import MarketDataProvider
from bot.orchestrator import Orchestrator
from bot.storage.db import in_memory_database
from bot.storage.repositories import Repositories
from fakes import (
    DEFAULT_SPEC,
    SETUP_END,
    FakeBroker,
    aligned_htf,
    bullish_setup_m15,
    flat_market_m15,
)


def build(config, broker, repos) -> Orchestrator:
    return Orchestrator(
        config,
        broker=broker,
        repositories=repos,
        market_data=MarketDataProvider(broker, config),
    )


# -- startup --------------------------------------------------------------


def test_startup_verifies_demo_connects_and_reconciles(config, broker, repos):
    orchestrator = build(config, broker, repos)
    result = orchestrator.startup()
    assert result["ok"] is True
    assert result["demo"]["verified"] is True
    assert orchestrator.startup_complete is True


def test_a_live_account_blocks_startup_and_trips_the_kill_switch(config, broker, repos):
    broker.metadata = {"id": "1", "accNum": "1", "accountType": "LIVE"}
    orchestrator = build(config, broker, repos)
    result = orchestrator.startup()
    assert result["ok"] is False
    assert orchestrator.startup_complete is False
    assert orchestrator.kill_switch.read().reason == "ENVIRONMENT_MISMATCH"


def test_no_scan_runs_before_startup_completes(config, broker, repos):
    orchestrator = build(config, broker, repos)
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert result.skipped_reason is not None
    assert "startup" in result.skipped_reason
    assert broker.submitted == []


# -- the full pipeline ----------------------------------------------------


def test_a_complete_setup_flows_all_the_way_to_a_persisted_trade(orchestrator, broker, repos):
    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert result.executed is not None and result.executed.ok is True
    assert len(broker.submitted) == 1
    submitted = broker.submitted[0]
    assert submitted["direction"] == "BUY"
    assert submitted["quantity"] > 0
    assert submitted["stopLoss"] < submitted["takeProfit"]

    trade = repos.trades.by_execution_id(result.executed.plan.execution_id)
    assert trade["status"] == "OPEN"
    assert trade["setup_grade"] in ("A+", "A", "B")
    assert trade["risk_amount"] > 0

    outcome = result.outcomes[0]
    assert outcome.outcome == "CANDIDATE"
    assert outcome.risk is not None and outcome.risk.approved


def test_the_levels_sent_to_the_broker_are_the_engines_not_a_models(orchestrator, broker):
    result = orchestrator.scan(source="manual", now=SETUP_END)
    candidate = result.outcomes[0].candidate
    submitted = broker.submitted[0]
    assert submitted["stopLoss"] == pytest.approx(candidate.stop_loss, abs=1e-5)
    assert submitted["takeProfit"] == pytest.approx(candidate.take_profit, abs=1e-5)


def test_a_flat_market_produces_no_trade_and_says_why(config, repos):
    broker = FakeBroker()
    flat = flat_market_m15(140)
    for timeframe in ("M15", "H1"):
        broker.set_series(
            "EURUSD", timeframe, flat if timeframe == "M15" else aligned_htf(flat, timeframe=timeframe)
        )
    orchestrator = build(config, broker, repos)
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert result.executed is None
    assert broker.submitted == []
    assert result.outcomes[0].outcome in ("NO_SETUP", "BELOW_TIER")
    assert result.outcomes[0].reason


def test_every_decision_including_no_trade_is_journalled(orchestrator, repos):
    orchestrator.scan(source="manual", now=SETUP_END)
    rows = repos.journal.recent()
    assert rows, "a scan must leave a record of what it decided and why"
    assert any(row["stage"] in ("CANDIDATE", "EXECUTION") for row in rows)


# -- gates ----------------------------------------------------------------


def test_the_kill_switch_stops_the_whole_scan(orchestrator, broker, repos):
    orchestrator.kill_switch.trip("DAILY_LOSS_LIMIT", "test")
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert "kill switch" in (result.skipped_reason or "")
    assert broker.submitted == []


def test_pausing_stops_new_trades(orchestrator, broker):
    orchestrator.set_trading_enabled(False)
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert "paused" in (result.skipped_reason or "")
    assert broker.submitted == []


def test_the_pause_flag_survives_a_restart(config, broker, repos):
    first = build(config, broker, repos)
    first.startup()
    first.set_trading_enabled(False)
    # A brand new orchestrator over the same database = a redeploy.
    second = build(config, broker, repos)
    assert second.trading_enabled is False


def test_a_daily_loss_breach_auto_trips_the_kill_switch(orchestrator, repos, broker):
    repos.daily.record_close(-400.0, now=SETUP_END)  # 4% of a 10k balance, limit is 3%
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert orchestrator.kill_switch.read().reason == "DAILY_LOSS_LIMIT"
    assert broker.submitted == []
    assert "kill switch" in (result.skipped_reason or "")


def test_demo_verification_failure_mid_flight_stops_the_scan(orchestrator, broker):
    broker.metadata = {"accountType": "LIVE"}
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert "DEMO verification failed" in (result.skipped_reason or "")
    assert broker.submitted == []


def test_one_symbol_failing_does_not_kill_the_scan(config, broker, repos):
    multi = dataclasses.replace(config, symbols=("EURUSD", "GBPUSD"))
    orchestrator = build(multi, broker, repos)
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)

    outcomes = {outcome.symbol: outcome for outcome in result.outcomes}
    assert outcomes["GBPUSD"].outcome in ("ERROR", "REJECTED")  # not configured on the fake
    assert outcomes["EURUSD"].outcome == "CANDIDATE"
    assert result.executed is not None and result.executed.ok


def test_only_the_best_opportunity_is_taken_per_cycle(config, broker, repos):
    """Selectivity: several acceptable setups still produce ONE trade."""

    m15 = bullish_setup_m15()
    broker.specs["GBPUSD"] = dataclasses.replace(
        DEFAULT_SPEC, symbol="GBPUSD", broker_name="GBPUSD", base_currency="GBP"
    )
    for timeframe, data in (
        ("M15", m15),
        ("H1", aligned_htf(m15, timeframe="H1")),
    ):
        broker.set_series("GBPUSD", timeframe, data)

    multi = dataclasses.replace(config, symbols=("EURUSD", "GBPUSD"))
    orchestrator = build(multi, broker, repos)
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)

    executable = [outcome for outcome in result.outcomes if outcome.executable]
    assert len(executable) == 2, "both symbols should qualify in this fixture"
    assert len(broker.submitted) == 1, "but only one trade may be taken"


# -- crash recovery -------------------------------------------------------


def test_a_position_survives_a_process_restart(config, broker, repos):
    """BOT OPEN -> POSITION OPEN -> RESTART -> MEMORY LOST -> RECOVERED."""

    first = build(config, broker, repos)
    first.startup()
    result = first.scan(source="manual", now=SETUP_END)
    position_id = result.executed.broker_position_id
    assert position_id

    # The process dies. A new orchestrator with fresh memory boots up.
    second = build(config, broker, repos)
    startup = second.startup()

    assert startup["ok"] is True
    trade = repos.trades.by_position_id(position_id)
    assert trade["status"] == "OPEN", "the position must still be tracked after a restart"
    assert trade["stop_loss"] and trade["take_profit"], "protection levels must be preserved"

    # And it must not open a second position on the same symbol.
    second.scan(source="manual", now=SETUP_END)
    assert len(broker.submitted) == 1


def test_a_position_discovered_after_a_crash_is_adopted(config, broker, repos):
    """The order landed but the process died before recording it."""

    broker.add_position(symbol="EURUSD", direction="BUY", position_id="7777")
    orchestrator = build(config, broker, repos)
    orchestrator.startup()

    trade = repos.trades.by_position_id("7777")
    assert trade is not None and trade["status"] == "ORPHANED"


def test_risk_accounting_survives_a_restart(config, broker, repos):
    repos.daily.record_open(now=SETUP_END)
    repos.daily.record_close(-120.0, now=SETUP_END)
    orchestrator = build(config, broker, repos)
    orchestrator.startup()
    state = orchestrator.build_account_state(now=SETUP_END)
    assert state.daily_realized_pnl == pytest.approx(-120.0)
    assert state.trades_today == 1
    assert state.consecutive_losses == 1


# -- position management cycle -------------------------------------------


def test_management_runs_even_when_scanning_is_paused(orchestrator, broker):
    """Pausing entries must never abandon an open position."""

    orchestrator.scan(source="manual", now=SETUP_END)
    orchestrator.set_trading_enabled(False)
    result = orchestrator.manage_positions(now=SETUP_END)
    assert result["ok"] is True
    assert len(result["positions"]) == 1


def test_management_reports_live_position_metrics(orchestrator, broker):
    orchestrator.scan(source="manual", now=SETUP_END)
    row = orchestrator.manage_positions(now=SETUP_END)["positions"][0]
    for field in ("symbol", "direction", "entryPrice", "stopLoss", "takeProfit", "rMultiple"):
        assert field in row
    assert row["setupGrade"] in ("A+", "A", "B")


# -- health ---------------------------------------------------------------


def test_health_is_honest_about_a_broken_database(orchestrator, repos):
    assert orchestrator.health()["ok"] is True
    repos.db.close()
    health = orchestrator.health()
    assert health["ok"] is False
    assert health["components"]["database"]["ok"] is False
    assert health["tradingPermitted"] is False


def test_health_reports_an_unverified_environment(config, broker, repos):
    broker.metadata = {"accountType": "LIVE"}
    orchestrator = build(config, broker, repos)
    orchestrator.startup()
    health = orchestrator.health()
    assert health["ok"] is False
    assert health["components"]["demo"]["ok"] is False


def test_a_tripped_kill_switch_shows_in_health(orchestrator):
    orchestrator.kill_switch.trip("MANUAL", "operator")
    health = orchestrator.health()
    assert health["tradingPermitted"] is False
    assert health["components"]["killSwitch"]["active"] is True


# -- dashboard projection -------------------------------------------------


def test_the_dashboard_snapshot_shows_only_real_state(config, orchestrator, repos):
    orchestrator.scan(source="manual", now=SETUP_END)
    api = DashboardApi(config, orchestrator, repos)
    snapshot = api.snapshot()

    assert snapshot["account"]["status"] == "LIVE"
    assert snapshot["account"]["data"]["demoVerified"] is True
    assert len(snapshot["positions"]["data"]) == 1
    assert snapshot["scan"]["data"]["decision"].startswith("BUY")
    assert snapshot["health"]["ok"] is True


def test_the_dashboard_reports_offline_rather_than_inventing_numbers(config, orchestrator, repos):
    """A value that was never read is a gap, not a zero."""

    from bot.errors import BrokerError
    from bot.broker.cache import LiveCache

    def explode():
        raise BrokerError("broker unreachable")

    orchestrator.broker.account_state = explode  # type: ignore[assignment]
    # Nothing has ever been read: startup's seed is discarded so this is
    # the cold case, which is the one rule 6 is about.
    orchestrator.live = LiveCache()
    api = DashboardApi(config, orchestrator, repos)
    account = api.account()
    assert account["status"] == "OFFLINE"
    assert account["data"] is None
    assert "has been read from the broker yet" in account["error"]


def test_a_broker_outage_serves_the_last_read_with_its_age_not_a_blank(
    config, orchestrator, repos
):
    """A number that WAS read stays visible, labelled with when.

    The dashboard no longer calls the broker (bot/broker/cache.py), so an
    outage no longer blanks the page — it freezes it. That is only honest
    if the age and the refresh failure travel with the value, which is
    what this pins. Rule 6 forbids inventing a number; it does not forbid
    showing a real one and saying how old it is.
    """

    from bot.errors import BrokerError

    orchestrator.startup()
    assert orchestrator.live.get("account").present

    def explode():
        raise BrokerError("broker unreachable")

    orchestrator.broker.account_state = explode  # type: ignore[assignment]
    orchestrator.broker.positions = explode  # type: ignore[assignment]

    # The TRADING path is unaffected: it still reads the broker and still
    # refuses to proceed. Only the display falls back to the last read.
    with pytest.raises(BrokerError):
        orchestrator.build_account_state()

    api = DashboardApi(config, orchestrator, repos)
    account = api.account()
    assert account["status"] == "LIVE"
    assert account["data"]["balance"] > 0
    assert account["asOf"] is not None
    assert account["ageSeconds"] is not None
    assert account["refreshError"] == "broker unreachable"


def test_the_dashboard_never_calls_the_broker(config, orchestrator, repos):
    """The 30-second proxy timeout, pinned as a property.

    Every broker read shares one throttle that spaces requests and holds a
    lock while it sleeps. A dashboard request that touches the broker
    therefore queues behind a whole scan, which is how /snapshot came to
    take longer than the proxy would wait while the bot was healthy. A
    test that only measured latency would pass on a quiet broker, so this
    asserts the structural fact instead: the page makes NO broker calls.
    """

    orchestrator.startup()
    orchestrator.scan(source="manual", now=SETUP_END)

    calls: list[str] = []

    def forbid(name):
        def guard(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"the dashboard called broker.{name}()")

        return guard

    for name in ("account_state", "positions", "orders", "quote", "candles"):
        setattr(orchestrator.broker, name, forbid(name))

    api = DashboardApi(config, orchestrator, repos)
    snapshot = api.snapshot()

    assert calls == []
    assert snapshot["account"]["status"] == "LIVE"
    assert snapshot["positions"]["status"] == "LIVE"
    assert snapshot["risk"]["status"] == "LIVE"


def test_performance_is_not_fabricated_from_an_empty_history(config, orchestrator, repos):
    api = DashboardApi(config, orchestrator, repos)
    data = api.performance()["data"]
    assert data["trades"] == 0
    assert data["winRate"] is None
    assert data["sample"] == "insufficient"


def test_the_dashboard_cannot_open_a_trade(config, orchestrator, repos):
    api = DashboardApi(config, orchestrator, repos)
    surface = {name for name in dir(api) if not name.startswith("_")}
    assert not {"place_order", "open_trade", "submit", "execute"} & surface
