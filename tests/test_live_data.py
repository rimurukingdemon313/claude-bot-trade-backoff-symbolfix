"""Live data integration: endpoint discovery, decoding, and the doctor.

The gap these close: the broker's history endpoint shape was previously
assumed. A deployment that used any other shape returned zero candles and
the bot silently never traded — no error, no signal, just permanent NO
TRADE. Discovery turns that into either working data or a loud failure.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from bot.broker.history import (
    STRATEGIES,
    HistoryFetcher,
    HistoryStrategy,
    decode_bars,
)
from bot.broker.tradelocker import TradeLockerBroker
from bot.errors import BrokerError, BrokerRejected
from bot.marketdata.candles import to_candles
from fakes import DEFAULT_SPEC, SETUP_END


def bar_objects(count: int = 120, *, unit: str = "ms", end: datetime | None = None) -> list[dict]:
    finish = end or SETUP_END
    scale = 1000 if unit == "ms" else 1
    rows = []
    for index in range(count):
        stamp = (finish - timedelta(minutes=15 * (count - index))).timestamp() * scale
        price = 1.1000 + index * 0.0001
        rows.append({"t": stamp, "o": price, "h": price + 0.0006, "l": price - 0.0006, "c": price + 0.0002, "v": 10})
    return rows


# -- decoding --------------------------------------------------------------


def test_object_bars_are_decoded():
    rows = decode_bars({"barDetails": bar_objects(5)}, "M15")
    assert len(rows) == 5
    assert rows[0]["timeframe"] == "M15"
    assert rows[0]["high"] >= rows[0]["close"]


def test_positional_bars_are_decoded():
    stamp = SETUP_END.timestamp()
    rows = decode_bars({"bars": [[stamp, 1.1, 1.12, 1.09, 1.11, 7]]}, "M15")
    assert rows[0]["open"] == pytest.approx(1.1)
    assert rows[0]["volume"] == pytest.approx(7)


@pytest.mark.parametrize("key", ["barDetails", "bars", "candles", "history", "data"])
def test_every_known_envelope_key_is_found(key):
    assert len(decode_bars({key: bar_objects(3)}, "M15")) == 3


def test_a_nested_envelope_is_unwrapped():
    assert len(decode_bars({"d": {"barDetails": bar_objects(4)}}, "M15")) == 4


def test_a_bare_list_response_is_accepted():
    assert len(decode_bars(bar_objects(6), "M15")) == 6


@pytest.mark.parametrize(
    "bar",
    [
        {"t": 1757000000000, "o": None, "h": 1.1, "l": 1.0, "c": 1.05},
        {"t": 1757000000000, "o": -1.0, "h": 1.1, "l": 1.0, "c": 1.05},
        {"t": 1757000000000, "o": "abc", "h": 1.1, "l": 1.0, "c": 1.05},
        {"t": None, "o": 1.1, "h": 1.1, "l": 1.0, "c": 1.05},
        {"o": 1.1, "h": 1.1, "l": 1.0, "c": 1.05},
    ],
)
def test_a_malformed_bar_is_dropped_never_defaulted(bar):
    """A fabricated price would flow straight into a stop calculation."""

    assert decode_bars({"bars": [bar]}, "M15") == []


def test_a_wick_inconsistent_by_a_rounding_tick_is_clamped_not_discarded():
    """Brokers occasionally report a high a hair below the close."""

    rows = decode_bars(
        {"bars": [{"t": SETUP_END.timestamp(), "o": 1.10, "h": 1.1049, "l": 1.09, "c": 1.1050}]},
        "M15",
    )
    assert len(rows) == 1
    assert rows[0]["high"] >= rows[0]["close"]
    # And the result must survive the strict Candle constructor.
    assert to_candles(rows)[0].high >= to_candles(rows)[0].close


def test_overlapping_pages_are_deduplicated():
    first = bar_objects(10)
    rows = decode_bars({"bars": first + first[-4:]}, "M15")
    assert len(rows) == 10


def test_bars_come_back_chronological():
    shuffled = list(reversed(bar_objects(20)))
    rows = decode_bars({"bars": shuffled}, "M15")
    assert [row["timestamp"] for row in rows] == sorted(row["timestamp"] for row in rows)


# -- discovery -------------------------------------------------------------


def test_discovery_finds_the_working_shape_and_caches_it():
    calls: list[str] = []

    def request(path, query):
        calls.append(path)
        # Only the third shape (seconds bounds) answers on this deployment.
        if path == "/trade/history" and query["from"] < 10**11:
            return {"barDetails": bar_objects(80, unit="s")}
        return {"barDetails": []}

    fetcher = HistoryFetcher(request)
    rows = fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=80, now=SETUP_END)
    assert len(rows) == 80
    assert fetcher.strategy is not None and fetcher.strategy.time_unit == "s"

    # Second call reuses the learned shape rather than re-probing.
    calls.clear()
    fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=80, now=SETUP_END)
    assert len(calls) == 1


def test_a_rejected_shape_is_skipped_and_the_next_is_tried():
    def request(path, query):
        if path != "/trade/bars":
            raise BrokerRejected(f"404 for {path}")
        return {"candles": bar_objects(70)}

    fetcher = HistoryFetcher(request)
    rows = fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=70, now=SETUP_END)
    assert len(rows) == 70
    assert fetcher.strategy.path == "/trade/bars"
    assert any(attempt["ok"] for attempt in fetcher.attempts)


def test_a_transport_failure_aborts_discovery_instead_of_caching_a_wrong_answer():
    """A broker outage is not evidence that no endpoint works."""

    def request(path, query):
        raise BrokerError("connection reset")

    fetcher = HistoryFetcher(request)
    with pytest.raises(BrokerError, match="probe aborted on a transport failure"):
        fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=60, now=SETUP_END)
    assert fetcher.strategy is None, "a transport failure must not conclude anything"


def test_every_shape_failing_raises_with_what_was_tried():
    fetcher = HistoryFetcher(lambda path, query: {"barDetails": []})
    with pytest.raises(BrokerError, match="no known TradeLocker history endpoint shape"):
        fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=60, now=SETUP_END)
    assert len(fetcher.attempts) == len(STRATEGIES)


def test_a_learned_shape_that_stops_working_is_re_probed():
    state = {"alive": True}

    def request(path, query):
        if path == "/trade/history" and state["alive"]:
            return {"barDetails": bar_objects(70)}
        if path == "/trade/quotes/history":
            return {"bars": bar_objects(70)}
        return {"barDetails": []}

    fetcher = HistoryFetcher(request)
    fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=70, now=SETUP_END)
    assert fetcher.strategy.path == "/trade/history"

    state["alive"] = False  # broker changed under us
    rows = fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=70, now=SETUP_END)
    assert len(rows) == 70
    assert fetcher.strategy.path == "/trade/quotes/history"


def test_the_requested_window_over_fetches_to_survive_weekends():
    captured: dict = {}

    def request(path, query):
        captured.update(query)
        return {"barDetails": bar_objects(100)}

    HistoryFetcher(request).fetch(
        instrument_id=1, route_id=2, timeframe="M15", count=100, now=SETUP_END
    )
    span_minutes = (captured["to"] - captured["from"]) / 1000 / 60
    assert span_minutes > 100 * 15, "a 1:1 window would return a short series across a weekend"


@pytest.mark.parametrize("timeframe,expected", [("M15", "15m"), ("H1", "1H"), ("H4", "4H")])
def test_resolution_strings_map_per_timeframe(timeframe, expected):
    assert STRATEGIES[0].resolution_for(timeframe) == expected


def test_an_unsupported_timeframe_is_refused():
    with pytest.raises(BrokerError, match="unsupported timeframe"):
        STRATEGIES[0].resolution_for("M3")


def test_describe_reports_the_discovery_for_the_health_endpoint():
    fetcher = HistoryFetcher(lambda path, query: {"barDetails": bar_objects(70)})
    fetcher.fetch(instrument_id=1, route_id=2, timeframe="M15", count=70, now=SETUP_END)
    described = fetcher.describe()
    assert described["discovered"] == "documented"
    assert described["path"] == "/trade/history"
    assert described["timeUnit"] == "ms"


# -- broker wiring ---------------------------------------------------------


def test_the_broker_feeds_discovered_candles_into_the_engine(config):
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker.get = lambda path, query=None: {"barDetails": bar_objects(150)}  # type: ignore[assignment]

    rows = broker.candles(DEFAULT_SPEC, "M15", count=120)
    assert len(rows) == 120
    candles = to_candles(rows)
    assert candles[0].timeframe == "M15"
    assert broker.health()["history"]["discovered"] == "documented"


def test_the_broker_refuses_an_unknown_timeframe(config):
    broker = TradeLockerBroker(config)
    with pytest.raises(BrokerError, match="unsupported timeframe"):
        broker.candles(DEFAULT_SPEC, "M3")


# -- doctor ----------------------------------------------------------------


def test_the_doctor_reports_missing_credentials_without_touching_the_network(config):
    from bot import doctor

    stripped = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, email=None, password=None)
    )
    report = doctor.run(stripped, ["EURUSD"])
    assert report.as_dict()["verdict"] == doctor.FAIL
    assert report.checks[0].name == "credentials"


def test_the_doctor_report_renders_and_redacts(monkeypatch, config):
    from bot import doctor

    monkeypatch.setenv("TRADELOCKER_PASSWORD", "hunter2-very-secret-value")
    report = doctor.Report()
    report.add("example", doctor.OK, "password is hunter2-very-secret-value")
    rendered = report.render()
    assert "hunter2-very-secret-value" not in rendered
    assert "redacted" in rendered
    assert "hunter2-very-secret-value" not in str(report.as_dict())


def test_the_doctor_is_read_only():
    """Asserted structurally: the module must not reference any write."""

    import inspect

    from bot import doctor

    source = inspect.getsource(doctor)
    for forbidden in ("place_market_order", "close_position", "modify_position", "write("):
        assert forbidden not in source, f"doctor references a write path: {forbidden}"
