"""The HTTP surface the dashboard talks to, and the broker decoding layer."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
import urllib.error
import urllib.request

import pytest

from bot.broker.models import InstrumentSpec
from bot.broker.tradelocker import TradeLockerBroker, normalize_symbol, split_currencies
from bot.errors import BrokerRejected, ConfigError
from bot.service import BotService, make_handler, serve
from fakes import SETUP_END


# -- broker decoding ------------------------------------------------------


def test_symbol_normalisation_handles_broker_suffixes():
    assert normalize_symbol("EUR/USD") == "EURUSD"
    assert normalize_symbol("EURUSD.r") == "EURUSDR"


def test_currency_splitting():
    assert split_currencies("GBPJPY") == ("GBP", "JPY")
    assert split_currencies("XAUUSD") == ("XAU", "USD")
    assert split_currencies("US500") == (None, None)


def test_lot_rounding_never_rounds_up(monkeypatch):
    spec = InstrumentSpec(
        "EURUSD", "EURUSD", 1, 2, None, 100_000, 0.00001, None, 0.01, 0.01, 100,
        "EUR", "USD", "USD", 5,
    )
    assert spec.round_lot(0.2789) == 0.27
    assert spec.round_lot(0.999) == 0.99


def test_an_ambiguous_symbol_is_refused_rather_than_guessed(config):
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker._instruments_raw = [
        {"name": "EURUSD.R", "tradableInstrumentId": 1, "routes": [{"id": 1, "type": "TRADE"}]},
        {"name": "EURUSDX", "tradableInstrumentId": 2, "routes": [{"id": 2, "type": "TRADE"}]},
    ]
    broker._instrument_cache_at = float("inf")
    with pytest.raises(BrokerRejected, match="matches multiple broker instruments"):
        broker.instrument("EURUSD")


def test_an_instrument_without_a_contract_size_is_refused(config):
    """Guessing the contract size would mis-size every order on it."""

    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker._instruments_raw = [
        {"name": "WEIRD", "tradableInstrumentId": 9, "routes": [{"id": 1, "type": "TRADE"}]}
    ]
    broker._instrument_cache_at = float("inf")
    with pytest.raises(BrokerRejected, match="does not expose a contract size"):
        broker.instrument("WEIRD")


def test_an_instrument_without_a_trade_route_is_refused(config):
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker._instruments_raw = [
        {"name": "NOROUTE", "tradableInstrumentId": 9, "contractSize": 100, "routes": []}
    ]
    broker._instrument_cache_at = float("inf")
    with pytest.raises(BrokerRejected, match="no TRADE route"):
        broker.instrument("NOROUTE")


def test_missing_credentials_are_reported_by_name(config):
    stripped = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, email=None, password=None)
    )
    broker = TradeLockerBroker(stripped)
    with pytest.raises(ConfigError, match="TRADELOCKER_EMAIL"):
        broker.login()


# -- HTTP service ---------------------------------------------------------


@pytest.fixture()
def service(config, broker, repos, monkeypatch):
    monkeypatch.setattr("bot.service.open_database", lambda _config: repos.db)
    instance = BotService(config, broker=broker)
    instance.orchestrator.startup()
    return instance


@pytest.fixture()
def base_url(service):
    server = serve(service, host="127.0.0.1", port=0)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def post(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_health_endpoint_reports_ok_when_everything_works(base_url):
    status, payload = get(f"{base_url}/healthz")
    assert status == 200 and payload["status"] == "ok"


def test_health_endpoint_reports_503_when_a_critical_component_is_down(base_url, service):
    service.repos.db.close()
    status, payload = get(f"{base_url}/healthz")
    assert status == 503
    assert payload["status"] == "degraded"


def test_the_snapshot_endpoint_fills_the_whole_dashboard(base_url):
    status, payload = get(f"{base_url}/api/snapshot")
    assert status == 200
    for section in ("account", "positions", "history", "performance", "scan", "risk", "health"):
        assert section in payload


def test_unknown_routes_404(base_url):
    assert get(f"{base_url}/api/nope")[0] == 404


def test_control_endpoints_pause_and_resume_scanning(base_url, service):
    assert post(f"{base_url}/api/control/scanning", {"enabled": False})[1]["enabled"] is False
    assert service.orchestrator.trading_enabled is False
    assert post(f"{base_url}/api/control/scanning", {"enabled": True})[1]["enabled"] is True


def test_a_bad_control_payload_is_rejected(base_url):
    status, payload = post(f"{base_url}/api/control/scanning", {"enabled": "yes please"})
    assert status == 400


def test_the_kill_switch_can_be_tripped_and_cleared_over_http(base_url, service):
    post(f"{base_url}/api/control/kill-switch", {"active": True, "reason": "manual"})
    assert service.orchestrator.kill_switch.active is True
    post(f"{base_url}/api/control/kill-switch", {"active": False})
    assert service.orchestrator.kill_switch.active is False


def test_a_safety_trip_cannot_be_cleared_over_http_without_force(base_url, service):
    service.orchestrator.kill_switch.trip("ENVIRONMENT_MISMATCH", "live detected")
    post(f"{base_url}/api/control/kill-switch", {"active": False})
    assert service.orchestrator.kill_switch.active is True
    post(f"{base_url}/api/control/kill-switch", {"active": False, "force": True})
    assert service.orchestrator.kill_switch.active is False


def test_commands_require_the_token_when_one_is_configured(config, broker, repos, monkeypatch):
    monkeypatch.setattr("bot.service.open_database", lambda _c: repos.db)
    secured = dataclasses.replace(config, dashboard_token="s3cret")
    instance = BotService(secured, broker=broker)
    instance.orchestrator.startup()
    server = serve(instance, host="127.0.0.1", port=0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert post(f"{url}/api/control/scanning", {"enabled": False})[0] == 401
        assert post(f"{url}/api/control/scanning", {"enabled": False}, token="s3cret")[0] == 200
        # Reads stay open so the dashboard can always display state.
        assert get(f"{url}/healthz")[0] == 200
    finally:
        server.shutdown()


def test_a_manual_scan_can_be_triggered_over_http(base_url, service, broker):
    status, payload = post(f"{base_url}/api/control/scan", {})
    assert status == 200
    assert "decision" in payload


def test_the_api_never_exposes_a_secret(base_url, monkeypatch):
    monkeypatch.setenv("TRADELOCKER_PASSWORD", "hunter2-very-secret")
    _, payload = get(f"{base_url}/api/snapshot")
    assert "hunter2-very-secret" not in json.dumps(payload)


def test_graceful_shutdown_does_not_touch_positions(service, broker):
    broker.add_position(symbol="EURUSD")
    service.shutdown()
    assert broker.closures == [], "a deploy must never close a live position"
    assert len(broker.positions()) == 1


# -- the doctor over HTTP --------------------------------------------------


def test_the_doctor_report_is_readable_over_http(base_url):
    """Exposed so the account can be verified from a browser — including a
    phone — without a terminal."""

    status, payload = get(f"{base_url}/api/doctor")
    assert status == 200
    assert "verdict" in payload
    assert "text" in payload, "a human-readable rendering must be included"
    assert isinstance(payload.get("checks"), list)


def test_the_doctor_endpoint_masks_account_figures(base_url, service, monkeypatch):
    """It is a read endpoint, so it must never publish a balance."""

    monkeypatch.setenv("TRADELOCKER_PASSWORD", "hunter2-very-secret-value")
    _, payload = get(f"{base_url}/api/doctor")
    body = json.dumps(payload)
    assert "hunter2-very-secret-value" not in body
    balance = service.broker.account_state().balance
    # 10000.0 formatted any of the ways the report might render it.
    for rendering in (f"{balance:.2f}", f"{balance:,.2f}", str(balance)):
        assert rendering not in body, f"account balance leaked as {rendering!r}"


def test_the_doctor_endpoint_answers_even_when_the_broker_is_broken(base_url, service):
    from bot.errors import BrokerError

    def explode(*args, **kwargs):
        raise BrokerError("total outage")

    service.orchestrator.broker.ensure_session = explode  # type: ignore[assignment]
    status, payload = get(f"{base_url}/api/doctor")
    assert status == 200, "a diagnostic must always answer"
    assert payload["verdict"] == "FAIL"


# -- configuration checklist -----------------------------------------------


def test_the_setup_endpoint_answers_before_credentials_exist(base_url):
    """This is the screen to read when nothing else works yet."""

    status, payload = get(f"{base_url}/api/setup")
    assert status == 200
    for key in ("ready", "missingRequired", "settings", "warnings", "pasteBlock", "nextStep"):
        assert key in payload
    assert payload["nextStep"]


def test_the_setup_endpoint_never_reveals_a_value(base_url, monkeypatch):
    monkeypatch.setenv("TRADELOCKER_PASSWORD", "hunter2-very-secret-value")
    monkeypatch.setenv("DASHBOARD_TOKEN", "token-abcdef-123456")
    _, payload = get(f"{base_url}/api/setup")
    body = json.dumps(payload)
    assert "hunter2-very-secret-value" not in body
    assert "token-abcdef-123456" not in body
    # Presence is reported; the value is not.
    names = {item["name"]: item for item in payload["settings"]}
    assert names["TRADELOCKER_PASSWORD"]["present"] is True
    assert "value" not in names["TRADELOCKER_PASSWORD"]


def test_missing_required_settings_are_named(monkeypatch):
    from bot.config import load_config
    from bot.setup_status import build_setup_report

    for name in ("TRADELOCKER_EMAIL", "TRADELOCKER_PASSWORD", "TRADELOCKER_SERVER", "TRADELOCKER_ACC_ID"):
        monkeypatch.delenv(name, raising=False)
    report = build_setup_report(load_config())
    assert report.ready is False
    assert "TRADELOCKER_SERVER" in report.missing_required
    assert "TRADELOCKER_SERVER=" in report.paste_block


def test_a_missing_database_url_is_warned_about(monkeypatch):
    from bot.config import load_config
    from bot.setup_status import build_setup_report

    monkeypatch.delenv("DATABASE_URL", raising=False)
    report = build_setup_report(load_config())
    assert any("reset on every redeploy" in warning for warning in report.warnings)


def test_demo_live_mode_is_warned_about(monkeypatch):
    from bot.config import load_config
    from bot.setup_status import build_setup_report

    monkeypatch.setenv("TRADING_MODE", "demo_live")
    report = build_setup_report(load_config())
    assert any("real orders" in warning.lower() for warning in report.warnings)


def test_an_unreachable_profit_floor_is_warned_about(monkeypatch):
    from bot.config import load_config
    from bot.setup_status import build_setup_report

    # $300 at the 1% ceiling risks $3; even an exceptional 1:10 structural
    # target returns $30, short of the $40 floor. Nothing but more equity
    # fixes that, and the operator must be told rather than left watching
    # a bot that never trades.
    report = build_setup_report(load_config(), equity=300.0)
    assert any("No setup can pass this filter" in warning for warning in report.warnings)


def test_a_reachable_but_demanding_profit_floor_is_also_warned_about(monkeypatch):
    """The quiet-bot case: it CAN trade, but only on exceptional setups.

    Silence here is what made the last deployment look broken — a healthy
    bot returning NO TRADE every day is indistinguishable from a stuck one
    unless it says why.
    """

    from bot.config import load_config
    from bot.setup_status import build_setup_report

    report = build_setup_report(load_config(), equity=1_000.0)
    assert any("reachable but demanding" in warning for warning in report.warnings)


def test_ai_enabled_without_a_provider_is_flagged_as_blocking(monkeypatch):
    """The silent never-trade trap: AI required, no key, no permission to
    proceed without one. Failing closed is correct; failing closed silently
    is not."""

    from bot.config import load_config
    from bot.setup_status import build_setup_report

    for name in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_ALLOW_TRADE_WITHOUT_AI", "false")

    report = build_setup_report(load_config())
    assert any("never open a trade" in warning for warning in report.warnings), report.warnings


@pytest.mark.parametrize(
    "env",
    [
        {"AI_ENABLED": "false"},
        {"AI_ENABLED": "true", "AI_ALLOW_TRADE_WITHOUT_AI": "true"},
        {"AI_ENABLED": "true", "GEMINI_API_KEY": "a-key"},
    ],
)
def test_each_documented_fix_clears_the_ai_block(monkeypatch, env):
    from bot.config import load_config
    from bot.setup_status import build_setup_report

    for name in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_ALLOW_TRADE_WITHOUT_AI", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    report = build_setup_report(load_config())
    assert not any("never open a trade" in warning for warning in report.warnings)


def test_health_reports_the_ai_gate_as_blocking(config, broker, repos, monkeypatch):
    import dataclasses

    from bot.marketdata.provider import MarketDataProvider
    from bot.orchestrator import Orchestrator

    blocked = dataclasses.replace(
        config,
        ai=dataclasses.replace(
            config.ai, enabled=True, gemini_key=None, groq_key=None,
            allow_trade_without_ai=False,
        ),
    )
    orchestrator = Orchestrator(
        blocked, broker=broker, repositories=repos,
        market_data=MarketDataProvider(broker, blocked),
    )
    orchestrator.startup()
    ai = orchestrator.health()["components"]["ai"]
    assert ai["blockingAllTrades"] is True
    assert "every setup is rejected" in ai["note"]


# -- a closed market is a schedule, not an outage -------------------------
#
# On a Sunday the broker's own answer is "circuit open after 5 consecutive
# failures", and every panel rendered that for two days. It taught the
# operator to ignore a warning that will one day be real, and it was the
# bot's own doing: it kept calling a broker it already knew was shut.


SUNDAY = datetime(2026, 9, 13, 11, 30, tzinfo=timezone.utc)
WEDNESDAY = datetime(2026, 9, 9, 11, 30, tzinfo=timezone.utc)


def test_no_broker_call_is_made_while_the_market_is_shut(orchestrator, broker):
    assert SUNDAY.strftime("%A") == "Sunday"
    before = len(broker.submitted), broker.calls if hasattr(broker, "calls") else 0

    result = orchestrator.scan(source="test", now=SUNDAY)

    assert "closed for the weekend" in (result.skipped_reason or "")
    assert len(broker.submitted) == before[0]  # nothing was sent


def test_position_polling_stands_down_over_the_weekend(orchestrator):
    outcome = orchestrator.manage_positions(now=SUNDAY)
    assert outcome["ok"] is True
    assert "closed for the weekend" in outcome["skipped"]


def test_a_weekday_scan_is_not_skipped_as_a_weekend(orchestrator):
    result = orchestrator.scan(source="test", now=WEDNESDAY)
    assert "closed for the weekend" not in (result.skipped_reason or "")


def test_health_reports_the_closed_market_without_calling_it_a_fault(orchestrator):
    health = orchestrator.health()
    market = health["components"]["market"]
    # Whatever day the suite runs on, a shut market is never a failure.
    assert market["ok"] is True
    assert market["open"] is not None
    assert health["marketClosed"] == (not market["open"])


# -- the contract size lives behind a second call -------------------------
#
# A correctly configured GATESFX account produced "no symbol produced an
# executable candidate" on every scan, with every symbol reporting
# "instrument 'EURUSD.R' does not expose a contract size". The account was
# fine: the account's instrument LIST is a directory — id, name, type,
# routes — and carries no contract size at all. The size is one request
# away, and sizing was reading only the directory.


def directory_broker(config, *, with_size: bool, detail: dict | None = None):
    """A broker whose instrument directory may or may not carry the size."""

    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    record = {
        "name": "EURUSD.R",
        "tradableInstrumentId": 278,
        "routes": [{"id": 10, "type": "TRADE"}, {"id": 11, "type": "INFO"}],
    }
    if with_size:
        record["contractSize"] = 100_000
    broker._instruments_raw = [record]
    broker._instrument_cache_at = float("inf")

    calls: list[tuple[str, dict]] = []

    def fake_get(path, query=None):
        calls.append((path, dict(query or {})))
        return {"d": detail or {}}

    broker.get = fake_get  # type: ignore[assignment]
    return broker, calls


def test_the_contract_size_is_fetched_from_the_instrument_detail(config):
    broker, calls = directory_broker(
        config,
        with_size=False,
        detail={"contractSize": 100_000, "tickSize": 0.00001, "digits": 5},
    )

    spec = broker.instrument("EURUSD")

    assert spec.contract_size == 100_000
    assert spec.broker_name == "EURUSD.R"
    assert calls, "the detail endpoint was never called"
    path, query = calls[0]
    assert path == "/trade/instruments/278"
    assert query.get("routeId") == 11, "the INFO route identifies the instrument"


def test_no_second_call_is_made_when_the_directory_already_has_the_size(config):
    """One request per symbol is worth paying; two is not."""

    broker, calls = directory_broker(config, with_size=True)
    spec = broker.instrument("EURUSD")

    assert spec.contract_size == 100_000
    assert calls == [], "the detail call must be skipped when the size is already known"


def test_a_size_still_missing_after_the_detail_call_is_refused(config):
    """Guessing it would mis-size every order on the instrument."""

    broker, _ = directory_broker(config, with_size=False, detail={"tickSize": 0.00001})
    with pytest.raises(BrokerRejected, match="does not expose a contract size"):
        broker.instrument("EURUSD")


def test_the_refusal_names_the_fields_the_broker_actually_returned(config):
    """The first version said only that the field was absent, which cost a
    deploy cycle to diagnose — exactly as the DEMO guard's message did."""

    broker, _ = directory_broker(
        config, with_size=False, detail={"tickSize": 0.00001, "marginRate": 0.02}
    )
    with pytest.raises(BrokerRejected) as excinfo:
        broker.instrument("EURUSD")
    assert "marginRate" in str(excinfo.value)
    assert "tickSize" in str(excinfo.value)


