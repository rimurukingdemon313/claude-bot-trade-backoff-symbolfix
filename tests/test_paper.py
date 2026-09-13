"""Paper trading mode.

The property these tests defend: paper mode exercises the REAL decision and
execution path — market data, SMC, scoring, risk, the execution intent, the
idempotency guard, position management — while never sending a write to the
broker. If it diverged from the live path it would manufacture confidence
instead of evidence.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from bot.broker.models import Quote
from bot.broker.paper import PaperBroker
from bot.config import ExecutionMode
from bot.errors import BrokerRejected
from bot.execution.executor import Executor
from bot.marketdata.provider import MarketDataProvider
from bot.orchestrator import Orchestrator
from fakes import BASE_TIME, DEFAULT_SPEC, SETUP_END, FakeBroker, aligned_htf, bullish_setup_m15


@pytest.fixture()
def paper_config(config):
    return dataclasses.replace(
        config,
        mode=ExecutionMode.PAPER,
        paper=dataclasses.replace(config.paper, starting_balance=10_000.0),
    )


@pytest.fixture()
def paper(paper_config, broker, repos) -> PaperBroker:
    wrapper = PaperBroker(broker, paper_config, repos)
    wrapper.ensure_session()
    return wrapper


def set_quote(live: FakeBroker, bid: float, ask: float) -> None:
    live.quotes["EURUSD"] = Quote("EURUSD", bid, ask, BASE_TIME)


# -- the central guarantee -------------------------------------------------


def test_paper_mode_never_writes_to_the_broker(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    position = paper.positions()[0]
    paper.modify_position(position.position_id, stop_loss=1.0960)
    paper.close_position(position.position_id)

    assert broker.submitted == [], "an order reached the live broker in paper mode"
    assert broker.modifications == [], "a modification reached the live broker in paper mode"
    assert broker.closures == [], "a close reached the live broker in paper mode"
    assert len(paper.simulated_writes) == 3


def test_paper_mode_still_enforces_the_demo_guard(paper_config, broker, repos):
    """Paper over a LIVE account must be refused exactly like live trading."""

    broker.metadata = {"id": "1", "accNum": "1", "accountType": "LIVE"}
    wrapper = PaperBroker(broker, paper_config, repos)
    executor = Executor(paper_config, wrapper, repos, sleeper=lambda _s: None)

    from test_execution import make_plan

    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False
    assert "DEMO verification failed" in result.reason
    assert wrapper.simulated_writes == []


# -- fills -----------------------------------------------------------------


def test_a_buy_crosses_the_spread_and_pays_slippage(paper, broker, paper_config):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.1, stop_loss=1.0950, take_profit=1.1150
    )
    fill = paper.positions()[0].entry_price
    slippage = paper_config.paper.entry_slippage_ticks * DEFAULT_SPEC.tick_size
    assert fill == pytest.approx(1.10010 + slippage, abs=1e-6)
    assert fill > 1.10010, "a buy must fill at or above the ask, never at the mid"


def test_a_sell_crosses_the_spread_the_other_way(paper, broker, paper_config):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="SELL", quantity=0.1, stop_loss=1.1050, take_profit=1.0900
    )
    fill = paper.positions()[0].entry_price
    slippage = paper_config.paper.entry_slippage_ticks * DEFAULT_SPEC.tick_size
    assert fill == pytest.approx(1.10000 - slippage, abs=1e-6)
    assert fill < 1.10000


def test_commission_is_charged_on_entry(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    before = paper.account_state().balance
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=1.0, stop_loss=1.0950, take_profit=1.1150
    )
    after = paper.account_state().balance
    assert after < before, "entry commission must reduce the balance"


def test_a_fill_that_lands_outside_the_plan_is_refused(paper, broker):
    """The market moved while the decision was in flight."""

    set_quote(broker, 1.11600, 1.11610)  # already beyond the intended target
    with pytest.raises(BrokerRejected, match="landed outside the plan's levels"):
        paper.place_market_order(
            DEFAULT_SPEC, direction="BUY", quantity=0.1, stop_loss=1.0950, take_profit=1.1150
        )
    assert paper.positions() == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"quantity": 0.0}, "non-positive quantity"),
        ({"stop_loss": 0.0}, "without a positive stop"),
        ({"take_profit": 0.0}, "without a positive stop"),
    ],
)
def test_paper_refuses_what_the_live_broker_would_refuse(paper, broker, kwargs, message):
    """Accepting an order the broker would reject would hide a bug."""

    set_quote(broker, 1.10000, 1.10010)
    params = {
        "direction": "BUY",
        "quantity": 0.1,
        "stop_loss": 1.0950,
        "take_profit": 1.1150,
        **kwargs,
    }
    with pytest.raises(BrokerRejected, match=message):
        paper.place_market_order(DEFAULT_SPEC, **params)


# -- protection settlement -------------------------------------------------


def test_a_stop_closes_the_position_and_fills_worse_than_the_stop(paper, broker, paper_config):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    assert len(paper.positions()) == 1

    set_quote(broker, 1.09400, 1.09410)  # gapped through the stop
    assert paper.positions() == []

    closed = paper.paper.closed_positions()[0]
    assert closed["exit_reason"] == "STOP"
    assert closed["exit_price"] < 1.0950, "a stop must fill worse than its price"
    assert closed["realized_pnl"] < 0


def test_a_target_closes_the_position_at_exactly_the_target(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    set_quote(broker, 1.11550, 1.11560)
    assert paper.positions() == []

    closed = paper.paper.closed_positions()[0]
    assert closed["exit_reason"] == "TARGET"
    assert closed["exit_price"] == pytest.approx(1.1150)
    assert closed["realized_pnl"] > 0


def test_the_minimum_profit_floor_is_realised_at_target(paper, broker):
    """A trade sized to make $40+ at target must actually book it.

    Ties the configured profit floor to a simulated outcome rather than to a
    projection: 0.2 lots of EURUSD over a 150-pip target is $300 gross, and
    commission must not eat it below the floor.
    """

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    set_quote(broker, 1.11550, 1.11560)
    paper.positions()

    closed = paper.paper.closed_positions()[0]
    assert closed["realized_pnl"] >= 40.0, (
        f"booked {closed['realized_pnl']} — below the configured profit floor"
    )


def test_both_levels_touched_in_one_window_resolves_as_the_stop(paper, broker):
    """Intrabar sequence is unknowable; optimism here would make paper
    results meaningless."""

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    # A candle that spans both levels, published after the entry.
    from fakes import candle

    opened = paper.paper.open_positions()[0]["opened_at"]
    from datetime import datetime

    start = datetime.fromisoformat(str(opened))
    broker.set_series(
        "EURUSD",
        "M15",
        [candle(start + timedelta(minutes=15), 1.1000, 1.1200, 1.0900, 1.1100)],
    )
    set_quote(broker, 1.10500, 1.10510)  # back in the middle now
    assert paper.positions() == []

    closed = paper.paper.closed_positions()[0]
    assert closed["exit_reason"] == "STOP_AND_TARGET_SAME_WINDOW"
    assert closed["realized_pnl"] < 0


def test_a_level_reached_between_polls_is_not_missed(paper, broker):
    """A quote-only check would miss a stop hit inside a 15-minute candle."""

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    from datetime import datetime

    from fakes import candle

    start = datetime.fromisoformat(str(paper.paper.open_positions()[0]["opened_at"]))
    # Price dipped through the stop and recovered before we looked.
    broker.set_series(
        "EURUSD",
        "M15",
        [candle(start + timedelta(minutes=15), 1.1000, 1.1010, 1.0930, 1.1005)],
    )
    set_quote(broker, 1.10050, 1.10060)
    assert paper.positions() == [], "the stop was reached inside the candle and must trigger"
    assert paper.paper.closed_positions()[0]["exit_reason"] == "STOP"


def test_an_unreadable_price_leaves_the_position_open_and_says_so(paper, broker):
    from bot.errors import BrokerError

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )

    def explode(_spec):
        raise BrokerError("quote feed down")

    broker.quote = explode  # type: ignore[assignment]
    assert len(paper.positions()) == 1, "a missing price must not be read as 'no trigger' and closed"


# -- account and persistence ----------------------------------------------


def test_equity_marks_to_live_prices(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    flat = paper.account_state()
    set_quote(broker, 1.10500, 1.10510)
    up = paper.account_state()
    assert up.equity > flat.equity
    assert up.open_pnl > 0
    assert up.balance == pytest.approx(flat.balance), "balance moves only on a realised close"


def test_paper_positions_survive_a_restart(paper_config, broker, repos):
    set_quote(broker, 1.10000, 1.10010)
    first = PaperBroker(broker, paper_config, repos)
    first.ensure_session()
    first.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    position_id = first.positions()[0].position_id

    # A brand new wrapper over the same database is what a redeploy looks like.
    second = PaperBroker(broker, paper_config, repos)
    second.ensure_session()
    reloaded = second.positions()
    assert [p.position_id for p in reloaded] == [position_id]
    assert reloaded[0].stop_loss == pytest.approx(1.0950)


def test_the_starting_balance_adopts_the_real_account_when_unset(config, broker, repos):
    unset = dataclasses.replace(
        config,
        mode=ExecutionMode.PAPER,
        paper=dataclasses.replace(config.paper, starting_balance=None),
    )
    wrapper = PaperBroker(broker, unset, repos)
    wrapper.ensure_session()
    assert wrapper.account_state().balance == pytest.approx(broker.account.balance, abs=0.01)


def test_partial_close_reduces_the_position(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.4, stop_loss=1.0950, take_profit=1.1150
    )
    position_id = paper.positions()[0].position_id
    paper.close_position(position_id, quantity=0.2)
    remaining = paper.positions()
    assert len(remaining) == 1
    assert remaining[0].quantity == pytest.approx(0.2)


def test_protection_changes_are_applied(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    position_id = paper.positions()[0].position_id
    paper.modify_position(position_id, stop_loss=1.09900)
    assert paper.positions()[0].stop_loss == pytest.approx(1.09900)


def test_a_stop_already_through_the_market_is_refused(paper, broker):
    """A live broker rejects this; accepting it would fill instantly at a
    flattering price and hide the bug."""

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    position_id = paper.positions()[0].position_id
    with pytest.raises(BrokerRejected, match="already through the market"):
        paper.modify_position(position_id, stop_loss=1.10050)
    assert paper.positions()[0].stop_loss == pytest.approx(1.0950), "the original stop must stand"


def test_order_history_is_shaped_like_the_brokers(paper, broker):
    """The reconciler reads this to settle an ambiguous intent."""

    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    set_quote(broker, 1.11550, 1.11560)
    paper.positions()

    history = paper.order_history()
    assert history and history[0].status == "FILLED"
    assert history[0].raw["realizedPl"] is not None


def test_health_reports_the_mode_and_simulated_state(paper, broker):
    set_quote(broker, 1.10000, 1.10010)
    paper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    health = paper.health()
    assert health["mode"] == "paper"
    assert health["paperOpenPositions"] == 1
    assert health["simulatedWrites"] == 1


# -- full pipeline in paper mode ------------------------------------------


def test_the_whole_pipeline_runs_in_paper_mode_without_touching_the_broker(
    paper_config, broker, repos
):
    wrapper = PaperBroker(broker, paper_config, repos)
    orchestrator = Orchestrator(
        paper_config,
        broker=wrapper,
        repositories=repos,
        market_data=MarketDataProvider(wrapper, paper_config),
    )
    assert orchestrator.startup()["ok"] is True

    result = orchestrator.scan(source="manual", now=SETUP_END)

    assert result.executed is not None and result.executed.ok is True, result.as_dict()["skippedReason"]
    assert broker.submitted == [], "paper mode placed a real order"

    trade = repos.trades.by_execution_id(result.executed.plan.execution_id)
    assert trade["status"] == "OPEN"
    assert trade["risk_amount"] > 0
    assert repos.paper.open_positions(), "the simulated position must be persisted"


def test_paper_mode_is_visible_in_health(paper_config, broker, repos):
    wrapper = PaperBroker(broker, paper_config, repos)
    orchestrator = Orchestrator(
        paper_config,
        broker=wrapper,
        repositories=repos,
        market_data=MarketDataProvider(wrapper, paper_config),
    )
    orchestrator.startup()
    health = orchestrator.health()
    assert health["paper"] is True
    assert health["mode"] == "paper"
    assert "simulated" in health["components"]["mode"]["description"]


def test_paper_reset_clears_simulated_state_only(paper_config, broker, repos):
    from bot.api import DashboardApi

    wrapper = PaperBroker(broker, paper_config, repos)
    orchestrator = Orchestrator(
        paper_config,
        broker=wrapper,
        repositories=repos,
        market_data=MarketDataProvider(wrapper, paper_config),
    )
    orchestrator.startup()
    set_quote(broker, 1.10000, 1.10010)
    wrapper.place_market_order(
        DEFAULT_SPEC, direction="BUY", quantity=0.2, stop_loss=1.0950, take_profit=1.1150
    )
    assert repos.paper.open_positions()

    api = DashboardApi(paper_config, orchestrator, repos)
    assert api.reset_paper()["ok"] is True
    assert repos.paper.open_positions() == []


def test_reset_is_refused_outside_paper_mode(config, orchestrator, repos):
    from bot.api import DashboardApi

    live = dataclasses.replace(config, mode=ExecutionMode.DEMO_LIVE)
    api = DashboardApi(live, orchestrator, repos)
    result = api.reset_paper()
    assert result["ok"] is False and "not in paper mode" in result["error"]
