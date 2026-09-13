"""Shared fixtures.

Two deliberate choices:

* every test that touches the clock pins it to `SETUP_END`, so session,
  news and staleness logic is asserted rather than accidentally observed;
* the network is never touched — `NewsFilter` is constructed disabled or
  pre-seeded, and the broker is always the in-memory fake.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from bot.config import TradingConfig, load_config
from bot.marketdata.provider import MarketDataProvider
from bot.orchestrator import Orchestrator
from bot.storage.db import in_memory_database
from bot.storage.repositories import Repositories
from fakes import FakeBroker, SETUP_END, aligned_htf, bullish_setup_m15


@pytest.fixture()
def config() -> TradingConfig:
    base = load_config({})
    return dataclasses.replace(
        base,
        symbols=("EURUSD",),
        ai=dataclasses.replace(base.ai, enabled=False),
        news=dataclasses.replace(base.news, enabled=False),
    )


@pytest.fixture()
def repos() -> Repositories:
    return Repositories(in_memory_database())


@pytest.fixture()
def broker() -> FakeBroker:
    fake = FakeBroker()
    m15 = bullish_setup_m15()
    fake.set_series("EURUSD", "M15", m15)
    fake.set_series("EURUSD", "H1", aligned_htf(m15, timeframe="H1"))
    fake.set_series("EURUSD", "H4", aligned_htf(m15, timeframe="H4"))
    return fake


@pytest.fixture()
def orchestrator(config: TradingConfig, broker: FakeBroker, repos: Repositories) -> Orchestrator:
    orchestrator = Orchestrator(
        config,
        broker=broker,
        repositories=repos,
        market_data=MarketDataProvider(broker, config),
    )
    orchestrator.startup()
    return orchestrator


@pytest.fixture()
def now() -> Any:
    return SETUP_END
