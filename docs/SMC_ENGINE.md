# SMC Engine

The strategy is Smart Money Concepts: structure, liquidity, and imbalance.
The engine is deterministic and non-repainting — the same candles always
produce the same answer, and an answer never changes once later candles
arrive.

## The look-ahead guarantee

This is the property everything else rests on.

A fractal pivot at bar *i* cannot be known until `window` bars **after** it
have printed. A detector that iterates forward in time and consults a swing
at bar *i* while standing at bar *i+1* is reading the future. That produces a
backtest that cannot be reproduced live and a "BOS" that un-happens.

Two mechanisms enforce it:

1. Every `SwingPoint` carries **`confirmed_index`** — the earliest bar at
   which it is knowable. Every downstream detector filters on
   `confirmed_index <= current_index`.
2. The backtester hands the engine `candles[:i+1]` and nothing else. The
   future is not in the data.

`tests/test_backtest.py::test_analysis_on_bar_i_cannot_see_bar_i_plus_one`
asserts this directly: re-running the engine on a truncated series must
produce the identical verdict it produced with the later bars present.

## Timeframes

| Timeframe | Role |
| --- | --- |
| **H1** | The trend. Disagreement raises the bar; it is never a veto. |
| **M15** | Execution: the trigger, the entry zone, the levels. |

Two, not three. H4 sat above H1 as macro context and was removed: at 240
minutes it is sixteen times the execution timeframe, and a bias that coarse
is stale relative to the entries it was judging. H1 is four times M15 — the
ratio trend-following actually uses — so the trend is now read where it can
still be acted on.

Direction is **not** taken from any single timeframe's bias. `bot/smc/mtf.py`
classifies *both* directions from the M15 evidence and takes the better one,
because deriving direction from one timeframe made the answer depend on which
timeframe was consulted rather than on what the market had done — and made a
reversal impossible to find, since a reversal is by definition the direction
the primary bias does not point in.

Each direction is classified, and the classification sets a **score floor**
rather than a yes/no:

| Classification | Meaning | Floor |
| --- | --- | --- |
| `CONTINUATION` | With the H1 trend | the configured B tier |
| `CONTINUATION` | H1 trendless; M15 structure is the only anchor | `MTF_FLOOR_NO_HTF_CONTEXT` |
| `RANGE_ROTATION` | H1 trendless; swept range extreme | `MTF_FLOOR_RANGE_ROTATION` |
| `REVERSAL` | Against H1, with sweep + displacement + CHoCH | `MTF_FLOOR_REVERSAL` |
| `RETRACEMENT` / `NOISE` | Against H1 without that evidence | never traded |

A floor is a one-way ratchet: it is `max(tier_b, floor)`, so a classification
can demand more evidence than the build does and never less.

**Retracement vs reversal** is the distinction the whole layer turns on. A
move against the H1 trend is only a reversal when it has swept liquidity
of real significance, displaced away from it, and printed a **CHoCH** — a
break against the prevailing M15 trend. A break in the direction the move was
already travelling is continuation of a pullback. Distance travelled is not
evidence, and treating it as evidence is how a system sells the bottom of one.

Between two directions that both classify, the **most recent** trigger wins:
on an execution timeframe recency is the signal, and ranking by
classification first let a stale continuation outrank a fresh reversal — the
engine took the side the market had just turned away from. Quality breaks a
tie on the same candle, and the classification preference breaks a tie on
both, so the result never depends on loop order.

A recency *veto* used to sit alongside that sort, letting a refused direction
stand down a staler opposite one. It is gone with H4, which is what made it
reachable: the only refusal that ever carried counter-evidence was a reversal
declined for fighting the macro as well as the trend. What remains is
`RETRACEMENT` — this layer saying the counter move has **not** earned the name
reversal, which is the definition of the pullback the strategy enters on.
Letting that veto rejected the setup the guard existed to protect, on three
symbols in one live scan.

