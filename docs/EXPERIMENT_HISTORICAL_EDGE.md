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

(Filled in after the runs complete.)
