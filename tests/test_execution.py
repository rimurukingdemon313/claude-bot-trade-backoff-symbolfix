"""Execution, idempotency, and failure handling.

The invariant every test here defends: a network failure, a crash, or a
concurrent cycle must NEVER result in two orders for the same setup.
"""

from __future__ import annotations

import dataclasses

import pytest

from bot.errors import AmbiguousExecution, BrokerRejected, StorageError
from bot.execution.executor import ExecutionResult, Executor
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
        alignment="aligned",
        setup_id="test-setup",
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


def test_a_close_whose_result_cannot_be_read_never_becomes_a_zero(config, broker, repos):
    """The reconciler used to book an unreadable close as 0.00 P/L.

    Its own docstring said a fabricated PnL "would corrupt the daily loss
    counter" — and then it handed the 0.0 straight to `record_close`,
    which is that counter. Three harms landed at once and every one of
    them in the unsafe direction: the real loss vanished from the daily
    total that drives the max-daily-loss kill switch, the losing streak
    RESET (0.0 is not a loss) so risk stopped being reduced by adverse
    state, and the close diluted the win rate as neither a win nor a
    loss.

    A close nobody could price is an unknown, not a scratch.
    """

    from bot.errors import BrokerRateLimited

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    # A real loss has already been booked today, so the streak is live.
    repos.daily.record_close(-50.0)
    before = repos.daily.today()
    assert before["consecutive_losses"] == 1

    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)

    def refuse_history(limit: int = 200):
        raise BrokerRateLimited("error 1015: you are being rate-limited")

    broker.order_history = refuse_history  # type: ignore[assignment]

    report = Reconciler(config, broker, repos).reconcile()

    # The counters first: this is the harm, and it lands whether or not
    # anything is reported anywhere.
    after = repos.daily.today()
    assert after["realized_pnl"] == pytest.approx(-50.0), "an unmeasured close is not a 0.00"
    assert after["consecutive_losses"] == 1, "a close nobody priced must not clear a losing run"
    assert after["trades_closed"] == before["trades_closed"]

    assert result.broker_position_id in report.unmeasured_closes
    assert result.broker_position_id in report.closed_stale

    trade = repos.trades.by_execution_id(result.plan.execution_id)
    assert trade["status"] == "CLOSED"
    assert trade["exit_reason"] == "BROKER_CLOSED_PNL_UNKNOWN"
    assert trade["realized_pnl"] is None, "None is a visible gap; 0.0 looks like information"

    kinds = [row["kind"] for row in repos.reconciliations.recent()]
    assert "CLOSE_WITHOUT_RESULT" in kinds


def test_a_history_that_carries_no_result_for_the_position_is_also_unknown(
    config, broker, repos
):
    """The other path into the same fabrication.

    `order_history` can answer perfectly well and simply not mention this
    position — it is capped at 200 orders, and a position closed long
    enough ago falls off the end. The old code walked the rows, matched
    nothing, and returned the 0.0 it had initialised `pnl` to, which is
    indistinguishable from a genuine scratch.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    repos.daily.record_close(-50.0)

    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)
    broker._history.clear()  # the close has aged out of the window

    report = Reconciler(config, broker, repos).reconcile()

    assert repos.daily.today()["realized_pnl"] == pytest.approx(-50.0)
    assert repos.daily.today()["consecutive_losses"] == 1
    assert result.broker_position_id in report.unmeasured_closes
    trade = repos.trades.by_execution_id(result.plan.execution_id)
    assert trade["exit_reason"] == "BROKER_CLOSED_PNL_UNKNOWN"


def test_an_unmeasured_close_still_starts_the_loss_cooldown(orchestrator, broker, repos):
    """Unknown is not "fine".

    The daily counters must not be told a number nobody measured — but
    the post-loss cooldown is a risk REDUCER, and there the conservative
    reading is free. A position that vanished for an unknown amount
    starts the cooldown as if it were a loss, because the only cost of
    being wrong about that is trading less.
    """

    from bot.errors import BrokerRateLimited

    executor = Executor(orchestrator.config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)

    def refuse_history(limit: int = 200):
        raise BrokerRateLimited("error 1015: you are being rate-limited")

    broker.order_history = refuse_history  # type: ignore[assignment]
    assert repos.state.get("last_loss_at") is None

    orchestrator.reconcile()

    assert repos.state.get("last_loss_at") is not None


def test_the_dashboard_total_says_when_it_is_missing_a_result(orchestrator, broker, repos):
    """The same fabrication, one layer up.

    `totalPnl` summed `realized_pnl or 0.0`. A trade stored as NULL
    precisely so it would not be mistaken for a scratch went straight
    back to counting as zero the moment it reached the page — and the
    page gave no sign it was short a trade. The sum of what was measured
    is honest; the gap has to travel with it (rule 6).
    """

    from bot.api import DashboardApi
    from bot.errors import BrokerRateLimited

    executor = Executor(orchestrator.config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)

    def refuse_history(limit: int = 200):
        raise BrokerRateLimited("error 1015: you are being rate-limited")

    broker.order_history = refuse_history  # type: ignore[assignment]
    orchestrator.reconcile()

    data = DashboardApi(orchestrator.config, orchestrator, repos).account()["data"]
    assert data["totalPnlUnpricedTrades"] == 1, "the page must admit the total is partial"
    assert data["totalPnl"] == pytest.approx(0.0), "and must not have invented the missing one"


def test_a_measured_close_still_reaches_the_daily_counters(config, broker, repos):
    """The guard above must not cost the normal path.

    Refusing to fabricate is only worth anything if a result the broker
    DOES report still lands in the counters that reduce risk.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    broker.remove_position(result.broker_position_id, realized_pnl=-82.5, exit_price=1.0950)

    report = Reconciler(config, broker, repos).reconcile()

    assert report.unmeasured_closes == []
    assert repos.daily.today()["realized_pnl"] == pytest.approx(-82.5)
    assert repos.daily.today()["consecutive_losses"] == 1
    trade = repos.trades.by_execution_id(result.plan.execution_id)
    assert trade["exit_reason"] == "BROKER_CLOSED"


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