def test_a_failed_detail_lookup_travels_with_the_refusal(config):
    """Otherwise the symbol is skipped for a reason nobody can see."""

    from bot.errors import BrokerError

    broker, _ = directory_broker(config, with_size=False)

    def failing_get(path, query=None):
        raise BrokerError("route 11 rejected the lookup")

    broker.get = failing_get  # type: ignore[assignment]
    with pytest.raises(BrokerRejected, match="route 11 rejected the lookup"):
        broker.instrument("EURUSD")


# -- brands rename the /trade/config panels -------------------------------
#
# working_orders and order_history failed on an account where
# authentication, instruments and account state all worked. The code asked
# for one spelling of a panel key and treated its absence as fatal, so a
# single renamed key took out the whole table.


def config_broker(config, trade_config: dict, payload: dict):
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker._trade_config = trade_config

    def fake_get(path, query=None):
        return payload

    broker.get = fake_get  # type: ignore[assignment]
    return broker


COLUMNS = {"columns": [{"id": "id"}, {"id": "side"}, {"id": "qty"}, {"id": "status"}]}


def test_orders_decode_under_an_alternative_panel_name(config):
    """openOrdersConfig is the same table by another name."""

    broker = config_broker(
        config,
        {"openOrdersConfig": COLUMNS},
        {"orders": [["1", "buy", 0.1, "working"]]},
    )
    orders = broker.orders()
    assert len(orders) == 1
    assert orders[0].order_id == "1"
    assert orders[0].direction == "BUY"
    assert orders[0].quantity == pytest.approx(0.1)


