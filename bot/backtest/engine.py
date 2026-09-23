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

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..broker.models import InstrumentSpec
from ..config import TradingConfig
from ..marketdata.candles import Candle
from ..marketdata.provider import Series, live_lookback
from ..marketdata.validation import ValidationReport
from ..execution.manager import plan_actions
from ..risk.engine import AccountRiskState, RiskEngine
from ..risk.reward import evaluate_reward
from ..marketdata.validation import validate_spread
from ..risk.sizing import (
    RateLookup,
    SizingError,
    calculate_position_size,
    expected_profit,
)
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
    #: How the MTF layer classified this setup, and whether its target
    #: was a measured level or a projection. Recorded because "which
    #: classification loses money" is the question a backtest exists to
    #: answer, and averaging a reversal's results with a continuation's
    #: describes neither (project rule 14, applied to the harness).
    setup_type: str = ""
    projected_target: bool = False
    #: The stop the trade was OPENED with. `stop_loss` moves with
    #: management; R must keep being measured against the original risk or
    #: a break-even stop would report every trade as infinite R.
    initial_stop: float = 0.0
    #: Lots still open. Falls below `lots` once a partial is taken.
    open_lots: float = 0.0
    #: Account-currency profit already realised by partial closes.
    banked_pnl: float = 0.0
    partial_taken: bool = False
    #: Management steps applied, for the record.
    managed: tuple[str, ...] = ()
    #: Value of one unit of the QUOTE currency in the ACCOUNT currency.
    #:
    #: Carried on the trade because P/L needs it and the sizer already
    #: had it. Without it `gross` was quote-currency units reported as
    #: account currency — correct for a USD-quoted pair where the rate is
    #: 1.0, and wrong by a factor of ~150 for anything quoted in JPY. The
    #: sizer applied the rate, so RISK was converted and RESULT was not:
    #: every JPY cross booked its wins and losses about 150x too large.
    conversion_rate: float = 1.0
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    exit_reason: str | None = None
    mfe: float = 0.0
    mae: float = 0.0

    def __post_init__(self) -> None:
        # A trade with lots but nothing open is nonsense, and a zero
        # `initial_stop` would make every R infinite. Both default to the
        # obvious thing rather than to zero, so a hand-built trade behaves
        # like one the engine produced.
        if self.open_lots <= 0:
            self.open_lots = self.lots
        if self.initial_stop <= 0:
            self.initial_stop = self.stop_loss

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
            "conversionRate": round(self.conversion_rate, 6),
            "partialTaken": self.partial_taken,
            "managed": list(self.managed),
            "setupType": self.setup_type,
            "projectedTarget": self.projected_target,
            "setupGrade": self.setup_grade,
            "setupScore": self.setup_score,
            "exitTime": self.exit_time.isoformat() if self.exit_time else None,
            "exitPrice": round(self.exit_price, 6) if self.exit_price else None,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
            "rMultiple": round(self.r_multiple, 3) if self.r_multiple is not None else None,
            "exitReason": self.exit_reason,
        }