def _setup_ids(repos):
    return repos.trades.traded_setup_ids(hours=24.0)


def test_a_refused_order_does_not_mark_the_setup_as_traded(config, broker, repos):
    """The bug that made a working bot look like a dead one.

    `create_pending` writes the trade row BEFORE the order is submitted,
    so it carries the setup_id from the start. Every clean abort after
    that point — the spread guard, the fresh duplicate check, the
    submission-time demo check, a broker rejection — left the row at
    PENDING forever, and `traded_setup_ids` counted PENDING.

    So a setup whose order was REFUSED was recorded as "already traded"
    and locked out for the whole 24-hour re-entry window. The row is
    invisible on the dashboard — open positions are read from the
    broker, the history shows CLOSED only — so the operator saw a card
    saying "this exact setup has already been traded" beside 0 open,
    0 closed and 0 trades today, and reasonably concluded the bot was
    broken.

    It was worse than random: the better the setup, the sooner it is
    ranked, attempted, refused and blocked. The system burned its own
    best candidates for a day at a time.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    plan = make_plan(setup_id="f6e0d871084c7cea")

    # A spread wide enough that the execution guard must refuse it.
    from fakes import Quote

    broker.quotes["EURUSD"] = Quote("EURUSD", 1.0990, 1.1050, SETUP_END)
    result = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)

    assert result.ok is False
    assert result.status == "ABORTED"
    assert broker.submitted == [], "nothing may have been sent"

    assert "f6e0d871084c7cea" not in _setup_ids(repos), (
        "an order that never reached the broker is not a trade and must not "
        "block the setup"
    )
    assert repos.trades.open_trades() == [], "nor may it sit in the book as open"


def test_an_ambiguous_submission_still_blocks_the_setup(config, broker, repos):
    """The control, and it matters more than the fix.

    When the outcome is UNKNOWN a position may exist, so the row stays
    PENDING and the setup stays blocked. Project rule 3: the only
    recovery for an unknown write is to ask the broker — never to try
    again. A fix that freed every pending row would have turned a
    reconciliation case into a duplicate position.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    broker.place_order_hook = ambiguous_hook
    plan = make_plan(setup_id="ambiguous-setup-id")

    result = executor.execute(plan, DEFAULT_SPEC, atr=0.0012)

    assert result.status == "AMBIGUOUS"
    assert "ambiguous-setup-id" in _setup_ids(repos), (
        "an unknown outcome must keep blocking the setup"
    )


