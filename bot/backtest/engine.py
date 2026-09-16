"""Sequential backtester. No look-ahead, no repainting, no future leakage.

The structural guarantee: on bar *i* the engine is handed `candles[:i+1]`
and nothing else. It is physically impossible for a detector to see bar
i+1, because that bar is not in the list. Every "no look-ahead" claim in
this project reduces to that one line, plus the `confirmed_index`
discipline inside the detectors themselves.

Fill modelling is pessimistic on purpose:
  * entries pay the spread (buy at ask, sell at bid);
  * stops fill at the stop price plus adverse slippage;
  * a bar that touches BOTH stop and target is resolved as the STOP,
    because intrabar order is unknown and assuming the good outcome is
    how backtests come to bear no resemblance to live results;
  * commission is charged per side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..broker.models import InstrumentSpec
from ..config import TradingConfig
from ..marketdata.candles import Candle
from ..marketdata.provider import Series
from ..marketdata.validation import ValidationReport
from ..risk.engine import AccountRiskState, RiskEngine
from ..risk.sizing import SizingError, calculate_position_size, expected_profit
from ..scoring.scorer import SetupScorer
from ..smc.engine import SmcEngine
from ..smc.sessions import is_forex_weekend


@dataclass
class SimulatedTrade:
    symbol: str
    direction: str
    entry_index: int
    entry_time: datetime
    entry: float
    stop_loss: float
    take_profit: float
    lots: float
    risk_amount: float
    setup_grade: str
    setup_score: float
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    exit_reason: str | None = None
    mfe: float = 0.0
    mae: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entryTime": self.entry_time.isoformat(),
            "entry": round(self.entry, 6),
            "stopLoss": round(self.stop_loss, 6),
            "takeProfit": round(self.take_profit, 6),
            "lots": self.lots,
            "riskAmount": round(self.risk_amount, 2),
            "setupGrade": self.setup_grade,
            "setupScore": self.setup_score,
            "exitTime": self.exit_time.isoformat() if self.exit_time else None,
            "exitPrice": round(self.exit_price, 6) if self.exit_price else None,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
            "rMultiple": round(self.r_multiple, 3) if self.r_multiple is not None else None,
            "exitReason": self.exit_reason,
        }


@dataclass
class BacktestResult:
    trades: list[SimulatedTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    starting_balance: float = 10_000.0
    bars_processed: int = 0
    setups_considered: int = 0
    setups_rejected: dict[str, int] = field(default_factory=dict)

    @property
    def closed(self) -> list[SimulatedTrade]:
        return [trade for trade in self.trades if trade.pnl is not None]

    def statistics(self) -> dict[str, Any]:
        from ..analytics.performance import compute_performance

        rows = [
            {
                "realized_pnl": trade.pnl,
                "r_multiple": trade.r_multiple,
                "closed_at": trade.exit_time.isoformat() if trade.exit_time else None,
                "symbol": trade.symbol,
                "setup_grade": trade.setup_grade,
            }
            for trade in self.closed
        ]
        stats = compute_performance(rows).as_dict()
        final = self.equity_curve[-1] if self.equity_curve else self.starting_balance
        stats["startingBalance"] = self.starting_balance
        stats["finalBalance"] = round(final, 2)
        # A percentage return on nothing is not zero, it is undefined —
        # and dividing by it raised instead of saying so.
        stats["returnPct"] = (
            round((final - self.starting_balance) / self.starting_balance * 100, 2)
            if self.starting_balance > 0
            else None
        )
        stats["barsProcessed"] = self.bars_processed
        stats["setupsConsidered"] = self.setups_considered
        stats["rejections"] = dict(
            sorted(self.setups_rejected.items(), key=lambda item: item[1], reverse=True)[:12]
        )
        return stats


@dataclass(frozen=True, slots=True)
class BacktestCosts:
    """Execution frictions. Defaults are deliberately conservative."""

    spread_points: float = 8.0        # in instrument ticks
    slippage_points: float = 3.0
    commission_per_lot: float = 7.0   # round turn, account currency


class Backtester:
    def __init__(
        self,
        config: TradingConfig,
        spec: InstrumentSpec,
        *,
        costs: BacktestCosts | None = None,
        starting_balance: float = 10_000.0,
    ) -> None:
        self.config = config
        self.spec = spec
        self.costs = costs or BacktestCosts()
        self.starting_balance = starting_balance
        self.smc = SmcEngine(config)
        self.scorer = SetupScorer(config)
        self.risk = RiskEngine(config)

    def run(
        self,
        m15: Sequence[Candle],
        h1: Sequence[Candle],
        *,
        warmup: int = 120,
        step: int = 1,
    ) -> BacktestResult:
        result = BacktestResult(starting_balance=self.starting_balance)
        balance = self.starting_balance
        peak = balance
        result.equity_curve.append(balance)
        open_trade: SimulatedTrade | None = None
        consecutive_losses = 0

        h1_by_time = sorted(h1, key=lambda candle: candle.timestamp)

        for index in range(warmup, len(m15)):
            bar = m15[index]
            result.bars_processed += 1

            # --- manage the open position on THIS bar ---
            if open_trade is not None:
                closed = self._resolve_exit(open_trade, bar, index)
                if closed:
                    balance += open_trade.pnl or 0.0
                    peak = max(peak, balance)
                    consecutive_losses = (
                        consecutive_losses + 1 if (open_trade.pnl or 0.0) < 0 else 0
                    )
                    result.equity_curve.append(round(balance, 2))
                    open_trade = None
                else:
                    continue  # one position at a time in this simulation

            if index % step != 0:
                continue
            if is_forex_weekend(bar.close_time):
                continue

            # --- analysis sees ONLY closed bars up to and including i ---
            visible_m15 = list(m15[: index + 1])
            cutoff = bar.close_time
            visible_h1 = [candle for candle in h1_by_time if candle.close_time <= cutoff]
            if len(visible_h1) < 40:
                continue

            series = {
                "M15": _series(self.spec.symbol, "M15", visible_m15),
                "H1": _series(self.spec.symbol, "H1", visible_h1),
            }
            analysis = self.smc.analyze(self.spec.symbol, series, now=cutoff)
            if analysis.candidate is None:
                reason = (analysis.rejection or "unknown").split(":")[0][:60]
                result.setups_rejected[reason] = result.setups_rejected.get(reason, 0) + 1
                continue

            candidate = analysis.candidate
            score = self.scorer.score(candidate)
            result.setups_considered += 1
            if not score.tradeable:
                result.setups_rejected["below tier"] = result.setups_rejected.get("below tier", 0) + 1
                continue

            drawdown = (peak - balance) / peak if peak > 0 else 0.0
            if drawdown >= self.config.risk.max_drawdown_pct:
                result.setups_rejected["max drawdown"] = result.setups_rejected.get("max drawdown", 0) + 1
                continue

            # Sizing goes through the SAME risk engine the live path uses.
            # Re-deriving the tier multipliers here would make the backtest
            # measure a strategy the bot does not actually run.
            risk_pct, _ = self.risk.risk_percentage(
                tier=score.tier,
                account=AccountRiskState(
                    balance=balance,
                    equity=balance,
                    available_margin=balance,
                    peak_equity=peak,
                    daily_realized_pnl=0.0,
                    open_pnl=0.0,
                    trades_today=0,
                    trades_this_session=0,
                    consecutive_losses=consecutive_losses,
                    open_positions=[],
                ),
            )

            # Entry pays the spread, exactly as it would live.
            spread = self.costs.spread_points * self.spec.tick_size
            entry = (
                candidate.entry + spread / 2
                if candidate.direction == "BUY"
                else candidate.entry - spread / 2
            )
            try:
                size = calculate_position_size(
                    spec=self.spec,
                    risk_amount=balance * risk_pct,
                    entry=entry,
                    stop_loss=candidate.stop_loss,
                )
            except SizingError:
                result.setups_rejected["sizing"] = result.setups_rejected.get("sizing", 0) + 1
                continue

            profit = expected_profit(
                spec=self.spec,
                lots=size.lots,
                entry=entry,
                take_profit=candidate.take_profit,
                conversion=size.conversion_rate,
            )
            if self.config.opportunity.enabled and profit < self.config.opportunity.target_profit * (
                self.config.opportunity.tolerance_fraction if score.tier == "A+" else 1.0
            ):
                result.setups_rejected["profit objective"] = (
                    result.setups_rejected.get("profit objective", 0) + 1
                )
                continue

            open_trade = SimulatedTrade(
                symbol=self.spec.symbol,
                direction=candidate.direction,
                entry_index=index,
                entry_time=bar.close_time,
                entry=entry,
                stop_loss=candidate.stop_loss,
                take_profit=candidate.take_profit,
                lots=size.lots,
                risk_amount=size.actual_risk,
                setup_grade=score.tier,
                setup_score=round(score.total, 2),
            )
            result.trades.append(open_trade)

        return result

    def _resolve_exit(self, trade: SimulatedTrade, bar: Candle, index: int) -> bool:
        """Did this bar close the trade? Pessimistic on ambiguity."""

        if index <= trade.entry_index:
            return False

        excursion_up = bar.high - trade.entry
        excursion_down = bar.low - trade.entry
        if trade.direction == "BUY":
            trade.mfe = max(trade.mfe, excursion_up)
            trade.mae = min(trade.mae, excursion_down)
            hit_stop = bar.low <= trade.stop_loss
            hit_target = bar.high >= trade.take_profit
        else:
            trade.mfe = max(trade.mfe, -excursion_down)
            trade.mae = min(trade.mae, -excursion_up)
            hit_stop = bar.high >= trade.stop_loss
            hit_target = bar.low <= trade.take_profit

        if not hit_stop and not hit_target:
            return False

        slippage = self.costs.slippage_points * self.spec.tick_size
        if hit_stop:
            # Both touched in one bar -> assume the stop. Intrabar sequence
            # is unknowable, and optimism here is what makes a backtest lie.
            exit_price = (
                trade.stop_loss - slippage if trade.direction == "BUY" else trade.stop_loss + slippage
            )
            reason = "STOP_AND_TARGET_SAME_BAR" if hit_target else "STOP"
        else:
            exit_price = trade.take_profit
            reason = "TARGET"

        move = (
            exit_price - trade.entry if trade.direction == "BUY" else trade.entry - exit_price
        )
        gross = move * self.spec.contract_size * trade.lots
        commission = self.costs.commission_per_lot * trade.lots
        trade.exit_index = index
        trade.exit_time = bar.close_time
        trade.exit_price = exit_price
        trade.pnl = gross - commission
        risk = abs(trade.entry - trade.stop_loss)
        trade.r_multiple = (move / risk) if risk > 0 else 0.0
        trade.exit_reason = reason
        return True


def _series(symbol: str, timeframe: str, candles: Sequence[Candle]) -> Series:
    report = ValidationReport(
        timeframe=timeframe,
        accepted=len(candles),
        dropped_forming=0,
        dropped_duplicate=0,
        dropped_invalid=0,
        gaps=0,
        newest_close=candles[-1].close_time if candles else None,
        age_minutes=0.0,
    )
    return Series(symbol=symbol, timeframe=timeframe, candles=tuple(candles), report=report)
