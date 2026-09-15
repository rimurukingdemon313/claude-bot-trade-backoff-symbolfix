"""A contract size is learned once, not once per restart.

TradeLocker's instrument DIRECTORY carries no contract size — the fields
it returns are barSource, country, description, hasDaily, hasIntraday,
id, localizedName, logoUrl, marketDataExchange, name,
tradableInstrumentId, tradingExchange, type — so every symbol costs one
extra request to size at all.

That was paid again on every restart and every hourly directory refresh,
because the only cache was in memory and `_load_instruments` clears it.
With 23 symbols it is 23 requests spent re-learning something that cannot
change, on an account where one scan was already near the limit. The
observed result was Cloudflare 1015, an open circuit, and every symbol
after that one skipped for "does not expose a contract size".

These tests measure the request count rather than asserting a cache
exists, because the count is the thing that was breaking.
"""

from __future__ import annotations

import pytest

from bot.broker.tradelocker import TradeLockerBroker
from bot.errors import BrokerError
from bot.storage.db import in_memory_database
from bot.storage.repositories import Repositories

DIRECTORY = {
    "instruments": [
        {
            "tradableInstrumentId": 13440,
            "id": 13440,
            "name": "USDJPY",
            "type": "FOREX",
            "routes": [
                {"id": 1250823, "type": "INFO"},
                {"id": 1250824, "type": "TRADE"},
            ],
        }
    ]
}
#: TradeLocker wraps one record in "d"; the size lives in the record.
DETAIL = {
    "d": {
        "tradableInstrumentId": 13440,
        "name": "USDJPY",
        "contractSize": 100000,
        "tickSize": 0.001,
        "digits": 3,
    }
}


def _broker(config, calls, store=None):
    broker = TradeLockerBroker(config, spec_store=store)
    broker._account_meta = {"currency": "USD"}

    def get(path, query=None):
        calls.append(path)
        if path.endswith("/instruments"):
            return DIRECTORY
        if "/trade/instruments/" in path:
            return DETAIL
        raise AssertionError(f"unexpected call: {path}")

    broker.get = get  # type: ignore[assignment]
    return broker


def test_without_memory_every_fresh_process_pays_for_the_detail_call(config):
    calls: list[str] = []
    broker = _broker(config, calls)
    assert broker.instrument("USDJPY").contract_size == 100000

    # A second process, no shared memory: the same two calls again.
    calls2: list[str] = []
    assert _broker(config, calls2).instrument("USDJPY").contract_size == 100000
    assert sum("/trade/instruments/" in c for c in calls2) == 1


def test_a_learned_contract_size_survives_a_restart(config):
    repos = Repositories(in_memory_database())

    first: list[str] = []
    assert _broker(config, first, repos.instruments).instrument("USDJPY").contract_size == 100000
    assert sum("/trade/instruments/" in c for c in first) == 1

    # A new broker object is a new process as far as memory is concerned.
    second: list[str] = []
    spec = _broker(config, second, repos.instruments).instrument("USDJPY")
    assert spec.contract_size == 100000
    assert sum("/trade/instruments/" in c for c in second) == 0, (
        "the detail call was made again for a size that cannot change"
    )


def test_a_failed_lookup_is_never_remembered_as_a_fact(config):
    """Caching a failure would make one bad minute permanent.

    And rule 6 bites harder here than on a balance: a wrong contract size
    does not show up as a gap, it mis-sizes every order on the instrument.
    """

    repos = Repositories(in_memory_database())
    broker = TradeLockerBroker(config, spec_store=repos.instruments)
    broker._account_meta = {"currency": "USD"}

    def get(path, query=None):
        if path.endswith("/instruments"):
            return DIRECTORY
        raise BrokerError("broker circuit open after 5 consecutive failures")

    broker.get = get  # type: ignore[assignment]

    with pytest.raises(BrokerError, match="does not expose a contract size"):
        broker.instrument("USDJPY")
    assert repos.instruments.contract_size("13440") is None

    # And once the broker recovers, the real value is learned and kept.
    calls: list[str] = []
    assert _broker(config, calls, repos.instruments).instrument("USDJPY").contract_size == 100000
    assert repos.instruments.contract_size("13440") == 100000


def test_the_hourly_directory_refresh_no_longer_costs_a_detail_call(config):
    """`_load_instruments` clears the in-memory spec cache by design.

    Route ids can change with the directory, so dropping the resolved spec
    is right. The contract size is not part of what can change, and it was
    being dropped with it.
    """

    repos = Repositories(in_memory_database())
    calls: list[str] = []
    broker = _broker(config, calls, repos.instruments)
    broker.instrument("USDJPY")
    before = sum("/trade/instruments/" in c for c in calls)

    broker._load_instruments(force=True)  # what the hourly refresh does
    broker.instrument("USDJPY")
    after = sum("/trade/instruments/" in c for c in calls)
    assert after == before == 1


# -- request spacing -------------------------------------------------------


def test_the_configured_request_spacing_is_the_one_actually_used(config):
    """A setting nothing reads is worse than no setting.

    BROKER_MIN_REQUEST_INTERVAL and its 0.6s default exist because this
    account sits behind Cloudflare and answers 1015 to a burst. The
    transport was built with a hardcoded `Throttle(min_interval=0.15)`, so
    the live bot ran four times faster than the number the operator could
    read in the config and in /health — and every attempt to slow it down
    by configuration did nothing at all.
    """

    import dataclasses

    slower = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, min_request_interval=1.25)
    )
    assert TradeLockerBroker(slower).transport.throttle.min_interval == 1.25

    faster = dataclasses.replace(
        config, broker=dataclasses.replace(config.broker, min_request_interval=0.0)
    )
    assert TradeLockerBroker(faster).transport.throttle.min_interval == 0.0


def test_the_health_endpoint_reports_the_spacing_it_is_really_using(config):
    """Rule 6 covers the bot's own settings, not just market values.

    /health reporting 0.6s while the transport used 0.15s made the
    dashboard state a fact that was not true, and sent the diagnosis of a
    rate limit in the wrong direction.
    """

    import dataclasses

    broker = TradeLockerBroker(
        dataclasses.replace(
            config, broker=dataclasses.replace(config.broker, min_request_interval=0.9)
        )
    )
    assert broker.health()["requestSpacingSeconds"] == 0.9