def test_rows_that_already_name_their_fields_are_not_zipped(config):
    """Zipping column names over a dict iterates its KEYS.

    That produces confident nonsense — {"id": "qty", "qty": "side"} — which
    every reader downstream would treat as real data.
    """

    broker = config_broker(
        config,
        {"ordersConfig": COLUMNS},
        {"orders": [{"id": "77", "side": "sell", "qty": 0.25, "status": "working"}]},
    )
    orders = broker.orders()
    assert orders[0].order_id == "77"
    assert orders[0].direction == "SELL"
    assert orders[0].quantity == pytest.approx(0.25)


def test_an_undescribed_panel_names_what_the_broker_did_describe(config):
    """So the next operator does not have to guess the brand's spelling."""

    broker = config_broker(
        config,
        {"somethingElseConfig": COLUMNS, "positionsConfig": COLUMNS},
        {"orders": [["1", "buy", 0.1, "working"]]},
    )
    with pytest.raises(Exception) as excinfo:
        broker.orders()
    message = str(excinfo.value)
    assert "somethingElseConfig" in message and "positionsConfig" in message


def test_an_empty_table_is_empty_not_an_error(config):
    """No open orders is the normal case, not a decoding failure."""

    broker = config_broker(config, {}, {"orders": []})
    assert broker.orders() == []


