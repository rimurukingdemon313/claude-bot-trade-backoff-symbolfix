"""Brokers whose instrument names carry a suffix (`EURUSD.R`).

Symbol identity is the join key for three safety controls, and every one of
them FAILS OPEN when the key does not match. Before canonical resolution, on
an account listing `EURUSD.R`:

  * `positions()` reported `EURUSDR` while the plan said `EURUSD`, so the
    executor's pre-submit duplicate check could not see an existing position;
  * the risk engine's per-symbol limit used the same comparison;
  * `currencies_for("EURUSD.R")` returned `()`, so news blackouts never fired;
  * currency exposure was `{}`, so every correlation score was 0 and the
    stacking limit was inert;
  * sizing derived quote `USDR` for `XAUUSD.R`.

Each test below pins one of those closed.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.broker.symbols import (
    alphanumeric,
    broker_suffix,
    canonical_symbol,
    same_instrument,
    split_currencies,
)
from bot.broker.tradelocker import TradeLockerBroker
from bot.errors import BrokerRejected
from bot.execution.executor import Executor
from bot.execution.reconciler import Reconciler
from bot.marketdata.provider import MarketDataProvider
from bot.news import currencies_for
from bot.orchestrator import Orchestrator
from bot.risk.correlation import correlation_score, currency_exposure
from bot.risk.engine import AccountRiskState, RiskEngine
from bot.risk.sizing import SizingError, calculate_position_size
from bot.smc.engine import SmcEngine
from fakes import DEFAULT_SPEC, SETUP_END, suffixed_broker


# -- resolution ------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("EURUSD.R", "EURUSD"),
        ("EUR/USD.R", "EURUSD"),
        ("eurusd.r", "EURUSD"),
        ("EURUSD_i", "EURUSD"),
        ("EURUSDm", "EURUSD"),
        ("EURUSD.pro", "EURUSD"),
        ("XAUUSD.R", "XAUUSD"),
        ("GBPJPY.R", "GBPJPY"),
        ("USDZAR.R", "USDZAR"),
        ("EURUSD", "EURUSD"),
    ],
)
def test_every_decoration_resolves_to_the_same_pair(name, expected):
    assert canonical_symbol(name) == expected


@pytest.mark.parametrize("name", ["US500", "NAS100", "WTI", "GER40", "XY", ""])
def test_a_non_pair_does_not_resolve_and_is_not_guessed(name):
    """(None, None) is a real answer: sizing then refuses rather than
    assuming a quote currency."""

    assert canonical_symbol(name) is None
    assert split_currencies(name) == (None, None)


def test_the_metal_quote_is_read_correctly_not_as_usdr():
    """The previous code derived ('XAU', 'USDR') from XAUUSD.R."""

    assert split_currencies("XAUUSD.R") == ("XAU", "USD")


def test_the_suffix_is_recoverable_for_diagnostics():
    assert broker_suffix("EURUSD.R") == ".R"
    assert broker_suffix("EURUSD") == ""


@pytest.mark.parametrize(
    "left,right,same",
    [
        ("EURUSD", "EURUSD.R", True),
        ("EURUSD.R", "EUR/USD", True),
        ("EURUSD.R", "GBPUSD.R", False),
        ("US500", "US500", True),
        ("US500", "NAS100", False),
    ],
)
def test_instrument_identity(left, right, same):
    assert same_instrument(left, right) is same


# -- broker lookup ---------------------------------------------------------


def tl_broker(config, names: list[str]) -> TradeLockerBroker:
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker._instruments_raw = [
        {
            "name": name,
            "tradableInstrumentId": index + 1,
            "contractSize": 100_000,
            "tickSize": 0.00001,
            "digits": 5,
            "lotStep": 0.01,
            "minLotSize": 0.01,
            "maxLotSize": 100,
            "routes": [{"id": 10, "type": "TRADE"}, {"id": 11, "type": "INFO"}],
        }
        for index, name in enumerate(names)
    ]
    broker._instrument_cache_at = float("inf")
    return broker


def test_a_bare_symbol_finds_the_suffixed_instrument(config):
    broker = tl_broker(config, ["EURUSD.R", "GBPUSD.R", "XAUUSD.R"])
    spec = broker.instrument("EURUSD")
    assert spec.broker_name == "EURUSD.R", "the API must be called with the broker's own name"
    assert spec.symbol == "EURUSD", "identity must be canonical"


def test_the_suffixed_symbol_also_works_if_configured_literally(config):
    broker = tl_broker(config, ["EURUSD.R", "GBPUSD.R"])
    spec = broker.instrument("EURUSD.R")
    assert spec.broker_name == "EURUSD.R"
    assert spec.symbol == "EURUSD"


def test_the_spec_carries_the_right_currencies_on_a_suffixed_account(config):
    broker = tl_broker(config, ["EURUSD.R"])
    spec = broker.instrument("EURUSD")
    assert (spec.base_currency, spec.quote_currency) == ("EUR", "USD")


def test_a_genuinely_ambiguous_account_still_refuses_to_guess(config):
    """Two instruments for the same pair must not be silently picked between."""

    broker = tl_broker(config, ["EURUSD.R", "EURUSD.RAW"])
    with pytest.raises(BrokerRejected, match="matches multiple broker instruments"):
        broker.instrument("EURUSD")


def test_an_exact_name_disambiguates_a_multi_variant_account(config):
    broker = tl_broker(config, ["EURUSD.R", "EURUSD.RAW"])
    assert broker.instrument("EURUSD.RAW").broker_name == "EURUSD.RAW"


def test_positions_report_the_canonical_symbol(config):
    """The reported symbol is the join key for duplicate detection."""

    broker = tl_broker(config, ["EURUSD.R"])
    broker._trade_config = {
        "positionsConfig": {"columns": [{"id": c} for c in
            ("id", "tradableInstrumentId", "side", "qty", "avgPrice", "unrealizedPl", "openDate")]},
        "ordersConfig": {"columns": [{"id": "id"}]},
    }
    calls = {"positions": {"positions": [["900", "1", "buy", "0.2", "1.1000", "0", ""]]}, "orders": {"orders": []}}
    broker.get = lambda path, query=None: (  # type: ignore[assignment]
        calls["positions"] if path.endswith("/positions") else calls["orders"]
    )
    position = broker.positions()[0]
    assert position.symbol == "EURUSD", f"got {position.symbol!r} — must be canonical"


# -- the safety controls that were failing open ---------------------------


def test_news_blackouts_resolve_on_a_suffixed_symbol():
    assert currencies_for("EURUSD.R") == ("EUR", "USD")
    assert currencies_for("XAUUSD.R") == ("USD",)
    assert currencies_for("GBPJPY.R") == ("GBP", "JPY")


def test_correlation_resolves_on_a_suffixed_symbol():
    assert currency_exposure("EURUSD.R", "BUY") == {"EUR": 1.0, "USD": -1.0}
    assert correlation_score("EURUSD.R", "BUY", "GBPUSD.R", "BUY") == pytest.approx(0.5)
    assert correlation_score("EURUSD", "BUY", "EURUSD.R", "SELL") == pytest.approx(-1.0)


def test_sizing_works_for_a_suffixed_metal():
    """XAUUSD.R previously derived quote 'USDR' and refused to size."""

    gold = dataclasses.replace(
        DEFAULT_SPEC,
        symbol="XAUUSD",
        broker_name="XAUUSD.R",
        contract_size=100.0,
        tick_size=0.01,
        digits=2,
        base_currency="XAU",
        quote_currency="USD",
        max_lot=50.0,
    )
    size = calculate_position_size(spec=gold, risk_amount=100.0, entry=2400.0, stop_loss=2390.0)
    assert size.lots == pytest.approx(0.10)
    assert size.actual_risk == pytest.approx(100.0, abs=0.01)


def test_an_unresolvable_instrument_refuses_to_size_rather_than_assuming_usd():
    index = dataclasses.replace(
        DEFAULT_SPEC,
        symbol="US500",
        broker_name="US500.R",
        base_currency=None,
        quote_currency=None,
    )
    with pytest.raises(SizingError, match="cannot determine the quote currency"):
        calculate_position_size(spec=index, risk_amount=100.0, entry=5000.0, stop_loss=4950.0)


def test_the_duplicate_check_sees_an_existing_suffixed_position(config, repos):
    """THE critical one: this returning False opened a second position."""

    from test_execution import make_plan

    broker = suffixed_broker(".R")
    broker.add_position(symbol="EURUSD", direction="BUY")
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)

    result = executor.execute(make_plan(), broker.instrument("EURUSD"), atr=0.0012)
    assert result.ok is False
    assert "already exists at the broker" in result.reason
    assert broker.submitted == [], "a second order was placed on a pair already held"


def test_the_per_symbol_limit_fires_on_a_suffixed_account(config, repos):
    broker = suffixed_broker(".R")
    series = MarketDataProvider(broker, config).multi_timeframe(
        broker.instrument("EURUSD"), now=SETUP_END
    )
    candidate = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END).candidate
    assert candidate is not None

    account = AccountRiskState(
        balance=10_000.0,
        equity=10_000.0,
        available_margin=10_000.0,
        peak_equity=10_000.0,
        daily_realized_pnl=0.0,
        open_pnl=0.0,
        trades_today=0,
        trades_this_session=0,
        consecutive_losses=0,
        # As the broker reports it after resolution.
        open_positions=[{"symbol": "EURUSD", "direction": "BUY", "risk_amount": 50.0}],
    )
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A",
        account=account,
        spec=broker.instrument("EURUSD"),
        now=SETUP_END,
    )
    assert decision.approved is False
    assert any("already holding a position" in reason for reason in decision.reasons)


def test_a_decorated_position_in_the_database_still_matches(config, repos):
    """An orphan adopted before this fix carries the decorated name."""

    from test_execution import make_plan

    broker = suffixed_broker(".R")
    repos.trades.adopt_orphan(
        broker_position_id="7001",
        snapshot={"symbol": "EURUSD.R", "direction": "BUY", "entry": 1.1},
    )
    broker.add_position(symbol="EURUSD", direction="BUY", position_id="7001")
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), broker.instrument("EURUSD"), atr=0.0012)
    assert result.ok is False
    assert broker.submitted == []


def test_the_reconciler_settles_an_intent_against_a_suffixed_position(config, repos):
    from fakes import ambiguous_hook
    from test_execution import make_plan

    broker = suffixed_broker(".R")
    broker.place_order_hook = ambiguous_hook
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), broker.instrument("EURUSD"), atr=0.0012)
    assert result.status == "AMBIGUOUS"

    # The order did land after all.
    broker.place_order_hook = None
    broker.add_position(symbol="EURUSD", direction="BUY", entry=1.1002)

    report = Reconciler(config, broker, repos).reconcile()
    assert result.plan.execution_id in report.resolved_intents
    assert repos.intents.get(result.plan.execution_id)["status"] == "FILLED", (
        "a filled order was misread as never having landed"
    )


# -- full pipeline ---------------------------------------------------------


def test_the_whole_pipeline_trades_a_suffix_only_account(config, repos):
    broker = suffixed_broker(".R")
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    assert orchestrator.startup()["ok"] is True
    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert result.executed is not None and result.executed.ok is True, (
        result.as_dict()["skippedReason"] or result.outcomes[0].reason
    )
    trade = repos.trades.by_execution_id(result.executed.plan.execution_id)
    assert trade["symbol"] == "EURUSD", "the trade must be recorded under the canonical symbol"


def test_a_second_scan_on_a_suffixed_account_does_not_duplicate(config, repos):
    broker = suffixed_broker(".R")
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    orchestrator.startup()
    orchestrator.scan(source="manual", now=SETUP_END)
    orchestrator.scan(source="manual", now=SETUP_END)
    assert len(broker.submitted) == 1


@pytest.mark.parametrize("configured", ["EURUSD", "EURUSD.R"])
def test_traded_symbols_accepts_either_form(config, repos, configured):
    broker = suffixed_broker(".R")
    tuned = dataclasses.replace(config, symbols=(configured,))
    orchestrator = Orchestrator(
        tuned, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, tuned)
    )
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert result.outcomes[0].outcome == "CANDIDATE", result.outcomes[0].reason


@pytest.mark.parametrize("suffix", [".R", "_i", "m", ".pro", ".ecn"])
def test_other_broker_suffixes_work_the_same_way(config, repos, suffix):
    broker = suffixed_broker(suffix)
    orchestrator = Orchestrator(
        config, broker=broker, repositories=repos, market_data=MarketDataProvider(broker, config)
    )
    orchestrator.startup()
    result = orchestrator.scan(source="manual", now=SETUP_END)
    assert result.executed is not None and result.executed.ok is True
