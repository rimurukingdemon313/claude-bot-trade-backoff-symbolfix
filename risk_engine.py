"""Strict, analysis-only risk gate for the paper-trading loop.

This module deliberately has no broker or order-routing dependencies. It
accepts an AI proposal plus the current paper-account snapshot and returns a
decision that the caller must honor before logging an open paper trade.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from economic_calendar import news_blackout_for_symbol


RISK_PER_TRADE = 0.02
MAX_OPEN_POSITIONS_PER_SYMBOL = 1
MAX_TOTAL_OPEN_POSITIONS = 6
MAX_DAILY_LOSS_PCT = 0.12
MIN_RISK_REWARD = 2.0
MIN_CONFIDENCE = 75


def is_forex_market_closed(now: datetime | None = None) -> bool:
    """True from Friday 22:00 UTC to Sunday 22:00 UTC (the FX weekend).

    Spot forex trades nearly 24/5 across overlapping global sessions, closing
    only for the weekend. Friday 22:00 UTC / Sunday 22:00 UTC is the
    standard approximation (5pm New York close, 5pm New York reopen via the
    Sydney session) used industry-wide; it can drift by an hour around
    daylight-saving transitions, which is an acceptable margin for a paper
    trading safety gate.
    """

    reference = now or datetime.now(timezone.utc)
    weekday = reference.weekday()  # Monday=0 ... Sunday=6
    hour = reference.hour
    if weekday == 4 and hour >= 22:  # Friday from 22:00 UTC
        return True
    if weekday == 5:  # all of Saturday
        return True
    if weekday == 6 and hour < 22:  # Sunday before 22:00 UTC
        return True
    return False


def risk_amount_for_balance(balance: float) -> float:
    """2% of the current paper balance, rounded to cents."""

    return round(max(balance, 0.0) * RISK_PER_TRADE, 2)


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """The minimum trade shape the AI must provide for risk review."""

    decision: str
    entry_price: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_reward_ratio: float | None
    risk_amount: float | None
    candle_range: float | None = None
    average_range: float | None = None
    confidence: int | None = None
    symbol: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TradeProposal":
        def number(name: str) -> float | None:
            raw = value.get(name)
            if raw is None or raw == "":
                return None
            try:
                result = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            return result if math.isfinite(result) else None

        confidence_raw = value.get("confidence")
        confidence = None
        if confidence_raw is not None and confidence_raw != "":
            try:
                confidence = int(round(float(confidence_raw)))
            except (TypeError, ValueError) as exc:
                raise ValueError("confidence must be numeric") from exc

        symbol_raw = value.get("symbol")
        symbol = str(symbol_raw).upper() if symbol_raw else None

        return cls(
            decision=str(value.get("decision", "NO TRADE")).upper(),
            entry_price=number("entry_price"),
            stop_loss=number("stop_loss"),
            take_profit=number("take_profit"),
            risk_reward_ratio=number("risk_reward_ratio"),
            risk_amount=number("risk_amount"),
            candle_range=number("candle_range"),
            average_range=number("average_range"),
            confidence=confidence,
            symbol=symbol,
        )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Current paper-account values used by the risk gate."""

    balance: float
    daily_pnl: float
    open_positions: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AccountSnapshot":
        if "balance" not in value or value.get("balance") in (None, ""):
            raise ValueError(
                "account snapshot is missing 'balance' — refusing to substitute a "
                "fake default; the caller must supply the real live account balance"
            )
        try:
            balance = float(value["balance"])
            daily_pnl = float(value.get("daily_pnl", 0.0))
            open_positions = int(value.get("open_positions", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("account snapshot contains invalid numeric values") from exc
        if not math.isfinite(balance) or not math.isfinite(daily_pnl):
            raise ValueError("account snapshot values must be finite")
        return cls(balance, daily_pnl, open_positions)


VOLATILITY_SPIKE_MULTIPLE = 3.0


def is_volatility_spike(candle_range: float | None, average_range: float | None) -> bool:
    """Flag a candle whose range is a multi-x outlier vs recent average range.

    There is no live economic-calendar feed wired in, so this is the
    practical proxy: high-impact news (NFP, rate decisions, CPI) reliably
    produces an M15 candle several times wider than the recent average. When
    that fires, the engine stands aside rather than trading into the spike.
    """

    if candle_range is None or average_range is None or average_range <= 0:
        return False
    return candle_range >= average_range * VOLATILITY_SPIKE_MULTIPLE


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The only output a trading loop needs from the risk engine."""

    approved: bool
    state: str
    reasons: tuple[str, ...]
    rules: dict[str, float | int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "state": self.state,
            "reasons": list(self.reasons),
            "rules": self.rules,
        }


class RiskEngine:
    """Apply every paper-trading rule with no AI override path."""

    def evaluate(
        self,
        proposal: TradeProposal | Mapping[str, Any],
        account: AccountSnapshot | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> RiskDecision:
        candidate = (
            proposal
            if isinstance(proposal, TradeProposal)
            else TradeProposal.from_mapping(proposal)
        )
        snapshot = (
            account
            if isinstance(account, AccountSnapshot)
            else AccountSnapshot.from_mapping(account)
        )
        reasons: list[str] = []

        if candidate.decision not in {"BUY", "SELL"}:
            reasons.append("AI decision is NO TRADE")
        if candidate.decision in {"BUY", "SELL"} and (
            candidate.confidence is None or candidate.confidence < MIN_CONFIDENCE
        ):
            shown = "missing" if candidate.confidence is None else f"{candidate.confidence}%"
            reasons.append(
                f"Confidence {shown} is below the required {MIN_CONFIDENCE}% minimum"
            )
        if snapshot.balance <= 0:
            reasons.append("Account balance must be positive")
        if snapshot.open_positions >= MAX_OPEN_POSITIONS_PER_SYMBOL:
            reasons.append(
                f"Open position limit reached for this symbol "
                f"({MAX_OPEN_POSITIONS_PER_SYMBOL} maximum)"
            )
        max_daily_loss = round(snapshot.balance * MAX_DAILY_LOSS_PCT, 2)
        if snapshot.daily_pnl <= -max_daily_loss:
            reasons.append(
                f"Daily Loss Exceeded: {abs(snapshot.daily_pnl):.2f} is at or above "
                f"the ${max_daily_loss:.2f} limit ({MAX_DAILY_LOSS_PCT * 100:g}% of balance)"
            )
        if is_forex_market_closed(now):
            reasons.append(
                "Forex market is closed for the weekend (Fri 22:00 UTC – "
                "Sun 22:00 UTC); standing aside"
            )
        if is_volatility_spike(candidate.candle_range, candidate.average_range):
            reasons.append(
                "Volatility spike detected on the latest candle (possible high-impact "
                f"news); range is {VOLATILITY_SPIKE_MULTIPLE:g}x+ the recent average, "
                "standing aside"
            )
        if candidate.symbol:
            blackout_events = news_blackout_for_symbol(candidate.symbol)
            if blackout_events:
                titles = ", ".join(
                    f"{event.get('country')} {event.get('title')}" for event in blackout_events
                )
                reasons.append(
                    f"High-impact news blackout active for {candidate.symbol}: {titles}"
                )

        if candidate.decision in {"BUY", "SELL"}:
            expected_risk = risk_amount_for_balance(snapshot.balance)
            if candidate.risk_amount is None:
                reasons.append(
                    f"Risk amount is missing; expected ${expected_risk:.2f} "
                    f"({RISK_PER_TRADE * 100:g}% of balance)"
                )
            elif not math.isclose(candidate.risk_amount, expected_risk, abs_tol=0.01):
                reasons.append(
                    f"Risk amount ${candidate.risk_amount:.2f} does not match the "
                    f"required ${expected_risk:.2f} ({RISK_PER_TRADE * 100:g}% of balance)"
                )

            if candidate.risk_reward_ratio is None:
                reasons.append("Risk/Reward ratio is missing; minimum is 1:2")
            elif candidate.risk_reward_ratio < MIN_RISK_REWARD:
                reasons.append(
                    f"Rejected: RR 1:{candidate.risk_reward_ratio:g} is below min 1:2"
                )

            if (
                candidate.entry_price is None
                or candidate.stop_loss is None
                or candidate.take_profit is None
            ):
                reasons.append(
                    "Entry, stop loss, and take profit are required for risk review"
                )
            elif candidate.decision == "BUY" and not (
                candidate.stop_loss < candidate.entry_price < candidate.take_profit
            ):
                reasons.append(
                    "BUY levels invalid: stop loss < entry < take profit is required"
                )
            elif candidate.decision == "SELL" and not (
                candidate.take_profit < candidate.entry_price < candidate.stop_loss
            ):
                reasons.append(
                    "SELL levels invalid: take profit < entry < stop loss is required"
                )

        rules = {
            "account_balance": snapshot.balance,
            "risk_per_trade": RISK_PER_TRADE,
            "risk_amount": risk_amount_for_balance(snapshot.balance),
            "max_open_positions_per_symbol": MAX_OPEN_POSITIONS_PER_SYMBOL,
            "max_total_open_positions": MAX_TOTAL_OPEN_POSITIONS,
            "max_daily_loss": round(snapshot.balance * MAX_DAILY_LOSS_PCT, 2),
            "min_risk_reward": MIN_RISK_REWARD,
            "min_confidence": MIN_CONFIDENCE,
        }
        return RiskDecision(
            approved=not reasons,
            state=candidate.decision if not reasons else "NO TRADE",
            reasons=tuple(reasons) if reasons else ("All risk rules passed",),
            rules=rules,
        )


def _main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 risk_engine.py '<json payload>'")
    payload = json.loads(sys.argv[1])
    decision = RiskEngine().evaluate(payload["proposal"], payload["account"])
    print(json.dumps(decision.to_dict()))


if __name__ == "__main__":
    _main()