# -- the response envelope ------------------------------------------------


def test_the_envelope_is_stripped_even_without_a_status_field():
    """Requiring both keys left readers holding {"d": {...}}.

    Every one of them then found none of the keys it wanted and reported
    an empty table — indistinguishable from an account with nothing in it.
    """

    unwrap = TradeLockerBroker._unwrap
    assert unwrap({"s": "ok", "d": {"orders": [1]}}) == {"orders": [1]}
    assert unwrap({"d": {"orders": [1]}}) == {"orders": [1]}


def test_a_payload_that_merely_contains_a_d_field_is_left_alone():
    """Unwrapping it would discard the rest of a legitimate response."""

    payload = {"d": 5, "orders": [1]}
    assert TradeLockerBroker._unwrap(payload) == payload


def test_a_non_ok_status_is_refused_rather_than_unwrapped():
    with pytest.raises(BrokerRejected, match="returned status"):
        TradeLockerBroker._unwrap({"s": "error", "d": {}, "errmsg": "rejected"})


def test_a_plain_payload_passes_through():
    assert TradeLockerBroker._unwrap({"orders": [1]}) == {"orders": [1]}
    assert TradeLockerBroker._unwrap([1, 2]) == [1, 2]


# -- the diagnostic must not spend the budget the trading path needs ------


