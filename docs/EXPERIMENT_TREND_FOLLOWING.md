# Pre-registration: does a published trend-following rule survive this test?

**Committed before the research engine exists and before any candidate
has been run.** The commit timestamp is the evidence.

---

## Why these candidates, and not more

The SMC strategy was measured on 14,594 trades and shown to have no edge,
even before costs (`EXPERIMENT_HISTORICAL_EDGE.md`). The obvious next move
— try many strategies and keep whichever tests best — is the one move
guaranteed to produce a false result. With enough candidates, one will
look good on nine years of history by chance alone, and it will fail on
the tenth.

So the search space is fixed here, small, and chosen from published work
rather than from this data:

- **Trend following / time-series momentum** is the family with the
  longest documented record in currencies (e.g. Moskowitz, Ooi & Pedersen,
  *Time Series Momentum*, 2012; Hurst, Ooi & Pedersen, *A Century of
  Evidence on Trend-Following Investing*). It trades on daily bars, so
  stops are wide — and the SMC result showed cost per R is what destroys
  an intraday strategy.
- **Parameters are the canonical published ones.** None is tuned here. A
  parameter chosen by looking at this data would be a degree of freedom
  spent on fitting it (rule 12).

Other documented FX premia — carry, value — need interest-rate or macro
data this dataset does not have. They are not tested, and not claimed.

**Prior expectation, stated before running:** the 2012–2022 decade is
widely reported as a difficult one for currency trend following. A null
result would be unsurprising and must be reported as one.

---

## Candidates (exactly three)

All daily bars. A signal is computed on the CLOSED bar i and filled at the
OPEN of bar i+1. ATR is ATR(20).

**C1 — Donchian breakout (Turtle System 2).**
Long when the close exceeds the highest high of the previous 55 bars;
short when it falls below the lowest low of the previous 55. Initial stop
2 × ATR. Exit when the close crosses the 20-bar channel the other way
(long: close below the previous 20-bar low).

**C2 — Time-series momentum (Moskowitz–Ooi–Pedersen).**
At each month's last bar: long if the 252-bar return is positive, short
if negative. Held until the next month-end re-evaluation, which holds,
flips or keeps it. Protective stop 3 × ATR from entry.

**C3 — Moving-average trend (50/200).**
Long while SMA(50) > SMA(200), short while below; enter on the cross,
exit on the opposite cross. Protective stop 3 × ATR.

One position per instrument per candidate. No pyramiding.

---

## Costs — conservative, fixed now

| | FX | Gold |
|---|---|---|
| spread (paid at entry and exit) | 0.8 pip | $0.30 |
| slippage (every fill, adverse) | 0.3 pip | $0.10 |
| commission (round trip, price-equivalent) | 0.7 pip | $0.07 |
| **swap / financing** | **1% of ATR per bar held, charged in BOTH directions** | same |

The swap line is an assumption and says so. The dataset has no rate data,
and a position held for weeks pays real financing that can be large. It
is charged against both longs and shorts because retail brokers mark up
both sides. The verdict uses it; a no-swap run is reported as sensitivity
only.

Fills are pessimistic: a stop that the day gaps through fills at the
open, not the stop; a bar that touches the stop and would also trigger
the exit resolves as the stop.

---

## Split and pass rule

- **Design:** trades entered before 2017-01-01.
- **Held-out:** trades entered on or after 2017-01-01.

A candidate **PASSES** only if, pooled across all twelve instruments:

1. held-out avgR (after all costs including swap) **> 0**, AND
2. held-out t-statistic **> 2.39** — the Bonferroni threshold for three
   candidates at 5%, because testing three raises the odds that one
   clears 2.0 by luck, AND
3. design-period avgR **> 0**, so the result is not one lucky regime.

If more than one passes, the one with the higher **design-period** t is
chosen. The held-out result is never used to choose.

**If none passes, the report says none passed, and no further candidates
are added in this round.** Searching until something clears the bar is
exactly the failure this document exists to prevent.

---

## Statistical power, stated in advance

Daily trend rules trade rarely — perhaps 3–10 trades per instrument per
year. Across twelve instruments and five held-out years, that is a few
hundred trades. Trend-following R is fat-tailed (many small losses, few
large wins), so its standard deviation is high, and a real edge of 0.1R
per trade might not reach t = 2.39 on that sample.

So a FAIL here has two possible meanings: no edge, or too few trades to
see one. **Both mean the same thing for the account: not deployable.**
A strategy whose edge cannot be distinguished from zero on five years of
twelve instruments is not one to put money behind.

---

## If one passes

It is then implemented as a real strategy in the bot (rule 14): it
proposes a priced candidate, and `risk/engine.py` alone sizes and
approves it, under the bot's own limits (max three open positions,
correlation caps), which this research does not model. It is re-tested
through that real code path before it is ever enabled, and the demo
account comes after that — never before.

---

## RESULTS

(Filled in after the run.)
