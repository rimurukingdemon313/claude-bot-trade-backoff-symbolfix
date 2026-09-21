# Experiment: can the win rate be raised without buying it?

## Why this file exists

The request was "raise the win rate". The failure mode is obvious and
already measured in this repository: move the target closer and the win
rate climbs to 63.6% while the balance falls from $5,060 to $4,731. A
win rate is trivially purchasable and worth nothing on its own.

So this is run as an experiment with the selection rule fixed **before**
the results were read, and the winner validated on data that chose
nothing. The rule below was committed before `sweep2_disc.out` was
opened; the git history of this file is the evidence of that.

## The two datasets

Both are 12 symbols x 2600 M15 bars, ~27 days each, generated from the
same process with different seeds. Costs: 8 ticks spread, 3 ticks
slippage, $7/lot. Starting balance $5,000, risk 0.50%.

* **discovery** (seed base 100) — the set every measurement in this
  session has used. Ten configurations are compared on it.
* **held out** (seed base 500) — never used to choose anything. Exactly
  two runs happen here: the control, and the one candidate the rule
  below selects.

A synthetic series is not a market, and a second synthetic set is not
independent evidence about the world. What it IS is a test of whether a
result survives a change of data at all — which a curve fit does not.

## The ten configurations

Control plus three families, chosen because each has a mechanism behind
it rather than because the grid was available:

* **partial at N x R** (1.00, 0.75, 0.50, and 1.00 at 70% of the
  position) — most trades that reach 1R never reach target, so banking
  part of the position converts them from scratches into partial wins.
* **tier A floor** (`SCORING_MIN_TRADEABLE=68`) — the A cohort measured
  +0.010R against B's -0.151R over 275 trades. The grade boundary
  separates even though the continuous score behind it does not.
* **wider structural stop** (`RISK_MIN_STOP_ATR=1.30`) — friction is
  `spread / stop_distance` as a share of R, so a wider stop is a cost
  control: measured 16.6% of R at 0.35 ATR against 8.2% at 1.30.

Plus the three pairwise/triple combinations of those.

## Selection rule — FIXED BEFORE READING THE RESULTS

1. A candidate must have **n >= 100** trades on the discovery set. A
   configuration that trades thirty times has told me nothing.
2. Among those, take the **highest win rate** subject to
   **avgR >= the control's avgR**. Raising the win rate while
   expectancy falls is the exact failure this experiment exists to
   avoid, so it is disqualifying rather than a trade-off.
3. **One** candidate goes to the held-out set, with the control.
4. It "survives" only if, on the held-out set, its win rate is higher
   than the control's **and** its avgR is no worse than the control's.
   Anything else is recorded as "did not survive" and no default moves.

Ten configurations were compared. With ten comparisons on one dataset,
the best-looking one is expected to look good partly by luck; that is
what step 3 and step 4 are for, and why only one candidate is promoted.

## Results: discovery set (seeds 100-111)

    config              n    win     avgR    maxDD   posShare  bootstrap 5%-95%
    shipped           180  33.33%  -0.111   444.84     3.9%   [-852.54,  -32.23]
    part_0.50         180  51.11%  -0.077   353.52    12.5%   [-660.19, +116.90]
    part_0.75         180  51.67%  -0.044   285.46    27.9%   [-599.38, +278.51]
    part_1.00         180  51.11%  -0.058   343.96    18.8%   [-735.25, +218.61]
    part_1.00_f70     180  51.11%  -0.040   304.94    25.7%   [-674.91, +287.69]
    tierA              62  41.94%  -0.048   254.80    23.8%   [-530.87, +228.24]
    tierA_part1.00     62  53.23%  +0.006   224.85    45.6%   [-398.42, +360.56]
    wide               30  46.67%  +0.031    95.43    60.2%   [-204.92, +322.98]
    wide_part1.00      30  53.33%  +0.043    95.43    60.2%   [-188.35, +270.58]
    tierA_wide_part    20  55.00%  +0.060    95.43    57.9%   [-177.45, +244.24]

`posShare` is the fraction of bootstrap resamples that finished in
profit — the direct answer to "could this have been luck?".

### The bug this sweep found before it found a setting

The first run of this table returned results for `tierA` that were
identical to `shipped` **to the cent** — same 180 trades, same -444.81,
same bootstrap interval. Two different configurations cannot agree to
the cent. `SCORING_MIN_TRADEABLE` turned out to be a dead setting: read
from the environment, carried on the config, documented in
.env.example, and consulted by no production code. Fixed in aaae9e9;
the three `tierA` rows above are from the re-run.

### Applying the rule

Step 1, n >= 100, eliminates five of the nine candidates: `tierA` (62),
`tierA_part1.00` (62), `wide` (30), `wide_part1.00` (30) and
`tierA_wide_part` (20).

This is the step that costs something, and it is supposed to. The three
eliminated rows with a **positive** avgR and 58-60% of resamples in
profit are the most attractive numbers in the table, and they are
attractive because they are thin: twenty to thirty trades over 27 days
across twelve pairs, with bootstrap intervals 400-500 wide. Had the
rule been written after the table was read, those rows are exactly what
it would have been written to select.

