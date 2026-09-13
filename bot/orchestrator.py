"""The scan cycle. Wires every component into one deterministic pipeline.

    DEMO -> ACCOUNT -> RISK STATE -> per symbol:
        NEWS -> DATA -> SMC -> SCORE -> RISK -> AI -> CANDIDATE
    -> RANK -> EXECUTE BEST -> JOURNAL

Two properties this file is responsible for:

1. AI is consulted LAST, and only for candidates that already passed
   every deterministic gate. That is both the cost control and the
   guarantee that a model can only ever subtract trades.
2. Only the single best opportunity is executed per cycle (§39). Ranking
   across symbols and taking one is what keeps the system selective
   instead of opening five mediocre positions because five symbols
   happened to look acceptable at once.

Every rejection is journalled with its stage and reason, which is what
makes §93's "which filter is actually costing us money?" answerable.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .ai.client import AIClient
from .ai.validator import AIValidation, validate_ai_decision
from .broker.models import InstrumentSpec
from .clock import trading_day, utc_now
from .config import TradingConfig, profit_floor_feasibility
from .errors import AIError, BotError, DemoVerificationError, MarketDataError
from .execution.executor import ExecutionResult, Executor
from .execution.manager import PositionManager, plan_actions
from .execution.plan import build_plan
from .execution.reconciler import ReconcileReport, Reconciler
from .marketdata.provider import MarketDataProvider
from .news import NewsFilter
from .observability import log_event, new_event_id
from .risk.engine import AccountRiskState, RiskDecision, RiskEngine
from .safety.demo_guard import DemoVerification, verify_demo
from .safety.kill_switch import KillSwitch
from .scoring.scorer import SetupScore, SetupScorer, tier_rank
from .smc.engine import SetupCandidate, SmcEngine, SmcResult
from .smc.sessions import classify_session
from .storage.repositories import Repositories

STATE_TRADING_ENABLED = "trading_enabled"
STATE_LAST_SCAN = "last_scan"
STATE_SESSION_COUNTS = "session_trade_counts"


@dataclass
class SymbolOutcome:
    symbol: str
    stage: str
    outcome: str
    reason: str | None = None
    candidate: SetupCandidate | None = None
    score: SetupScore | None = None
    risk: RiskDecision | None = None
    ai: AIValidation | None = None
    spec: InstrumentSpec | None = None
    atr: float = 0.0
    smc: SmcResult | None = None

    @property
    def executable(self) -> bool:
        return self.outcome == "CANDIDATE" and self.candidate is not None and self.risk is not None

    def as_dict(self, *, detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "stage": self.stage,
            "outcome": self.outcome,
            "reason": self.reason,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "score": self.score.as_dict() if self.score else None,
            "risk": self.risk.as_dict() if self.risk else None,
            "ai": self.ai.as_dict() if self.ai else None,
        }
        if detail and self.smc is not None:
            payload["smc"] = self.smc.as_dict()
        return payload


@dataclass
class ScanResult:
    scan_id: str
    started_at: str
    finished_at: str | None = None
    demo: DemoVerification | None = None
    account: dict[str, Any] = field(default_factory=dict)
    outcomes: list[SymbolOutcome] = field(default_factory=list)
    executed: ExecutionResult | None = None
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self, *, detail: bool = False) -> dict[str, Any]:
        return {
            "scanId": self.scan_id,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "demo": self.demo.as_dict() if self.demo else None,
            "account": self.account,
            "symbols": [outcome.as_dict(detail=detail) for outcome in self.outcomes],
            "executed": self.executed.as_dict() if self.executed else None,
            "skippedReason": self.skipped_reason,
            "errors": self.errors,
            "decision": (
                f"{self.executed.plan.direction} {self.executed.plan.symbol}"
                if self.executed and self.executed.ok
                else "NO TRADE"
            ),
        }


class Orchestrator:
    def __init__(
        self,
        config: TradingConfig,
        *,
        broker: Any,
        repositories: Repositories,
        market_data: MarketDataProvider,
        smc: SmcEngine | None = None,
        scorer: SetupScorer | None = None,
        risk: RiskEngine | None = None,
        ai: AIClient | None = None,
        news: NewsFilter | None = None,
        executor: Executor | None = None,
        reconciler: Reconciler | None = None,
        manager: PositionManager | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.repos = repositories
        self.market_data = market_data
        self.kill_switch = KillSwitch(repositories.state)
        self.smc = smc or SmcEngine(config)
        self.scorer = scorer or SetupScorer(config)
        self.risk = risk or RiskEngine(config, self.kill_switch)
        self.ai = ai or AIClient(config.ai)
        self.news = news or NewsFilter(config.news)
        self.executor = executor or Executor(config, broker, repositories)
        self.reconciler = reconciler or Reconciler(config, broker, repositories)
        self.manager = manager or PositionManager(config, broker, repositories)

        self._scan_lock = threading.Lock()
        self._scanning = False
        self.last_scan: ScanResult | None = None
        self.last_reconcile: ReconcileReport | None = None
        self.last_demo: DemoVerification | None = None
        self.startup_complete = False
        self.startup_error: str | None = None
        self.feasibility: dict[str, Any] = {}

    # -- state -----------------------------------------------------------

    @property
    def trading_enabled(self) -> bool:
        value = self.repos.state.get(STATE_TRADING_ENABLED)
        if value is None:
            return self.config.trading_enabled_default
        return bool(value)

    def set_trading_enabled(self, enabled: bool, *, actor: str = "dashboard") -> bool:
        self.repos.state.set(STATE_TRADING_ENABLED, bool(enabled))
        log_event("CONTROL", f"scanning {'enabled' if enabled else 'paused'}", actor=actor)
        return self.trading_enabled

    # -- startup ---------------------------------------------------------

    def startup(self) -> dict[str, Any]:
        """The mandatory boot sequence (MASTER_MISSION §8).

        VERIFY DEMO -> CONNECT BROKER -> CONNECT DB -> READ BROKER STATE
        -> RECONCILE -> only then allow new trades.

        `startup_complete` gates scanning, so a failed reconcile means no
        new orders rather than trading on top of an unknown position.
        """

        self.startup_error = None
        self.startup_complete = False
        try:
            self.broker.ensure_session()
        except BotError as exc:
            self.startup_error = f"broker connection failed: {exc}"
            log_event("STARTUP", self.startup_error, severity="critical")
            return {"ok": False, "error": self.startup_error}

        verification = verify_demo(
            self.config, self.broker.account_metadata, stage="startup"
        )
        self.last_demo = verification
        if not verification.verified:
            self.startup_error = f"DEMO verification failed: {verification.reason}"
            self.kill_switch.trip("ENVIRONMENT_MISMATCH", verification.reason or "")
            return {"ok": False, "error": self.startup_error, "demo": verification.as_dict()}

        if not self.repos.db.ping():
            self.startup_error = "database is unreachable"
            log_event("STARTUP", self.startup_error, severity="critical")
            return {"ok": False, "error": self.startup_error}

        try:
            account = self.broker.account_state()
            self.repos.equity.snapshot(account.balance, account.equity)
            self.repos.daily.today(start_balance=account.balance)
        except BotError as exc:
            self.startup_error = f"could not read broker account state: {exc}"
            return {"ok": False, "error": self.startup_error}

        report = self.reconciler.reconcile()
        self.last_reconcile = report
        if not report.clean:
            log_event(
                "STARTUP",
                "reconciliation reported discrepancies; review before trusting local state",
                severity="warning",
                report=report.as_dict(),
            )

        # A profit floor that cannot be reached at this equity would make the
        # bot silently never trade. Say so at boot instead.
        self.feasibility = profit_floor_feasibility(self.config, account.equity)
        if not self.feasibility.get("feasible"):
            log_event(
                "STARTUP",
                f"profit objective is UNREACHABLE at current equity: {self.feasibility['reason']}",
                severity="error",
                **{k: v for k, v in self.feasibility.items() if k != "reason"},
            )

        self.startup_complete = True
        log_event(
            "STARTUP",
            "startup sequence complete — trading permitted",
            mode=self.config.mode.value,
            demo=verification.verified,
            positions=report.checked_positions,
            adopted=len(report.adopted_orphans),
            profit_floor_feasible=self.feasibility.get("feasible"),
        )
        return {
            "ok": True,
            "demo": verification.as_dict(),
            "account": account.as_dict(),
            "reconcile": report.as_dict(),
        }

    # -- account risk state ----------------------------------------------

    def build_account_state(self, *, now: datetime | None = None) -> AccountRiskState:
        moment = now or utc_now()
        account = self.broker.account_state()
        positions = self.broker.positions()

        self.repos.equity.snapshot(account.balance, account.equity)
        peak = self.repos.equity.peak_equity() or account.equity
        daily = self.repos.daily.today(now=moment, start_balance=account.balance)

        open_rows = []
        for position in positions:
            trade = self.repos.trades.by_position_id(position.position_id)
            open_rows.append(
                {
                    "symbol": position.symbol,
                    "direction": position.direction,
                    "risk_amount": (trade or {}).get("risk_amount")
                    or self._implied_risk(position),
                }
            )

        session_counts = self.repos.state.get(STATE_SESSION_COUNTS) or {}
        session_key = f"{trading_day(moment)}:{classify_session(moment, self.config.sessions).name}"

        last_loss = self._last_event_time("loss")
        last_failure = self._last_event_time("execution_failure")

        return AccountRiskState(
            balance=account.balance,
            equity=account.equity,
            available_margin=account.margin_available,
            peak_equity=max(peak, account.equity),
            daily_realized_pnl=float(daily.get("realized_pnl") or 0.0),
            open_pnl=account.open_pnl,
            trades_today=int(daily.get("trades_opened") or 0),
            trades_this_session=int(session_counts.get(session_key, 0)),
            consecutive_losses=self.repos.daily.consecutive_losses(now=moment),
            open_positions=open_rows,
            last_loss_at=last_loss,
            last_execution_failure_at=last_failure,
        )

    def _implied_risk(self, position: Any) -> float:
        """Risk for a position the database has no plan for (an orphan).

        Estimated from its actual stop distance — conservative, and far
        better than counting it as zero risk in the portfolio limits.
        """

        if not position.stop_loss or not position.entry_price:
            return 0.0
        try:
            spec = self.broker.instrument(position.symbol)
        except BotError:
            return 0.0
        distance = abs(position.entry_price - position.stop_loss)
        return distance * spec.contract_size * position.quantity

    def _last_event_time(self, kind: str) -> datetime | None:
        from datetime import datetime as _dt

        raw = self.repos.state.get(f"last_{kind}_at")
        if not raw:
            return None
        try:
            from .clock import ensure_utc

            return ensure_utc(_dt.fromisoformat(str(raw)))
        except ValueError:
            return None

    def _record_event_time(self, kind: str, moment: datetime | None = None) -> None:
        self.repos.state.set(f"last_{kind}_at", (moment or utc_now()).isoformat())

    # -- the scan --------------------------------------------------------

    def scan(self, *, source: str = "scheduled", now: datetime | None = None) -> ScanResult:
        """One full cycle. Never runs concurrently with itself (§60)."""

        if not self._scan_lock.acquire(blocking=False):
            existing = self.last_scan
            log_event("SCAN", "scan already in progress; skipping this trigger", source=source)
            return existing or ScanResult(
                scan_id="skipped",
                started_at=utc_now().isoformat(),
                skipped_reason="a scan is already running",
            )
        try:
            self._scanning = True
            return self._scan(source=source, now=now)
        finally:
            self._scanning = False
            self._scan_lock.release()

    def _scan(self, *, source: str, now: datetime | None) -> ScanResult:
        moment = now or utc_now()
        scan_id = uuid.uuid4().hex[:12]
        result = ScanResult(scan_id=scan_id, started_at=moment.isoformat())
        log_event("SCAN", "scan started", event_id=scan_id, source=source)

        # --- gates that stop the whole cycle ---
        if not self.startup_complete:
            result.skipped_reason = (
                self.startup_error or "startup sequence has not completed; no trading permitted"
            )
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            return result

        if not self.trading_enabled:
            result.skipped_reason = "scanning is paused by the operator"
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            return result

        try:
            verification = verify_demo(
                self.config, self.broker.account_metadata, stage="scan"
            )
            self.last_demo = verification
            result.demo = verification
            if not verification.verified:
                self.kill_switch.trip("ENVIRONMENT_MISMATCH", verification.reason or "")
                result.skipped_reason = f"DEMO verification failed: {verification.reason}"
                result.finished_at = utc_now().isoformat()
                self.last_scan = result
                return result
        except DemoVerificationError as exc:
            result.skipped_reason = str(exc)
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            return result

        try:
            account_state = self.build_account_state(now=moment)
        except BotError as exc:
            result.errors.append(f"could not build account state: {exc}")
            result.skipped_reason = "broker account state unavailable"
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            return result

        result.account = account_state.as_dict()

        trip_reason = self.risk.evaluate_kill_switch(account_state)
        if trip_reason:
            self.kill_switch.trip(trip_reason, f"auto-tripped during scan {scan_id}")

        kill = self.kill_switch.read()
        if kill.active:
            result.skipped_reason = f"kill switch active: {kill.reason}"
            self.repos.journal.record(
                scan_id=scan_id, symbol="*", stage="RISK", outcome="KILL_SWITCH", reason=kill.reason
            )
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            return result

        # --- per-symbol evaluation ---
        for symbol in self.config.symbols:
            try:
                outcome = self._evaluate_symbol(
                    symbol, account_state, scan_id=scan_id, now=moment
                )
            except BotError as exc:
                outcome = SymbolOutcome(
                    symbol=symbol, stage="ERROR", outcome="ERROR", reason=str(exc)
                )
                result.errors.append(f"{symbol}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the scan
                outcome = SymbolOutcome(
                    symbol=symbol, stage="ERROR", outcome="ERROR", reason=f"unexpected: {exc}"
                )
                result.errors.append(f"{symbol}: unexpected {type(exc).__name__}: {exc}")
                log_event(
                    "SCAN", f"unexpected failure on {symbol}: {exc}", severity="error", symbol=symbol
                )
            result.outcomes.append(outcome)
            self.repos.journal.record(
                scan_id=scan_id,
                symbol=symbol,
                stage=outcome.stage,
                outcome=outcome.outcome,
                reason=outcome.reason,
                payload=outcome.as_dict(),
            )

        # --- rank and execute the single best opportunity ---
        executable = [outcome for outcome in result.outcomes if outcome.executable]
        if not executable:
            result.skipped_reason = "no symbol produced an executable candidate"
        else:
            best = self._rank(executable)[0]
            result.executed = self._execute(best, scan_id=scan_id)
            if result.executed is not None and result.executed.ok:
                self._bump_session_count(moment)
            elif result.executed is not None:
                self._record_event_time("execution_failure")

        result.finished_at = utc_now().isoformat()
        self.last_scan = result
        self.repos.state.set(
            STATE_LAST_SCAN,
            {
                "scanId": scan_id,
                "at": result.finished_at,
                "source": source,
                "decision": result.as_dict()["decision"],
            },
        )
        log_event(
            "SCAN",
            f"scan complete: {result.as_dict()['decision']}",
            event_id=scan_id,
            candidates=len(executable),
            errors=len(result.errors),
        )
        return result

    def _evaluate_symbol(
        self,
        symbol: str,
        account_state: AccountRiskState,
        *,
        scan_id: str,
        now: datetime,
    ) -> SymbolOutcome:
        # 1. Instrument specification (also the symbol-availability check).
        spec = self.broker.instrument(symbol)

        # 2. News blackout — checked before data work, because it is cheap
        #    and definitive.
        news = self.news.check(symbol, now=now)
        if news.blocked:
            return SymbolOutcome(symbol, "NEWS", "REJECTED", news.reason)

        # 3. Market data.
        try:
            series = self.market_data.multi_timeframe(spec, now=now)
        except MarketDataError as exc:
            return SymbolOutcome(symbol, "DATA", "REJECTED", str(exc))

        # 4. SMC.
        smc_result = self.smc.analyze(symbol, series, now=now)
        if smc_result.candidate is None:
            return SymbolOutcome(
                symbol, "SMC", "NO_SETUP", smc_result.rejection, smc=smc_result
            )
        candidate = smc_result.candidate
        atr_value = candidate.atr

        # 5. Deterministic score and tier.
        score = self.scorer.score(candidate)
        if not score.tradeable:
            return SymbolOutcome(
                symbol,
                "SCORE",
                "BELOW_TIER",
                f"score {score.total:.1f} graded {score.tier} (minimum B is "
                f"{self.config.scoring.tier_b:.0f})",
                candidate=candidate,
                score=score,
                smc=smc_result,
            )

        # 6. Risk — the single authority. Runs BEFORE the AI so a model is
        #    never asked about a trade risk would have refused anyway.
        risk_decision = self.risk.evaluate(
            candidate=candidate,
            tier=score.tier,
            account=account_state,
            spec=spec,
            rate_lookup=self._rate_lookup,
            now=now,
        )
        if not risk_decision.approved:
            return SymbolOutcome(
                symbol,
                "RISK",
                "REJECTED",
                "; ".join(risk_decision.reasons),
                candidate=candidate,
                score=score,
                risk=risk_decision,
                smc=smc_result,
            )

        # 7. AI validation — last, and only for serious candidates.
        ai_validation = self._ai_review(candidate, score)
        if ai_validation is not None and not ai_validation.approved:
            return SymbolOutcome(
                symbol,
                "AI",
                "REJECTED",
                "; ".join(ai_validation.reasons),
                candidate=candidate,
                score=score,
                risk=risk_decision,
                ai=ai_validation,
                smc=smc_result,
            )

        return SymbolOutcome(
            symbol,
            "CANDIDATE",
            "CANDIDATE",
            None,
            candidate=candidate,
            score=score,
            risk=risk_decision,
            ai=ai_validation,
            spec=spec,
            atr=atr_value,
            smc=smc_result,
        )

    def _ai_review(self, candidate: SetupCandidate, score: SetupScore) -> AIValidation | None:
        config = self.config.ai
        if not config.enabled:
            return None
        if tier_rank(score.tier) < tier_rank(config.min_tier_for_ai):
            return None
        if not self.ai.available:
            if config.allow_trade_without_ai:
                return None
            return AIValidation(
                False,
                ("AI validation is required but no provider is configured",),
                None,
            )
        try:
            decision = self.ai.review(candidate, score)
        except AIError as exc:
            if config.allow_trade_without_ai:
                log_event(
                    "AI",
                    f"all providers failed ({exc}); proceeding on deterministic evidence "
                    "because AI_ALLOW_TRADE_WITHOUT_AI is set",
                    severity="warning",
                    symbol=candidate.symbol,
                )
                return None
            return AIValidation(False, (f"AI validation unavailable: {exc}",), None)
        return validate_ai_decision(decision, candidate, config)

    def _rate_lookup(self, base: str, quote: str) -> float | None:
        """Broker-sourced FX rate for cross-currency risk conversion."""

        try:
            spec = self.broker.instrument(f"{base}{quote}")
            return self.broker.quote(spec).mid
        except BotError:
            return None

    @staticmethod
    def _rank(outcomes: Sequence[SymbolOutcome]) -> list[SymbolOutcome]:
        """Best opportunity first: tier, then score, then expected value.

        Expected profit is the final tiebreak rather than the primary key
        — otherwise the system would systematically prefer whichever
        symbol happens to have the largest contract size.
        """

        def key(outcome: SymbolOutcome) -> tuple[float, ...]:
            score = outcome.score
            risk = outcome.risk
            candidate = outcome.candidate
            return (
                tier_rank(score.tier if score else "NO_TRADE"),
                score.total if score else 0.0,
                candidate.risk_reward if candidate else 0.0,
                risk.expected_profit or 0.0 if risk else 0.0,
            )

        return sorted(outcomes, key=key, reverse=True)

    def _execute(self, outcome: SymbolOutcome, *, scan_id: str) -> ExecutionResult | None:
        candidate = outcome.candidate
        risk = outcome.risk
        score = outcome.score
        spec = outcome.spec
        if candidate is None or risk is None or score is None or spec is None:
            return None

        plan = build_plan(
            candidate=candidate,
            spec=spec,
            risk_decision=risk,
            score=score,
            ai_confidence=(
                outcome.ai.decision.confidence
                if outcome.ai is not None and outcome.ai.decision is not None
                else None
            ),
        )
        result = self.executor.execute(plan, spec, atr=outcome.atr)
        self.repos.journal.record(
            scan_id=scan_id,
            symbol=plan.symbol,
            stage="EXECUTION",
            outcome=result.status,
            reason=result.reason,
            payload=result.as_dict(),
        )
        return result

    def _bump_session_count(self, moment: datetime) -> None:
        counts = dict(self.repos.state.get(STATE_SESSION_COUNTS) or {})
        key = f"{trading_day(moment)}:{classify_session(moment, self.config.sessions).name}"
        counts[key] = int(counts.get(key, 0)) + 1
        # Keep only today's keys so this never grows without bound.
        today = trading_day(moment)
        counts = {k: v for k, v in counts.items() if k.startswith(today)}
        self.repos.state.set(STATE_SESSION_COUNTS, counts)

    # -- position management cycle ---------------------------------------

    def manage_positions(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Runs on a faster timer than the scan.

        Deliberately independent of `trading_enabled` and of the kill
        switch: pausing new entries must never abandon an open position.
        """

        moment = now or utc_now()
        try:
            positions = self.broker.positions()
        except BotError as exc:
            return {"ok": False, "error": str(exc)}

        rows = self.manager.track(positions)
        actions = []
        for position, row in zip(positions, rows):
            trade = self.repos.trades.by_position_id(position.position_id)
            price = row.get("currentPrice")
            if price is None:
                continue
            invalidated = self._structure_invalidated(position, price)
            actions.extend(
                plan_actions(
                    position=position,
                    trade=trade,
                    price=float(price),
                    config=self.config,
                    structure_invalidated=invalidated,
                    now=moment,
                )
            )
        applied = self.manager.apply(actions) if actions else []
        return {"ok": True, "positions": rows, "actions": applied}

    def _structure_invalidated(self, position: Any, price: float) -> bool:
        """A confirmed close beyond the setup's structural invalidation.

        Uses the recorded plan's stop as the reference rather than
        re-deriving structure, so this can never disagree with the level
        the trade was actually sized against.
        """

        trade = self.repos.trades.by_position_id(position.position_id)
        if not trade or not trade.get("stop_loss"):
            return False
        stop = float(trade["stop_loss"])
        if position.direction == "BUY":
            return price < stop
        return price > stop

    # -- periodic reconcile ----------------------------------------------

    def reconcile(self) -> ReconcileReport:
        report = self.reconciler.reconcile()
        self.last_reconcile = report
        for position_id in report.closed_stale:
            trade = self.repos.trades.by_position_id(position_id)
            if trade and float(trade.get("realized_pnl") or 0.0) < 0:
                self._record_event_time("loss")
        return report

    # -- health ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Honest health. A component that is broken is reported broken,
        and `ok` is false whenever anything trading-critical is down
        (MASTER_MISSION §61)."""

        database_ok = self.repos.db.ping()
        try:
            broker_health = self.broker.health()
        except Exception as exc:  # noqa: BLE001 - health must never raise
            broker_health = {"connected": False, "error": str(exc)[:200]}
        demo_ok = bool(self.last_demo and self.last_demo.verified)
        kill = self.kill_switch.read()

        def safe(component: str, probe: Any) -> dict[str, Any]:
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{component} health probe failed: {exc}"[:300]}

        components = {
            "mode": {
                "ok": True,
                "value": self.config.mode.value,
                "paper": self.config.is_paper,
                "description": (
                    "orders are simulated against live prices; nothing reaches the broker"
                    if self.config.is_paper
                    else "real orders are placed on the TradeLocker DEMO account"
                ),
            },
            "profitObjective": {
                "ok": bool(self.feasibility.get("feasible", True)),
                **self.feasibility,
            },
            "database": {
                "ok": database_ok,
                "backend": self.repos.db.backend,
                "error": self.repos.db.last_error,
            },
            "broker": {"ok": bool(broker_health.get("connected")), **broker_health},
            "demo": {"ok": demo_ok, **(self.last_demo.as_dict() if self.last_demo else {})},
            "marketData": safe("marketData", lambda: {"ok": True, **self.market_data.health()}),
            "ai": safe("ai", self.ai.health),
            "news": safe("news", self.news.health),
            "killSwitch": kill.as_dict(),
            "startup": {"ok": self.startup_complete, "error": self.startup_error},
            "scanner": {
                "enabled": safe("scanner", lambda: {"ok": self.trading_enabled}).get("ok", False),
                "running": self._scanning,
                "lastScan": safe("lastScan", lambda: self.repos.state.get(STATE_LAST_SCAN)),
            },
            "reconcile": self.last_reconcile.as_dict() if self.last_reconcile else None,
        }
        critical_ok = database_ok and components["broker"]["ok"] and demo_ok and self.startup_complete
        return {
            "ok": critical_ok,
            "tradingPermitted": critical_ok and not kill.active and self.trading_enabled,
            "mode": self.config.mode.value,
            "paper": self.config.is_paper,
            "components": components,
        }