### Signal states

`NO_TRADE`, `WATCH` (context favourable, trigger incomplete), `VALID_SETUP`
(structurally valid, entry conditions unmet) and `TRADE`. A state is a label,
never a permission — only a candidate can become an order.

## Swing structure

Strict fractal pivots: a high must strictly exceed every high in the window
on both sides. Strictness avoids emitting a cluster of identical pivots
across a flat range, which is the main source of swing noise.

### Reading trend from one side

A clean impulsive uptrend often prints **no confirmed pivot high at all** —
each new high is immediately exceeded, so no fractal ever completes.
Requiring both higher highs *and* higher lows would report "range" at the
most trending moment. So one side establishing a direction is sufficient, as
long as the other side does not contradict it.

### The swept-pivot correction

A stop run prints a textbook lower low: price spikes under an old low and
closes straight back above it. Counted literally, that reads as *bearish
structure* at the exact moment the setup is bullish — which is how a
structure-following system ends up fading its own signal.

`without_swept_points()` removes pivots that a confirmed sweep created and
price immediately reclaimed. A swept pivot is not structure; it is liquidity
that has been removed. This is the single most consequential correction in
the engine.

## Break of Structure / Change of Character

A break requires **all** of:

- a **close** beyond the level, never a wick;
- clearance of an **ATR-scaled buffer**, so a one-tick poke is not a break;
- a **confirmed** swing as the broken level (the look-ahead guard);
- the level not having been broken before — re-crossing an old level is not
  a new event.

BOS continues the prevailing direction; CHoCH is the first break against it.
The prevailing direction is tracked *forward* through the series, not
recomputed from the end (which would be hindsight).

Displacement on the breaking candle is recorded so the scorer can tell a
decisive break from a drift-through.

## Displacement

"A large candle" is not displacement. Four conditions together:

1. body large relative to **ATR** — not relative to the last five bodies,
   which collapses during a squeeze and fires on noise;
2. body **dominating its own range** — a wide candle closing mid-range is a
   rejection, not displacement;
3. **directional continuity** with the net move across the impulse window;
4. an **imbalance left behind**, or a multi-candle impulse leg.

Quality is continuous (0–1) so the scorer can reward strength rather than
treating a threshold crossing as binary.

## Liquidity

### The map

Where the stops are:

- **equal highs / equal lows**, clustered with an ATR-scaled tolerance —
  more touches, more liquidity, higher weight;
- **swing liquidity** — recent pivots (internal);
- **previous day high / low**;
- **session high / low** — Asian, London, New York.

Buy-side sits above highs, sell-side below lows. Each level carries a weight
reflecting how many participants watch it: previous-day and session extremes
outrank an arbitrary intraday pivot.

A level is invisible before the bar at which it becomes knowable.

### Sweeps — five stages, graded

A wick through a level is necessary and nowhere near sufficient:

1. **identifiable liquidity** — a level with real significance;
2. **approach** — price came from the correct side;
3. **taken** — wick through, close back;
4. **rejection** — the close rejects a meaningful share of the excursion;
5. **reaction** — displacement and/or a structure shift shortly after.

Stages 1–4 are knowable on the sweep candle. Stage 5 needs the bars after it,
so every sweep carries a `confirmed_index` and the entry logic uses that.

Quality blends level weight, rejection ratio, excursion size, displacement
quality and whether structure shifted. A bare wick scores low and a
liquidity-grab-plus-displacement-plus-CHoCH scores high.

## Fair value gaps

Three-candle imbalances, with a full lifecycle: size (ATR-relative), age,
partial fill, mitigation, invalidation, and whether displacement created
them. Mitigation is evaluated bar by bar going *forward* — never by looking
at the last candle and asking "did price ever come back", which would be
hindsight.

Only live (unmitigated, un-invalidated, recent) gaps are entry candidates.

## Order blocks

