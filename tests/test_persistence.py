"""Persistence: the state that must survive a Railway restart."""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.storage.db import Database, in_memory_database
from bot.storage.repositories import Repositories
from fakes import SETUP_END


@pytest.fixture()
def db() -> Database:
    return in_memory_database()


def test_state_survives_a_new_process(db):
    Repositories(db).state.set("trading_enabled", False)
    # A second Repositories over the same file is what a restart looks like.
    reopened = Database(url=None, sqlite_path=db.sqlite_path)
    reopened.connect()
    assert Repositories(reopened).state.get("trading_enabled") is False


def test_daily_counters_accumulate_and_persist(repos):
    repos.daily.record_open()
    repos.daily.record_open()
    repos.daily.record_close(-50.0)
    repos.daily.record_close(120.0)
    today = repos.daily.today()
    assert today["trades_opened"] == 2
    assert today["trades_closed"] == 2
    assert today["realized_pnl"] == pytest.approx(70.0)
    assert today["wins"] == 1 and today["losses"] == 1


def test_a_win_resets_the_losing_streak(repos):
    repos.daily.record_close(-10.0)
    repos.daily.record_close(-10.0)
    assert repos.daily.consecutive_losses() == 2
    repos.daily.record_close(30.0)
    assert repos.daily.consecutive_losses() == 0


def test_the_losing_streak_crosses_day_boundaries(repos):
    """A streak must not reset just because the UTC date rolled over."""

    yesterday = SETUP_END - timedelta(days=1)
    repos.daily.record_close(-10.0, now=yesterday)
    repos.daily.record_close(-10.0, now=yesterday)
    repos.daily.record_close(-10.0, now=SETUP_END)
    assert repos.daily.consecutive_losses(now=SETUP_END) == 3


def test_equity_peak_and_curve(repos):
    for value in (10_000, 10_400, 9_800):
        repos.equity.snapshot(value, value)
    assert repos.equity.peak_equity() == pytest.approx(10_400)
    assert len(repos.equity.curve()) == 3


def test_equity_snapshots_are_pruned(repos):
    for index in range(50):
        repos.equity.snapshot(10_000 + index, 10_000 + index)
    repos.equity.prune(keep=10)
    assert len(repos.equity.curve(limit=1000)) == 10


def test_the_decision_journal_records_rejections_with_reasons(repos):
    repos.journal.record(
        scan_id="s1", symbol="EURUSD", stage="SMC", outcome="NO_SETUP", reason="no sweep"
    )
    repos.journal.record(
        scan_id="s1", symbol="GBPUSD", stage="RISK", outcome="REJECTED", reason="daily loss"
    )
    rows = repos.journal.recent()
    assert len(rows) == 2
    assert {row["stage"] for row in rows} == {"SMC", "RISK"}
    histogram = repos.journal.rejection_histogram()
    assert sum(row["count"] for row in histogram) == 2


def test_the_trade_lifecycle_is_recorded_end_to_end(repos):
    plan = {
        "symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "stop_loss": 1.09,
        "take_profit": 1.13, "quantity": 0.2, "risk_amount": 100.0, "risk_pct": 0.01,
        "expected_profit": 300.0, "risk_reward": 3.0, "setup_grade": "A",
        "setup_score": 70.0, "ai_confidence": 80.0, "context": {"session": "LONDON"},
    }
    repos.trades.create_pending(execution_id="exec-1", plan=plan)
    assert repos.trades.by_execution_id("exec-1")["status"] == "PENDING"

    repos.trades.mark_open(
        "exec-1", broker_position_id="p1", broker_order_id="o1", actual_entry=1.1002, quantity=0.2
    )
    assert repos.trades.by_position_id("p1")["status"] == "OPEN"

    repos.trades.update_excursions("p1", mfe=0.004, mae=-0.001)
    closed = repos.trades.mark_closed(
        broker_position_id="p1", exit_price=1.13, realized_pnl=280.0,
        exit_reason="TARGET", r_multiple=2.8,
    )
    assert closed["status"] == "CLOSED"
    assert closed["realized_pnl"] == pytest.approx(280.0)
    assert closed["mfe"] == pytest.approx(0.004)
    assert closed["versions"]["risk"]


def test_excursions_only_ever_widen(repos):
    repos.trades.create_pending(
        execution_id="e", plan={"symbol": "EURUSD", "direction": "BUY"}
    )
    repos.trades.mark_open("e", broker_position_id="p", broker_order_id=None, actual_entry=1.1, quantity=0.1)
    repos.trades.update_excursions("p", mfe=0.005, mae=-0.002)
    repos.trades.update_excursions("p", mfe=0.001, mae=-0.001)
    trade = repos.trades.by_position_id("p")
    assert trade["mfe"] == pytest.approx(0.005)
    assert trade["mae"] == pytest.approx(-0.002)


