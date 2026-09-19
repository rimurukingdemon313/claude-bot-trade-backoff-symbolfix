"""What makes two setups the same setup.

A stop-out does not remove the evidence that produced the trade. The
sweep is still in the journal, the break of structure still stands, and
the imbalance is often still unmitigated — so fifteen minutes later the
engine re-derives the identical setup, from the identical candles, and
would take it again. Nothing upstream is wrong; each scan is correct in
isolation. The missing idea is that these are ONE setup seen twice.

So a setup gets an identity, and it is built from the things that make it
that setup rather than another:

    symbol · timeframe · direction · the level that was swept ·
    the candle the sweep happened on · the break of structure

Deliberately NOT in it: the entry price, the current bar, the score, the
account, or anything else that moves between scans. An identity that
changed every fifteen minutes would be a hash, not an identity.

`bot/risk/engine.py` is what refuses a repeat — identity is a fact about
the chart, and whether a fact bars a trade is the risk engine's call
(project rule 2).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .liquidity import LiquiditySweep
    from .structure import StructureEvent

#: Enough to make a collision irrelevant, short enough to read in a log.
_DIGEST_CHARS = 16


def _level(price: float) -> str:
    """A price as a stable string.

    Five decimals covers FX and is finer than any level this engine
    builds; on metals it simply carries more precision than it needs.
    Formatting rather than rounding keeps -0.0 and 0.0 from differing.
    """

    return f"{price:.5f}"


def setup_identity(
    *,
    symbol: str,
    timeframe: str,
    direction: str,
    sweep: "LiquiditySweep | None",
    structure_event: "StructureEvent | None",
) -> str:
    """A stable id for the setup this evidence describes.

    Returns "" when there is nothing durable to key on. An empty identity
    is never treated as a match, so a setup the engine cannot name is
    allowed through rather than silently blocked by a collision with every
    other unnamed setup — a fail-open that is safe here precisely because
    every other gate still runs.
    """

    parts: list[str] = [symbol.upper(), timeframe.upper(), direction.upper()]

    if sweep is not None:
        parts.append(f"sweep:{sweep.level.label}@{_level(sweep.level.price)}")
        parts.append(f"at:{sweep.timestamp.isoformat()}")
    if structure_event is not None:
        parts.append(
            f"{structure_event.event_type}:{_level(structure_event.level)}"
            f"@{structure_event.timestamp.isoformat()}"
        )

    if len(parts) == 3:
        # Direction and symbol alone are not a setup - every future bar in
        # this direction would collide with this one and be refused.
        return ""

    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:_DIGEST_CHARS]
