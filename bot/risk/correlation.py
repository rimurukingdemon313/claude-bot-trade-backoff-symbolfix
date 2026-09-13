"""Portfolio correlation.

Two EUR-long positions are one bigger EUR position. Treating them as
independent is how a "1% per trade" system takes a 3% hit on a single ECB
headline (MASTER_MISSION §38).

Correlation is derived from currency exposure rather than from a rolling
price correlation matrix. That is a deliberate trade-off: exposure is
exact, needs no history, cannot be distorted by a quiet sample window,
and is the mechanism by which correlated FX pairs actually move together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..broker.tradelocker import split_currencies

#: Non-FX instruments that behave like a currency bloc for exposure
#: purposes. Gold is priced in USD and trades as an anti-dollar asset.
SYNTHETIC_EXPOSURE = {
    "XAUUSD": (("XAU", 1.0), ("USD", -1.0)),
    "XAGUSD": (("XAG", 1.0), ("USD", -1.0)),
}


def currency_exposure(symbol: str, direction: str) -> dict[str, float]:
    """Signed exposure per currency for one unit of risk.

    A long EURUSD is +1 EUR and -1 USD. A short is the inverse.
    """

    sign = 1.0 if direction.upper() == "BUY" else -1.0
    upper = symbol.upper()
    if upper in SYNTHETIC_EXPOSURE:
        return {currency: weight * sign for currency, weight in SYNTHETIC_EXPOSURE[upper]}
    base, quote = split_currencies(upper)
    if not base or not quote:
        return {}
    return {base: sign, quote: -sign}


def correlation_score(symbol_a: str, direction_a: str, symbol_b: str, direction_b: str) -> float:
    """-1..1 overlap between two positions' currency exposure.

    +1 means the two positions express the same view; -1 means they hedge.
    """

    a = currency_exposure(symbol_a, direction_a)
    b = currency_exposure(symbol_b, direction_b)
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[currency] * b[currency] for currency in shared)
    norm_a = sum(value * value for value in a.values()) ** 0.5
    norm_b = sum(value * value for value in b.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


@dataclass(frozen=True, slots=True)
class ExposureReport:
    per_currency: dict[str, float]
    correlated_risk: float
    worst_pair: tuple[str, float] | None
    total_open_risk: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "perCurrency": {k: round(v, 2) for k, v in sorted(self.per_currency.items())},
            "correlatedRisk": round(self.correlated_risk, 2),
            "worstPair": [self.worst_pair[0], round(self.worst_pair[1], 3)] if self.worst_pair else None,
            "totalOpenRisk": round(self.total_open_risk, 2),
        }


def analyse_exposure(
    open_positions: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any] | None = None,
    *,
    correlation_threshold: float = 0.6,
) -> ExposureReport:
    """Aggregate risk-weighted exposure, optionally including a candidate.

    `correlated_risk` is the money at risk across the candidate and every
    existing position that moves with it — the number the limit is
    actually about.
    """

    entries = [
        {
            "symbol": str(position.get("symbol", "")),
            "direction": str(position.get("direction", "BUY")).upper(),
            "risk": float(position.get("risk_amount") or position.get("riskAmount") or 0.0),
        }
        for position in open_positions
    ]

    per_currency: dict[str, float] = {}
    for entry in entries:
        for currency, weight in currency_exposure(entry["symbol"], entry["direction"]).items():
            per_currency[currency] = per_currency.get(currency, 0.0) + weight * entry["risk"]

    total_open_risk = sum(entry["risk"] for entry in entries)

    if candidate is None:
        worst = None
        correlated = total_open_risk
        if len(entries) >= 2:
            pairs = [
                (
                    f"{a['symbol']}/{b['symbol']}",
                    correlation_score(a["symbol"], a["direction"], b["symbol"], b["direction"]),
                )
                for index, a in enumerate(entries)
                for b in entries[index + 1 :]
            ]
            if pairs:
                worst = max(pairs, key=lambda item: item[1])
        return ExposureReport(per_currency, correlated, worst, total_open_risk)

    candidate_symbol = str(candidate.get("symbol", ""))
    candidate_direction = str(candidate.get("direction", "BUY")).upper()
    candidate_risk = float(candidate.get("risk_amount") or candidate.get("riskAmount") or 0.0)

    for currency, weight in currency_exposure(candidate_symbol, candidate_direction).items():
        per_currency[currency] = per_currency.get(currency, 0.0) + weight * candidate_risk

    correlated = candidate_risk
    worst: tuple[str, float] | None = None
    for entry in entries:
        score = correlation_score(
            candidate_symbol, candidate_direction, entry["symbol"], entry["direction"]
        )
        if worst is None or score > worst[1]:
            worst = (f"{candidate_symbol}/{entry['symbol']}", score)
        if score >= correlation_threshold:
            correlated += entry["risk"] * score

    return ExposureReport(
        per_currency=per_currency,
        correlated_risk=correlated,
        worst_pair=worst,
        total_open_risk=total_open_risk + candidate_risk,
    )
