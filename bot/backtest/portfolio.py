"""Backtesting the system the bot actually runs.

`Backtester` measures one symbol in isolation, and that is the wrong
shape in two ways that compound.

SAMPLE. The live bot scans 23 symbols. A single-symbol backtest sees a
twentieth of the evidence, which is why walk-forward on this strategy
returned "no parameter set produced enough trades" rather than a verdict:
the folds were not small because the windows were short, they were small
because 22 symbols were missing. No amount of tuning fixes a measurement
that cannot reach significance.

BEHAVIOUR. The live bot does not take every setup it finds. It ranks the
whole scan and executes the single best opportunity, under a cap on
concurrent positions. A one-symbol run models neither the selection nor
the competition for slots, so it measures a strategy nobody runs — and it
measures it optimistically, because in isolation every setup gets a slot.

So this holds the money and the position slots, asks each symbol's
`Backtester.propose` for its offer on the same bar, ranks them the way
`Orchestrator._rank` does, and fills what the limits allow. The strategy
itself is untouched: every proposal still comes from the one engine, and
nothing here can approve or size a trade.

What it still does NOT model, and should not be read as modelling:
correlation limits, the daily and session trade caps, news blackouts,
spread rejection at submission time, and the loss cooldown. Each of those
makes the live bot MORE selective than this, so the simulation is
optimistic by construction and its numbers are a ceiling rather than an
estimate.

One more, and it cuts the other way: `structure_invalidated` is always
False here, because recomputing the structural read on every open
position on every bar would double the cost of a run. So the structural
exit never fires in simulation. Live it closes losing trades early, which
means a measured comparison of it against nothing would show no
difference — and reporting that as "the structural exit does not help"
would be a claim about the harness, not about the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..broker.models import InstrumentSpec
from ..config import TradingConfig
from ..marketdata.candles import Candle
from ..scoring.scorer import tier_rank
from ..smc.sessions import is_forex_weekend
from .engine import BacktestCosts, Backtester, BacktestResult, SimulatedTrade


@dataclass(frozen=True, slots=True)
class SymbolData:
    spec: InstrumentSpec
    m15: tuple[Candle, ...]
    h1: tuple[Candle, ...]


class PortfolioBacktester:
    """One account, many symbols, the live selection rule."""

    def __init__(
        self,
        config: TradingConfig,
        *,
        costs: BacktestCosts | None = None,
        starting_balance: float = 10_000.0,
        rate_lookup: Any | None = None,
    ) -> None:
        self.config = config
        self.costs = costs or BacktestCosts()
        self.starting_balance = starting_balance
        #: Needed for crosses; see `Backtester.rate_lookup`. A portfolio
        #: run is exactly where its absence hides, because the symbols it
        #: silently drops are a subset rather than all of them.
        self.rate_lookup = rate_lookup
        self._symbols: dict[str, SymbolData] = {}
        self._engines: dict[str, Backtester] = {}

    def add(self, spec: InstrumentSpec, m15: Sequence[Candle], h1: Sequence[Candle]) -> None:
        self._symbols[spec.symbol] = SymbolData(
            spec=spec,
            m15=tuple(m15),
            h1=tuple(sorted(h1, key=lambda candle: candle.timestamp)),
        )
        self._engines[spec.symbol] = Backtester(
            self.config,
            spec,
            costs=self.costs,
            starting_balance=self.starting_balance,
            rate_lookup=self.rate_lookup,
        )

    # -- the run ---------------------------------------------------------

    def run(self, *, warmup: int = 250, step: int = 1) -> BacktestResult:
        if not self._symbols:
            raise ValueError("no symbols added")

        result = BacktestResult(starting_balance=self.starting_balance)
        balance = self.starting_balance
        peak = balance
        result.equity_curve.append(balance)
        consecutive_losses = 0
        open_trades: dict[str, SimulatedTrade] = {}

        # A shared clock, so every symbol is judged on the same moment.
        # Indices differ per symbol only if their series differ in length;
        # the timeline is taken from the longest and each symbol is walked
        # by its own index into it.
        longest = max(self._symbols.values(), key=lambda data: len(data.m15))
        timeline = [candle.close_time for candle in longest.m15]

        for position, cutoff in enumerate(timeline):
            if position < warmup:
                continue
            result.bars_processed += 1

            # --- manage what is open, on this bar, before anything else ---
            for symbol in list(open_trades):
                data = self._symbols[symbol]
                index = _index_at(data.m15, cutoff)
                if index is None:
                    continue
                trade = open_trades[symbol]
                if self._engines[symbol]._resolve_exit(trade, data.m15[index], index):
                    balance += trade.pnl or 0.0
                    peak = max(peak, balance)
                    consecutive_losses = (
                        consecutive_losses + 1 if (trade.pnl or 0.0) < 0 else 0
                    )
                    result.equity_curve.append(round(balance, 2))
                    open_trades.pop(symbol)

            if position % step != 0 or is_forex_weekend(cutoff):
                continue
            if len(open_trades) >= self.config.risk.max_open_positions:
                result.setups_rejected["position slots full"] = (
                    result.setups_rejected.get("position slots full", 0) + 1
                )
                continue

            # --- every symbol proposes; the best one is taken ---
            proposals: list[SimulatedTrade] = []
            for symbol, data in self._symbols.items():
                if symbol in open_trades:
                    continue
                index = _index_at(data.m15, cutoff)
                if index is None or index < warmup:
                    continue
                visible_h1 = [c for c in data.h1 if c.close_time <= cutoff]
                if len(visible_h1) < 40:
                    continue

                proposal, reason = self._engines[symbol].propose(
                    data.m15[: index + 1],
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
                proposals.append(proposal)

            if not proposals:
                continue

            # Best first: tier, then score. The same order the live
            # orchestrator ranks by, so the simulation takes the trade the
            # bot would have taken rather than the first one it found.
            proposals.sort(key=lambda t: (-tier_rank(t.setup_grade), -t.setup_score))
            taken = proposals[0]
            open_trades[taken.symbol] = taken
            result.trades.append(taken)
            result.setups_rejected["passed over for a better setup"] = (
                result.setups_rejected.get("passed over for a better setup", 0)
                + len(proposals)
                - 1
            )

        return result


def _index_at(candles: Sequence[Candle], cutoff: datetime) -> int | None:
    """The last candle that had CLOSED at `cutoff`.

    Linear from the end would be O(n) per symbol per bar. Bisect on close
    time keeps a 23-symbol, 2,600-bar run from becoming quadratic — and
    the guarantee that matters is unchanged: nothing at or after `cutoff`
    is ever visible (project rule 4).
    """

    low, high = 0, len(candles) - 1
    if high < 0 or candles[0].close_time > cutoff:
        return None
    while low < high:
        middle = (low + high + 1) // 2
        if candles[middle].close_time <= cutoff:
            low = middle
        else:
            high = middle - 1
    return low
