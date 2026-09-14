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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Sequence

from .ai.client import AIClient
from .ai.validator import AIValidation, validate_ai_decision
from .broker.cache import CachedRead, LiveCache
from .broker.models import InstrumentSpec
from .clock import trading_day, utc_now
from .config import TradingConfig, profit_floor_feasibility
from .errors import (
    AIError,
    BotError,
    ConfigError,
    DemoVerificationError,
    MarketDataError,
    SymbolUnavailable,
)
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
from . import strategy as strategies
from .smc.sessions import classify_session, is_forex_weekend
from .storage.repositories import Repositories

STATE_TRADING_ENABLED = "trading_enabled"
STATE_LAST_SCAN = "last_scan"
STATE_SESSION_COUNTS = "session_trade_counts"
#: The selected strategy, persisted so a restart keeps running the mode
#: the operator chose rather than silently reverting to the default.
STATE_STRATEGY = "active_strategy"
#: The broker account the stored state belongs to. A different one means
#: the daily counters, losing streak and trade history describe somebody
#: else's account, and saying so beats letting them be read as this one's.
STATE_ACCOUNT_ID = "broker_account_id"


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
    #: Configured symbols the broker does not offer on this account. Kept
    #: apart from `errors` because they are a config edit away from fixed
    #: and will otherwise repeat identically on every scan forever.
    unavailable: list[dict[str, Any]] = field(default_factory=list)

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
            "unavailable": self.unavailable,
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
        #: What the scan and the position poll last read from the broker.
        #: The dashboard serves from this and never calls the broker
        #: itself — see bot/broker/cache.py for why that had to change.
        self.live = LiveCache()
        self.smc = smc or SmcEngine(config)
        self.scorer = scorer or SetupScorer(config)
        self.risk = risk or RiskEngine(config, self.kill_switch)
        self._risk_injected = risk is not None
        self._strategy_key = strategies.DEFAULT_STRATEGY
        self._strategy: strategies.Strategy | None = None
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

    # -- strategy selection ----------------------------------------------

    @property
    def strategy_key(self) -> str:
        """The mode in force, read from storage so it survives a restart."""

        try:
            stored = self.repos.state.get(STATE_STRATEGY)
        except Exception:  # noqa: BLE001 - storage is checked elsewhere
            stored = None
        try:
            key = strategies.normalise(stored or self.config.strategy)
        except ConfigError:
            # A stored name this build no longer knows. Fall back to the
            # default rather than refusing to trade at all, but say so —
            # silence here would run a different strategy than the record
            # of every past trade claims.
            log_event(
                "STRATEGY",
                f"stored strategy {stored!r} is not available in this build; "
                f"falling back to {strategies.DEFAULT_STRATEGY}",
                severity="warning",
            )
            key = strategies.DEFAULT_STRATEGY
        if key != self._strategy_key or self._strategy is None:
            self._strategy_key = key
            self._strategy = strategies.build(key, self.config)
            if not self._risk_injected:
                # The risk engine keeps its authority (project rule 2); it
                # is simply given the floor that belongs to the mode now
                # running. Nothing here can raise a limit: the profile sets
                # min_risk_reward and cannot go below the build minimum,
                # which StrategyProfile asserts on construction.
                self.risk = RiskEngine(
                    replace(
                        self.config,
                        risk=replace(
                            self.config.risk,
                            min_risk_reward=self._strategy.profile.min_risk_reward,
                        ),
                    ),
                    self.kill_switch,
                )
        return self._strategy_key

    @property
    def strategy(self) -> strategies.Strategy:
        """The strategy object for the mode in force.

        Resolution happens in `strategy_key`, which always leaves
        `_strategy` populated. The re-check is a real branch rather than an
        assert: `python -O` strips asserts, and an invariant that vanishes
        under an optimisation flag is not an invariant — it would surface
        here as an AttributeError inside the scan loop instead.
        """

        key = self.strategy_key
        strategy = self._strategy
        if strategy is None:  # pragma: no cover - defensive
            strategy = self._strategy = strategies.build(key, self.config)
        return strategy

    def set_strategy(self, name: str) -> dict[str, Any]:
        """Switch mode. Refuses an unknown name instead of defaulting."""

        key = strategies.normalise(name)
        previous = self.strategy_key
        self.repos.state.set(STATE_STRATEGY, key)
        self._strategy = None  # force a rebuild, including the risk floor
        active = self.strategy_key
        log_event(
            "STRATEGY",
            f"strategy switched from {previous} to {active}",
            severity="info",
            previous=previous,
            active=active,
        )
        return self.strategy_status()

    def strategy_status(self) -> dict[str, Any]:
        """Every mode, which is active, and whether it can actually trade.

        The last part matters: a mode whose targets are smaller than the
        profit floor will never place a trade, and an operator switching to
        it deserves to be told that at the switch rather than discovering
        it over a silent week.
        """

        active = self.strategy_key
        equity = float(self.feasibility.get("equity") or 0.0)
        max_risk = float(self.feasibility.get("maxRiskPerTrade") or 0.0)
        floor = float(self.config.opportunity.minimum_profit)

        options = []
        for profile in strategies.available():
            payload = profile.as_dict()
            payload["active"] = profile.key == active
            if self.config.opportunity.enabled and max_risk > 0:
                typical = max_risk * profile.min_risk_reward
                payload["typicalProfitAtFloorRisk"] = round(typical, 2)
                payload["clearsProfitFloor"] = typical >= floor
                payload["note"] = (
                    None
                    if typical >= floor
                    else (
                        f"at ${equity:,.0f} equity this mode's 1:{profile.min_risk_reward:g} "
                        f"target is worth about ${typical:,.0f}, under the ${floor:,.0f} profit "
                        f"floor — it will find setups and the floor will reject them. Lower "
                        f"OPPORTUNITY_MINIMUM_PROFIT or grow the account."
                    )
                )
            options.append(payload)
        return {"active": active, "options": options}

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
            self.config,
            self.broker.account_metadata,
            stage="startup",
            claims=getattr(self.broker, "session_claims", None),
        )
        self.last_demo = verification
        if not verification.verified:
            self.startup_error = f"DEMO verification failed: {verification.reason}"
            self._trip_for_failed_verification(verification)
            return {"ok": False, "error": self.startup_error, "demo": verification.as_dict()}

        self._note_account_identity()

        if not self.repos.db.ping():
            self.startup_error = "database is unreachable"
            log_event("STARTUP", self.startup_error, severity="critical")
            return {"ok": False, "error": self.startup_error}

        try:
            account = self.broker.account_state()
            self.live.put("account", account)
            self.repos.equity.snapshot(
                account.balance, account.equity, self.config.broker.account_id
            )
            self.repos.daily.today(start_balance=account.balance)
        except BotError as exc:
            self.live.fail("account", str(exc))
            self.startup_error = f"could not read broker account state: {exc}"
            return {"ok": False, "error": self.startup_error}

        # Seed the page before the first scheduled poll, which does not run
        # until a whole interval has elapsed. Without this the dashboard is
        # blank for the first 30 seconds of every deploy, which is exactly
        # when somebody is watching it to see whether the deploy worked.
        try:
            self.refresh_live_positions(now=utc_now())
        except BotError as exc:
            log_event(
                "STARTUP",
                f"could not seed the dashboard's position view: {exc}",
                severity="warning",
            )

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
        """Read the broker and compose the state the risk engine needs.

        The two broker reads are also deposited in `self.live`, so the
        dashboard can answer from them instead of repeating the calls on
        its own thread and queueing behind a scan.
        """

        moment = now or utc_now()
        try:
            account = self.broker.account_state()
            positions = self.broker.positions()
        except BotError as exc:
            self.live.fail("account", str(exc))
            self.live.fail("positions", str(exc))
            raise
        self.live.put("account", account, now=moment)
        self.live.put("positions", positions, now=moment)

        self.repos.equity.snapshot(
            account.balance, account.equity, self.config.broker.account_id
        )
        return self._compose_account_state(account, positions, now=moment)

    def cached_account_state(
        self, *, now: datetime | None = None
    ) -> tuple[AccountRiskState, CachedRead] | None:
        """The same state composed from the last broker read, or None.

        Everything after the two broker calls is local: the database and
        the clock. So the risk panel can be answered without touching the
        network at all, which is what keeps the dashboard off the shared
        throttle. The `CachedRead` is returned alongside so the caller can
        say how old the broker half is — a number served without its age
        would be exactly the fabrication rule 6 forbids.
        """

        account = self.live.get("account")
        positions = self.live.get("positions")
        if not account.present or not positions.present:
            return None
        # Report the age of the OLDER half: the state is only as current as
        # its least current input.
        oldest = account if account.at <= positions.at else positions
        return (
            self._compose_account_state(
                account.value, positions.value, now=now or utc_now()
            ),
            oldest,
        )

    def refresh_live_positions(
        self, *, now: datetime
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Re-read positions, track them, and publish both for the page.

        The single place that fills the position half of the read model,
        so the poll and the scan cannot drift into filling it differently.
        """

        try:
            positions = list(self.broker.positions())
        except BotError as exc:
            self.live.fail("positions", str(exc))
            self.live.fail("position_rows", str(exc))
            raise
        self.live.put("positions", positions, now=now)
        rows = self.manager.track(positions, now=now)
        self.live.put("position_rows", rows, now=now)
        return positions, rows

    def _compose_account_state(
        self,
        account: Any,
        positions: Sequence[Any],
        *,
        now: datetime,
    ) -> AccountRiskState:
        """Compose the risk state from two broker reads plus local data.

        Everything here is the database and the clock, so the dashboard
        can run it against a cached pair of reads. Writing the equity
        snapshot stays in `build_account_state`: a page left open on a
        phone must not fill that table with duplicates of whatever the
        scan last saw and distort the series peak_equity is drawn from.
        """

        moment = now
        peak = self.repos.equity.peak_equity(self.config.broker.account_id) or account.equity
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

        # Before anything touches the network. The risk engine and the SMC
        # engine both refuse a closed market anyway, but they refuse it
        # after a full round of broker calls — and a broker in weekend
        # maintenance answers those with errors, five of which open the
        # circuit and leave the dashboard reading OFFLINE all weekend for
        # no reason. There is nothing to analyse on a shut market; asking
        # is the bug.
        if is_forex_weekend(moment):
            result.skipped_reason = (
                "the forex market is closed for the weekend (it reopens Sunday 22:00 UTC)"
            )
            result.finished_at = utc_now().isoformat()
            self.last_scan = result
            log_event("SCAN", result.skipped_reason, event_id=scan_id, source=source)
            return result

        try:
            verification = verify_demo(
                self.config,
                self.broker.account_metadata,
                stage="scan",
                claims=getattr(self.broker, "session_claims", None),
            )
            self.last_demo = verification
            result.demo = verification
            if not verification.verified:
                self._trip_for_failed_verification(verification)
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
            except SymbolUnavailable as exc:
                outcome = SymbolOutcome(
                    symbol=symbol, stage="CONFIG", outcome="UNAVAILABLE", reason=str(exc)
                )
                result.unavailable.append(
                    {"symbol": symbol, "reason": str(exc), "suggestions": list(exc.suggestions)}
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
                # Show the new position now, not at the next poll. A trade
                # that appears to have vanished for thirty seconds is the
                # one thing an operator will not sit calmly through — and
                # the positions cached at the top of this scan predate it.
                try:
                    self.refresh_live_positions(now=moment)
                except BotError as exc:
                    log_event(
                        "SCAN",
                        f"could not refresh positions after executing: {exc}",
                        severity="warning",
                        event_id=scan_id,
                    )
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
        if result.unavailable:
            # Named, not counted. Four symbols hidden inside "errors: 4" is
            # a permanent config fault that reads like a transient one.
            log_event(
                "SCAN",
                "configured symbols this account does not carry (they will fail "
                "identically on every scan until TRADED_SYMBOLS changes): "
                + "; ".join(
                    entry["symbol"]
                    + (
                        " -> try " + ", ".join(entry["suggestions"])
                        if entry.get("suggestions")
                        else ""
                    )
                    for entry in result.unavailable
                ),
                event_id=scan_id,
                severity="warning",
            )
        log_event(
            "SCAN",
            f"scan complete: {result.as_dict()['decision']}",
            event_id=scan_id,
            candidates=len(executable),
            errors=len(result.errors),
            unavailable=len(result.unavailable),
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

        # 4. Strategy — SMC or the reversion mode, whichever is selected.
        #    Everything after this point is identical either way: a
        #    candidate is a candidate, and risk decides its fate.
        smc_result = self.strategy.analyze(symbol, series, now=now)
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

    def _note_account_identity(self) -> None:
        """Say plainly when the configured account has changed.

        Account-derived state — the daily loss counter, the losing streak,
        the trade history — belongs to whichever account produced it.
        Peak equity is scoped in storage so a drawdown limit can never be
        computed against a different account's high-water mark, but the
        rest is worth flagging rather than silently carrying over: a
        losing streak from an account that no longer exists should not
        reduce risk on a new one, and an operator who switched accounts
        deserves to be told which numbers are now stale.
        """

        configured = str(self.config.broker.account_id or "")
        try:
            previous = self.repos.state.get(STATE_ACCOUNT_ID)
        except Exception:  # noqa: BLE001 - storage health is checked elsewhere
            return
        if not configured:
            return
        if previous and str(previous) != configured:
            log_event(
                "STARTUP",
                f"broker account changed from {previous} to {configured}. Peak equity is "
                "tracked per account, so the drawdown limit starts fresh. Daily counters, "
                "the losing streak and the trade history still describe the previous "
                "account until they roll over.",
                severity="warning",
                previous_account=str(previous),
                account=configured,
            )
        if str(previous or "") != configured:
            self.repos.state.set(STATE_ACCOUNT_ID, configured)

    def _trip_for_failed_verification(self, verification: DemoVerification) -> None:
        """Block trading, and latch only when latching is warranted.

        Every failure here stops trading — the caller returns before any
        order path, and `health()` reports the system as not permitted to
        trade. The question this answers is narrower: does a human have to
        come and unlock it afterwards?

        Only a contradiction earns that. "The broker says this is LIVE", or
        "the configured URL is not a demo endpoint", is a misconfiguration
        that must not be cleared by a passing retry, so it trips the
        SAFETY-class ENVIRONMENT_MISMATCH which requires a force clear.

        A broker we could not reach is a different thing entirely. Treating
        it as a mismatch latched the kill switch through a transient
        outage and kept the bot down long after the broker returned, with
        a message accusing the account of being live when nobody had
        managed to ask it. That failure is loud in health and in the
        scan result; it does not need a human with a key.
        """

        if verification.contradicted:
            self.kill_switch.trip("ENVIRONMENT_MISMATCH", verification.reason or "")
            return
        log_event(
            "SAFETY",
            f"DEMO status could not be verified, so no trade will be placed: "
            f"{verification.reason}",
            severity="critical",
            **verification.as_dict(),
        )

    def _ai_gate_status(self) -> dict[str, Any]:
        """Whether the AI stage can ever pass.

        Reported because the failure mode is invisible otherwise: AI enabled
        with no provider and no permission to proceed without one rejects
        every candidate, and the only evidence is a journal line.
        """

        ai = self.config.ai
        blocking = (
            ai.enabled
            and not (ai.gemini_key or ai.groq_key)
            and not ai.allow_trade_without_ai
        )
        return {
            "ok": not blocking,
            "blockingAllTrades": blocking,
            "note": (
                "AI is required but no provider is configured — every setup is rejected "
                "at the AI gate. Set AI_ENABLED=false, or AI_ALLOW_TRADE_WITHOUT_AI=true, "
                "or add an API key."
            )
            if blocking
            else None,
        }

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
            strategy=self.strategy_key,
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
        # A shut market cannot move a stop into profit or invalidate a
        # structure, and it cannot be asked for a price either. Polling it
        # every 30 seconds only feeds the circuit breaker.
        if is_forex_weekend(moment):
            return {
                "ok": True,
                "skipped": "the forex market is closed for the weekend",
                "actions": [],
            }
        try:
            positions, rows = self.refresh_live_positions(now=moment)
        except BotError as exc:
            return {"ok": False, "error": str(exc)}
        # One request every poll, against five to seven per dashboard
        # refresh before this. It keeps the balance on the page current
        # between scans without the page ever asking the broker itself.
        try:
            self.live.put("account", self.broker.account_state(), now=moment)
        except BotError as exc:
            self.live.fail("account", str(exc))
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
            "broker": {
                "ok": bool(broker_health.get("connected")),
                # A cooldown is the broker asking us to wait, not a fault.
                "note": (
                    f"rate limited by the broker; requests resume in "
                    f"{broker_health['rateLimitedFor']:.0f}s"
                    if broker_health.get("rateLimitedFor")
                    else None
                ),
                **broker_health,
            },
            "demo": {"ok": demo_ok, **(self.last_demo.as_dict() if self.last_demo else {})},
            "marketData": safe("marketData", lambda: {"ok": True, **self.market_data.health()}),
            "ai": safe("ai", lambda: {**self.ai.health(), **self._ai_gate_status()}),
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
        market_closed = is_forex_weekend(utc_now())
        components["market"] = {
            "ok": True,  # a shut market is a schedule, never a fault
            "open": not market_closed,
            "note": (
                "the forex market is closed for the weekend; it reopens Sunday 22:00 UTC"
                if market_closed
                else None
            ),
        }
        critical_ok = database_ok and components["broker"]["ok"] and demo_ok and self.startup_complete
        return {
            "ok": critical_ok,
            "marketClosed": market_closed,
            "tradingPermitted": critical_ok and not kill.active and self.trading_enabled,
            "mode": self.config.mode.value,
            "paper": self.config.is_paper,
            "components": components,
        }
