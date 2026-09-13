"""THE risk engine. The only place risk is calculated in this system.

There is no risk maths in the frontend, none in the execution layer, none
in the AI layer, and none in the SMC engine. Every trade passes through
`RiskEngine.evaluate()`, which is the only function that may approve one
(MASTER_MISSION §33).

Order of operations matters and is deliberate:

  1. hard blocks first (kill switch, demo, market closed, cooldowns,
     limits) — these are cheap and definitive;
  2. dynamic risk percentage, always clamped to the configured ceiling;
  3. position sizing from broker specs;
  4. portfolio/correlation checks against the SIZED risk;
  5. the profit objective, evaluated last because it needs the size.

Dynamic risk may only move DOWN for adverse account state. There is no
branch anywhere in this file that increases risk after a loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..broker.models import InstrumentSpec
from ..clock import trading_day, utc_now
from ..config import TradingConfig
from ..observability import log_event
from ..safety.kill_switch import KillSwitch
from ..smc.engine import SetupCandidate
from ..smc.sessions import classify_session, is_forex_weekend
from ..version import RISK_ENGINE_VERSION
from .correlation import ExposureReport, analyse_exposure
from .opportunity import OpportunityVerdict, evaluate_opportunity
from .sizing import PositionSize, RateLookup, SizingError, calculate_position_size, expected_profit


@dataclass(frozen=True, slots=True)
class AccountRiskState:
    """Everything about the account the risk engine needs, in one object."""

    balance: float
    equity: float
    available_margin: float
    peak_equity: float
    daily_realized_pnl: float
    open_pnl: float
    trades_today: int
    trades_this_session: int
    consecutive_losses: int
    open_positions: Sequence[Mapping[str, Any]]
    last_loss_at: datetime | None = None
    last_execution_failure_at: datetime | None = None

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_pnl(self) -> float:
        """Realized plus unrealized — the number that actually matters for
        a daily loss limit. Ignoring open losses lets a limit be evaded by
        simply not closing."""

        return self.daily_realized_pnl + self.open_pnl

    def as_dict(self) -> dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "peakEquity": round(self.peak_equity, 2),
            "drawdownPct": round(self.drawdown_pct * 100, 2),
            "dailyRealizedPnl": round(self.daily_realized_pnl, 2),
            "openPnl": round(self.open_pnl, 2),
            "dailyPnl": round(self.daily_pnl, 2),
            "tradesToday": self.trades_today,
            "consecutiveLosses": self.consecutive_losses,
            "openPositions": len(self.open_positions),
        }


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    symbol: str
    direction: str
    reasons: tuple[str, ...]
    risk_pct: float | None = None
    risk_amount: float | None = None
    size: PositionSize | None = None
    expected_profit: float | None = None
    opportunity: OpportunityVerdict | None = None
    exposure: ExposureReport | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    version: str = RISK_ENGINE_VERSION

    @property
    def state(self) -> str:
        return self.direction if self.approved else "NO TRADE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "state": self.state,
            "symbol": self.symbol,
            "direction": self.direction,
            "reasons": list(self.reasons),
            "riskPct": round(self.risk_pct * 100, 4) if self.risk_pct else None,
            "riskAmount": round(self.risk_amount, 2) if self.risk_amount else None,
            "size": self.size.as_dict() if self.size else None,
            "expectedProfit": round(self.expected_profit, 2) if self.expected_profit else None,
            "opportunity": self.opportunity.as_dict() if self.opportunity else None,
            "exposure": self.exposure.as_dict() if self.exposure else None,
            "limits": self.limits,
            "version": self.version,
        }


class RiskEngine:
    def __init__(self, config: TradingConfig, kill_switch: KillSwitch | None = None) -> None:
        self.config = config
        self.limits = config.risk
        self.kill_switch = kill_switch

    # -- dynamic risk ----------------------------------------------------

    def risk_percentage(self, *, tier: str, account: AccountRiskState) -> tuple[float, list[str]]:
        """Base risk adjusted for setup quality and account health.

        Upward adjustment comes ONLY from setup quality and is capped.
        Every downward adjustment (drawdown, losing streak) is applied
        after it, so adverse account state always wins.
        """

        notes: list[str] = []
        risk = self.limits.base_risk_pct

        if tier == "A+":
            risk *= 1.4
            notes.append("A+ setup: risk scaled up 1.4x within hard limits")
        elif tier == "A":
            risk *= 1.15
            notes.append("A setup: risk scaled up 1.15x")
        elif tier == "B":
            risk *= 0.75
            notes.append("B setup: risk reduced to 0.75x")

        drawdown = account.drawdown_pct
        if drawdown >= self.limits.drawdown_derisk_pct:
            # Linear de-risking from the de-risk threshold to the hard
            # maximum drawdown; at the hard limit the kill switch fires
            # anyway, so this never reaches zero in practice.
            span = max(1e-9, self.limits.max_drawdown_pct - self.limits.drawdown_derisk_pct)
            progress = min(1.0, (drawdown - self.limits.drawdown_derisk_pct) / span)
            factor = 1.0 - 0.6 * progress
            risk *= factor
            notes.append(f"drawdown {drawdown:.1%}: risk scaled to {factor:.2f}x")

        if account.consecutive_losses >= 2:
            factor = max(0.5, 1.0 - 0.2 * (account.consecutive_losses - 1))
            risk *= factor
            notes.append(
                f"{account.consecutive_losses} consecutive losses: risk scaled to {factor:.2f}x "
                "(risk is never increased after losses)"
            )

        clamped = max(self.limits.min_risk_pct, min(self.limits.max_risk_pct, risk))
        if clamped != risk:
            notes.append(
                f"risk clamped from {risk:.4%} to {clamped:.4%} by the hard "
                f"[{self.limits.min_risk_pct:.2%}, {self.limits.max_risk_pct:.2%}] bounds"
            )
        return clamped, notes

    # -- hard blocks -----------------------------------------------------

    def _hard_blocks(
        self, candidate: SetupCandidate, account: AccountRiskState, now: datetime
    ) -> list[str]:
        reasons: list[str] = []
        limits = self.limits

        if self.kill_switch is not None:
            state = self.kill_switch.read()
            if state.active:
                reasons.append(f"kill switch is active ({state.reason}) — no new trades")

        if account.balance <= 0:
            reasons.append("account balance is not positive")
        if account.equity <= 0:
            reasons.append("account equity is not positive")

        if is_forex_weekend(now):
            reasons.append("forex market is closed for the weekend")

        session = classify_session(now, self.config.sessions)
        if not session.tradeable:
            reasons.append(f"{session.name} session liquidity is too thin to trade")

        max_daily_loss = account.balance * limits.max_daily_loss_pct
        if account.daily_pnl <= -max_daily_loss:
            reasons.append(
                f"daily loss limit reached: {account.daily_pnl:.2f} vs limit "
                f"-{max_daily_loss:.2f} ({limits.max_daily_loss_pct:.1%} of balance)"
            )

        if account.drawdown_pct >= limits.max_drawdown_pct:
            reasons.append(
                f"maximum drawdown reached: {account.drawdown_pct:.2%} vs limit "
                f"{limits.max_drawdown_pct:.2%}"
            )

        if account.consecutive_losses >= limits.max_consecutive_losses:
            reasons.append(
                f"{account.consecutive_losses} consecutive losses reached the limit of "
                f"{limits.max_consecutive_losses}"
            )

        if len(account.open_positions) >= limits.max_open_positions:
            reasons.append(
                f"maximum open positions reached ({len(account.open_positions)}/"
                f"{limits.max_open_positions})"
            )

        same_symbol = [
            position
            for position in account.open_positions
            if str(position.get("symbol", "")).upper() == candidate.symbol.upper()
        ]
        if len(same_symbol) >= limits.max_open_per_symbol:
            reasons.append(f"already holding a position in {candidate.symbol}")

        if account.trades_today >= limits.max_trades_per_day:
            reasons.append(
                f"daily trade limit reached ({account.trades_today}/{limits.max_trades_per_day})"
            )
        if account.trades_this_session >= limits.max_trades_per_session:
            reasons.append(
                f"session trade limit reached "
                f"({account.trades_this_session}/{limits.max_trades_per_session})"
            )

        if account.last_loss_at is not None:
            elapsed = (now - account.last_loss_at).total_seconds() / 60.0
            if elapsed < limits.loss_cooldown_minutes:
                reasons.append(
                    f"cooling down after a loss ({elapsed:.0f}m of "
                    f"{limits.loss_cooldown_minutes}m elapsed)"
                )
        if account.last_execution_failure_at is not None:
            elapsed = (now - account.last_execution_failure_at).total_seconds() / 60.0
            if elapsed < limits.execution_failure_cooldown_minutes:
                reasons.append(
                    f"cooling down after an execution failure ({elapsed:.0f}m of "
                    f"{limits.execution_failure_cooldown_minutes}m elapsed)"
                )
        return reasons

    # -- main entry point ------------------------------------------------

    def evaluate(
        self,
        *,
        candidate: SetupCandidate,
        tier: str,
        account: AccountRiskState,
        spec: InstrumentSpec,
        rate_lookup: RateLookup | None = None,
        leverage: float | None = None,
        now: datetime | None = None,
    ) -> RiskDecision:
        moment = now or utc_now()
        limits_snapshot = {
            "maxRiskPct": self.limits.max_risk_pct,
            "maxPortfolioRiskPct": self.limits.max_portfolio_risk_pct,
            "maxDailyLossPct": self.limits.max_daily_loss_pct,
            "maxDrawdownPct": self.limits.max_drawdown_pct,
            "maxOpenPositions": self.limits.max_open_positions,
            "maxTradesPerDay": self.limits.max_trades_per_day,
            "minRiskReward": self.limits.min_risk_reward,
            "maxConsecutiveLosses": self.limits.max_consecutive_losses,
        }

        reasons = self._hard_blocks(candidate, account, moment)
        if reasons:
            return RiskDecision(
                approved=False,
                symbol=candidate.symbol,
                direction=candidate.direction,
                reasons=tuple(reasons),
                limits=limits_snapshot,
            )

        # Structural sanity — re-checked here even though the SMC engine
        # already enforced it, because this is the last gate before money
        # moves and a single authority cannot delegate its own invariants.
        if candidate.direction == "BUY" and not (
            candidate.stop_loss < candidate.entry < candidate.take_profit
        ):
            return RiskDecision(
                False, candidate.symbol, candidate.direction,
                ("BUY levels invalid: require stop < entry < target",), limits=limits_snapshot
            )
        if candidate.direction == "SELL" and not (
            candidate.take_profit < candidate.entry < candidate.stop_loss
        ):
            return RiskDecision(
                False, candidate.symbol, candidate.direction,
                ("SELL levels invalid: require target < entry < stop",), limits=limits_snapshot
            )
        if candidate.risk_reward < self.limits.min_risk_reward:
            return RiskDecision(
                False, candidate.symbol, candidate.direction,
                (
                    f"R:R 1:{candidate.risk_reward:.2f} is below the minimum "
                    f"1:{self.limits.min_risk_reward:g}",
                ),
                limits=limits_snapshot,
            )

        risk_pct, risk_notes = self.risk_percentage(tier=tier, account=account)
        risk_amount = round(account.equity * risk_pct, 2)

        try:
            size = calculate_position_size(
                spec=spec,
                risk_amount=risk_amount,
                entry=candidate.entry,
                stop_loss=candidate.stop_loss,
                rate_lookup=rate_lookup,
                leverage=leverage,
                available_margin=account.available_margin,
            )
        except SizingError as exc:
            return RiskDecision(
                False,
                candidate.symbol,
                candidate.direction,
                (f"position sizing refused: {exc}",),
                risk_pct=risk_pct,
                risk_amount=risk_amount,
                limits=limits_snapshot,
            )

        # Sizing rounds DOWN, so actual risk should never exceed the
        # budget. Assert it anyway: this is the invariant the whole risk
        # model rests on.
        if size.actual_risk > risk_amount * 1.02:
            return RiskDecision(
                False,
                candidate.symbol,
                candidate.direction,
                (
                    f"sizing produced ${size.actual_risk:.2f} of risk against an approved "
                    f"${risk_amount:.2f} — refusing",
                ),
                risk_pct=risk_pct,
                risk_amount=risk_amount,
                limits=limits_snapshot,
            )

        exposure = analyse_exposure(
            account.open_positions,
            {
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "risk_amount": size.actual_risk,
            },
            correlation_threshold=self.limits.correlation_threshold,
        )
        portfolio_cap = account.equity * self.limits.max_portfolio_risk_pct
        if exposure.total_open_risk > portfolio_cap:
            return RiskDecision(
                False,
                candidate.symbol,
                candidate.direction,
                (
                    f"portfolio risk ${exposure.total_open_risk:.2f} would exceed the "
                    f"${portfolio_cap:.2f} cap ({self.limits.max_portfolio_risk_pct:.1%} of equity)",
                ),
                risk_pct=risk_pct,
                risk_amount=risk_amount,
                size=size,
                exposure=exposure,
                limits=limits_snapshot,
            )

        correlated_cap = account.equity * self.limits.max_correlated_risk_pct
        if exposure.correlated_risk > correlated_cap:
            worst = exposure.worst_pair
            return RiskDecision(
                False,
                candidate.symbol,
                candidate.direction,
                (
                    f"correlated exposure ${exposure.correlated_risk:.2f} would exceed the "
                    f"${correlated_cap:.2f} cap"
                    + (f" (most correlated: {worst[0]} at {worst[1]:.2f})" if worst else ""),
                ),
                risk_pct=risk_pct,
                risk_amount=risk_amount,
                size=size,
                exposure=exposure,
                limits=limits_snapshot,
            )

        profit = expected_profit(
            spec=spec,
            lots=size.lots,
            entry=candidate.entry,
            take_profit=candidate.take_profit,
            conversion=size.conversion_rate,
        )
        opportunity = evaluate_opportunity(
            expected_profit=profit, config=self.config.opportunity, tier=tier
        )
        if not opportunity.meets_objective:
            return RiskDecision(
                False,
                candidate.symbol,
                candidate.direction,
                (opportunity.reason,),
                risk_pct=risk_pct,
                risk_amount=risk_amount,
                size=size,
                expected_profit=profit,
                opportunity=opportunity,
                exposure=exposure,
                limits=limits_snapshot,
            )

        approved_reasons = tuple(
            risk_notes
            + [
                f"risking ${size.actual_risk:.2f} ({risk_pct:.2%} of ${account.equity:.2f} equity)",
                f"{size.lots} lots, expected ${profit:.2f} at target, R:R 1:{candidate.risk_reward:.2f}",
                opportunity.reason,
            ]
        )
        decision = RiskDecision(
            approved=True,
            symbol=candidate.symbol,
            direction=candidate.direction,
            reasons=approved_reasons,
            risk_pct=risk_pct,
            risk_amount=risk_amount,
            size=size,
            expected_profit=profit,
            opportunity=opportunity,
            exposure=exposure,
            limits=limits_snapshot,
        )
        log_event(
            "RISK",
            f"approved {candidate.direction} {candidate.symbol}",
            symbol=candidate.symbol,
            risk_amount=size.actual_risk,
            lots=size.lots,
            tier=tier,
        )
        return decision

    # -- auto kill-switch evaluation -------------------------------------

    def evaluate_kill_switch(self, account: AccountRiskState) -> str | None:
        """Conditions that should stop the system entirely, not just skip
        one trade. Returns the trip reason, or None."""

        if account.balance > 0:
            max_daily_loss = account.balance * self.limits.max_daily_loss_pct
            if account.daily_pnl <= -max_daily_loss:
                return "DAILY_LOSS_LIMIT"
        if account.drawdown_pct >= self.limits.max_drawdown_pct:
            return "MAX_DRAWDOWN"
        if account.consecutive_losses >= self.limits.max_consecutive_losses:
            return "CONSECUTIVE_LOSSES"
        return None