The last opposing candle before a displacement leg — **but only when that leg
did something**: broke structure, or left an imbalance. A pullback candle
before a move that achieved nothing is not an order block; it is a red
candle. Strength blends displacement quality, whether structure broke, and
whether an imbalance formed.

## Premium / discount

The dealing range is anchored on the most recent *confirmed* external swing
high and low, so it moves with structure instead of being a fixed window.
Buying is preferred in discount and selling in premium — but this is a
**graded preference, not a veto**, worth six points of one hundred. A
top-tier structural setup is not discarded because a simplistic 50% rule
disliked it.

## Regime

Trend is classified from directional efficiency (net movement ÷ total path
travelled) combined with the consistency of recent structure breaks.
Volatility is classified against the ATR distribution of the lookback
window. **Extreme volatility is a hard veto** — trading a structure break
inside a violent news expansion is how a stop-loss becomes a slippage event.

## Sessions

Asian / London / New York / overlap, plus the FX weekend. The Friday edge is
deliberately an hour early: the last hour before the close has thin
liquidity and widening spreads, which is when a stop is most likely to be
hit by a spread spike rather than by price.

## From structure to levels

This is where the previous build was most wrong: it asked an LLM for the
entry, stop and target and submitted those numbers to the broker verbatim.

Here every level is derived from structure:

- **Entry** — the current price, and only if price has actually retraced
  into the chosen FVG or order block. Without that gate the engine would
  chase an extended move and anchor its stop at the far side of the impulse.
- **Stop** — beyond the entry zone's far edge plus an ATR buffer; extended to
  the sweep extreme only when the sweep is the relevant invalidation. The
  protected swing level is respected as the *structure-exit* trigger, not as
  the initial stop — anchoring there makes the stop as wide as the whole leg
  and destroys the R:R the setup was selected for.
- **Target** — the next opposing liquidity pool, stopping slightly short of
  it (the fill happens on the way in, not where everyone else's orders sit).
  If that pool is too close to justify the risk, an R-multiple projection is
  used instead.

A setup whose structural stop is under 0.35 ATR (too tight to survive noise)
or over 3.5 ATR (structurally meaningless) is rejected, as is one whose
structural R:R falls below the configured minimum.

## Setup scoring

A deterministic 100-point score, with the weights chosen by evidential value
rather than fitted to a backtest:

| Component | Weight | Why |
| --- | --- | --- |
| Trigger quality | 25 | The sweep/structure event *is* the edge. |
| Context | 18 | How much the setup has to fight (see the table above). |
| Displacement | 14 | Proves intent behind the move. |
| Entry zone | 12 | A fresh, displaced POI beats a stale one. |
| Risk/reward | 12 | Expectancy scales directly with it. |
| Regime | 10 | The same setup is worth less in a dead range. |
| Location | 6 | Premium/discount preference, deliberately small. |
| Session | 3 | A tiebreak, not a thesis. |

Three components are **critical** and cannot be outvoted by the other five:
trigger, entry zone and risk/reward. Each must reach
`MTF_MIN_CRITICAL_COMPONENT_FRACTION` of its own weight, or the setup is
NO_TRADE whatever the total says. A setup missing one of those is not a weak
trade; it is not a trade.

R:R saturates at 4R: rewarding an 8R target encourages picking targets price
will never reach.

Three **hard gates** bypass the score entirely — a setup missing its trigger
or entry zone, trading into timeframe conflict, or sitting in a session too
thin to trade, is not a weak trade. It is not a trade.

Tiers: **A+** ≥ 80, **A** ≥ 68, **B** ≥ 56, otherwise **NO TRADE**. Most
scans end in NO TRADE, and that is the intended behaviour.

## Indicators

There are none beyond ATR. RSI, MACD, ADX and volume profile would add
parameters without adding independent information to a structure-based
strategy, and every added parameter is another degree of freedom for a
backtest to curve-fit against.
