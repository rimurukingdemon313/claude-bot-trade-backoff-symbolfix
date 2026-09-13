"""Position sizing from real broker specifications.

This replaces the single most dangerous piece of the previous build,
which computed `lots = (risk / stop_distance) / 100000` for every FX pair
and `/100` for gold. That formula is only correct when the quote currency
equals the account currency. On USDJPY with a USD account it was wrong by
roughly two orders of magnitude, and it floored the result at 0.01 lots,
which silently exceeded the approved risk on small accounts.

The correct chain is:

    loss_per_lot(account ccy) = stop_distance(price)
                               * contract_size(units per lot)
                               * fx_rate(quote ccy -> account ccy)

    lots = risk_amount / loss_per_lot        (then rounded DOWN to lot step)

Every input comes from the broker. When the conversion rate cannot be
established, sizing FAILS rather than assuming 1.0 — an assumed rate is
an unbounded sizing error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..broker.models import InstrumentSpec
from ..errors import BotError, Category, Severity

#: Callable that returns the price of ONE unit of `base` in `quote`,
#: or None when the pair cannot be priced.
RateLookup = Callable[[str, str], float | None]


class SizingError(BotError):
    severity = Severity.PERMANENT
    category = Category.EXECUTION


@dataclass(frozen=True, slots=True)
class PositionSize:
    lots: float
    units: float
    risk_amount: float
    actual_risk: float
    loss_per_lot: float
    conversion_rate: float
    margin_estimate: float | None
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lots": self.lots,
            "units": self.units,
            "riskAmount": round(self.risk_amount, 2),
            "actualRisk": round(self.actual_risk, 2),
            "lossPerLot": round(self.loss_per_lot, 4),
            "conversionRate": round(self.conversion_rate, 6),
            "marginEstimate": round(self.margin_estimate, 2) if self.margin_estimate else None,
            "notes": list(self.notes),
        }


def conversion_rate(
    spec: InstrumentSpec, entry_price: float, rate_lookup: RateLookup | None = None
) -> tuple[float, str]:
    """Value of one unit of the QUOTE currency in the ACCOUNT currency.

    Three cases, in order of reliability:
      1. quote == account            -> 1.0, exact.
      2. base == account             -> 1/entry_price, exact from this
                                        instrument's own price (e.g. a USD
                                        account trading USDJPY).
      3. anything else (a cross)     -> ask the broker for the bridging
                                        pair. If it cannot be priced, this
                                        raises instead of guessing.
    """

    account = (spec.account_currency or "").upper()
    quote = (spec.quote_currency or "").upper()
    base = (spec.base_currency or "").upper()

    if not account:
        raise SizingError("account currency is unknown — cannot convert risk into lots")
    if quote and quote == account:
        return 1.0, f"quote currency {quote} equals account currency"
    if base and base == account:
        if entry_price <= 0:
            raise SizingError("entry price must be positive to invert the conversion rate")
        return 1.0 / entry_price, f"inverted {base}{quote} price {entry_price}"

    if not quote:
        raise SizingError(
            f"cannot determine the quote currency of {spec.broker_name!r}; "
            "refusing to size a position from an assumed rate"
        )
    if rate_lookup is None:
        raise SizingError(
            f"{spec.symbol} is quoted in {quote} but the account is in {account}, "
            "and no conversion-rate source was provided"
        )

    direct = rate_lookup(quote, account)
    if direct and direct > 0:
        return direct, f"broker quote {quote}{account}={direct}"
    inverse = rate_lookup(account, quote)
    if inverse and inverse > 0:
        return 1.0 / inverse, f"inverted broker quote {account}{quote}={inverse}"

    raise SizingError(
        f"no broker price available to convert {quote} into {account} for {spec.symbol}; "
        "this symbol will be skipped rather than sized from a guessed rate"
    )


def calculate_position_size(
    *,
    spec: InstrumentSpec,
    risk_amount: float,
    entry: float,
    stop_loss: float,
    rate_lookup: RateLookup | None = None,
    leverage: float | None = None,
    available_margin: float | None = None,
) -> PositionSize:
    """Lots that risk `risk_amount` (account currency) if the stop is hit."""

    notes: list[str] = []
    if risk_amount <= 0:
        raise SizingError(f"risk amount must be positive, got {risk_amount}")
    if entry <= 0 or stop_loss <= 0:
        raise SizingError("entry and stop loss must both be positive prices")

    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        raise SizingError("stop distance is zero — the stop must be away from the entry")
    if spec.contract_size <= 0:
        raise SizingError(f"{spec.symbol} has no usable contract size")

    rate, rate_note = conversion_rate(spec, entry, rate_lookup)
    notes.append(rate_note)

    loss_per_lot = stop_distance * spec.contract_size * rate
    if loss_per_lot <= 0:
        raise SizingError("computed loss per lot is not positive — refusing to size")

    raw_lots = risk_amount / loss_per_lot
    lots = spec.round_lot(raw_lots)
    notes.append(f"raw {raw_lots:.4f} lots rounded down to {lots} (step {spec.lot_step})")

    if lots < spec.min_lot:
        # Rounding UP to the minimum would risk more than approved. The
        # only correct answer is to decline the trade.
        raise SizingError(
            f"risking ${risk_amount:.2f} on a {stop_distance:.5f} stop needs {raw_lots:.4f} lots, "
            f"below the broker minimum of {spec.min_lot}. Increasing size to reach the minimum "
            "would exceed the approved risk, so this trade is declined."
        )
    if lots > spec.max_lot:
        lots = spec.round_lot(spec.max_lot)
        notes.append(f"capped at broker maximum {spec.max_lot} lots")

    actual_risk = lots * loss_per_lot
    units = lots * spec.contract_size

    margin_estimate = None
    if leverage and leverage > 0:
        notional_account_ccy = units * entry * rate
        margin_estimate = notional_account_ccy / leverage
        if available_margin is not None and margin_estimate > available_margin * 0.5:
            raise SizingError(
                f"estimated margin ${margin_estimate:.2f} exceeds half of the available "
                f"${available_margin:.2f} — refusing to commit that much of the account to one trade"
            )

    return PositionSize(
        lots=lots,
        units=units,
        risk_amount=risk_amount,
        actual_risk=actual_risk,
        loss_per_lot=loss_per_lot,
        conversion_rate=rate,
        margin_estimate=margin_estimate,
        notes=tuple(notes),
    )


def expected_profit(
    *,
    spec: InstrumentSpec,
    lots: float,
    entry: float,
    take_profit: float,
    conversion: float,
) -> float:
    """Profit in the account currency if the target is reached exactly."""

    distance = abs(take_profit - entry)
    return distance * spec.contract_size * conversion * lots
