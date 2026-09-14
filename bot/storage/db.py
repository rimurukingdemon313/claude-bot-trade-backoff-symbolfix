"""Database abstraction over SQLite (default) and PostgreSQL (Railway).

Why an abstraction rather than "just use SQLite": Railway's filesystem is
ephemeral, so SQLite alone would lose the daily-loss counter, the kill
switch, and every execution intent on each redeploy — exactly the state
MASTER_MISSION §84 says must survive a restart. When DATABASE_URL is set
the Postgres backend is used and a missing driver is fatal rather than a
silent downgrade to a store that cannot survive the next deploy.

SQL is written once with `?` placeholders and rewritten to `%s` for
Postgres. The dialect differences that matter (autoincrement, upsert,
JSON column type) are isolated in `Database.ddl()`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import StorageError
from ..observability import log_event

#: Bump when ddl() changes. Every statement is CREATE ... IF NOT EXISTS, so
#: migration stays idempotent and safe to run on every boot; the version is
#: recorded so a deployment's schema generation is attributable.
SCHEMA_VERSION = 3

#: (table, column, definition) added after the first release. Applied on
#: every boot and safe to re-run: an existing column raises and is ignored.
ADDED_COLUMNS = (("equity_snapshots", "account_id", "TEXT"),)


class Database:
    """Thin, thread-safe connection holder with an explicit transaction API."""

    def __init__(self, url: str | None = None, sqlite_path: str = "data/bot.db") -> None:
        self.url = url
        self.sqlite_path = sqlite_path
        self.backend = "postgres" if url else "sqlite"
        self._lock = threading.RLock()
        self._conn: Any = None
        self._healthy = False
        self._last_error: str | None = None

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            if self.backend == "postgres":
                self._connect_postgres()
            else:
                self._connect_sqlite()
            self._healthy = True
            self._last_error = None

    def _connect_sqlite(self) -> None:
        path = Path(self.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn

    def _connect_postgres(self) -> None:
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise StorageError(
                "DATABASE_URL is set but the 'psycopg' driver is not installed. "
                "Refusing to silently fall back to ephemeral SQLite, which would "
                "lose the kill switch and daily-loss state on the next restart."
            ) from exc
        try:
            self._conn = psycopg.connect(self.url, autocommit=False, row_factory=dict_row)
        except Exception as exc:  # pragma: no cover - network dependent
            raise StorageError(f"Unable to connect to PostgreSQL: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
                    self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _mark_failed(self, exc: Exception) -> None:
        self._healthy = False
        self._last_error = str(exc)

    # -- SQL helpers -----------------------------------------------------

    def _rewrite(self, sql: str) -> str:
        if self.backend != "postgres":
            return sql
        out: list[str] = []
        in_string = False
        for char in sql:
            if char == "'":
                in_string = not in_string
            if char == "?" and not in_string:
                out.append("%s")
            else:
                out.append(char)
        return "".join(out)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """One atomic unit. Any exception rolls the whole thing back.

        Used for every multi-row write (e.g. "record the fill AND advance
        the trade state AND bump daily stats") so a crash mid-write can
        never leave risk accounting half-applied.
        """

        with self._lock:
            if self._conn is None:
                raise StorageError("database is not connected")
            cursor = self._conn.cursor()
            try:
                yield cursor
                self._conn.commit()
            except Exception as exc:
                try:
                    self._conn.rollback()
                except Exception:  # pragma: no cover - rollback of a dead conn
                    pass
                if not isinstance(exc, StorageError):
                    self._mark_failed(exc)
                raise
            finally:
                cursor.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.transaction() as cursor:
            cursor.execute(self._rewrite(sql), tuple(params))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self.transaction() as cursor:
            cursor.executemany(self._rewrite(sql), [tuple(row) for row in rows])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                raise StorageError("database is not connected")
            cursor = self._conn.cursor()
            try:
                cursor.execute(self._rewrite(sql), tuple(params))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
            except Exception as exc:
                self._mark_failed(exc)
                raise StorageError(f"query failed: {exc}") from exc
            finally:
                cursor.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def ping(self) -> bool:
        try:
            self.query("SELECT 1 AS ok")
            self._healthy = True
            self._last_error = None
            return True
        except Exception as exc:
            self._mark_failed(exc)
            return False

    # -- schema ----------------------------------------------------------

    def ddl(self) -> list[str]:
        """Schema statements, dialect-adjusted.

        Constraints here are load-bearing, not decoration:
          * execution_intents.idempotency_key UNIQUE is what makes a
            duplicate order physically impossible to record twice.
          * trades.broker_position_id UNIQUE prevents the reconciler from
            inserting a second row for a position it rediscovers.
        """

        serial = "BIGSERIAL PRIMARY KEY" if self.backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        json_type = "JSONB" if self.backend == "postgres" else "TEXT"
        return [
            f"""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS kv_state (
                key TEXT PRIMARY KEY,
                value {json_type} NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS execution_intents (
                id {serial},
                idempotency_key TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                plan {json_type} NOT NULL,
                broker_order_id TEXT,
                broker_position_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                failure_reason TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS trades (
                id {serial},
                execution_id TEXT NOT NULL UNIQUE,
                broker_position_id TEXT UNIQUE,
                broker_order_id TEXT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                planned_entry REAL,
                actual_entry REAL,
                stop_loss REAL,
                take_profit REAL,
                quantity REAL,
                risk_amount REAL,
                risk_pct REAL,
                expected_profit REAL,
                risk_reward REAL,
                setup_grade TEXT,
                setup_score REAL,
                ai_confidence REAL,
                exit_price REAL,
                realized_pnl REAL,
                r_multiple REAL,
                mfe REAL,
                mae REAL,
                exit_reason TEXT,
                opened_at TEXT,
                closed_at TEXT,
                versions {json_type},
                context {json_type},
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS decisions (
                id {serial},
                scan_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                stage TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT,
                payload {json_type},
                versions {json_type},
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS execution_events (
                id {serial},
                execution_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail {json_type},
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS reconciliations (
                id {serial},
                kind TEXT NOT NULL,
                symbol TEXT,
                detail {json_type} NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS daily_stats (
                day TEXT PRIMARY KEY,
                realized_pnl REAL NOT NULL DEFAULT 0,
                trades_opened INTEGER NOT NULL DEFAULT 0,
                trades_closed INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                consecutive_losses INTEGER NOT NULL DEFAULT 0,
                start_balance REAL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id {serial},
                balance REAL NOT NULL,
                equity REAL NOT NULL,
                -- Which broker account this reading belongs to. Peak equity
                -- drives the drawdown limit, and a peak inherited from a
                -- different account is not a drawdown, it is a different
                -- account.
                account_id TEXT,
                created_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS paper_positions (
                position_id TEXT PRIMARY KEY,
                execution_id TEXT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                contract_size REAL NOT NULL,
                conversion_rate REAL NOT NULL DEFAULT 1,
                commission REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                exit_price REAL,
                realized_pnl REAL,
                exit_reason TEXT,
                mark_price REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY,
                starting_balance REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                commission_paid REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status)",
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status)",
            "CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at)",
            "CREATE INDEX IF NOT EXISTS idx_decisions_scan ON decisions(scan_id)",
            "CREATE INDEX IF NOT EXISTS idx_decisions_symbol_created ON decisions(symbol, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_intents_status ON execution_intents(status)",
            "CREATE INDEX IF NOT EXISTS idx_exec_events_id ON execution_events(execution_id)",
            "CREATE INDEX IF NOT EXISTS idx_equity_created ON equity_snapshots(created_at)",
        ]

    def migrate(self) -> None:
        """Idempotent schema creation. Safe to run on every boot."""

        from ..clock import utc_now

        with self.transaction() as cursor:
            for statement in self.ddl():
                cursor.execute(statement)
            # Additive column migrations. CREATE TABLE IF NOT EXISTS does
            # nothing to a table that already exists, so a column added
            # after the first release needs this. Both backends accept
            # ADD COLUMN; neither agrees on IF NOT EXISTS, so a duplicate
            # is caught and ignored rather than guarded.
            for table, column, definition in ADDED_COLUMNS:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except Exception:  # noqa: BLE001 - already present
                    pass
            cursor.execute(
                self._rewrite(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)"
                    + (
                        " ON CONFLICT (version) DO NOTHING"
                        if self.backend == "postgres"
                        else " ON CONFLICT(version) DO NOTHING"
                    )
                ),
                (SCHEMA_VERSION, utc_now().isoformat()),
            )
        log_event("STORAGE", "schema migrated", backend=self.backend, version=SCHEMA_VERSION)


def open_database(config: Any) -> Database:
    """Build and connect a Database from a StorageConfig-shaped object."""

    database = Database(url=config.database_url, sqlite_path=config.sqlite_path)
    database.connect()
    database.migrate()
    return database


def in_memory_database() -> Database:
    """A connected, migrated SQLite database in a temp file (tests)."""

    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="bot-test-"), "test.db")
    database = Database(url=None, sqlite_path=path)
    database.connect()
    database.migrate()
    return database