def test_a_sparse_broker_payload_does_not_crash_adoption(repos):
    """A position discovered with missing fields must still be recorded —
    an unrecorded live position is far worse than an incomplete row."""

    repos.trades.adopt_orphan(broker_position_id="p8", snapshot={})
    trade = repos.trades.by_position_id("p8")
    assert trade["symbol"] == "UNKNOWN" and trade["direction"] == "UNKNOWN"


def test_a_position_id_can_only_be_recorded_once(repos):
    snapshot = {"symbol": "EURUSD", "direction": "BUY", "entry": 1.1}
    repos.trades.adopt_orphan(broker_position_id="p9", snapshot=snapshot)
    repos.trades.adopt_orphan(broker_position_id="p9", snapshot=snapshot)
    assert len(repos.trades.open_trades()) == 1


def test_unresolved_intents_are_listed_for_recovery(repos):
    repos.intents.create(idempotency_key="a", symbol="EURUSD", direction="BUY", plan={})
    repos.intents.create(idempotency_key="b", symbol="GBPUSD", direction="SELL", plan={})
    repos.intents.mark("b", "FILLED")
    unresolved = [item["idempotency_key"] for item in repos.intents.unresolved()]
    assert unresolved == ["a"]


def test_an_unknown_intent_status_is_rejected(repos):
    from bot.errors import StorageError

    repos.intents.create(idempotency_key="a", symbol="EURUSD", direction="BUY", plan={})
    with pytest.raises(StorageError, match="unknown intent status"):
        repos.intents.mark("a", "PROBABLY_FINE")


def test_a_transaction_rolls_back_as_a_unit(db):
    repos = Repositories(db)
    repos.state.set("before", 1)
    with pytest.raises(RuntimeError):
        with db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES ('during', '1', 'now')"
            )
            raise RuntimeError("boom")
    assert repos.state.get("during") is None
    assert repos.state.get("before") == 1


def test_a_closed_database_reports_unhealthy(db):
    assert db.ping() is True
    db.close()
    assert db.ping() is False


def test_postgres_url_without_a_driver_fails_loudly_instead_of_downgrading():
    """Silently falling back to ephemeral SQLite would lose the kill
    switch on the next deploy."""

    from bot.errors import StorageError

    database = Database(url="postgresql://user:pass@host/db")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        with pytest.raises(StorageError, match="Refusing to silently fall back"):
            database.connect()
    else:  # pragma: no cover - driver present in this environment
        pytest.skip("psycopg is installed; the no-driver path cannot be exercised")


def test_sql_placeholders_are_rewritten_for_postgres():
    database = Database(url="postgresql://x")
    assert database._rewrite("SELECT * FROM t WHERE a = ? AND b = ?") == (
        "SELECT * FROM t WHERE a = %s AND b = %s"
    )
    # A literal question mark inside a string must survive untouched.
    assert database._rewrite("SELECT 'why?' WHERE a = ?") == "SELECT 'why?' WHERE a = %s"


# -- what is actually blocking the trades ---------------------------------


def test_blockers_are_ranked_by_cause_not_by_stage(repos):
    """A stage histogram says SMC/NO_SETUP and explains nothing."""

    for _ in range(5):
        repos.journal.record(
            scan_id="s1", symbol="EURUSD", stage="SMC", outcome="NO_SETUP",
            reason="M15 has no confirmed directional structure",
        )
    for _ in range(2):
        repos.journal.record(
            scan_id="s1", symbol="GBPUSD", stage="RISK", outcome="REJECTED",
            reason="spread too wide",
        )

    blockers = repos.journal.blocker_histogram(days=7)
    assert blockers[0]["reason"] == "M15 has no confirmed directional structure"
    assert blockers[0]["count"] == 5
    assert blockers[0]["share"] == pytest.approx(5 / 7, abs=1e-4)  # stored rounded for display
    assert blockers[1]["count"] == 2


def test_identical_causes_fold_despite_differing_numbers(repos):
    """Otherwise the one real answer is scattered across a hundred rows."""

    for rr in ("2.13", "2.44", "3.01"):
        repos.journal.record(
            scan_id="s1", symbol="EURUSD", stage="SMC", outcome="NO_SETUP",
            reason=f"structural R:R is 1:{rr}, below the required 1:4",
        )

    blockers = repos.journal.blocker_histogram(days=7)
    assert len(blockers) == 1
    assert blockers[0]["count"] == 3
    assert "#" in blockers[0]["reason"]


def test_accepted_candidates_are_not_counted_as_blockers(repos):
    repos.journal.record(
        scan_id="s1", symbol="EURUSD", stage="SMC", outcome="CANDIDATE", reason="clean setup"
    )
    assert repos.journal.blocker_histogram(days=7) == []


def test_a_blockerless_journal_returns_nothing_rather_than_failing(repos):
    assert repos.journal.blocker_histogram(days=7) == []
