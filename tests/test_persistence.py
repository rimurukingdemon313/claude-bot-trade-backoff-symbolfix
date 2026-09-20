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


# -- state belongs to the account that produced it ------------------------


def test_peak_equity_is_scoped_to_its_account(repos):
    """A peak from a DIFFERENT account is not a drawdown.

    Switching a $10,000 demo for a fresh $1,000 one would otherwise read
    as a 90% loss and trip MAX_DRAWDOWN before the first trade — a safety
    limit firing on arithmetic about an account that no longer exists.
    """

    repos.equity.snapshot(10_000.0, 10_000.0, "old-account")
    repos.equity.snapshot(1_000.0, 1_000.0, "new-account")

    assert repos.equity.peak_equity("old-account") == pytest.approx(10_000.0)
    assert repos.equity.peak_equity("new-account") == pytest.approx(1_000.0)


def test_the_drawdown_on_a_fresh_account_starts_at_zero(repos):
    repos.equity.snapshot(10_000.0, 10_000.0, "old-account")
    repos.equity.snapshot(1_000.0, 1_000.0, "new-account")

    peak = repos.equity.peak_equity("new-account") or 1_000.0
    drawdown = max(0.0, (peak - 1_000.0) / peak)
    assert drawdown == pytest.approx(0.0), "a new account must not open in drawdown"


def test_an_unscoped_read_still_sees_everything(repos):
    """The dashboard's equity curve is not per-account and must not change."""

    repos.equity.snapshot(10_000.0, 10_000.0, "old-account")
    repos.equity.snapshot(1_000.0, 1_000.0, "new-account")
    assert repos.equity.peak_equity() == pytest.approx(10_000.0)


def test_rows_written_before_the_column_existed_are_not_claimed_by_an_account(repos):
    """They belong to an account nobody recorded, so they belong to none."""

    repos.equity.snapshot(5_000.0, 5_000.0)  # no account id
    assert repos.equity.peak_equity("some-account") is None
    assert repos.equity.peak_equity() == pytest.approx(5_000.0)


def test_switching_accounts_is_announced_rather_than_silent(orchestrator, repos, capsys):
    import dataclasses

    from bot.orchestrator import STATE_ACCOUNT_ID

    orchestrator.config = dataclasses.replace(
        orchestrator.config,
        broker=dataclasses.replace(orchestrator.config.broker, account_id="999-new"),
    )
    repos.state.set(STATE_ACCOUNT_ID, "2475112")

    orchestrator._note_account_identity()

    captured = capsys.readouterr()
    assert "broker account changed" in (captured.out + captured.err)
    # And the new identity is remembered, so it is announced once, not
    # every boot.
    assert str(repos.state.get(STATE_ACCOUNT_ID)) == "999-new"

    orchestrator._note_account_identity()
    assert "broker account changed" not in (capsys.readouterr().out + captured.err[:0])


# -- migrations must survive PostgreSQL, not merely SQLite ----------------


def test_migrating_twice_is_a_no_op(tmp_path):
    """Every boot runs migrate(). The second one must not fail."""

    from bot.storage.db import Database

    database = Database(sqlite_path=str(tmp_path / "bot.db"))
    database.connect()
    database.migrate()
    database.migrate()  # must not raise
    assert database.ping()
    database.close()


def test_an_added_column_is_checked_for_rather_than_attempted(tmp_path):
    """The bug this exists to prevent crash-looped the bot on startup.

    In PostgreSQL ANY failed statement aborts the whole transaction, and
    every command after it raises InFailedSqlTransaction. So "try the
    ALTER and ignore the duplicate-column error" is not a portable idiom —
    it is a way to destroy the migration on the SECOND boot. It worked in
    SQLite, which is exactly why it shipped: the failure only appeared
    once a real PostgreSQL was attached.

    This drives the migration through a cursor with PostgreSQL's
    semantics: once a statement raises, every later one does too.
    """

    from bot.storage.db import ADDED_COLUMNS, Database

    assert ADDED_COLUMNS, "nothing to check"

    class PoisonedOnError:
        """A cursor that behaves the way PostgreSQL actually behaves."""

        def __init__(self, real):
            self.real = real
            self.aborted = False

        def execute(self, statement, params=()):
            if self.aborted:
                raise RuntimeError(
                    "current transaction is aborted, commands ignored "
                    "until end of transaction block"
                )
            try:
                return self.real.execute(statement, params)
            except Exception:
                self.aborted = True
                raise

        def fetchone(self):
            return self.real.fetchone()

        def fetchall(self):
            return self.real.fetchall()

    database = Database(sqlite_path=str(tmp_path / "bot.db"))
    database.connect()
    database.migrate()  # first boot: creates everything

    # Second boot, with a cursor that punishes a provoked error the way
    # PostgreSQL does.
    with database.transaction() as real_cursor:
        cursor = PoisonedOnError(real_cursor)
        for table, column, definition in ADDED_COLUMNS:
            if database._column_exists(cursor, table, column):
                continue
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        assert not cursor.aborted, (
            "the migration provoked an error; on PostgreSQL this aborts the "
            "transaction and every statement after it fails"
        )


