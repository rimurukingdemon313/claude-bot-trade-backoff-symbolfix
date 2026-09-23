# Pre-registration: a search for a durable edge across eight strategy families

**Committed before any of these rules is implemented or run.**

The brief (from the operator, verbatim in spirit): do not assume any
strategy is profitable; define exact rules first; never optimise after
seeing results; realistic costs; no look-ahead; separate in-sample,
validation and unseen data; walk-forward; Monte Carlo; many pairs and
regimes; reject anything that works only on one pair, one period or one
parameter; never choose by highest profit; and look for an edge that
SURVIVES — positive after costs, broad, and stable under small parameter
changes — not the best-looking curve.

---

## What this round can and cannot test

**Tested (8):** F1 adaptive trend, F3 cross-sectional momentum, F4 z-score
mean reversion, F5 volatility breakout, F6 tick-VWAP mean reversion, F7
regime switching, F9 trend pullback, F10 London opening-range breakout.

**Deferred, not faked (2):** F2 carry and F8 carry+momentum+volatility.
The brief requires the broker's REAL swap, not an assumed number, and no
historical swap series exists in this data. In a carry strategy the swap
is the return itself, so testing it with an assumption would only report
the assumption back. They wait for real swap data.

**Also stated:** F6 uses TICK volume. Spot FX has no consolidated traded
volume, so this is a tick-weighted session average, not a true VWAP. F10
has no historical news calendar, so its news filter cannot be applied.

---

## Data and clock

Twelve instruments (eleven FX pairs + XAUUSD; F3 uses the eleven FX pairs
only), 2012-11 → 2022-03, ejtraderLabs/historical-data. Timestamps are
converted from broker server time to UTC (New York + 7h), and session rules
are written in **London local time**, so they follow UK daylight saving.

## Split

| Segment | Entries |
|---|---|
| In-sample (IS) | before 2016-01-01 |
| Validation | 2016-01-01 to 2018-12-31 |
| **Unseen out-of-sample (OOS)** | **from 2019-01-01** |

No parameter is fitted in any segment. Every parameter below is fixed now.

## Mechanics common to all

- Signal on the CLOSED bar i, filled at the OPEN of bar i+1.
- Stop checked intrabar from the entry bar on; a gap through it fills at
  the open; stop and target touched in one bar resolve as the stop.
- Costs — FX: spread 0.8 pip (half per fill), slippage 0.3 pip per fill,
  commission 0.7 pip round trip. Gold: $0.30 / $0.10 / $0.07.
- Swap: per New York close crossed while holding, 1% of the ATR(20) of the
  last closed daily bar, charged on longs AND shorts (a conservative cost
  assumption — this round contains no carry strategy).
- F6 and F10 are flat before the New York close, so they never pay swap.
- ATR(n): mean true range. EMA(n): α = 2/(n+1), seeded with SMA(n).
  ER(n): Kaufman efficiency ratio, |C_i − C_{i−n}| / Σ|C_k − C_{k−1}|.

---

## The eight rule sets (parameters fixed here)

**F1 Adaptive trend following — D1.**
Trend up if EMA50 > EMA200, down if below. Long when up, close > highest
high of the previous 20 bars, ER(20) ≥ 0.30, and ATR(20) > ATR(20) five bars
earlier; short mirrored. Initial stop 2.5×ATR20. Chandelier trail: from the
next bar, stop = max(stop, highest close since entry − 3.0×ATR20). Exit also
when the EMA trend flips. *Plateau parameters: breakout lookback 20,
trail multiple 3.0.*

**F3 Cross-sectional currency momentum — D1, FX only.**
Strength of each of USD, EUR, GBP, JPY, CHF, CAD, AUD = mean of ±ln(C_i /
C_{i−63}) over every pair containing it (+ as base, − as quote). At the
first bar of each trading week, trade the available pair with the largest
|strength(base) − strength(quote)|, in the sign's direction; hold until a
later rebalance picks a different pair or direction. Protective stop
3.0×ATR20. Dates aligned across all eleven pairs. *Plateau: lookback 63,
stop multiple 3.0.*

**F4 Statistical mean reversion — H1.**
z = (C − SMA48) / SD48. Only when ER(48) < 0.25. Short when z_{i−1} ≥ 2.5,
bar i closes below its open, and z_i < z_{i−1}; long mirrored. Stop
1.5×ATR24 beyond entry; target the SMA48 at the signal bar; exit after 48
bars. *Plateau: z threshold 2.5, lookback 48.*

**F5 Volatility breakout — H1.**
Compressed when ATR10/ATR50 < 0.75 on bar i−1. Long when compressed, close
> highest high of the previous 20 bars, and ATR10 rising; short mirrored.
Stop 1.5×ATR20, target 2.0R, exit after 48 bars. *Plateau: compression
ratio 0.75, range lookback 20.*

