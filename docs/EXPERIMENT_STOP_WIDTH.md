# Experiment: which stop floor actually produces executable trades?

## Why this is a different question from every earlier measurement

Until commit 87b4fd8 the backtest paid the spread and never applied
`validate_spread`, the gate the live executor applies last. So it
counted trades the bot refuses at submission, and every trade-frequency
number in this repository was answering the wrong question.

With the guard in place the numbers change meaning. "Trades" now means
orders that would actually have gone out.

## The discovery table (seeds 100-111, already seen)

Twelve symbols x 2600 M15 bars, $5,000, risk 0.50%, 8-tick spread,
3-tick slippage, $7/lot, partial TP on at 0.75R.

    stop ATR / minRR   trades  spread-killed   win     avgR      P/L
    0.35 / 2.0  LIVE      165        22       51.5%  -0.109   -378.76
    0.35 / 1.2               140        60       50.7%  -0.030    -10.95
    0.60 / 1.2               124        38       53.2%  +0.013   +115.03
    0.90 / 1.2                80        10       53.8%  +0.032    +94.33
    0.90 / 1.5                86         3       55.8%  +0.009    -10.66
    1.30 / 1.2                29         1       51.7%  +0.036    +34.35

The mechanism is arithmetic, not a pattern: friction as a share of risk
is `spread / stop_distance`, so a tighter stop magnifies the cost of
every trade AND fails the execution guard more often. The
"spread-killed" column falls from 60 to 1 across the range.

## The honest problem with this table

I read it before choosing anything. Any threshold picked now is
post-hoc, and the two rows that look best are the two with the fewest
trades — which is exactly how a thin sample flatters itself.

So the rule below is fixed BEFORE the held-out runs, and its git
history is the evidence. Held-out seeds are 500-511; they have chosen
nothing in this experiment.

## Selection rule — FIXED BEFORE THE HELD-OUT RUNS

Four configurations go to the held-out seeds: the two baselines
(`0.35/2.0`, the live setting, and `0.35/1.2`, the documented default)
and the two candidates (`0.60/1.2` and `0.90/1.2`).

A candidate replaces the default only if, on the held-out set:

1. its **avgR is positive**, and
2. it takes at least **60 trades** (over 27 days and 12 symbols, that is
   roughly two a day — below this the sample cannot carry a conclusion
   whatever it says), and
3. its avgR beats BOTH baselines.

If two candidates qualify, the one with MORE trades wins: between two
configurations that both make money per trade, frequency is the
tiebreak, because an edge that trades twice a month compounds nothing
and cannot be verified in reasonable time.

If neither qualifies, nothing moves and this file records that.

No result on the discovery set can promote anything by itself. The
discovery table exists to choose which four runs are worth doing.

## Results: held out (seeds 500-511)

    stop ATR / minRR   trades   win     avgR      P/L     maxDD
    0.35 / 2.0  LIVE      158   46.8%  -0.125   -406.35   459.07
    0.35 / 1.2               138   45.7%  -0.145   -330.52   457.02
    0.60 / 1.2               129   45.7%  -0.151   -337.00   418.89
    0.90 / 1.2                83   50.6%  -0.090   -109.66   295.20

**Neither candidate qualifies. Nothing moves.**

Condition 1 was a positive avgR on data that chose nothing. No row
achieved it, so the rule stops here and the defaults stand.

`0.60/1.2` is the instructive failure. On the discovery seeds it
returned +0.013R and +$115 and was the best-balanced row in the table.
On fresh seeds it is the WORST of the four. That is not bad luck; it is
the signature of noise — a number that looks like an edge and inverts
when the seed changes.

## A correction I owe the record

Reading the discovery table I wrote that the live setting (0.35 ATR,
1:2) was "the worst row of six". On the held-out seeds it is the second
BEST of four. I was generalising from one dataset, which is the exact
error this document exists to prevent. The live setting is not the
problem I described it as.

## The finding that outlives the table

    spread between best and worst config, same seeds   ~0.06R
    spread for the SAME config across seed sets        ~0.16R

Dataset variance is roughly three times configuration variance. This
harness cannot separate these settings — not with more runs, not with a
finer grid. Every positive number it has produced sits inside its own
sampling noise.

So the conclusion is about the method, not the parameter: **synthetic
tuning of the stop floor is finished.** Continuing would not be work,
it would be manufacturing attractive numbers that die on contact with a
market, and the operator carries that cost.

`0.90 ATR` is the only setting better than both baselines on BOTH sets
— best win rate, best drawdown, smallest loss. It was never positive
out of sample, so the rule refuses it, and a rule written before the
results that is then argued around is decoration. It is recorded here
as the best candidate for a properly powered test on real data, not as
a recommendation.

## What replaces this

Paper trading, with the tuning fingerprint (d30f944) on every trade so
runs stay separable. Twenty or thirty real fills will settle more than
a thousand synthetic runs, because they carry the one thing no
generated series has: a real spread that widens exactly when it hurts.

The partial take-profit at 0.75R remains the only change in this
repository that survived a held-out test (docs/EXPERIMENT_WIN_RATE.md).
It is on by default. That is the configuration to run.
