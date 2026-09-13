"""Execution, idempotency, and failure handling.

The invariant every test here defends: a network failure, a crash, or a
concurrent cycle must NEVER result in two orders for the same setup.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.errors import AmbiguousExecution, BrokerRejected, StorageError
from bot.execution.executor import Executor
from bot.execution.plan import TradePlan, build_execution_id, build_plan
from bot.execution.reconciler import Reconciler
from bot.storage.repositories import Repositories
from fakes import DEFAULT_SPEC, SETUP_END, FakeBroker, ambiguous_hook, rejecting_hook


def make_plan(**overrides) -> TradePlan:
    defaults = dict(
        execution_id=build_execution_id(
            symbol="EURUSD",
            direction="BUY",
            trigger_time=SETUP_END,
            entry=1.1000,
            stop_loss=1.0950,
            take_profit=1.1150,
        ),
        symbol="EURUSD",
        broker_symbol="EURUSD",
        direction="BUY",
        entry=1.1000,
        stop_loss=1.0950,
        take_profit=1.1150,
        quantity=0.2,
        risk_amount=100.0,
        risk_pct=0.01,
        expected_profit=300.0,
        risk_reward=3.0,
        setup_grade="A",
        setup_score=72.0,
        ai_confidence=80.0,
        htf_bias="bullish",
        alignment="aligned",
        instrument_id=1,
        route_id=10,
        created_at=SETUP_END.isoformat(),
        context={},
    )
    defaults.update(overrides)
    return TradePlan(**defaults)


@pytest.fixture()
def executor(config, broker, repos) -> Executor:
    return Executor(config, broker, repos, sleeper=lambda _seconds: None)


# -- execution identity ---------------------------------------------------


def test_the_same_setup_always_produces_the_same_execution_id():
    args = dict(
        symbol="EURUSD", direction="BUY", trigger_time=SETUP_END,
        entry=1.1, stop_loss=1.09, take_profit=1.13,
    )
    assert build_execution_id(**args) == build_execution_id(**args)


@pytest.mark.parametrize(
    "change",
    [{"entry": 1.1001}, {"stop_loss": 1.0901}, {"direction": "SELL"}, {"symbol": "GBPUSD"}],
)
def test_a_different_setup_produces_a_different_execution_id(change):
    args = dict(
        symbol="EURUSD", direction="BUY", trigger_time=SETUP_END,
        entry=1.1, stop_loss=1.09, take_profit=1.13,
    )
    assert build_execution_id(**args) != build_execution_id(**{**args, **change})


def test_the_database_physically_rejects_a_duplicate_intent(repos):
    repos.intents.create(idempotency_key="abc", symbol="EURUSD", direction="BUY", plan={})
    with pytest.raises(StorageError, match="already exists"):
        repos.intents.create(idempotency_key="abc", symbol="EURUSD", direction="BUY", plan={})


# -- happy path -----------------------------------------------------------


def test_a_successful_execution_persists_the_whole_lifecycle(executor, broker, repos):
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok and result.status == "FILLED"
    assert len(broker.submitted) == 1

    intent = repos.intents.get(result.plan.execution_id)
    assert intent["status"] == "FILLED"
    assert intent["broker_position_id"] == result.broker_position_id

    trade = repos.trades.by_execution_id(result.plan.execution_id)
    assert trade["status"] == "OPEN"
    assert trade["risk_amount"] == pytest.approx(100.0)

    events = [event["event"] for event in repos.events.for_execution(result.plan.execution_id)]
    assert events == ["INTENT_CREATED", "SUBMITTED", "ACKNOWLEDGED", "FILLED"]


def test_the_order_carries_a_stop_and_a_target(executor, broker):
    executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    submitted = broker.submitted[0]
    assert submitted["stopLoss"] > 0 and submitted["takeProfit"] > 0


# -- duplicate prevention -------------------------------------------------

def test_running_the_same_plan_twice_submits_exactly_one_order(executor, broker):
    plan = make_plan()
    first = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)
    second = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)
    assert first.ok is True
    assert second.ok is False and second.status == "DUPLICATE"
    assert len(broker.submitted) == 1


def test_a_position_opened_by_a_concurrent_cycle_aborts_this_one(executor, broker):
    """The pre-submit check is a LIVE broker query, not a cached snapshot."""

    broker.add_position(symbol="EURUSD", direction="BUY")
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False
    assert "already exists at the broker" in result.reason
    assert broker.submitted == []


def test_an_unresolved_intent_blocks_a_new_order_on_the_same_symbol(executor, repos, broker):
    repos.intents.create(idempotency_key="stale", symbol="EURUSD", direction="BUY", plan={})
    repos.intents.mark("stale", "AMBIGUOUS")
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False
    assert "must be reconciled" in result.reason
    assert broker.submitted == []


# -- failure handling -----------------------------------------------------


def test_an_ambiguous_submission_is_never_retried(executor, broker, repos):
    broker.place_order_hook = ambiguous_hook
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    assert result.status == "AMBIGUOUS"
    assert len(broker.submitted) == 1, "an unknown outcome must not trigger a resend"
    assert repos.intents.get(result.plan.execution_id)["status"] == "AMBIGUOUS"
    assert any(
        entry["kind"] == "AMBIGUOUS_SUBMISSION" for entry in repos.reconciliations.recent()
    )


def test_a_broker_rejection_is_permanent(executor, broker, repos):
    broker.place_order_hook = rejecting_hook
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.status == "REJECTED"
    assert repos.intents.get(result.plan.execution_id)["status"] == "FAILED"
    assert len(broker.submitted) == 1


def test_an_acknowledged_order_with_no_position_is_ambiguous(executor, broker, repos):
    broker.fill_on_submit = False
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.status == "UNVERIFIED"
    assert repos.intents.get(result.plan.execution_id)["status"] == "AMBIGUOUS"


def test_a_wide_spread_aborts_before_submission(config, broker, repos):
    from bot.broker.models import Quote
    from fakes import BASE_TIME

    broker.quotes["EURUSD"] = Quote("EURUSD", 1.0990, 1.1010, BASE_TIME)  # 20 pip spread
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False and result.status == "ABORTED"
    assert "spread" in result.reason
    assert broker.submitted == []


def test_a_live_environment_blocks_submission(config, broker, repos):
    broker.metadata = {"id": "1", "accNum": "1", "accountType": "LIVE"}
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False
    assert "DEMO verification failed" in result.reason
    assert broker.submitted == []


def test_a_broken_database_blocks_order_creation(config, broker, repos):
    """Fail closed: an order whose intent cannot be persisted is
    unrecoverable after a restart."""

    repos.db.close()
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok is False and result.status == "ABORTED"
    assert "database is unavailable" in result.reason
    assert broker.submitted == []


def test_missing_broker_protection_is_repaired(config, broker, repos):
    """A position without a stop is an unbounded loss."""

    original = broker.place_market_order

    def strip_protection(spec, **kwargs):
        result = original(spec, **kwargs)
        broker._positions[-1] = dataclasses.replace(broker._positions[-1], stop_loss=None)
        return result

    broker.place_market_order = strip_protection  # type: ignore[assignment]
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.ok
    assert broker.modifications, "the executor must reattach a missing stop loss"
    assert broker.modifications[0]["stopLoss"] == pytest.approx(1.0950)


# -- reconciliation -------------------------------------------------------


def test_reconciler_resolves_an_ambiguous_intent_that_did_fill(config, broker, repos):
    broker.place_order_hook = ambiguous_hook
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.status == "AMBIGUOUS"

    # The order actually DID land; the response was simply lost.
    broker.add_position(symbol="EURUSD", direction="BUY", entry=1.1002)

    report = Reconciler(config, broker, repos).reconcile()
    assert result.plan.execution_id in report.resolved_intents
    intent = repos.intents.get(result.plan.execution_id)
    assert intent["status"] == "FILLED"
    assert repos.trades.by_execution_id(result.plan.execution_id)["status"] == "OPEN"


def test_reconciler_abandons_an_ambiguous_intent_that_never_landed(config, broker, repos):
    broker.place_order_hook = ambiguous_hook
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    report = Reconciler(config, broker, repos).reconcile()
    assert result.plan.execution_id in report.resolved_intents
    assert repos.intents.get(result.plan.execution_id)["status"] == "ABANDONED"


def test_reconciler_adopts_a_position_the_database_never_saw(config, broker, repos):
    """Crash recovery: the order landed, the process died before recording it."""

    broker.add_position(symbol="EURUSD", direction="BUY", position_id="9001", entry=1.1005)
    report = Reconciler(config, broker, repos).reconcile()
    assert "9001" in report.adopted_orphans
    trade = repos.trades.by_position_id("9001")
    assert trade["status"] == "ORPHANED"
    assert trade["symbol"] == "EURUSD"


def test_adoption_is_idempotent(config, broker, repos):
    broker.add_position(symbol="EURUSD", position_id="9002")
    reconciler = Reconciler(config, broker, repos)
    reconciler.reconcile()
    second = reconciler.reconcile()
    assert second.adopted_orphans == []
    assert len(repos.trades.open_trades()) == 1


def test_reconciler_closes_a_trade_the_broker_no_longer_has(config, broker, repos):
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)

    report = Reconciler(config, broker, repos).reconcile()
    assert result.broker_position_id in report.closed_stale
    trade = repos.trades.by_execution_id(result.plan.execution_id)
    assert trade["status"] == "CLOSED"
    assert trade["realized_pnl"] == pytest.approx(-82.5)
    assert repos.daily.today()["realized_pnl"] == pytest.approx(-82.5)


def test_reconciler_restores_a_missing_stop_from_the_recorded_plan(config, broker, repos):
    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    broker._positions[0] = dataclasses.replace(broker._positions[0], stop_loss=None)
    broker.modifications.clear()

    report = Reconciler(config, broker, repos).reconcile()
    assert result.broker_position_id in report.unprotected
    assert broker.modifications[-1]["stopLoss"] == pytest.approx(1.0950)


# -- the two failures that only appear once a trade is real ---------------


def test_an_unreadable_broker_aborts_cleanly_instead_of_raising(config, broker, repos):
    """A rate-limited read used to escape as an exception.

    The intent is persisted before this check, so the exception left it
    CREATED — and the NEXT scan then found that unresolved intent and
    refused the same symbol again. One rate-limited read blocked a pair
    until a reconcile came round. This account is rate-limited in the
    normal course of things, so that was not hypothetical.
    """

    from bot.errors import BrokerRateLimited

    def refuse_positions():
        raise BrokerRateLimited("error 1015: you are being rate-limited")

    broker.positions = refuse_positions  # type: ignore[assignment]
    executor = Executor(config, broker, repos)
    plan = make_plan()

    result = executor.execute(plan, DEFAULT_SPEC, atr=0.0010)

    assert result.ok is False
    assert result.status == "ABORTED"
    assert "rule out a duplicate" in (result.reason or "")
    assert broker.submitted == [], "no order may be sent when a duplicate cannot be excluded"
    # And the symbol is free to trade again: the intent is resolved, not
    # left CREATED for the next scan to trip over.
    assert repos.intents.unresolved() == []


def test_a_protection_repair_that_did_not_take_is_reported_as_unprotected(
    config, broker, repos
):
    """A write that returned without raising is not a write that took.

    This is the one repair whose silent failure leaves an unbounded loss
    open while the system records the position as protected.
    """

    executor = Executor(config, broker, repos)
    plan = make_plan()

    broker.strip_protection = True
    # The broker accepts the modify and changes nothing — the exact shape
    # of the failure this check exists for.
    broker.modify_position = lambda position_id, **kwargs: {}  # type: ignore[assignment]

    executor.execute(plan, DEFAULT_SPEC, atr=0.0010)

    kinds = [row.get("kind") for row in repos.reconciliations.recent(limit=20)]
    assert "UNPROTECTED_POSITION" in kinds, (
        "a repair that did not take must be recorded as unprotected, not as repaired"
    )


def test_a_protection_repair_that_did_take_is_not_reported_as_unprotected(
    config, broker, repos
):
    """The fake's own modify DOES apply, so the read-back finds it."""

    executor = Executor(config, broker, repos)
    broker.strip_protection = True

    executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0010)

    kinds = [row.get("kind") for row in repos.reconciliations.recent(limit=20)]
    assert "UNPROTECTED_POSITION" not in kinds
    assert broker.modifications, "the repair must actually have been attempted"