**F6 Tick-VWAP mean reversion — M15.**
Session average from 08:00 London local: Σ(typical × tick volume) /
Σ tick volume. d = (C − VWAP)/ATR20. Entries 10:00–16:00 London local,
only when ER(32) < 0.30 and the spread is ≤ 25% of ATR20. Short when
d_{i−1} ≥ 2.0 and C_i < C_{i−1}; long mirrored. Stop 1.0×ATR20 beyond
entry, target the VWAP at the signal bar, forced exit at 17:00 London
local. *Plateau: deviation 2.0, ER threshold 0.30.*

**F7 Regime switching — H1.**
X = ATR10/ATR50. EXTREME if X > 1.8 → no entry. Else COMPRESSED if X < 0.75
→ F5's entry. Else TRENDING if ER(48) ≥ 0.35 → F1's entry rule on H1 with
F1's stops. Else RANGING if ER(48) < 0.25 → F4's entry. Otherwise no entry.
A position exits by the rules of the regime that opened it. *Plateau:
trending threshold 0.35, ranging threshold 0.25.*

**F9 Trend pullback — H4.**
Up if EMA50 > EMA200. Long when up, low_{i−1} ≤ EMA20_{i−1}, close_{i−1} >
EMA50_{i−1}, close_i > high_{i−1} and close_i > EMA20_i; short mirrored.
Stop 2.0×ATR14, target 2.0R, exit after 30 bars. *Plateau: pullback EMA
20, target R 2.0.*

**F10 London opening-range breakout — M15.**
Range = the M15 bars starting 08:00–08:59 London local. From 09:00 to
11:59, the first close above the range high goes long, below the low goes
short; one entry per pair per day. Only if range ≥ 5× spread and ≤ 1.0×
the last closed daily ATR20. Stop at the other side of the range, target
1.5× the range width, forced exit at 16:00 London local. *Plateau: range
length 4 bars, target multiple 1.5.*

---

## Pass rule — ALL gates, on the unseen OOS unless stated

| Gate | Requirement |
|---|---|
| G1 | avgR > 0 in IS, in validation, **and** in OOS, after all costs |
| G2 | OOS t-statistic **> 2.89** |
| G3 | OOS avgR > 0 on ≥ 2/3 of instruments with ≥ 20 OOS trades, and no single instrument supplies > 50% of OOS total R |
| G4 | OOS avgR > 0 in ≥ 3 of the 4 OOS calendar years (2019–2022) |
| G5 | Parameter plateau: each of the two named parameters × {0.75, 1.0, 1.25} (integers rounded) → 9 variants; ≥ 7 of 9 have OOS avgR > 0 |
| G6 | With the spread doubled, G1 and G2 still hold |

**Why 2.89.** Thirteen strategies have now been tested on this dataset —
SMC, reversion, three trend rules, and these eight. Bonferroni at a
two-sided 5% over thirteen tests gives |t| > 2.89. Anything lower would let
one of thirteen pass by luck with meaningful probability.

**G5 is a stability test, never a selection step.** The reported result is
always the base parameters above. If the base passes but its neighbours
do not, the edge is a spike in parameter space and it fails.

## Reported for every family, gating nothing

Trade count; expectancy in R; profit factor; win rate; average win and
average loss in R; average cost per trade in R; maximum drawdown in
cumulative R; Sharpe and Sortino of the daily R series (annualised by √252);
Monte Carlo — 10,000 bootstrap resamples of OOS trades, reporting the 5th
percentile of avgR and P(avgR ≤ 0); and a walk-forward view — rolling
12-month windows over the full period, fraction with avgR > 0. There is no
re-fitting inside the walk-forward, because nothing is fitted anywhere.

## After the verdict

- **Nothing is chosen by profit.** Every family that passes all six gates
  proceeds to paper trading. None that fails is recommended in any form.
- **Nothing reaches even the demo account's live mode** until it has also
  passed paper trading, with its own rule written before paper trading
  starts.
- **If none passes, the report says none passed.** No ninth family is added
  to this round to find one that does.

## Clarifications

Any ambiguity found while implementing is resolved in a commit to this
section BEFORE the first run, never after.

**C-1 (F4 lookback).** F4's "lookback 48" sets the SMA, the SD and the ER
windows together; the plateau varies all three at once. The 48-bar time
exit is separate and stays fixed at 48.

**C-2 (F7 regime timing).** F7 classifies the regime from bar i−1's values
(X and ER(48)), then asks the chosen family's entry rule about bar i. F5
defines compression on bar i−1, and a breakout bar's own rising ATR would
otherwise reclassify it out of the regime that produced it. In the RANGING
branch, F4's internal ER limit is F7's ranging threshold.

**C-3 (session windows).** "Entries from 09:00 to 11:59" means the decision
bar STARTS in that window; its order fills at the next open. For F10's
plateau, the range is the first N bars from 08:00 London and the entry
window begins immediately after them; "one entry per day" means only the
first bar of the day's window that closes beyond the range may signal, a
rule computed from that day's bars alone. F6's entry window is decision
bars starting 10:00–15:59, and "forced exit at 17:00" means the decision at
the close of the bar ending 17:00, filled at the next open. F10's 16:00 exit
is read the same way.

## RESULTS

(Filled in after the run.)