def test_the_startup_verification_runs_after_the_startup_sequence(
    config, broker, repos, monkeypatch
):
    """It ran first, and that is why the bot could not start.

    The verification makes a few dozen broker calls, and TradeLocker sits
    behind Cloudflare. Running it before the startup sequence spent the
    rate-limit budget on a diagnostic and left the circuit open, so the
    sequence that decides whether the bot may trade failed on "broker
    circuit open after 5 consecutive failures" at an uptime of zero
    minutes.
    """

    monkeypatch.setattr("bot.service.open_database", lambda _c: repos.db)
    monkeypatch.setenv("STARTUP_DOCTOR", "true")

    order: list[str] = []
    service = BotService(config, broker=broker)
    real_startup = service.orchestrator.startup

    def traced_startup():
        order.append("startup")
        return real_startup()

    service.orchestrator.startup = traced_startup  # type: ignore[assignment]
    monkeypatch.setattr(
        service, "_log_startup_verification", lambda: order.append("doctor")
    )
    service.start()
    service.scheduler.stop(timeout=0.1)

    assert order == ["startup", "doctor"], (
        f"the diagnostic must never precede the startup sequence, got {order}"
    )


def test_the_startup_verification_is_skipped_when_the_circuit_is_open(
    config, repos, monkeypatch, capsys
):
    """Adding calls to a broker that has stopped answering helps nothing."""

    from bot.broker.http import CircuitBreaker
    from bot.broker.tradelocker import TradeLockerBroker

    live = TradeLockerBroker(config)
    circuit = CircuitBreaker(failure_threshold=1)
    circuit.record_failure()
    live.transport.circuit = circuit
    assert circuit.state != "closed"

    monkeypatch.setattr("bot.service.open_database", lambda _c: repos.db)
    service = BotService(config, broker=live)

    called: list[str] = []
    monkeypatch.setattr(
        service.api, "doctor_report", lambda **_k: called.append("ran") or {}
    )
    service._log_startup_verification()

    assert called == [], "the verification must stand down while the circuit is open"