def test_a_setup_refused_once_is_attempted_again_when_conditions_clear(
    config, broker, repos
):
    """The second blocker, under the one fixed a commit earlier.

    `execution_id` is derived from symbol, direction, trigger time and
    the three levels. None of those change while the same sweep and
    break of structure sit on the chart, so every later scan produced
    the IDENTICAL key — and `intents.create` refused any existing
    intent whatever its status. A setup the spread guard turned away
    once could never be attempted again, however much conditions
    improved. The idempotency key had become a lifetime ban on a plan
    rather than a guard against a second order.

    A spread that widened for one minute cost that setup permanently.
    """

    from fakes import Quote

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)

    broker.quotes["EURUSD"] = Quote("EURUSD", 1.0990, 1.1050, SETUP_END)
    first = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert first.status == "ABORTED"
    assert broker.submitted == []

    # The spread narrows; the next scan re-derives the same setup.
    broker.quotes["EURUSD"] = Quote("EURUSD", 1.12197, 1.12199, SETUP_END)
    second = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    assert second.ok is True, second.reason
    assert len(broker.submitted) == 1, "exactly one order, on the second attempt"


def test_a_filled_setup_is_never_attempted_again(config, broker, repos):
    """The control that protects rule 3, and the reason this was delicate.

    Re-arming is allowed ONLY from a state that proves nothing reached
    the broker. A FILLED intent means a position exists, so the same
    plan must still be refused — otherwise the fix above would have
    turned the duplicate guard into a duplicate-position generator.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    first = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert first.ok is True

    second = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    assert second.ok is False
    assert second.status in ("DUPLICATE", "ABORTED")
    assert len(broker.submitted) == 1, "a filled plan must never send a second order"


def test_an_ambiguous_intent_is_never_re_armed(config, broker, repos):
    """The other half of rule 3.

    After an ambiguous submission a position MAY exist. Re-arming there
    would resend a write whose outcome is unknown, which is the single
    thing this codebase forbids most plainly.
    """

    executor = Executor(config, broker, repos, sleeper=lambda _s: None)
    broker.place_order_hook = ambiguous_hook
    first = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert first.status == "AMBIGUOUS"

    sent_before = len(broker.submitted)
    broker.place_order_hook = None
    second = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)

    assert second.status == "DUPLICATE"
    assert len(broker.submitted) == sent_before, "no resend while the outcome is unknown"


def test_a_spread_abort_does_not_stop_the_whole_bot_for_twenty_minutes(
    orchestrator, broker, repos
):
    """The cooldown fired on ANY unsuccessful execution.

    By far the commonest is ABORTED from the spread guard, which is
    market state and not a failure: no order was sent and nothing broke.
    But it started `execution_failure_cooldown_minutes` — 20 minutes
    blocking EVERY symbol. The scan interval is 15, so one temporarily
    wide spread on one pair silently skipped the next whole scan across
    all twelve.

    It also cancelled the retry fix in df5b18d: setups may now be
    re-attempted when conditions clear, and each attempt that met a wide
    spread would have re-armed a global block.
    """

    from fakes import Quote

    executor = Executor(orchestrator.config, broker, repos, sleeper=lambda _s: None)
    broker.quotes["EURUSD"] = Quote("EURUSD", 1.0990, 1.1050, SETUP_END)
    result = executor.execute(make_plan(), DEFAULT_SPEC, atr=0.0012)
    assert result.status == "ABORTED"

    started = orchestrator._record_execution_outcome(result)

    assert started is False, "a guard refusing to send an order is not a failure"
    assert repos.state.get("last_execution_failure_at") is None


@pytest.mark.parametrize("status", ["AMBIGUOUS", "REJECTED"])
def test_a_real_execution_failure_still_starts_the_cooldown(
    orchestrator, repos, status
):
    """The control: the cooldown must still exist for what it was built for.

    An unknown broker outcome, or an order the broker refused, are the
    cases worth pausing on. Narrowing the trigger must not remove it.
    """

    import dataclasses

    from fakes import Quote  # noqa: F401  (kept for symmetry with the test above)

    refused = ExecutionResult(False, make_plan(), status, "broker said no")
    assert orchestrator._record_execution_outcome(refused) is True
    assert repos.state.get("last_execution_failure_at") is not None


def test_a_successful_execution_never_starts_the_cooldown(orchestrator, repos):
    filled = ExecutionResult(True, make_plan(), "FILLED", None)
    assert orchestrator._record_execution_outcome(filled) is False
    assert repos.state.get("last_execution_failure_at") is None
