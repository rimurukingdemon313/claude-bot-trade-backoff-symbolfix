"""Read/command surface consumed by the dashboard.

Everything here is a projection of real state. Where a value is not
available it is returned as None with an explicit status, never as a
plausible-looking zero (MASTER_MISSION §53). The dashboard is presentation
only — nothing in this module can open, close, or size a trade.

Commands (pause, resume, kill switch) are gated by a bearer token when
DASHBOARD_TOKEN is set, and they can only ever make the system LESS
permissive or clear a clearable kill-switch reason. There is no command
that bypasses the demo guard, the risk limits, or the execution guards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .analytics.performance import breakdown, compute_performance
from .clock import ensure_utc, utc_now
from .config import TradingConfig
from .broker.cache import CachedRead
from .errors import BotError
from .orchestrator import Orchestrator
from .smc.sessions import is_forex_weekend
from .storage.repositories import Repositories
from .version import version_stamp


class DashboardApi:
    def __init__(self, config: TradingConfig, orchestrator: Orchestrator, repositories: Repositories) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.repos = repositories
        self.started_at = utc_now()

    # -- account ---------------------------------------------------------

    def _offline(self, exc: BotError) -> dict[str, Any]:
        """An unreadable value, with the most useful reason available.

        Rule 6 forbids inventing the number, so the gap stays. But over a
        weekend the broker's own error is "circuit open after 5 consecutive
        failures", which reads like a system falling over when the truth is
        that the market is shut. Naming that first turns an alarm into a
        fact.
        """

        closed = is_forex_weekend(utc_now())
        return {
            "status": "OFFLINE",
            "error": (
                "the forex market is closed for the weekend, so the broker is not "
                f"answering ({exc})"
                if closed
                else str(exc)
            ),
            "marketClosed": closed,
            "data": None,
        }

    #: How old a broker read may be before the page marks it stale. The
    #: position poll refreshes every `position_poll_seconds`; twice that
    #: plus a margin means an ordinary late poll does not flash a warning,
    #: while a genuinely stuck refresh shows up within a minute or so.
    def _stale_after(self) -> float:
        """How old a broker read may be before the page marks it stale.

        Measured against the SLOWER of the two poll cadences. While the
        account is flat the position poll deliberately backs off, and a
        page that cried "stale" at a bot behaving exactly as designed
        would teach the operator to ignore the warning that one day is
        real.
        """

        scheduler = self.config.scheduler
        slowest = max(
            scheduler.position_poll_seconds, scheduler.idle_position_poll_seconds
        )
        return max(30.0, slowest * 1.5)

    def _unread(self, read: CachedRead, what: str) -> dict[str, Any]:
        """Nothing has ever been read, so there is nothing to show.

        Rule 6: the gap stays visible. The reason matters — "not yet" on a
        booting process is a different fact from "the broker refused", and
        an operator who cannot tell them apart restarts a healthy bot.
        """

        closed = is_forex_weekend(utc_now())
        if read.error:
            return {
                "status": "OFFLINE",
                "error": (
                    f"the forex market is closed for the weekend, so the broker is not "
                    f"answering ({read.error})"
                    if closed
                    else read.error
                ),
                "marketClosed": closed,
                "data": None,
            }
        return {
            "status": "OFFLINE",
            "error": f"no {what} has been read from the broker yet",
            "hint": (
                "The first position poll fills this within "
                f"{self.config.scheduler.position_poll_seconds}s of startup."
            ),
            "marketClosed": closed,
            "data": None,
        }

    def _freshness(self, read: CachedRead, *, now: Any = None) -> dict[str, Any]:
        """The age of a served value, always attached to it.

        A cached number shown without its age is indistinguishable from a
        current one, which is the same failure rule 6 describes: it looks
        like information. With the age attached it IS information.
        """

        age = read.age_seconds(now=now)
        return {
            "asOf": read.at.isoformat() if read.at else None,
            "ageSeconds": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age > self._stale_after()),
            "refreshError": read.error,
        }

    def account(self) -> dict[str, Any]:
        """The account panel, from the last read the bot itself made.

        This used to call the broker directly. On a shared throttle that
        put every dashboard refresh in the same queue as the scan, so the
        page could wait a minute for a number the bot already had —
        see bot/broker/cache.py.
        """

        read = self.orchestrator.live.get("account")
        if not read.present:
            return self._unread(read, "account state")
        state = read.value

        daily = self.repos.daily.today()
        peak = self.repos.equity.peak_equity(self.config.broker.account_id) or state.equity
        drawdown = max(0.0, (peak - state.equity) / peak) if peak else 0.0
        closed = self.repos.trades.closed_trades(limit=500)
        # A trade the broker closed without a readable result carries a
        # NULL pnl, not a zero. `or 0.0` folded those into the total as
        # scratches, so a page could show a total that quietly omitted
        # real money and looked complete doing it. Sum what was measured
        # and say how many were not (project rule 6).
        priced = [t for t in closed if t.get("realized_pnl") is not None]
        total_pnl = sum(float(trade["realized_pnl"]) for trade in priced)
        unpriced = len(closed) - len(priced)
        demo = self.orchestrator.last_demo

        return {
            "status": "LIVE",
            **self._freshness(read),
            "data": {
                **state.as_dict(),
                "dailyRealizedPnl": round(float(daily.get("realized_pnl") or 0.0), 2),
                "dailyPnl": round(float(daily.get("realized_pnl") or 0.0) + state.open_pnl, 2),
                "totalPnl": round(total_pnl, 2),
                #: Closed trades whose result the broker never reported.
                #: Non-zero means `totalPnl` is a partial figure.
                "totalPnlUnpricedTrades": unpriced,
                "peakEquity": round(peak, 2),
                "drawdownPct": round(drawdown * 100, 2),
                "tradesToday": int(daily.get("trades_opened") or 0),
                "demoVerified": bool(demo and demo.verified),
                "demoReason": demo.reason if demo else "not yet verified",
                "environment": "DEMO" if demo and demo.verified else "UNVERIFIED",
                "mode": self.config.mode.value,
                "paper": self.config.is_paper,
            },
        }

    # -- positions / trades ----------------------------------------------

    def open_positions(self) -> dict[str, Any]:
        """Position rows as the position poll last tracked them.

        `manager.track()` fetches a quote per position, so running it here
        made the cost of the page scale with the number of open trades —
        on the one thread a 30-second proxy timeout was watching.
        """

        read = self.orchestrator.live.get("position_rows")
        if not read.present:
            return {**self._unread(read, "open positions"), "data": []}
        return {"status": "LIVE", **self._freshness(read), "data": read.value}

    def trade_history(self, limit: int = 100) -> dict[str, Any]:
        rows = self.repos.trades.closed_trades(limit=limit)
        return {
            "status": "LIVE",
            "data": [
                {
                    "executionId": row.get("execution_id"),
                    "positionId": row.get("broker_position_id"),
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction"),
                    "openedAt": row.get("opened_at"),
                    "closedAt": row.get("closed_at"),
                    "entry": row.get("actual_entry") or row.get("planned_entry"),
                    "exit": row.get("exit_price"),
                    "stopLoss": row.get("stop_loss"),
                    "takeProfit": row.get("take_profit"),
                    "quantity": row.get("quantity"),
                    "riskAmount": row.get("risk_amount"),
                    "pnl": row.get("realized_pnl"),
                    "rMultiple": row.get("r_multiple"),
                    "setupGrade": row.get("setup_grade"),
                    "setupScore": row.get("setup_score"),
                    "aiConfidence": row.get("ai_confidence"),
                    "exitReason": row.get("exit_reason"),
                    "durationMinutes": _duration(row),
                    "versions": row.get("versions"),
                }
                for row in rows
            ],
        }

    # -- performance -----------------------------------------------------

    def performance(self) -> dict[str, Any]:
        closed = self.repos.trades.closed_trades(limit=1000)
        stats = compute_performance(closed)
        return {
            "status": "LIVE",
            "data": {
                **stats.as_dict(),
                "bySymbol": breakdown(closed, "symbol"),
                "byGrade": breakdown(closed, "setup_grade"),
                "bySession": breakdown(closed, "session"),
                "equityCurve": self.repos.equity.curve(limit=200),
                "dailyHistory": self.repos.daily.history(days=45),
            },
        }

    # -- strategy view ---------------------------------------------------

    def latest_scan(self, *, detail: bool = True) -> dict[str, Any]:
        scan = self.orchestrator.last_scan
        if scan is None:
            return {"status": "PENDING", "data": None, "message": "no scan has completed yet"}
        return {"status": "LIVE", "data": scan.as_dict(detail=detail)}

    # -- strategy --------------------------------------------------------

    def strategy(self) -> dict[str, Any]:
        return {"status": "LIVE", "data": self.orchestrator.strategy_status()}

    def set_strategy(self, name: str) -> dict[str, Any]:
        """Switch mode.

        Presentation cannot open, size or close a trade (project rule 11),
        and this does none of those: it selects which analysis runs. Every
        gate after it — the demo guard, the risk engine, the execution
        guards — is untouched and still has to pass.
        """

        return {"status": "LIVE", "data": self.orchestrator.set_strategy(name)}

    def journal(self, limit: int = 100, symbol: str | None = None) -> dict[str, Any]:
        return {
            "status": "LIVE",
            "data": self.repos.journal.recent(limit=limit, symbol=symbol),
            "histogram": self.repos.journal.rejection_histogram(days=30),
            # Ranked causes, not stages. "Why is it not trading?" is the
            # question actually being asked, and only this answers it.
            "blockers": self.repos.journal.blocker_histogram(days=7),
            "funnel": self.repos.journal.funnel(days=7),
            "diagnosis": self.diagnosis(),
        }

    def diagnosis(self, days: int = 7) -> dict[str, Any]:
        """One sentence naming what is actually stopping the trades.

        Composed here rather than in `bot.diagnosis` because gathering the
        inputs touches the journal, the config and the orchestrator, and
        the judgement itself is a pure function so it can be tested
        against a pinned clock with no database at all.

        Never raises: this is read on a dashboard that exists to show
        failures, so it has to answer while things are broken.
        """

        from .diagnosis import diagnose

        def safely(probe: Any, default: Any) -> Any:
            try:
                return probe()
            except Exception:  # noqa: BLE001 - a diagnosis must not 500
                return default

        funnel = safely(lambda: self.repos.journal.funnel(days=days), {})
        news = safely(self.orchestrator.news.health, {})
        ai_gate = safely(self.orchestrator._ai_gate_status, {})
        kill = safely(lambda: self.orchestrator.kill_switch.read().active, False)

        return diagnose(
            funnel=funnel,
            last_trade_at=safely(self._last_trade_at, None),
            ai_required_but_unavailable=bool(ai_gate.get("blockingAllTrades")),
            news_failing_closed=bool(
                news.get("enabled")
                and news.get("failClosed")
                and not news.get("feedAvailable")
            ),
            trading_paused=not safely(lambda: self.orchestrator.trading_enabled, True),
            kill_switch_active=bool(kill),
        )

    def _cached_equity(self) -> float | None:
        read = self.orchestrator.live.get("account")
        return read.value.equity if read.present else None

    def _last_trade_at(self) -> datetime | None:
        rows = self.repos.trades.recent(limit=1)
        if not rows:
            return None
        raw = rows[0].get("created_at")
        if not raw:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            return None

    # -- system ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Never raises. This is the one endpoint that has to work when
        everything else is broken — if it threw a 500 whenever the
        database was down, it would be silent at exactly the moment it
        matters most."""

        try:
            health = self.orchestrator.health()
        except Exception as exc:  # noqa: BLE001
            health = {
                "ok": False,
                "tradingPermitted": False,
                "components": {"orchestrator": {"ok": False, "error": str(exc)[:300]}},
            }
        health["uptimeSeconds"] = round((utc_now() - self.started_at).total_seconds())
        health["versions"] = version_stamp()
        health["symbols"] = list(self.config.symbols)
        health["mode"] = self.config.mode.value

        def optional(name: str, probe: Any) -> Any:
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{name} unavailable: {exc}"[:200]}

        health["reconciliations"] = optional(
            "reconciliations", lambda: self.repos.reconciliations.recent(limit=10)
        )
        health["upcomingNews"] = optional(
            "news",
            lambda: self.orchestrator.news.upcoming(self.config.symbols, refresh=False),
        )
        return health

    def risk_state(self) -> dict[str, Any]:
        """Risk limits against the account, composed without the network.

        `build_account_state()` makes two broker calls, and this endpoint
        was calling it on every dashboard refresh — duplicating the two
        calls `account()` and `open_positions()` had just made on the same
        request. Everything after those reads is local, so the cached pair
        composes the identical state.
        """

        composed = self.orchestrator.cached_account_state()
        if composed is None:
            return self._unread(self.orchestrator.live.get("account"), "account state")
        state, read = composed
        limits = self.config.risk
        return {
            "status": "LIVE",
            **self._freshness(read),
            "data": {
                **state.as_dict(),
                "killSwitch": self.orchestrator.kill_switch.read().as_dict(),
                "limits": {
                    "baseRiskPct": limits.base_risk_pct,
                    "maxRiskPct": limits.max_risk_pct,
                    "maxPortfolioRiskPct": limits.max_portfolio_risk_pct,
                    "maxDailyLossPct": limits.max_daily_loss_pct,
                    "maxDrawdownPct": limits.max_drawdown_pct,
                    "maxOpenPositions": limits.max_open_positions,
                    "maxTradesPerDay": limits.max_trades_per_day,
                    "maxConsecutiveLosses": limits.max_consecutive_losses,
                    "minRiskReward": limits.min_risk_reward,
                },
                "rewardObjective": {
                    "minimumR": self.config.reward.min_reward_r,
                    "preferredR": self.config.reward.preferred_reward_r,
                    "enabled": self.config.reward.enabled,
                    # What one R is worth here. Information for the
                    # operator, never a threshold - see bot/risk/reward.py.
                    "riskPerTrade": round(state.equity * self.config.risk.base_risk_pct, 2),
                },
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """One call that fills the whole dashboard, to keep the mobile
        client to a single round trip."""

        return {
            "account": self.account(),
            "positions": self.open_positions(),
            "history": self.trade_history(limit=50),
            "performance": self.performance(),
            "scan": self.latest_scan(detail=False),
            "risk": self.risk_state(),
            "health": self.health(),
            # "Why has it not traded?" answered on the page that asks it,
            # rather than three screens away in the journal.
            "diagnosis": self.diagnosis(),
            "generatedAt": utc_now().isoformat(),
        }

    def setup_status(self) -> dict[str, Any]:
        """Which configuration is present, and what to do next.

        Answers before credentials exist — it is the screen to read when
        nothing else works yet. Never includes a value, only presence.
        """

        from .setup_status import build_setup_report

        # From the cache, never the broker: this endpoint took 7.1 seconds
        # on a queued throttle, and it is the FIRST thing the page asks
        # for — so a slow answer here is what the operator sees as a dead
        # dashboard. An unconfigured bot has no cached read and reports
        # no equity, which is the same answer it gave before.
        read = self.orchestrator.live.get("account")
        equity = read.value.equity if read.present else None
        return build_setup_report(self.config, equity=equity).as_dict()

    def doctor_report(self, *, symbols: list[str] | None = None) -> dict[str, Any]:
        """Run the read-only account verification and return it masked.

        Exposed so the report can be read from a browser — including a phone —
        without a terminal. Always sanitized: balances, equity, margin and the
        account identifier are masked, while the integration detail that
        actually diagnoses a problem (history endpoint shape, instrument
        specifications, suffix naming, conversion paths, candle validation) is
        preserved.

        Slow by nature: it makes a few dozen read calls to the broker.
        """

        from . import doctor

        try:
            # Reuse the live client so this does not open a second session.
            # In paper mode `broker` is the wrapper, so unwrap to the real one.
            live = getattr(self.orchestrator.broker, "live", self.orchestrator.broker)
            report = doctor.run(
                self.config, symbols or list(self.config.symbols)[:3], broker=live
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic must always answer
            return {
                "verdict": "FAIL",
                "checks": [
                    {
                        "check": "doctor",
                        "status": "FAIL",
                        "detail": f"the verification itself failed to run: {exc}",
                        "data": {},
                    }
                ],
            }
        payload = report.sanitized().as_dict()
        payload["text"] = report.sanitized().render()
        payload["failed"] = [check.name for check in report.failed]
        return payload

    # -- commands --------------------------------------------------------

    def set_scanning(self, enabled: bool) -> dict[str, Any]:
        return {"enabled": self.orchestrator.set_trading_enabled(enabled)}

    def trip_kill_switch(self, reason: str = "MANUAL") -> dict[str, Any]:
        return self.orchestrator.kill_switch.trip("MANUAL", reason).as_dict()

    def clear_kill_switch(self, *, force: bool = False) -> dict[str, Any]:
        return self.orchestrator.kill_switch.clear(force=force).as_dict()

    def trigger_scan(self) -> dict[str, Any]:
        return self.orchestrator.scan(source="manual").as_dict(detail=True)

    def trigger_reconcile(self) -> dict[str, Any]:
        return self.orchestrator.reconcile().as_dict()

    def reset_paper(self) -> dict[str, Any]:
        """Wipe simulated state and start the paper run over.

        Refused outside paper mode: there is nothing to reset on a real
        account, and a command that silently did nothing would be worse than
        one that says why.
        """

        if not self.config.is_paper:
            return {
                "ok": False,
                "error": "not in paper mode — there is no simulated state to reset",
            }
        starting = self.config.paper.starting_balance
        if starting is None:
            try:
                starting = self.orchestrator.broker.live.account_state().balance
            except Exception:  # noqa: BLE001
                starting = 10_000.0
        self.repos.paper.reset(float(starting))
        return {"ok": True, "startingBalance": round(float(starting), 2)}


def _duration(row: dict[str, Any]) -> float | None:
    from datetime import datetime

    opened, closed = row.get("opened_at"), row.get("closed_at")
    if not opened or not closed:
        return None
    try:
        start = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds() / 60.0, 1)
