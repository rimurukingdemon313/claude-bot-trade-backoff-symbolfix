"""The setup, condition by condition, as a record rather than a sentence.

The engine has always said WHY it stood aside, and the reason string is
genuinely good — it names the level, the ratio, the shortfall. What it
cannot do is say what PASSED. "no live fair value gap or order block to
enter from" leaves an operator guessing whether the sweep was found, the
break confirmed, the trend read, or whether the whole thing fell over at
the first hurdle.

So the strategy chain is recorded as a list of named checks, in the order
the engine evaluates them, each PASS, FAIL or PENDING:

    liquidity_sweep -> displacement -> structure_break -> entry_zone
    -> zone_retest -> stop_loss -> take_profit -> risk_reward

PENDING is not a euphemism for FAIL. It means the engine stopped before
reaching that condition, which is different information: a setup that
failed at `risk_reward` got everything else right and was priced out,
while one that failed at `liquidity_sweep` never started. Collapsing the
two would put them in the same bucket on the dashboard, and they call for
opposite reactions.

Nothing here decides anything. It is a transcript of decisions made
elsewhere, so it cannot drift from them by being wrong — only by being
incomplete, which the engine's own test pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

PASS = "PASS"
FAIL = "FAIL"
PENDING = "PENDING"

#: The chain, in evaluation order. A name not in here is a bug rather
#: than an extension: the dashboard renders this order.
CHAIN = (
    "liquidity_sweep",
    "displacement",
    "structure_break",
    "entry_zone",
    "zone_retest",
    "stop_loss",
    "take_profit",
    "risk_reward",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str            # PASS | FAIL | PENDING
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


class Checklist:
    """Records the chain as the engine walks it.

    Append-only by design: a condition cannot be re-graded once recorded,
    because a later stage re-writing an earlier verdict is exactly how a
    transcript stops being one.
    """

    __slots__ = ("_checks",)

    def __init__(self) -> None:
        self._checks: list[Check] = []

    def record(self, name: str, status: str, detail: str = "") -> None:
        if name not in CHAIN:  # pragma: no cover - guarded by a test
            raise ValueError(f"{name!r} is not part of the strategy chain")
        if any(check.name == name for check in self._checks):
            raise ValueError(f"{name!r} was already recorded; the chain is append-only")
        self._checks.append(Check(name, status, detail))

    def passed(self, name: str, detail: str = "") -> None:
        self.record(name, PASS, detail)

    def failed(self, name: str, detail: str = "") -> None:
        self.record(name, FAIL, detail)

    def finish(self) -> tuple[Check, ...]:
        """The full chain, with everything unreached marked PENDING."""

        recorded = {check.name: check for check in self._checks}
        return tuple(
            recorded.get(name, Check(name, PENDING, "not reached"))
            for name in CHAIN
        )


def summarise(checks: Sequence[Check]) -> str:
    """One line for a log: `sweep PASS · displacement PASS · BOS FAIL`."""

    return " · ".join(f"{check.name} {check.status}" for check in checks)
