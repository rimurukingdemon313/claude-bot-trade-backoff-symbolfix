# Pre-registration: where does the historical loss come from, and does grade A fix it?

**Written and committed before any per-trade breakdown was looked at.**
The commit timestamp is the evidence. Everything below the line
"RESULTS" is filled in afterwards and must be judged against what is
written here, not re-interpreted to fit it.

---

## What is already known (aggregate only)

Offline backtest, real engine, windowed to the live lookback, public
dataset (ejtraderLabs/historical-data, 2012-11 → 2022-03), costs 0.8 pip
spread + 0.3 pip slippage + $7/lot. Drawdown halt lifted for measurement.

| Symbol | Trades | avgR | t |
|---|---|---|---|
| EURUSD | 1,159 | −0.090 | −3.14 |
| GBPUSD | 1,727 | −0.084 | −3.51 |
| AUDUSD | 763 | −0.058 | −1.59 |
| USDCHF | 786 | −0.085 | −2.46 |

The strategy as it stands has negative expectancy after costs on these
pairs. That is the starting point, not a hypothesis.

---

## Data split — fixed now

- **Design period:** trades whose entry is before **2017-01-01**.
- **Held-out period:** trades whose entry is on or after **2017-01-01**.

Any rule is chosen by looking at the design period ONLY. It is then
applied, unchanged, to the held-out period. Only the held-out result
decides anything.

---

## Hypothesis H1 — grade A only

Carried over from the one earlier dataset (generated, 180 trades): A grades
averaged +0.014R and B grades −0.152R; "the entire loss lived in the B
cohort".

**H1 passes only if, on the held-out period, pooled across all symbols
with trade records:**

1. A-or-better trades have avgR **> 0**, AND
2. their t-statistic is **> 2.0**, AND
3. A-or-better avgR exceeds B avgR.

If (1) and (3) hold but not (2): **inconclusive** — `SCORING_MIN_TIER=A`
is not recommended on this evidence.

If (1) fails: **H1 is rejected.** Grade A does not rescue the strategy, and
I say so plainly.

---

## Exploratory breakdowns — reported, not acted on

By setup type, session, direction, exit reason and projected-vs-structural
target, on the design period, with n and t per bucket.

These are for finding a hypothesis, **not** for adopting a filter. Slicing
a losing strategy six ways will produce some bucket that looks profitable
by chance alone; with ~40 buckets, roughly two will clear |t| > 2 with no
real effect behind them. So any bucket that looks promising becomes H2,
written down here in a follow-up commit, and tested on the held-out
period exactly like H1 — before it is recommended.

---

## What will NOT be done

- No parameter will be tuned on the full period.
- No filter will be recommended from the design period alone.
- The held-out period will not be looked at bucket-by-bucket before a
  hypothesis about it is written down.
- If nothing survives, the report says nothing survived.

---

## RESULTS

All twelve symbols, 2012-11 → 2022-03, real engine windowed to the live
lookback, costs as above, drawdown halt lifted for measurement. The first
eight symbols were run twice (with and without trade records) and matched
to the cent, so the engine is deterministic and the records changed
nothing.

### Aggregate

| Period | Trades | Win | avgR | t |
|---|---|---|---|---|
| All | 14,594 | 48.9% | **−0.075** | **−9.29** |
| Design (< 2017) | 7,794 | 48.9% | −0.069 | −6.24 |
| Held-out (≥ 2017) | 6,800 | 49.0% | −0.083 | −6.93 |

| Symbol | Trades | avgR | t | Live kill switch would have fired |
|---|---|---|---|---|
| EURCHF | 370 | −0.180 | −3.66 | after 126 trades, 2015-07-03 |
| GBPUSD | 1,727 | −0.084 | −3.51 | after 209, 2013-12-31 |
| XAUUSD | 2,449 | −0.066 | −3.31 | after 352, 2013-08-22 |
| AUDJPY | 1,065 | −0.098 | −3.28 | after 105, 2013-06-25 |
| EURUSD | 1,159 | −0.090 | −3.14 | after 164, 2013-08-20 |
| GBPJPY | 2,117 | −0.062 | −2.89 | after 284, 2013-12-17 |
| EURJPY | 1,578 | −0.065 | −2.66 | after 232, 2013-09-25 |
| USDJPY | 964 | −0.083 | −2.58 | after 80, 2013-06-04 |
| USDCHF | 786 | −0.085 | −2.46 | after 312, 2015-08-07 |
| USDCAD | 1,032 | −0.066 | −2.21 | after 217, 2015-07-30 |
| AUDUSD | 763 | −0.058 | −1.59 | after 247, 2015-02-26 |
| EURGBP | 584 | −0.046 | −1.13 | after 209, 2016-05-26 |

**No symbol is positive. Ten of twelve are significantly negative.** The
loss is the same in both periods, so it is not one bad regime. Every
symbol would have tripped the live 10% drawdown kill switch, the last by
mid-2016 — the safety system would have capped each loss at 10%.

### H1 — grade A or better only: **REJECTED**

| Held-out | Trades | avgR | t |
|---|---|---|---|
| A or better | 5,160 | −0.086 | −6.23 |
| B | 1,640 | −0.074 | −3.06 |

All three conditions fail. A-or-better is not profitable out of sample,
and it is not better than B — it is slightly worse. The earlier result
this came from (A +0.014R, B −0.152R, 180 generated trades) does not
replicate on real prices. `SCORING_MIN_TIER=A` is **not** recommended.

### Exploratory, design period only

Every session, both directions and every setup type with a usable sample
is negative. There is no slice where the strategy works.

Two patterns stood out, and both point the same way — **the scorer's
preferences look inverted**:

| Design period | Trades | avgR | t |
|---|---|---|---|
| grade A+ | 2,000 | **−0.119** | −5.45 |
| grade A | 3,707 | −0.061 | −3.84 |
| grade B | 2,087 | **−0.034** | −1.60 |
| structural target | 6,615 | −0.075 | −6.20 |
| projected target (scored ×0.85) | 1,179 | −0.035 | −1.28 |

The setups the scorer rates highest did worst, and the targets it
penalises did better. The risk engine then sizes A+ at 1.4× and B at
0.75× — which, if this holds, puts the most money on the worst trades.

Neither is a route to profit: even the best of these buckets is below
zero. They are findings about the SCORER, and they are hypotheses until
tested below.

---

## H2 — pre-registered after the design-period look, before any held-out breakdown

**H2a — the grade ordering is inverted.** Passes only if, on the held-out
period, pooled: avgR(B) > avgR(A+), AND the Welch t-statistic of that
difference is > 2.0.

**H2b — projected targets beat structural ones.** Passes only if, on the
held-out period, pooled: avgR(projected) > avgR(structural), AND the
Welch t of the difference is > 2.0.

**What passing would and would not mean, fixed now:**

- It would NOT justify trading B-only or projected-only. Both are
  negative in the design period; a less-bad subset of a losing strategy
  is still a losing strategy.
- H2a passing WOULD mean the score is anti-informative, and that the tier
  risk multipliers (A+ 1.4×, A 1.15×, B 0.75×) amplify losses. The only
  change that would follow is one rule 2 already permits without further
  evidence: stop scaling risk UP by grade. Nothing would scale it up
  elsewhere.
- Failing means the design-period pattern was noise, and nothing changes.
