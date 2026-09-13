"""Canonical symbol identity.

Brokers decorate instrument names: `EURUSD.R`, `EURUSD_i`, `EURUSDm`,
`EUR/USD`, `EURUSD.pro`, `XAUUSD.R`. The decoration marks account type or
routing; the instrument is the same pair.

Getting this wrong is not cosmetic. Symbol identity is the join key for
three safety controls, and every one of them fails OPEN when the key does
not match:

  * the executor's pre-submit duplicate check compares the plan's symbol to
    the symbol `positions()` reports — a mismatch means it cannot see an
    existing position and may open a second one;
  * the risk engine's per-symbol limit uses the same comparison;
  * news blackouts and portfolio correlation are both derived from the
    pair's two currencies — an unparsed name yields no currencies, so both
    filters silently pass everything.

So this module answers one question deterministically: given any broker
name, what pair is this, and what are its two currencies?

The rule is a known-code match rather than suffix stripping by pattern.
Suffix lists are guesswork and go stale; "does this start with a known
currency code followed by another known currency code" is decidable. A name
that does not resolve returns None, and callers then refuse to size it —
failing closed, loudly, instead of trading an instrument we cannot identify.
"""

from __future__ import annotations

#: ISO 4217 codes for the currencies an FX/metals bot may encounter, plus
#: the metal and crypto codes that occupy the base slot. Deliberately a
#: closed set: an unknown code must fail to resolve rather than be guessed.
CURRENCIES: frozenset[str] = frozenset(
    {
        # majors and crosses
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
        # commonly quoted others
        "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "ZAR", "MXN",
        "SGD", "HKD", "CNH", "CNY", "INR", "THB", "ILS", "RUB",
        # metals (base slot)
        "XAU", "XAG", "XPT", "XPD",
        # crypto (base slot)
        "BTC", "ETH", "LTC", "XRP", "BCH", "SOL", "ADA", "DOT",
    }
)

#: Separators that appear INSIDE a pair name and carry no meaning.
_INTRA_PAIR = ("/", " ", "\t")


def strip_separators(name: str) -> str:
    """Uppercase and remove intra-pair separators, keeping suffix markers.

    `EUR/USD.R` -> `EURUSD.R`. The dot is preserved because it separates the
    pair from the broker's suffix, which `canonical_symbol` needs to see.
    """

    text = str(name).upper().strip()
    for separator in _INTRA_PAIR:
        text = text.replace(separator, "")
    return text


def alphanumeric(name: str) -> str:
    """Uppercase, letters and digits only. `EURUSD.R` -> `EURUSDR`."""

    return "".join(char for char in str(name).upper() if char.isalnum())


def canonical_symbol(name: str) -> str | None:
    """The 6-character pair inside a broker name, or None.

    `EURUSD.R` -> `EURUSD`. `XAUUSD_i` -> `XAUUSD`. `eur/usd` -> `EURUSD`.
    `US500`, `NAS100`, `WTI` -> None (not a resolvable currency pair).

    Resolution requires BOTH halves to be known codes. That is what makes
    `XAUUSD.R` resolve to XAU/USD instead of the previous code's XAU/`USDR`.
    """

    text = alphanumeric(name)
    if len(text) < 6:
        return None
    base, quote = text[:3], text[3:6]
    if base in CURRENCIES and quote in CURRENCIES:
        return f"{base}{quote}"
    return None


def split_currencies(name: str) -> tuple[str | None, str | None]:
    """The pair's (base, quote), or (None, None) if it does not resolve.

    (None, None) is a meaningful answer: position sizing refuses to compute a
    conversion rate without a known quote currency rather than assuming one,
    so an unrecognised instrument is skipped instead of mis-sized.
    """

    canonical = canonical_symbol(name)
    if canonical is None:
        return None, None
    return canonical[:3], canonical[3:]


def broker_suffix(name: str) -> str:
    """The decoration after the pair, for logging and diagnostics.

    `EURUSD.R` -> `.R`. Returns "" when the name is a bare pair, and the
    whole name when it does not resolve to a pair at all.
    """

    canonical = canonical_symbol(name)
    if canonical is None:
        return str(name)
    text = strip_separators(name)
    index = 0
    letters = 0
    # Walk past the six pair characters, ignoring any separators among them.
    while index < len(text) and letters < 6:
        if text[index].isalnum():
            letters += 1
        index += 1
    return text[index:]


def same_instrument(left: str, right: str) -> bool:
    """Do two names refer to the same tradable instrument?

    This is the comparison the duplicate-order check, the per-symbol limit
    and the reconciler all need: `EURUSD` and `EURUSD.R` are the same
    instrument, and treating them as different is how a second position gets
    opened on a pair that is already held.
    """

    left_canonical = canonical_symbol(left)
    right_canonical = canonical_symbol(right)
    if left_canonical is not None and right_canonical is not None:
        return left_canonical == right_canonical
    # Neither resolves to a known pair (an index, say): fall back to an
    # exact alphanumeric comparison rather than declaring them equal.
    return alphanumeric(left) == alphanumeric(right)
