# Autonomous trading intelligence — architecture review

Written before any code for this phase, as the brief requires. It answers
the ten questions of "FIRST TASK", then states the plan. Every claim about
the codebase was checked against it; every claim about the data was
measured.

---

## 0. The honest prior, stated first

Thirteen strategy families were tested on this data under pre-registered
rules and realistic costs. **None has an edge, and — more importantly —
none has a meaningful edge even BEFORE costs**: SMC's frictionless run gave
+0.016R (t 1.68); the eight-family program sat between −0.07R and +0.02R
per trade gross. Simple price-derived rules on these instruments, at these
horizons, carry almost no directional information.

A machine-learning system is a more flexible function of the same inputs.
It can find interactions no single rule expresses, so it is worth building.
But flexibility cuts both ways: **the freer the search, the more certainly
it finds patterns in noise.** The brief's call for autonomy and its call
for rigour pull in opposite directions, and the architecture below is
mostly about holding the second while granting the first.

The most likely outcome of this phase, on current evidence, is a system
that correctly concludes it has no edge and outputs NO_TRADE nearly all the
time. That outcome is a success of the system, not a failure of it. This
document does not promise otherwise.

---

## 1. Current architecture

```
node index.js  (Express 5; DASHBOARD_TOKEN gate, rule 15)  →  Python service (bot/service.py)
                                                                  │
 scheduler → orchestrator.scan():                                 │
   DEMO guard → account state → RISK STATE → per symbol:          │
     news → market data (M15 400 + H1 300) → strategy (SMC)       │
     → scorer (8 components, tiers) → risk engine → AI veto       │
   → rank → executor (intent → demo check ×2 → spread → submit)   │
   → reconciler ↺ → position manager (BE, structural exit)        │
 storage: SQLite / Postgres (13 tables), React dashboard (14 panels)
 research: bot/backtest (real engine, windowed), bot/research (daily +
           general engines, 8 families), scripts/ (offline runners, gates)
```

Deployed on Railway from a two-stage Docker image. Production Python has
**no third-party dependencies beyond psycopg**. 833 Python tests and 12
server tests.

## 2. What is already reliable (tested, and exercised live)

| Component | Why it is trusted |
|---|---|
| DEMO guard (`safety/demo_guard.py`) | two independent signals, four checkpoints, fails closed |
| Risk engine (`risk/engine.py`) | single sizing authority; only ever reduces risk on adverse state; per-symbol bench; correlation caps |
| Executor + reconciler | write-once orders, `AmbiguousExecution` → broker query, never resend; ambiguous management actions no longer repeat |
| Kill switch, drawdown halt | fired correctly on all 12 symbols in the historical measurement |
| Storage / repositories | persistent intents, trades, journal, equity, spread samples; survives restarts |
| Spread recorder | the only live microstructure measurement we have |
| Dashboard auth split by direction | tested over real HTTP |
| Research engines + causality tests | every decision checked against truncated history; injected leaks are caught |
| Data loader | price scale derived and refused when ambiguous; server time → UTC |
| Pre-registration discipline | four experiment documents, every rule committed before its run |

## 3. What must be preserved

All of section 2, plus **the research history as data**: the four
`docs/EXPERIMENT_*.md` files, the scripts that produced them, and the
per-trade records they generated. Every recorded result stays reproducible
from its commit. The SMC and reversion code stays importable for that
reason even though neither will make decisions.

## 4. What should be replaced or isolated

| Component | Action | Reason |
|---|---|---|
| SMC engine as decision-maker | **isolate** — becomes one feature source among many | measured no edge; its outputs may still carry information in combination |
| Scorer (8 hand-weighted components) | **retire from the decision path** | hand-set weights; score was shown not to rank outcomes |
| Strategy registry / dashboard switch | **replace** with model selection by version | a strategy is now a trained, versioned artifact, not a hand-written class |
| LLM veto (Gemini/Groq) | **keep as optional subtractive layer; never in research** | see section 8 — it cannot be backtested without leakage |
| News filter | **keep for live gating only** | the feed is this-week only; no history exists to learn from |
| Telegram | **new** | there is none in the codebase |

## 5. The proposed architecture