@dataclass
class _SimPosition:
    """What `plan_actions` reads off an open position.

    A view, not a copy of the logic. The backtest drives the SAME function
    the live manager drives, because a simulated break-even that behaved
    differently from the real one would make the whole exercise a
    measurement of a system nobody runs — which is exactly the fault this
    harness was rebuilt to remove.
    """

    position_id: str
    symbol: str
    direction: str
    quantity: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: datetime


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
        rate_lookup: RateLookup | None = None,
        halt_on_max_drawdown: bool = True,
    ) -> None:
        self.config = config
        self.spec = spec
        #: Whether reaching `max_drawdown_pct` stops the run for good, as
        #: the kill switch does live. On by default: that IS what the bot
        #: does.
        #:
        #: Off is for MEASURING, never for simulating the bot. With it on,
        #: a long history answers only "when would it have stopped itself?"
        #: — EURUSD reached the limit after 164 trades and the remaining
        #: years contributed nothing but 3,425 "max drawdown" rejections.
        #: That is a real and useful answer, and it is not "does the
        #: strategy have an edge", which needs the whole history.
        #:
        #: Only the halt is lifted. The drawdown de-risking inside the risk
        #: engine is untouched, so a run with this off follows exactly the
        #: same path as one with it on up to the bar where the halt would
        #: have fired — which is how a single run can report both answers.
        self.halt_on_max_drawdown = halt_on_max_drawdown
        self.costs = costs or BacktestCosts()
        self.starting_balance = starting_balance
        #: How a quote currency converts into the account currency.
        #:
        #: Required for any CROSS - EURGBP or GBPJPY on a USD account.
        #: `conversion_rate` raises rather than guess a rate, which is
        #: right, so without this every cross was rejected at "sizing" and
        #: a twelve-symbol run measured the four quoted in the account
        #: currency while reporting twelve. Leaving it None keeps that
        #: behaviour, and now it is a choice rather than an oversight.
        self.rate_lookup = rate_lookup
        self.smc = SmcEngine(config)
        self.scorer = SetupScorer(config)
        self.risk = RiskEngine(config)

    def _modelled_spread(self) -> float | None:
        """The cost model's spread, converted from ticks to price units.

        The strategy pads its stop by the spread and the fill model
        charges one. Handing those two different numbers would simulate a
        bot that does not exist, so they come from the same place.
        """

        tick = self.spec.tick_size or 0.0
        if tick <= 0 or self.costs.spread_points <= 0:
            return None
        return self.costs.spread_points * tick

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
        # Close times in order, so "every H1 bar closed by the cutoff" is a
        # binary search rather than a scan of the whole series per bar.
        h1_close_times = [candle.close_time for candle in h1_by_time]
        m15_window = live_lookback("M15")
        h1_window = live_lookback("H1")

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
            #
            # And only as MANY of them as the live bot sees. The upper
            # bound is the look-ahead guarantee (unchanged: nothing after
            # bar i is ever in the list). The lower bound is fidelity: the
            # live provider hands the engine the last `live_lookback` bars
            # per timeframe, and every structure the engine builds —
            # swings, dealing range, liquidity pools — depends on the
            # window it is given. See LIVE_LOOKBACK for what feeding it the
            # full history used to do.
            visible_m15 = list(m15[max(0, index + 1 - m15_window): index + 1])
            cutoff = bar.close_time
            h1_end = bisect_right(h1_close_times, cutoff)
            visible_h1 = h1_by_time[max(0, h1_end - h1_window): h1_end]
            if len(visible_h1) < 40:
                continue

            proposal, reason = self.propose(
                visible_m15,
                visible_h1,
                index=index,
                cutoff=cutoff,
                balance=balance,
                peak=peak,
                consecutive_losses=consecutive_losses,
            )
            if proposal is None:
                result.setups_rejected[reason] = result.setups_rejected.get(reason, 0) + 1
                continue
            result.setups_considered += 1
            open_trade = proposal
            result.trades.append(open_trade)

        return result

    def propose(
        self,
        visible_m15: Sequence[Candle],
        visible_h1: Sequence[Candle],
        *,
        index: int,
        cutoff: datetime,
        balance: float,
        peak: float,
        consecutive_losses: int,
    ) -> tuple[SimulatedTrade | None, str]:
        """One symbol's proposal at one moment, sized and costed.

        Extracted from `run` so the PORTFOLIO backtester can call it for
        every symbol on the same bar and then rank the results, exactly as
        `Orchestrator._scan` does live. Duplicating this body there would
        have been a second implementation of the strategy, drifting from
        this one the first time either changed (project rule 2's spirit,
        applied to the harness).

        Returns `(trade, "")` or `(None, reason)`; the reason is the
        bucket the rejection is counted under.
        """

        series = {
            "M15": _series(self.spec.symbol, "M15", list(visible_m15)),
            "H1": _series(self.spec.symbol, "H1", list(visible_h1)),
        }
        # The same spread the fill model charges is the spread the stop
        # is padded by. Handing the strategy a different number here than
        # the one the simulation costs would measure a bot that does not
        # exist.
        analysis = self.smc.analyze(
            self.spec.symbol, series, now=cutoff, spread=self._modelled_spread()
        )
        if analysis.candidate is None:
            return None, (analysis.rejection or "unknown").split(":")[0][:60]

        candidate = analysis.candidate
        score = self.scorer.score(candidate)
        if not score.tradeable:
            return None, "below tier"

        drawdown = (peak - balance) / peak if peak > 0 else 0.0
        if self.halt_on_max_drawdown and drawdown >= self.config.risk.max_drawdown_pct:
            return None, "max drawdown"

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
                rate_lookup=self.rate_lookup,
            )
        except SizingError:
            # Without a rate source this is where every CROSS-currency
            # symbol died - EURGBP, GBPJPY, AUDJPY and the rest all raise
            # in `conversion_rate`, correctly, rather than size from a
            # guessed rate. The backtester never supplied one, so a
            # 12-symbol run silently measured only the four quoted in the
            # account currency and reported it as twelve.
            return None, "sizing"

        profit = expected_profit(
            spec=self.spec,
            lots=size.lots,
            entry=entry,
            take_profit=candidate.take_profit,
            conversion=size.conversion_rate,
        )
        # The same objective the live risk engine applies, in R. A
        # backtest filtering on dollars while production filtered on
        # ratio would be measuring a different system.
        reward = evaluate_reward(
            risk_reward=candidate.risk_reward,
            expected_profit=profit,
            config=self.config.reward,
        )
        if not reward.meets_objective:
            return None, "reward objective"

        # The execution spread guard, which the backtest did not have.
        #
        # This harness PAID the spread on entry and then never asked the
        # question the live executor asks last: is this spread too large
        # relative to THIS setup's stop and target? So it counted trades
        # the bot would refuse at submission, and a backtest reporting
        # 180 trades described a live system that took far fewer. That is
        # not a small discrepancy — it is the difference between "the
        # strategy is too selective" and "the strategy proposes setups it
        # cannot execute", which are opposite problems with opposite
        # fixes.
        #
        # Same function, same config as `Executor._spread_check`, so the
        # two cannot drift.
        ok, spread_reason = validate_spread(
            spread=spread,
            atr=candidate.atr,
            stop_distance=abs(entry - candidate.stop_loss),
            take_profit_distance=abs(candidate.take_profit - entry),
            max_spread_atr_fraction=self.config.execution.max_spread_atr_fraction,
            max_spread_tp_fraction=self.config.execution.max_spread_tp_fraction,
        )
        if not ok:
            return None, "spread guard"

        return (
            SimulatedTrade(
                symbol=self.spec.symbol,
                direction=candidate.direction,
                entry_index=index,
                entry_time=cutoff,
                entry=entry,
                stop_loss=candidate.stop_loss,
                take_profit=candidate.take_profit,
                lots=size.lots,
                open_lots=size.lots,
                initial_stop=candidate.stop_loss,
                risk_amount=size.actual_risk,
                setup_grade=score.tier,
                setup_score=round(score.total, 2),
                setup_type=candidate.setup_type,
                projected_target=bool(
                    (candidate.liquidity_target or {}).get("projected")
                ),
                conversion_rate=size.conversion_rate,
            ),
            "",
        )

    def _manage(self, trade: SimulatedTrade, bar: Candle, index: int) -> None:
        """Apply position management for this bar, one poll behind.

        Called AFTER the exit check, so a break-even stop can never
        retroactively rescue a trade the same bar stopped out. That
        matches the live cadence — management runs on a timer, not
        intrabar — and it is the pessimistic direction, which is the only
        safe one for a simulator.

        The price handed to `plan_actions` is the bar's FAVOURABLE
        extreme, because that is the best reading a poll during this bar
        could have seen. A management step is an improvement to an
        existing position, so using the favourable extreme cannot invent a
        profit: it can only move a stop that the next bar still has to
        trade through, or bank a partial at a level price genuinely
        reached.
        """

        if trade.open_lots <= 0 or trade.exit_index is not None:
            return

        price = bar.high if trade.direction == "BUY" else bar.low
        position = _SimPosition(
            position_id=f"sim-{id(trade)}",
            symbol=trade.symbol,
            direction=trade.direction,
            quantity=trade.open_lots,
            entry_price=trade.entry,
            stop_loss=trade.stop_loss,
            take_profit=trade.take_profit,
            opened_at=trade.entry_time,
        )
        actions = plan_actions(
            position=position,
            trade={
                "actual_entry": trade.entry,
                "stop_loss": trade.initial_stop,
                "partial_taken": trade.partial_taken,
            },
            price=price,
            config=self.config,
            now=bar.close_time,
        )

        slippage = self.costs.slippage_points * self.spec.tick_size
        for action in actions:
            if action.kind == "MOVE_STOP" and action.stop_loss is not None:
                trade.stop_loss = action.stop_loss
                trade.managed += (action.reason,)
            elif action.kind == "PARTIAL_CLOSE" and not trade.partial_taken:
                closing = min(trade.open_lots, float(action.quantity or 0.0))
                if closing <= 0:
                    continue
                # Filled at the trigger level, paying slippage — not at
                # the bar extreme, which a poll would rarely catch.
                level = trade.entry + (
                    (trade.entry - trade.initial_stop) * self.config.execution.partial_tp_at_r
                )
                if trade.direction == "SELL":
                    level = trade.entry - (
                        (trade.initial_stop - trade.entry)
                        * self.config.execution.partial_tp_at_r
                    )
                fill = level - slippage if trade.direction == "BUY" else level + slippage
                move = fill - trade.entry if trade.direction == "BUY" else trade.entry - fill
                trade.banked_pnl += (
                    move * self.spec.contract_size * closing * trade.conversion_rate
                    - self.costs.commission_per_lot * closing
                )
                trade.open_lots = round(trade.open_lots - closing, 6)
                trade.partial_taken = True
                trade.managed += (action.reason,)
            elif action.kind == "CLOSE":
                self._close(trade, bar, index, bar.close, action.reason)
                return

    def _resolve_exit(self, trade: SimulatedTrade, bar: Candle, index: int) -> bool:
        """Did this bar close the trade? Pessimistic on ambiguity."""

        if index <= trade.entry_index or trade.exit_index is not None:
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
            self._manage(trade, bar, index)
            return trade.exit_index is not None

        slippage = self.costs.slippage_points * self.spec.tick_size
        if hit_stop:
            # Both touched in one bar -> assume the stop. Intrabar sequence
            # is unknowable, and optimism here is what makes a backtest lie.
            exit_price = (
                trade.stop_loss - slippage
                if trade.direction == "BUY"
                else trade.stop_loss + slippage
            )
            reason = "STOP_AND_TARGET_SAME_BAR" if hit_target else "STOP"
        else:
            exit_price = trade.take_profit
            reason = "TARGET"

        self._close(trade, bar, index, exit_price, reason)
        return True

    def _close(
        self,
        trade: SimulatedTrade,
        bar: Candle,
        index: int,
        exit_price: float,
        reason: str,
    ) -> None:
        """Book the remaining lots and finish the trade."""

        move = (
            exit_price - trade.entry if trade.direction == "BUY" else trade.entry - exit_price
        )
        # The same conversion the sizer used. `move` is in the quote
        # currency; the account is not necessarily quoted in it.
        gross = move * self.spec.contract_size * trade.open_lots * trade.conversion_rate
        commission = self.costs.commission_per_lot * trade.open_lots
        trade.exit_index = index
        trade.exit_time = bar.close_time
        trade.exit_price = exit_price
        trade.pnl = trade.banked_pnl + gross - commission
        # R against the money actually risked, not against a price ratio.
        # With partials the two stop agreeing, and the money is the one
        # that means anything once part of the position is already closed.
        trade.r_multiple = (
            (trade.pnl / trade.risk_amount) if trade.risk_amount > 0 else 0.0
        )
        trade.exit_reason = reason
        trade.open_lots = 0.0


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