def test_the_scheduler_can_actually_stop_its_jobs():
    """Graceful shutdown was broken outright, and silently.

    PeriodicJob subclasses threading.Thread, which defines a PRIVATE
    _stop() that join() calls internally. The scheduler shadowed it with
    an Event, so every join raised TypeError: 'Event' object is not
    callable. The scheduler could not wait for in-flight work, which on a
    redeploy means the process can be killed mid-order — the exact state
    the whole idempotency design exists to avoid.
    """

    import threading

    from bot.scheduler import Scheduler

    ran = threading.Event()
    scheduler = Scheduler()
    scheduler.add("probe", 0.05, ran.set)
    scheduler.start()
    assert ran.wait(timeout=3.0), "the job never ran"

    scheduler.stop(timeout=2.0)  # must not raise

    assert all(not job.is_alive() for job in scheduler._jobs), (
        "stop() returned while jobs were still running"
    )


# -- switching accounts should not require the broker's own app -----------


def fully_configured(config, account_id: str):
    """The doctor returns at the credentials check unless all four are set."""

    return dataclasses.replace(
        config,
        broker=dataclasses.replace(
            config.broker,
            email="x@example.test",
            password="secret",
            server="GATESFX",
            account_id=account_id,
        ),
    )


def test_the_doctor_lists_every_account_under_the_login(config, monkeypatch):
    """Moving the bot to another account needs one thing: its id.

    Finding that otherwise means navigating the broker's app, which is not
    always possible — a second demo account can be awkward to reach in the
    UI even when the API lists it plainly.
    """

    from bot import doctor

    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD", "accountType": "DEMO"}
    broker._available_accounts = [
        {"id": "2475112", "accNum": "1", "currency": "USD", "name": "old"},
        {"id": "9001234", "accNum": "2", "currency": "USD", "name": "new"},
    ]
    broker.ensure_session = lambda: None  # type: ignore[assignment]

    configured = fully_configured(config, "9001234")
    report = doctor.run(configured, ["EURUSD"], broker=broker)
    listing = next(check for check in report.checks if check.name == "accounts")

    assert "2475112" in listing.detail and "9001234" in listing.detail
    assert "<-- configured" in listing.detail
    assert listing.status == doctor.OK


