"""Typed repositories over the schema in db.py.

Everything the trading loop needs to survive a restart lives here:
execution intents (idempotency), the trade lifecycle, the decision
journal, daily risk counters, the kill switch, and reconciliation
records.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from ..clock import trading_day, utc_now
from ..errors import StorageError
from ..observability import log_event
from ..version import version_stamp
from .db import Database

# Intent lifecycle. The order matters: an intent may only move forward.
INTENT_STATES = (
    "CREATED",      # persisted before anything left this process
    "SUBMITTED",    # request handed to the broker
    "ACKNOWLEDGED", # broker returned an order id
    "FILLED",       # a position was verified at the broker
    "AMBIGUOUS",    # outcome unknown - requires reconciliation, never a retry
    "FAILED",       # broker rejected, or we aborted before submitting
    "ABANDONED",    # reconciliation proved nothing happened
)

TRADE_STATES = ("PENDING", "OPEN", "CLOSED", "ORPHANED")


def _is_unique_violation(exc: BaseException) -> bool:
    """Backend-agnostic detection of a UNIQUE constraint failure.

    Matching on the message rather than the exception class keeps this
    working for sqlite3.IntegrityError and psycopg.errors.UniqueViolation
    without importing either.
    """

    text = f"{type(exc).__name__}: {exc}".lower()
    return "unique" in text and ("constraint" in text or "violation" in text or "duplicate" in text)


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class StateRepository:
    """Key/value state that must outlive the process (kill switch, flags)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM kv_state WHERE key = ?", (key,))
        if row is None:
            return default
        value = _loads(row["value"])
        return default if value is None else value

    def set(self, key: str, value: Any) -> None:
        now = utc_now().isoformat()
        payload = _dumps(value)
        conflict = (
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"
            if self.db.backend == "postgres"
            else "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        )
        self.db.execute(
            f"INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) {conflict}",
            (key, payload, now),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv_state WHERE key = ?", (key,))


class IntentRepository:
    """Execution intents — the idempotency spine.

    The flow is always: create (persisted) -> submit -> resolve. Because
    `idempotency_key` is UNIQUE, a second attempt to create the same
    intent raises instead of producing a second order path. That is the
    structural defence against duplicate trades, not a best-effort check.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        idempotency_key: str,
        symbol: str,
        direction: str,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        existing = self.get(idempotency_key)
        if existing is not None:
            raise StorageError(
                f"execution intent {idempotency_key} already exists with status "
                f"{existing['status']} — refusing to create a duplicate"
            )
        try:
            self.db.execute(
                """
                INSERT INTO execution_intents
                    (idempotency_key, symbol, direction, status, plan, created_at, updated_at)
                VALUES (?, ?, ?, 'CREATED', ?, ?, ?)
                """,
                (idempotency_key, symbol, direction, _dumps(plan), now, now),
            )
        except Exception as exc:  # noqa: BLE001 - backend-specific integrity error
            # The check above is not atomic with the insert: two threads (a
            # timer and a manual trigger) can both pass it. The UNIQUE
            # constraint is what actually prevents the duplicate order — this
            # translates the backend's raw integrity error into the same
            # classified StorageError the caller already handles, so the
            # losing thread reports DUPLICATE instead of crashing with an
            # unhandled sqlite3/psycopg exception.
            if _is_unique_violation(exc):
                raise StorageError(
                    f"execution intent {idempotency_key} was created concurrently — "
                    "refusing to create a duplicate"
                ) from exc
            raise
        return self.get(idempotency_key)  # type: ignore[return-value]

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM execution_intents WHERE idempotency_key = ?", (idempotency_key,)
        )
        if row is None:
            return None
        row["plan"] = _loads(row.get("plan"))
        return row

    def mark(
        self,
        idempotency_key: str,
        status: str,
        *,
        broker_order_id: str | None = None,
        broker_position_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if status not in INTENT_STATES:
            raise StorageError(f"unknown intent status {status!r}")
        now = utc_now().isoformat()
        resolved = now if status in ("FILLED", "FAILED", "ABANDONED") else None
        self.db.execute(
            """
            UPDATE execution_intents
               SET status = ?,
                   broker_order_id = COALESCE(?, broker_order_id),
                   broker_position_id = COALESCE(?, broker_position_id),
                   failure_reason = COALESCE(?, failure_reason),
                   resolved_at = COALESCE(?, resolved_at),
                   updated_at = ?
             WHERE idempotency_key = ?
            """,
            (status, broker_order_id, broker_position_id, failure_reason, resolved, now, idempotency_key),
        )

    def unresolved(self) -> list[dict[str, Any]]:
        """Intents that must be settled against the broker before any new
        order is allowed (startup recovery, MASTER_MISSION §8/§10)."""

        rows = self.db.query(
            "SELECT * FROM execution_intents WHERE status IN ('CREATED','SUBMITTED','ACKNOWLEDGED','AMBIGUOUS') "
            "ORDER BY created_at ASC"
        )
        for row in rows:
            row["plan"] = _loads(row.get("plan"))
        return rows

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM execution_intents ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["plan"] = _loads(row.get("plan"))
        return rows


class TradeRepository:
    """The trade lifecycle: PENDING -> OPEN -> CLOSED (or ORPHANED)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create_pending(self, *, execution_id: str, plan: Mapping[str, Any]) -> None:
        now = utc_now().isoformat()
        self.db.execute(
            """
            INSERT INTO trades (
                execution_id, symbol, direction, status, planned_entry, stop_loss,
                take_profit, quantity, risk_amount, risk_pct, expected_profit,
                risk_reward, setup_grade, setup_score, ai_confidence, versions,
                context, created_at, updated_at
            ) VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                # symbol/direction are NOT NULL: a trade row without them
                # cannot be reconciled against the broker later. Defaulting
                # keeps a sparse payload from crashing the write path, and
                # "UNKNOWN" is visibly wrong on the dashboard rather than
                # quietly absent.
                plan.get("symbol") or "UNKNOWN",
                str(plan.get("direction") or "UNKNOWN").upper(),
                plan.get("entry"),
                plan.get("stop_loss"),
                plan.get("take_profit"),
                plan.get("quantity"),
                plan.get("risk_amount"),
                plan.get("risk_pct"),
                plan.get("expected_profit"),
                plan.get("risk_reward"),
                plan.get("setup_grade"),
                plan.get("setup_score"),
                plan.get("ai_confidence"),
                _dumps(version_stamp()),
                _dumps(plan.get("context", {})),
                now,
                now,
            ),
        )

    def mark_open(
        self,
        execution_id: str,
        *,
        broker_position_id: str,
        broker_order_id: str | None,
        actual_entry: float | None,
        quantity: float | None,
        opened_at: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        self.db.execute(
            """
            UPDATE trades
               SET status = 'OPEN',
                   broker_position_id = ?,
                   broker_order_id = COALESCE(?, broker_order_id),
                   actual_entry = COALESCE(?, actual_entry),
                   quantity = COALESCE(?, quantity),
                   opened_at = COALESCE(?, opened_at, ?),
                   updated_at = ?
             WHERE execution_id = ?
            """,
            (broker_position_id, broker_order_id, actual_entry, quantity, opened_at, now, now, execution_id),
        )

    def adopt_orphan(self, *, broker_position_id: str, snapshot: Mapping[str, Any]) -> str:
        """Record a broker position the database has never seen.

        Happens after a crash between "order sent" and "intent updated",
        or when a position is opened outside the bot. It is recorded as
        ORPHANED (not silently as a normal trade) so the distinction is
        visible in analysis and on the dashboard.
        """

        now = utc_now().isoformat()
        execution_id = f"orphan-{broker_position_id}"
        existing = self.db.query_one(
            "SELECT execution_id FROM trades WHERE broker_position_id = ?", (broker_position_id,)
        )
        if existing:
            return str(existing["execution_id"])
        self.db.execute(
            """
            INSERT INTO trades (
                execution_id, broker_position_id, symbol, direction, status,
                actual_entry, stop_loss, take_profit, quantity, opened_at,
                versions, context, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'ORPHANED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                broker_position_id,
                snapshot.get("symbol") or "UNKNOWN",
                str(snapshot.get("direction") or "UNKNOWN").upper(),
                snapshot.get("entry"),
                snapshot.get("stop_loss"),
                snapshot.get("take_profit"),
                snapshot.get("quantity"),
                snapshot.get("opened_at"),
                _dumps(version_stamp()),
                _dumps({"discovered_by": "reconciler"}),
                now,
                now,
            ),
        )
        return execution_id

    def mark_closed(
        self,
        *,
        broker_position_id: str,
        exit_price: float | None,
        realized_pnl: float,
        exit_reason: str,
        closed_at: str | None = None,
        r_multiple: float | None = None,
        mfe: float | None = None,
        mae: float | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        trade = self.by_position_id(broker_position_id)
        if trade is None:
            return None
        self.db.execute(
            """
            UPDATE trades
               SET status = 'CLOSED',
                   exit_price = ?,
                   realized_pnl = ?,
                   exit_reason = ?,
                   r_multiple = COALESCE(?, r_multiple),
                   mfe = COALESCE(?, mfe),
                   mae = COALESCE(?, mae),
                   closed_at = COALESCE(?, ?),
                   updated_at = ?
             WHERE broker_position_id = ?
            """,
            (exit_price, realized_pnl, exit_reason, r_multiple, mfe, mae, closed_at, now, now, broker_position_id),
        )
        return self.by_position_id(broker_position_id)

    def update_excursions(self, broker_position_id: str, *, mfe: float, mae: float) -> None:
        self.db.execute(
            """
            UPDATE trades
               SET mfe = CASE WHEN mfe IS NULL OR ? > mfe THEN ? ELSE mfe END,
                   mae = CASE WHEN mae IS NULL OR ? < mae THEN ? ELSE mae END,
                   updated_at = ?
             WHERE broker_position_id = ?
            """,
            (mfe, mfe, mae, mae, utc_now().isoformat(), broker_position_id),
        )

    def update_protection(
        self, broker_position_id: str, *, stop_loss: float | None, take_profit: float | None
    ) -> None:
        self.db.execute(
            """
            UPDATE trades
               SET stop_loss = COALESCE(?, stop_loss),
                   take_profit = COALESCE(?, take_profit),
                   updated_at = ?
             WHERE broker_position_id = ?
            """,
            (stop_loss, take_profit, utc_now().isoformat(), broker_position_id),
        )

    def by_execution_id(self, execution_id: str) -> dict[str, Any] | None:
        return self._hydrate(
            self.db.query_one("SELECT * FROM trades WHERE execution_id = ?", (execution_id,))
        )

    def by_position_id(self, broker_position_id: str) -> dict[str, Any] | None:
        return self._hydrate(
            self.db.query_one(
                "SELECT * FROM trades WHERE broker_position_id = ?", (broker_position_id,)
            )
        )

    def open_trades(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM trades WHERE status IN ('OPEN','ORPHANED','PENDING') ORDER BY created_at DESC"
        )
        return [self._hydrate(row) for row in rows]  # type: ignore[misc]

    def closed_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?", (limit,)
        )
        return [self._hydrate(row) for row in rows]  # type: ignore[misc]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._hydrate(row) for row in rows]  # type: ignore[misc]

    @staticmethod
    def _hydrate(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["versions"] = _loads(row.get("versions"))
        row["context"] = _loads(row.get("context"))
        return row


#: Numbers inside a rejection reason are the instance, not the cause.
#: Folding them keeps "R:R 1:2.13 is below the minimum 1:4" and
#: "R:R 1:3.04 is below the minimum 1:4" as one row instead of two.
#:
#: The lookbehind matters: a digit attached to a letter or to another
#: digit of the same token is part of a NAME,
#: not a measurement. Without it "M15 has no structure" folds to "M# has
#: no structure" and silently merges M15 with M5, H1 with H4 — destroying
#: exactly the distinction the histogram exists to show.
_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?%?")


def _reason_stem(reason: str) -> str:
    """The cause a rejection reason describes, without its instance."""

    stem = _NUMBER.sub("#", reason.strip())
    return stem[:160]


class DecisionJournal:
    """Every decision — including NO TRADE — with its reason.

    This is the research substrate for MASTER_MISSION §91/§93: without a
    recorded rejection reason it is impossible to answer "what is this
    system actually filtering out, and was it right to?".
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        *,
        scan_id: str,
        symbol: str,
        stage: str,
        outcome: str,
        reason: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO decisions (scan_id, symbol, stage, outcome, reason, payload, versions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                symbol,
                stage,
                outcome,
                reason,
                _dumps(payload or {}),
                _dumps(version_stamp()),
                utc_now().isoformat(),
            ),
        )

    def recent(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self.db.query(
                "SELECT * FROM decisions WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = _loads(row.get("payload"))
        return rows

    def rejection_histogram(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = utc_now().timestamp() - days * 86400
        from datetime import datetime, timezone

        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        return self.db.query(
            """
            SELECT stage, outcome, COUNT(*) AS count
              FROM decisions
             WHERE created_at >= ?
             GROUP BY stage, outcome
             ORDER BY count DESC
            """,
            (cutoff_iso,),
        )

    def blocker_histogram(self, days: int = 7, limit: int = 15) -> list[dict[str, Any]]:
        """What actually stopped the trades, ranked.

        `rejection_histogram` groups by stage, which answers "where" but
        never "why" — and "why" is the only version anyone can act on. Two
        weeks of SMC/NO_SETUP tells you nothing; two weeks of "structural
        R:R is 1:2.1, below the required 1:4" tells you the profit floor is
        the binding constraint, not the strategy.

        Reasons carry prices and symbols, so identical causes would each
        appear once. They are folded to their stem first.
        """

        from datetime import datetime, timezone

        cutoff = datetime.fromtimestamp(
            utc_now().timestamp() - days * 86400, tz=timezone.utc
        ).isoformat()
        rows = self.db.query(
            """
            SELECT stage, reason, COUNT(*) AS count
              FROM decisions
             WHERE created_at >= ? AND outcome <> 'CANDIDATE' AND reason IS NOT NULL
             GROUP BY stage, reason
            """,
            (cutoff,),
        )

        folded: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (str(row.get("stage") or "?"), _reason_stem(str(row.get("reason") or "")))
            folded[key] = folded.get(key, 0) + int(row.get("count") or 0)

        ranked = sorted(folded.items(), key=lambda item: -item[1])[:limit]
        total = sum(folded.values())
        return [
            {
                "stage": stage,
                "reason": reason,
                "count": count,
                "share": round(count / total, 4) if total else 0.0,
            }
            for (stage, reason), count in ranked
        ]

    def latest_scan(self) -> list[dict[str, Any]]:
        row = self.db.query_one("SELECT scan_id FROM decisions ORDER BY created_at DESC LIMIT 1")
        if row is None:
            return []
        rows = self.db.query(
            "SELECT * FROM decisions WHERE scan_id = ? ORDER BY created_at ASC", (row["scan_id"],)
        )
        for item in rows:
            item["payload"] = _loads(item.get("payload"))
        return rows


class ExecutionEventLog:
    """Append-only order-lifecycle trail (MASTER_MISSION §42)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, execution_id: str, event: str, detail: Mapping[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO execution_events (execution_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (execution_id, event, _dumps(detail or {}), utc_now().isoformat()),
        )

    def for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM execution_events WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id,),
        )
        for row in rows:
            row["detail"] = _loads(row.get("detail"))
        return rows


class ReconciliationLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, kind: str, detail: Mapping[str, Any], symbol: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO reconciliations (kind, symbol, detail, created_at) VALUES (?, ?, ?, ?)",
            (kind, symbol, _dumps(detail), utc_now().isoformat()),
        )
        log_event("RECONCILE", kind, symbol=symbol, detail=dict(detail))

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM reconciliations ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["detail"] = _loads(row.get("detail"))
        return rows


class DailyStatsRepository:
    """Daily risk counters that must survive a Railway restart."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _ensure(self, day: str, start_balance: float | None = None) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM daily_stats WHERE day = ?", (day,))
        if row is not None:
            if row.get("start_balance") is None and start_balance is not None:
                self.db.execute(
                    "UPDATE daily_stats SET start_balance = ?, updated_at = ? WHERE day = ?",
                    (start_balance, utc_now().isoformat(), day),
                )
                row["start_balance"] = start_balance
            return row
        self.db.execute(
            "INSERT INTO daily_stats (day, start_balance, updated_at) VALUES (?, ?, ?)",
            (day, start_balance, utc_now().isoformat()),
        )
        return self.db.query_one("SELECT * FROM daily_stats WHERE day = ?", (day,))  # type: ignore[return-value]

    def today(self, *, now: Any = None, start_balance: float | None = None) -> dict[str, Any]:
        return self._ensure(trading_day(now or utc_now()), start_balance)

    def record_open(self, *, now: Any = None) -> None:
        day = trading_day(now or utc_now())
        self._ensure(day)
        self.db.execute(
            "UPDATE daily_stats SET trades_opened = trades_opened + 1, updated_at = ? WHERE day = ?",
            (utc_now().isoformat(), day),
        )

    def record_close(self, pnl: float, *, now: Any = None) -> None:
        """Realized PnL and the win/loss streak. The streak resets on any
        non-losing close — it is used only to REDUCE risk, never to
        increase it (no martingale, MASTER_MISSION §35)."""

        day = trading_day(now or utc_now())
        self._ensure(day)
        is_loss = pnl < 0
        self.db.execute(
            """
            UPDATE daily_stats
               SET realized_pnl = realized_pnl + ?,
                   trades_closed = trades_closed + 1,
                   wins = wins + ?,
                   losses = losses + ?,
                   consecutive_losses = CASE WHEN ? = 1 THEN consecutive_losses + 1 ELSE 0 END,
                   updated_at = ?
             WHERE day = ?
            """,
            (pnl, 1 if pnl > 0 else 0, 1 if is_loss else 0, 1 if is_loss else 0, utc_now().isoformat(), day),
        )

    def history(self, days: int = 60) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM daily_stats ORDER BY day DESC LIMIT ?", (days,))

    def consecutive_losses(self, *, now: Any = None) -> int:
        """The losing streak, walked backwards across day boundaries.

        Two rules matter and both were wrong in the first cut:

        * a day with NO closed trades does not reset the streak — a
          weekend or a quiet session is not a win;
        * a day is only "fully losing", and therefore continues the streak
          into the previous day, when its loss count equals its closed
          count. Otherwise the streak ends inside that day.

        The streak is only ever used to REDUCE risk, never to raise it.
        """

        rows = self.db.query(
            "SELECT day, consecutive_losses, trades_closed FROM daily_stats ORDER BY day DESC LIMIT 30"
        )
        streak = 0
        for row in rows:
            closed = int(row.get("trades_closed") or 0)
            if closed == 0:
                continue
            day_streak = int(row.get("consecutive_losses") or 0)
            streak += day_streak
            if day_streak < closed:
                break
        return streak


class EquityRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def snapshot(self, balance: float, equity: float) -> None:
        self.db.execute(
            "INSERT INTO equity_snapshots (balance, equity, created_at) VALUES (?, ?, ?)",
            (balance, equity, utc_now().isoformat()),
        )

    def peak_equity(self) -> float | None:
        row = self.db.query_one("SELECT MAX(equity) AS peak FROM equity_snapshots")
        return None if row is None or row["peak"] is None else float(row["peak"])

    def curve(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT balance, equity, created_at FROM equity_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    def prune(self, keep: int = 5000) -> None:
        """Bounded growth — Railway volumes are small and this table is
        the only one that grows on a timer rather than per trade."""

        self.db.execute(
            "DELETE FROM equity_snapshots WHERE id NOT IN "
            "(SELECT id FROM equity_snapshots ORDER BY created_at DESC LIMIT ?)",
            (keep,),
        )


class PaperRepository:
    """Simulated positions and account, persisted like the real thing.

    Paper state lives in the database rather than in memory for the same
    reason real state does: a redeploy must not erase an open simulated
    position, or the paper run stops being a faithful rehearsal of the live
    path.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- account ---------------------------------------------------------

    def ensure_account(self, starting_balance: float, currency: str = "USD") -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM paper_account WHERE id = 1")
        if row is not None:
            return row
        now = utc_now().isoformat()
        self.db.execute(
            "INSERT INTO paper_account (id, starting_balance, currency, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (starting_balance, currency, now, now),
        )
        log_event(
            "PAPER",
            f"paper account opened with {starting_balance:.2f} {currency}",
            starting_balance=starting_balance,
        )
        return self.db.query_one("SELECT * FROM paper_account WHERE id = 1")  # type: ignore[return-value]

    def account(self) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM paper_account WHERE id = 1")

    def apply_realized(self, pnl: float, commission: float) -> None:
        self.db.execute(
            "UPDATE paper_account SET realized_pnl = realized_pnl + ?, "
            "commission_paid = commission_paid + ?, updated_at = ? WHERE id = 1",
            (pnl, commission, utc_now().isoformat()),
        )

    def reset(self, starting_balance: float, currency: str = "USD") -> None:
        """Wipe simulated state. Only ever called explicitly by an operator."""

        with self.db.transaction() as cursor:
            cursor.execute(self.db._rewrite("DELETE FROM paper_positions"))
            cursor.execute(self.db._rewrite("DELETE FROM paper_account"))
        self.ensure_account(starting_balance, currency)
        log_event("PAPER", "paper state reset", severity="warning")

    # -- positions -------------------------------------------------------

    def open_position(self, position: Mapping[str, Any]) -> None:
        now = utc_now().isoformat()
        self.db.execute(
            """
            INSERT INTO paper_positions (
                position_id, execution_id, symbol, direction, quantity, entry_price,
                stop_loss, take_profit, contract_size, conversion_rate, commission,
                status, mark_price, opened_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
            """,
            (
                position["position_id"],
                position.get("execution_id"),
                position["symbol"],
                position["direction"],
                position["quantity"],
                position["entry_price"],
                position.get("stop_loss"),
                position.get("take_profit"),
                position["contract_size"],
                position.get("conversion_rate", 1.0),
                position.get("commission", 0.0),
                position["entry_price"],
                position.get("opened_at") or now,
                now,
            ),
        )

    def open_positions(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY opened_at ASC"
        )

    def by_id(self, position_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)
        )

    def update_protection(
        self, position_id: str, *, stop_loss: float | None, take_profit: float | None
    ) -> None:
        self.db.execute(
            "UPDATE paper_positions SET stop_loss = COALESCE(?, stop_loss), "
            "take_profit = COALESCE(?, take_profit), updated_at = ? WHERE position_id = ?",
            (stop_loss, take_profit, utc_now().isoformat(), position_id),
        )

    def update_mark(self, position_id: str, mark_price: float) -> None:
        self.db.execute(
            "UPDATE paper_positions SET mark_price = ?, updated_at = ? WHERE position_id = ?",
            (mark_price, utc_now().isoformat(), position_id),
        )

    def reduce_quantity(self, position_id: str, remaining: float) -> None:
        self.db.execute(
            "UPDATE paper_positions SET quantity = ?, updated_at = ? WHERE position_id = ?",
            (remaining, utc_now().isoformat(), position_id),
        )

    def close_position(
        self,
        position_id: str,
        *,
        exit_price: float,
        realized_pnl: float,
        exit_reason: str,
    ) -> None:
        now = utc_now().isoformat()
        self.db.execute(
            """
            UPDATE paper_positions
               SET status = 'CLOSED', exit_price = ?, realized_pnl = ?, exit_reason = ?,
                   closed_at = ?, updated_at = ?
             WHERE position_id = ?
            """,
            (exit_price, realized_pnl, exit_reason, now, now, position_id),
        )

    def closed_positions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM paper_positions WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )


class Repositories:
    """Bundle handed to every component that needs persistence."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.state = StateRepository(db)
        self.intents = IntentRepository(db)
        self.trades = TradeRepository(db)
        self.journal = DecisionJournal(db)
        self.events = ExecutionEventLog(db)
        self.reconciliations = ReconciliationLog(db)
        self.daily = DailyStatsRepository(db)
        self.equity = EquityRepository(db)
        self.paper = PaperRepository(db)

    @property
    def healthy(self) -> bool:
        return self.db.healthy