`tierA_part1.00` is the genuinely painful one — avgR +0.006, 45.6%
positive, best drawdown of any n>60 row — and 62 trades is still 62
trades. It is disqualified.

Step 2, highest win rate subject to avgR >= the control's -0.111, over
the five survivors: all four partial variants clear the expectancy
condition comfortably, and `part_0.75` has the highest win rate at
**51.67%**.

**Promoted: `part_0.75`** — `EXEC_ENABLE_PARTIAL_TP=true`,
`EXEC_PARTIAL_TP_AT_R=0.75`, everything else stock.

### Declared before the held-out runs

Three runs happen on the held-out seeds, and their status is fixed now:

* `shipped` — the control.
* `part_0.75` — the promoted candidate. Step 4 decides it.
* `tierA_part1.00` — an **observation only, non-promotable whatever it
  returns**. It is run because the tier floor deserves its own larger
  experiment and knowing whether 62 trades pointed anywhere useful
  helps design that one. It cannot move a default in this experiment,
  and no result it produces will be described as validated.

## Results: held out (seeds 500-511)

    config              n    win     avgR    maxDD   posShare  bootstrap 5%-95%
    shipped           118  25.42%  -0.248   521.70     9.4%   [-699.75,  +87.90]
    part_0.75         173  44.51%  -0.128   442.74    10.6%   [-714.58,  +98.05]
    tierA_part1.00     57  50.88%  +0.005   280.22    50.1%   [-386.83, +417.75]

Step 4: the candidate survives only if its win rate beats the control
AND its avgR is no worse. Win rate 44.51% against 25.42%, avgR -0.128
against -0.248. **It survives**, on both conditions, with room.

Note the control got WORSE on the held-out seeds, from -0.111R to
-0.248R. These are harder seeds. That strengthens the result rather
than weakening it: the gap held up on data where the baseline suffered.

### Why the trade counts differ, and why it is the best line in the run

118 trades for the control against 173 for the candidate, where the
discovery set had 180 for both. That asymmetry had to be explained
before anything could be called a result, because comparing win rates
across different trade populations is weaker than comparing them on the
same one.

The rejection histograms settle it:

    shipped     ... ('max drawdown', 93) ...
    part_0.75   ... no drawdown refusals at all ...

`max_drawdown_pct` is 0.10, which on a $5,000 account is $500. The
control's drawdown reached **$521.70** and breached it, so the risk
engine refused 93 setups. The candidate's peaked at **$442.74** and
never did.

So the difference in n is not a simulator artifact and not slot
contention. It is the control drawing down far enough to switch itself
off. A configuration whose losses trip its own safety limit is worse in
a way the per-trade numbers alone do not capture, and the candidate's
lower drawdown compounds: fewer refusals, more of the sample actually
traded.

### The mechanism, visible in the exit reasons

    part_0.75   TARGET 48   STOP 118   SAME_BAR 7   = 173, of which 77 won
    shipped     TARGET 29   STOP  83   SAME_BAR 6   = 118, of which 30 won

The control's wins are its target hits and nothing else: 29 targets, 30
wins. The candidate has 48 targets but 77 wins — so **29 of its wins
exited at STOP and were still net positive**, because the partial at
0.75R was already banked before price came back. That is precisely the
mechanism the configuration was chosen for, showing up in the data
rather than being asserted.

## Decision

`EXEC_ENABLE_PARTIAL_TP` now defaults to **true** and
`EXEC_PARTIAL_TP_AT_R` to **0.75**.

Project rule 12 puts partials off "until evidence justifies them". That
is a standard of evidence, not a permanent ban, and refusing to act on
a test I designed, pre-registered and passed would make the test
theatre. This is the strongest evidence in this repository for any
setting: a rule fixed before the data was read, one candidate out of
ten promoted, a held-out set that chose nothing, both conditions met
with room, and a mechanism confirmed in the exit reasons rather than
inferred.

Trailing stays off. It measured no better than nothing.

### What this is NOT

It is not profitability, and nothing here should be read as a step
towards a promise of it. Both datasets are **negative** (-0.044R and
-0.128R) and both are **synthetic**. A generated price series has no
news, no sessions that matter, no spread that widens when it hurts and
no broker that fills you badly. What survived here is the comparison
between two configurations on the same data, not a claim about either
one against a market.

The 66% win rate that was asked for was not reached and was not
approached. The best figure produced under a rule fixed in advance is
44.5% out of sample.

Reversing this is one line: `EXEC_ENABLE_PARTIAL_TP=false`.

## The next experiment, and why it is not this one

`tierA_part1.00` returned +0.005R on the held-out seeds with 50.1% of
its resamples in profit — the only configuration anywhere in this
document that is not distinguishable from break-even in BOTH runs. It
was declared non-promotable before it ran, because 62 and 57 trades are
too few, and it stays non-promotable. Nothing above has been moved on
account of it.

It earns its own experiment, pre-registered separately, on more data.
Two things make that worth doing rather than just tempting: the tier
floor only started working at all in commit aaae9e9, so it has never
actually been tested before this document; and the A/B grade boundary
separating while the continuous score behind it ranks at random
(+0.037 correlation) is an unexplained fact about the scorer, not a
tuning opportunity. The right next step is to understand that, not to
raise a threshold and hope.
