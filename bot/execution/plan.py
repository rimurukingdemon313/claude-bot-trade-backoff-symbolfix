"""The immutable trade plan and its deterministic execution identity.

Once built, a plan is frozen. Nothing between here and the broker may
change entry, stop, target, size or risk (MASTER_MISSION §43) — the
executor reads the plan, it never rewrites it.

The execution id is a hash of the things that make this trade THIS trade:
symbol, direction, the triggering candle's timestamp, and the rounded
levels. Two scans of the same unchanged setup therefore produce the same
id, and the UNIQUE constraint on execution_intents turns a duplicate into
a database error instead of a second live order.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ..clock import utc_now
from ..version import version_stamp


@dataclass(frozen=True, slots=True)
class TradePlan:
    execution_id: str
    symbol: str
    broker_symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    risk_amount: float
    risk_pct: float
    expected_profit: float
    risk_reward: float
    setup_grade: str
    setup_score: float
    ai_confidence: float | None
    htf_bias: str
    alignment: str
    instrument_id: int
    route_id: int
    created_at: str
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["versions"] = version_stamp()
        return data


def build_execution_id(
    *,
    symbol: str,
    direction: str,
    trigger_time: datetime,
    entry: float,
    stop_loss: float,
    take_profit: float,
    digits: int = 5,
) -> str:
    """Deterministic, collision-resistant execution identity."""

    material = "|".join(
        [
            symbol.upper(),
            direction.upper(),
            trigger_time.replace(second=0, microsecond=0).isoformat(),
            f"{entry:.{digits}f}",
            f"{stop_loss:.{digits}f}",
            f"{take_profit:.{digits}f}",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_plan(
    *,
    candidate: Any,
    spec: Any,
    risk_decision: Any,
    score: Any,
    ai_confidence: float | None,
    trigger_time: datetime | None = None,
) -> TradePlan:
    moment = trigger_time or candidate.timestamp or utc_now()
    size = risk_decision.size
    execution_id = build_execution_id(
        symbol=candidate.symbol,
        direction=candidate.direction,
        trigger_time=moment,
        entry=candidate.entry,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        digits=spec.digits,
    )
    return TradePlan(
        execution_id=execution_id,
        symbol=candidate.symbol,
        broker_symbol=spec.broker_name,
        direction=candidate.direction,
        entry=spec.round_price(candidate.entry),
        stop_loss=spec.round_price(candidate.stop_loss),
        take_profit=spec.round_price(candidate.take_profit),
        quantity=size.lots,
        risk_amount=round(size.actual_risk, 2),
        risk_pct=risk_decision.risk_pct or 0.0,
        expected_profit=round(risk_decision.expected_profit or 0.0, 2),
        risk_reward=round(candidate.risk_reward, 3),
        setup_grade=score.tier,
        setup_score=round(score.total, 2),
        ai_confidence=ai_confidence,
        htf_bias=candidate.htf_bias,
        alignment=candidate.alignment,
        instrument_id=spec.tradable_instrument_id,
        route_id=spec.route_id,
        created_at=utc_now().isoformat(),
        context={
            "evidence": list(candidate.evidence),
            "session": candidate.session.name,
            "regime": candidate.regime.as_dict(),
            "sweep": candidate.sweep.as_dict() if candidate.sweep else None,
            "structureEvent": candidate.structure_event.as_dict()
            if candidate.structure_event
            else None,
            "pointOfInterest": candidate.point_of_interest,
            "scoreComponents": score.as_dict(),
            "sizing": size.as_dict(),
        },
    )
