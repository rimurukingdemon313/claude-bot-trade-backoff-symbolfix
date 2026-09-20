"""Performance statistics computed honestly from closed trades.

Two rules (MASTER_MISSION §50/§53):

* nothing is fabricated — a metric with no data returns None, never 0.0
  dressed up as a result;
* small samples are labelled. A 100% win rate over two trades is reported
  with `sample: "insufficient"` so the dashboard can show it as what it
  is rather than as evidence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from ..clock import ensure_utc, utc_now

MIN_MEANINGFUL_SAMPLE = 20


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Performance:
    trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    average_win: float | None
    average_loss: float | None
    average_r: float | None
    total_pnl: float
    max_drawdown: float | None
    current_drawdown: float | None
    consecutive_wins: int
    consecutive_losses: int
    daily_pnl: float
    weekly_pnl: float
    monthly_pnl: float
    sample: str
    #: Closed trades left OUT of every figure above because the broker
    #: never reported a result for them. Dropping them is right — a
    #: fabricated zero would move the win rate and the drawdown — but
    #: dropping them SILENTLY makes `trades` disagree with the trade
    #: list on the same page, which reads as a bug rather than a gap.
    unpriced: int = 0

    def as_dict(self) -> dict[str, Any]:
        def rounded(value: float | None, places: int = 2) -> float | None:
            return None if value is None else round(value, places)

        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "winRate": rounded(self.win_rate, 4),
            "profitFactor": rounded(self.profit_factor, 3),
            "expectancy": rounded(self.expectancy),
            "averageWin": rounded(self.average_win),
            "averageLoss": rounded(self.average_loss),
            "averageR": rounded(self.average_r, 3),
            "totalPnl": rounded(self.total_pnl),
            "maxDrawdown": rounded(self.max_drawdown),
            "currentDrawdown": rounded(self.current_drawdown),
            "consecutiveWins": self.consecutive_wins,
            "consecutiveLosses": self.consecutive_losses,
            "dailyPnl": rounded(self.daily_pnl),
            "weeklyPnl": rounded(self.weekly_pnl),
            "monthlyPnl": rounded(self.monthly_pnl),
            "sample": self.sample,
            "unpriced": self.unpriced,
        }


def compute_performance(
    closed_trades: Sequence[dict[str, Any]], *, now: datetime | None = None
) -> Performance:
    moment = now or utc_now()
    trades = [
        trade
        for trade in closed_trades
        if trade.get("realized_pnl") is not None
    ]
    ordered = sorted(trades, key=lambda trade: str(trade.get("closed_at") or ""))

    pnls = [float(trade["realized_pnl"]) for trade in ordered]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    breakeven = len([value for value in pnls if value == 0])

    total = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    # Running equity curve from the trade sequence, for drawdown.
    peak = 0.0
    equity = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    current_drawdown = max(0.0, peak - equity)

    streak_wins = streak_losses = 0
    best_wins = best_losses = 0
    for value in pnls:
        if value > 0:
            streak_wins += 1
            streak_losses = 0
        elif value < 0:
            streak_losses += 1
            streak_wins = 0
        else:
            streak_wins = streak_losses = 0
        best_wins = max(best_wins, streak_wins)
        best_losses = max(best_losses, streak_losses)

    r_values = [
        float(trade["r_multiple"])
        for trade in ordered
        if trade.get("r_multiple") is not None
    ]

    def window_sum(days: int) -> float:
        cutoff = moment - timedelta(days=days)
        return sum(
            float(trade["realized_pnl"])
            for trade in ordered
            if (_parse(trade.get("closed_at")) or moment) >= cutoff
        )

    count = len(ordered)
    sample = "insufficient" if count < MIN_MEANINGFUL_SAMPLE else "adequate"

    return Performance(
        trades=count,
        wins=len(wins),
        losses=len(losses),
        breakeven=breakeven,
        win_rate=(len(wins) / count) if count else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        expectancy=(total / count) if count else None,
        average_win=(gross_win / len(wins)) if wins else None,
        average_loss=(-gross_loss / len(losses)) if losses else None,
        average_r=(sum(r_values) / len(r_values)) if r_values else None,
        total_pnl=total,
        max_drawdown=max_drawdown if count else None,
        current_drawdown=current_drawdown if count else None,
        consecutive_wins=best_wins,
        consecutive_losses=best_losses,
        daily_pnl=window_sum(1),
        weekly_pnl=window_sum(7),
        monthly_pnl=window_sum(30),
        sample=sample,
        unpriced=len(closed_trades) - count,
    )


def breakdown(
    closed_trades: Sequence[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    """Group performance by an attribute (symbol, grade, session, version).

    This is how MASTER_MISSION §93's questions get answered with measured
    data instead of assumption.
    """

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in closed_trades:
        if trade.get("realized_pnl") is None:
            continue
        if key in trade:
            value = trade.get(key)
        else:
            context = trade.get("context") or {}
            value = context.get(key)
        groups[str(value or "unknown")].append(trade)

    rows = []
    for name, items in groups.items():
        pnls = [float(item["realized_pnl"]) for item in items]
        wins = len([value for value in pnls if value > 0])
        gross_win = sum(value for value in pnls if value > 0)
        gross_loss = abs(sum(value for value in pnls if value < 0))
        rows.append(
            {
                "key": name,
                "trades": len(items),
                "winRate": round(wins / len(items), 4) if items else None,
                "totalPnl": round(sum(pnls), 2),
                "expectancy": round(sum(pnls) / len(items), 2) if items else None,
                "profitFactor": round(gross_win / gross_loss, 3) if gross_loss else None,
                "sample": "insufficient" if len(items) < MIN_MEANINGFUL_SAMPLE else "adequate",
            }
        )
    rows.sort(key=lambda row: row["trades"], reverse=True)
    return rows
