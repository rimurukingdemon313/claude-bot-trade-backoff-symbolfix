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

from typing import Any

from .analytics.performance import breakdown, compute_performance
from .clock import utc_now
from .config import TradingConfig, profit_floor_feasibility
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

    def account(self) -> dict[str, Any]:
        try:
            state = self.orchestrator.broker.account_state()
        except BotError as exc:
            return self._offline(exc)

        daily = self.repos.daily.today()
        peak = self.repos.equity.peak_equity() or state.equity
        drawdown = max(0.0, (peak - state.equity) / peak) if peak else 0.0
        closed = self.repos.trades.closed_trades(limit=500)
        total_pnl = sum(float(trade.get("realized_pnl") or 0.0) for trade in closed)
        demo = self.orchestrator.last_demo

        return {
            "status": "LIVE",
            "data": {
                **state.as_dict(),
                "dailyRealizedPnl": round(float(daily.get("realized_pnl") or 0.0), 2),
                "dailyPnl": round(float(daily.get("realized_pnl") or 0.0) + state.open_pnl, 2),
                "totalPnl": round(total_pnl, 2),
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
        try:
            positions = self.orchestrator.broker.positions()
        except BotError as exc:
            return {**self._offline(exc), "data": []}
        return {"status": "LIVE", "data": self.orchestrator.manager.track(positions)}

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
        }

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
            "news", lambda: self.orchestrator.news.upcoming(self.config.symbols)
        )
        return health

    def risk_state(self) -> dict[str, Any]:
        try:
            state = self.orchestrator.build_account_state()
        except BotError as exc:
            return self._offline(exc)
        limits = self.config.risk
        return {
            "status": "LIVE",
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
                "opportunityTarget": self.config.opportunity.target_profit,
                "opportunityMinimum": self.config.opportunity.minimum_profit,
                "profitObjective": profit_floor_feasibility(self.config, state.equity),
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
            "generatedAt": utc_now().isoformat(),
        }

    def setup_status(self) -> dict[str, Any]:
        """Which configuration is present, and what to do next.

        Answers before credentials exist — it is the screen to read when
        nothing else works yet. Never includes a value, only presence.
        """

        from .setup_status import build_setup_report

        equity: float | None = None
        try:
            equity = self.orchestrator.broker.account_state().equity
        except Exception:  # noqa: BLE001 - unconfigured is the normal case here
            equity = None
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
