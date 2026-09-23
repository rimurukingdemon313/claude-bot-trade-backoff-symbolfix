# Pre-registration: does the bot's built-in `reversion` mode have an edge?

**Committed before the backtester can run it, and before it has run.**

## Why this is tested at all

The dashboard lets an operator switch the bot from SMC to `reversion`. SMC
has been measured and has no edge (`EXPERIMENT_HISTORICAL_EDGE.md`). The
trend-following round found nothing either (`EXPERIMENT_TREND_FOLLOWING.md`).
`reversion` has never been measured on any history. Leaving it untested
would leave a switch on the dashboard that looks like an alternative
without anyone knowing whether it is one.

This is a single, separate question about code that already exists. It
adds nothing to the trend-following search, whose "no new candidates this
round" rule stands.

## Prior expectation, stated before running

Unfavourable. `reversion` trades the same M15 bars with the same cost
structure as SMC, fires "far more often for a smaller target" (its own
profile), and targets exactly its 1.5R floor. SMC showed no edge even
before costs. A mode with more trades and smaller targets pays more cost
per unit of edge, not less.

## Method — identical to the SMC measurement

- Real engine: `ReversionStrategy.analyze` inside `bot/backtest/engine.py`,
  windowed to the live lookback, scored by the real `SetupScorer`, sized by
  the real `RiskEngine`.
- Same dataset, same twelve symbols, `--step 4`, drawdown halt lifted for
  measurement, same costs (0.8 pip spread, 0.3 pip slippage, $7/lot).
- Design before 2017-01-01; held-out from 2017-01-01.

## Pass rule

PASSES only if, pooled across all symbols:

1. held-out avgR > 0, AND
2. held-out t > **2.0** (one test, so no multiple-comparison correction), AND
3. design avgR > 0.

A frictionless run on the four majors is reported as a DIAGNOSTIC only
(signal versus cost), exactly as it was for SMC. It cannot pass anything.

## If it fails

Then neither built-in strategy has a demonstrated edge, and the honest
state of the bot is: no strategy that should be trading.

## RESULTS — part 1: as the bot actually runs it

**Zero trades. The pre-registered test FAILS: there is no held-out avgR to
be positive.**

Diagnosed on four months of EURUSD M15 (12,000 bars, every fourth bar):

| Outcome | Count |
|---|---|
| refused inside the strategy — "may not fade a decided H1 trend" | 1,529 |
| refused inside the strategy — price too close to a range extreme | ~200 |
| **candidates produced and handed to the scorer** | **691** |
| **of those, vetoed with `entry_zone is absent`** | **691** |

The scorer treats `entry_zone` — an FVG or order block to enter from — as a
CRITICAL component: zero means NO TRADE regardless of the other seven. The
reversion strategy fades a sweep at market and never has one. So every
candidate it can produce is vetoed, by construction, forever. Even setting
that aside, its highest score was 54.9 against a B floor of 56.

**The dashboard's `reversion` mode is a dead switch.** Selecting it makes the
bot trade nothing while appearing to run a "high-frequency mode". That is
precisely the failure `scorer.py` warns about for the old dead
`SCORING_MIN_TRADEABLE` knob: "a control that silently does nothing is worse
than an absent one, because the operator draws a conclusion from it."

---

## Part 2 — pre-registered before running: does the SIGNAL have an edge?

Part 1 says the mode cannot trade. It does not say whether it SHOULD. That
decides the remedy — fix the mode, or remove the switch — so it is measured
separately.

**Method:** identical to above, except the scorer's gate is bypassed and
every reversion candidate is taken at tier B. Tier only sets position size,
and R is size-independent, so the R figures do not depend on that choice.
This is a research setting (`Backtester(score_gate=False)`), off by default,
and never available to the live bot.

**Pass rule — unchanged:** held-out avgR > 0, held-out t > 2.0, design
avgR > 0, pooled over all twelve symbols.

**What follows, fixed now:**

- **FAIL** → the switch should be REMOVED. Fixing the scorer so an edgeless
  strategy can trade would turn a harmless dead switch into a harmful live
  one.
- **PASS** → the scorer is wrong for this mode, and fixing that becomes its
  own change, with its own test, before anything goes near the account.

## RESULTS — part 2: the signal, with the scorer bypassed

| Period | Trades | Win | avgR | t |
|---|---|---|---|---|
| All | 38,971 | 48.8% | **−0.069** | **−14.30** |
| Design (< 2017) | 20,257 | 49.0% | −0.062 | −9.28 |
| Held-out (≥ 2017) | 18,714 | 48.6% | −0.076 | −11.00 |

**FAILED on all three conditions.** No symbol is positive; all twelve are
significantly negative (t from −2.23 on EURGBP to −7.59 on GBPJPY). The
prior stated before the run — more trades and smaller targets than SMC,
on the same cost structure, so more cost per unit of edge — was right.

**Consequence, as fixed before the run: the switch is removed.** Fixing
the scorer so this mode could trade would turn a dead switch into a live
one that loses about 0.07R on every trade, several times a day.

So the scorer's veto was, by accident, protecting the account. That is not
a reason to keep a control that looks like it does something and doesn't.