def test_column_existence_is_reported_correctly(tmp_path):
    from bot.storage.db import Database

    database = Database(sqlite_path=str(tmp_path / "bot.db"))
    database.connect()
    database.migrate()
    with database.transaction() as cursor:
        assert database._column_exists(cursor, "equity_snapshots", "account_id") is True
        assert database._column_exists(cursor, "equity_snapshots", "not_a_column") is False
    database.close()


def test_statistics_say_how_many_trades_they_had_to_leave_out():
    """Dropping an unpriced trade is right; dropping it silently is not.

    `compute_performance` correctly refuses to count a trade the broker
    never priced — a fabricated zero would move the win rate, the
    expectancy and the drawdown. But it dropped them without a word, so
    `trades` disagreed with the trade list on the same page and the gap
    read as a bug rather than as the missing information it is.
    """

    from bot.analytics.performance import compute_performance

    closed = [
        {"realized_pnl": 100.0, "closed_at": "2026-01-01T10:00:00+00:00"},
        {"realized_pnl": -50.0, "closed_at": "2026-01-01T11:00:00+00:00"},
        {"realized_pnl": None, "closed_at": "2026-01-01T12:00:00+00:00"},
    ]

    stats = compute_performance(closed).as_dict()

    assert stats["trades"] == 2, "the unpriced trade is not counted as a result"
    assert stats["unpriced"] == 1, "but the page is told one is missing"
    assert stats["winRate"] == pytest.approx(0.5), (
        "and the win rate is not diluted by a trade nobody measured"
    )
    assert stats["totalPnl"] == pytest.approx(50.0)


def test_statistics_with_nothing_but_unpriced_trades_do_not_claim_emptiness():
    """`trades: 0` with closed trades on the books is a misleading pair."""

    from bot.analytics.performance import compute_performance

    stats = compute_performance(
        [{"realized_pnl": None, "closed_at": "2026-01-01T12:00:00+00:00"}]
    ).as_dict()

    assert stats["trades"] == 0
    assert stats["unpriced"] == 1
    assert stats["winRate"] is None, "no data is None, never a zero win rate"


def test_an_env_tuned_build_records_a_different_fingerprint_than_the_default():
    """Rule 9, for the settings that no longer live in the source.

    Stop width, the score floor, the R:R minimum and the position
    management switches are all settable from the environment — on
    purpose, so an experiment can run on paper without a deploy. That
    quietly broke the rule the environment variables were added under:
    every trade, experiment and control alike, recorded the same
    `risk-x.y.z`, so afterwards nothing could tell which setting had
    produced which result. That is precisely the "silent behaviour
    change under an unchanged version" rule 9 exists to prevent, and I
    introduced it by adding the knobs.

    The stamp now carries a fingerprint of the tuning actually in force.
    """

    from bot.config import load_config
    from bot.version import set_tuning_fingerprint, version_stamp

    stock = load_config()
    set_tuning_fingerprint(stock)
    assert version_stamp()["tuning"] == "default"

    tuned = load_config({"RISK_MIN_STOP_ATR": "1.30"})
    set_tuning_fingerprint(tuned)
    tuned_stamp = version_stamp()["tuning"]
    assert tuned_stamp != "default"
    assert tuned_stamp.startswith("tuned-")

    # And it is a function of the settings, not of the run: the same
    # override on a later process has to group with the earlier trades.
    set_tuning_fingerprint(load_config({"RISK_MIN_STOP_ATR": "1.30"}))
    assert version_stamp()["tuning"] == tuned_stamp

    # A different override is a different experiment.
    set_tuning_fingerprint(load_config({"RISK_MIN_STOP_ATR": "0.90"}))
    assert version_stamp()["tuning"] != tuned_stamp

    set_tuning_fingerprint(stock)  # leave the process as we found it


def test_the_fingerprint_never_touches_a_credential():
    """It is written into every trade row, so it must hash nothing secret."""

    from bot.version import _TUNED_SECTIONS

    for forbidden in ("broker", "storage", "dashboard_token"):
        assert forbidden not in _TUNED_SECTIONS