```
MARKET DATA (broker, recorded spreads, recorded swaps)
  ↓
DATA VALIDATION  (existing: forming-bar removal, staleness, gaps, scale, clock)
  ↓
FEATURE STORE  — one causal implementation shared by research and live
  ↓
MARKET STATE   — regime clusters + FAMILIARITY (distance to training data)
  ↓
MODELS         — calibrated E[R | state, action] per action template,
                 with uncertainty; baselines always run alongside
  ↓
DECISION       — structured record written BEFORE any order:
                 trade only if the LOWER bound of expected R after costs
                 clears zero AND the state is familiar; otherwise NO_TRADE
  ↓
RISK ENGINE    — unchanged authority (+ funded-account compliance layer)
  ↓
EXECUTOR → BROKER → RECONCILER   (unchanged)
  ↓
JOURNAL / MEMORY → OUTCOME ATTRIBUTION → RESEARCH LOOP
                                       ↺ (offline, versioned, gated)
```

**How the AI gets freedom without becoming unfalsifiable.** It chooses, per
moment and instrument: whether to act, direction, and an action *template* —
a (stop distance, target or exit rule, horizon) pair drawn from a finite,
declared menu. It also chooses which features matter, since the models
learn that; which instruments to ignore (familiarity and expected value per
instrument); and when to do nothing (the default). What it does not get is
an unbounded continuous search over entry/exit rules, because that makes the
number of hypotheses tested uncountable, and an uncountable number of
hypotheses cannot be corrected for. The menu can grow through the research
loop — each addition registered as a trial.

**The research loop is offline and gated.** Observe → hypothesis (from a
person, from the LLM, or from automated feature search) → registered
experiment → train on training data only → validate → if promising, a
challenger model → compared against the live champion on data after the
champion's training period → promoted only by a pre-registered rule →
versioned, reversible. Nothing in the live loop changes model weights. A
losing trade changes nothing except the memory.

**Random outcome versus systematic error.** Each live decision carries a
predicted distribution of R. Calibration is monitored with a sequential
test (CUSUM on standardised prediction error): a run of losses inside the
predicted distribution is recorded as noise; a sustained drift beyond it
flags the model for review and can only ever reduce its risk budget or stop
it.

## 6. What data is actually available

| Source | Coverage | Usable for |
|---|---|---|
| ejtraderLabs OHLC + tick volume | 12 instruments, M15–D1, 2012-11 → 2022-03, UTC-corrected | research, training |
| Broker candles (TradeLocker, live) | recent history, any symbol offered | live features; **the sealed final test** (see §9) |
| Broker bid/ask quotes | live only | live execution, spread recording |
| Recorded spread samples | since the recorder shipped | cost model calibration, going forward |
| Account, positions, fills | live | execution quality, outcome attribution |
| ForexFactory calendar | this week only | live news gating; **not** research |
| Previous research | 13 families, ~60,000 simulated trades | prior knowledge, meta-analysis |

## 7. What data is missing

Real bid/ask history (spread is a modelled constant in research); real
traded volume (spot FX has none — tick counts only); order book depth;
historical swap rates (blocks carry research); a news and economic-calendar
archive; macro and rate data; sentiment; any data after 2022-03 in the
research set; tick data. **None of these may be simulated and presented as
real.** A feature that needs one of them is not built until the data exists.

## 8. Which AI/ML approaches are realistically feasible

| Approach | Verdict | Why |
|---|---|---|
| Regularised linear / logistic models | **yes — first** | strong baseline, stable, few degrees of freedom |
| Gradient-boosted trees | **yes** | captures interactions without a GPU; trained offline, exported, inferred in pure Python so Railway needs no new dependencies |
| Probability calibration + uncertainty (ensembles, bootstrap, conformal intervals) | **yes — required** | the decision rule acts on a lower bound, not a point estimate |
| Regime clustering, familiarity / out-of-distribution scoring, nearest-analog retrieval | **yes** | answers "have I seen this before?" with data, not prose |
| Small neural sequence models | **later, only if the simpler models show signal** | CPU-only training is feasible, but on data with this little signal they overfit first |
| Reinforcement learning | **no, for now** | needs a signal to exploit; on ≈zero-edge data it learns the noise, and it is the hardest family to validate honestly |
| LLM as the trader | **no** | see below |
| LLM as research assistant | **yes** | proposing hypotheses, formalising them into registered experiments, summarising results |

