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

## Results

To be filled in below, from `sweep2_disc.out` and the held-out runs.