def test_the_doctor_says_when_the_configured_account_is_not_in_the_list(config):
    """The exact state after a broker recreates an account under a new id."""

    from bot import doctor

    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD", "accountType": "DEMO"}
    broker._available_accounts = [{"id": "9001234", "accNum": "2", "currency": "USD"}]
    broker.ensure_session = lambda: None  # type: ignore[assignment]

    configured = fully_configured(config, "2475112")
    report = doctor.run(configured, ["EURUSD"], broker=broker)
    listing = next(check for check in report.checks if check.name == "accounts")

    assert listing.status == doctor.FAIL
    assert "NOT in this list" in listing.detail


def test_the_account_ids_survive_sanitising_because_they_must_be_copyable(config):
    """A masked id helps nobody switch accounts.

    Balances stay masked; this asserts the two are treated differently and
    on purpose.
    """

    from bot import doctor

    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD", "accountType": "DEMO"}
    broker._available_accounts = [{"id": "9001234", "accNum": "2", "currency": "USD"}]
    broker.ensure_session = lambda: None  # type: ignore[assignment]

    configured = fully_configured(config, "9001234")
    safe = doctor.run(configured, ["EURUSD"], broker=broker).sanitized()
    listing = next(check for check in safe.checks if check.name == "accounts")
    assert "9001234" in listing.detail