**Why an LLM cannot be in the backtested decision path.** A language model
trained on internet text *knows what happened* after most dates in 2012–2022
— rates, crises, the direction of the dollar. Asking it to decide on
EURUSD in March 2015 is look-ahead bias that no code can remove, because the
leak is in the model's weights. So an LLM's trading judgement cannot be
validated on this history at all, and an unvalidated component does not get
to trade. It can still veto (rule 5), and it can still help research, where
its ideas are tested on data by code.

## 9. How overfitting and leakage are prevented

**Leakage**
1. Features are pure functions of `bars[:i+1]` (plus other causal inputs), one
   implementation for research and live. Every feature is run through the
   existing per-decision truncation test.
2. Normalisation statistics (means, scales, cluster centres) are fitted on
   training data only. A z-score computed over the whole series is a leak and
   is tested for.
3. Labels look forward by design, so training rows whose label window
   overlaps a validation or test period are **purged**, with an **embargo**
   gap after each boundary.
4. Adversarial tests: a feature built from the future return is injected on
   purpose, and the pipeline's checks must reject it — both the truncation
   test and an "implausible validation skill" alarm.
5. The LLM never sees research data or results for periods it might
   remember as a trader.

**Overfitting**
1. **An experiment registry** records every hypothesis, variant and parameter
   tried, and which data segments it touched. It is the denominator every
   significance claim is divided by.
2. **The 2019–2022 segment is already contaminated.** Thirteen families were
   judged on it. It can still train and validate; it can no longer serve as
   an untouched test.
3. **The sealed final test is broker data from 2022-03 to the present**, which
   no one in this project has used for research. It is exported and
   evaluated **once, on Railway, by a committed script**, and only the
   pre-registered verdict is read. It cannot be looked at twice.
4. Significance is corrected for every trial in the registry (Holm), and
   Sharpe-type metrics are deflated for the number of trials.
5. Every model must beat, net of costs, three baselines: NO_TRADE; random
   entries with the same frequency, holding time and costs; and a trivial
   momentum sign. A permutation test (labels shuffled in time blocks) must
   place the real result above the 99th percentile of shuffled results.
6. The validation segment has a reuse budget; exceeding it marks it
   contaminated in the registry, automatically.

## 10. How we will know whether the AI discovered an edge

All of these, pre-registered before the sealed test is opened:

- walk-forward net expectancy after full costs has a **positive lower
  confidence bound**, corrected for every trial in the registry;
- it beats all three baselines and the permutation distribution;
- it holds across instruments and years (the six-gate logic already built);
- it survives costs ×1.5 and ×2, one bar of added latency, and dropped data;
- its predictions are **calibrated**: predicted and realised R agree;
- it passes the sealed 2022→present test, run once;
- then PAPER on live data lands inside the backtest's predicted range, then
  DEMO inside paper's.

Failing any one of these is reported as failing it.

---

## The money goal, as arithmetic

Monthly income ≈ equity × risk per trade × trades per month × average R.

| Assumed edge | Risk | Trades/month | Monthly return | Equity for $1,000/month |
|---|---|---|---|---|
| +0.10R (would be excellent) | 0.5% | 20 | 1.0% | $100,000 |
| +0.05R (would still be good) | 0.5% | 20 | 0.5% | $200,000 |
| −0.08R (what was measured) | 0.5% | 20 | −0.8% | — |

Even a genuine +0.10R edge has a monthly standard deviation of about
±2.5% at that risk, so roughly one month in three would lose. The target is
an input to sizing arithmetic, never an instruction to trade.

---

## Build order

| Stage | Deliverable | Gate to pass before the next |
|---|---|---|
| 4 | Research environment: experiment registry, sealed holdout, feature store, cost-aware labels, purged walk-forward splitter, leakage suite | adversarial leakage tests fail on injected leaks |
| 5 | Models and decision layer: baselines, linear, GBM, calibration, familiarity, structured decision record | beats baselines on validation, or the report says it does not |
| 6 | Memory and outcome attribution | as-of queries cannot return future rows |
| 7 | Walk-forward + robustness battery + registry-aware significance | pre-registered edge criteria written |
| 8 | Sealed final test, run once on Railway | pass, or stop and report |
| 9–10 | Risk-engine integration, compliance layer, Telegram, dashboard; PAPER | paper inside the predicted range |
| 11 | DEMO | demo inside paper's range |
| 12 | Evidence review for real / funded | only if every gate above passed |

If a gate fails, the system still ships — as a monitored, recording,
NO_TRADE system — and the report says which gate and why.
