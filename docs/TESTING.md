# Testing

```sh
python3 -m pytest        # the full suite; no network, no broker, no clock drift
npm run typecheck        # TypeScript
npm run build            # production frontend build
npm run verify           # all three
```

The suite is offline by construction: the broker is an in-memory fake, the
news feed is seeded, and every test that touches time pins it explicitly.

## Principles

**Tests never weaken production validation to pass.** If a test fails, the
question is which side is wrong — several tests in this suite found real
bugs and the *code* changed:

- break-even silently failed at exactly 1.0R because floating-point price
  arithmetic put it at `0.9999999999999556`;
- a losing streak reset whenever a day had no closed trades, so a weekend
  erased it;
- `/healthz` returned 500 instead of 503 when the database was down — the one
  endpoint that has to work when everything else is broken;
- a perfectly flat market was classified as "expanded" volatility because
  the upper quartile equalled the current value.

**Scenarios are hand-built, not random.** A random walk proves nothing about
whether a detector found the *right* swing. `tests/fakes.py` constructs each
market stage by stage — trend, pullback, equal lows, sweep, displacement,
retracement — so the correct answer is known by construction.

**The important logic is not mocked away.** The fake broker replaces the
network, not the engine. Risk, sizing, scoring, structure detection and the
executor all run for real.

## What is covered (280+ tests)

### Safety
DEMO verification passes only on positive proof and fails on absent,
ambiguous, or live metadata; a live URL is refused at config load and beats a
demo-looking account; `require_demo` cannot be disabled; the kill switch
survives a restart, keeps its first reason, and refuses to clear a
safety-class trip without force.

### SMC detectors
Swing confirmation timing (`known_highs(3)` is empty for a pivot confirmed at
4); strict pivots do not fire on a flat range; an impulsive uptrend with no
confirmed pivot high still reads bullish; a wick through a level is not a
break; each level breaks once; **no structure event ever references an
unconfirmed swing**; a stop run does not flip the bias; displacement requires
body dominance, not just size; a bare wick is not a sweep; sweep quality
rises with displacement and structure; FVG mitigation retires an entry zone;
an order block requires a consequential impulse; premium/discount grading;
regime separates a trend from a dead range and vetoes extreme volatility;
session boundaries and the FX weekend.

### Risk and sizing
Exact lot maths for quote-currency, base-currency and cross-currency
instruments plus metals; a cross without a conversion source **fails** rather
than assuming 1.0; lots round down; a sub-minimum position is declined rather
than rounded up; the margin ceiling; every hard limit blocking a trade; the
kill switch blocking everything; cooldowns; correlated-exposure stacking;
risk scaling with tier; **risk only ever decreasing after losses**; the clamp
holding under absurd configuration; the profit objective rejecting a trade it
cannot reach without ever raising size.

### Market data
Forming candles removed; duplicates dropped; chronological ordering; stale
series refused; too few candles refused; a gappy series refused; the weekend
gap not counted as a hole; malformed OHLC rejected at the type boundary;
spread judged against ATR, stop distance and target distance; multi-timeframe
fetch failing as a unit.

### Execution
The same setup always yields the same execution id and any change yields a
different one; the database physically rejects a duplicate intent; the full
lifecycle is persisted (`INTENT_CREATED → SUBMITTED → ACKNOWLEDGED → FILLED`);
**running the same plan twice submits exactly one order**; a position opened
by a concurrent cycle aborts this one; an unresolved intent blocks a new
order; **an ambiguous submission is never retried**; a rejection is
permanent; an acknowledged order with no position is ambiguous; a wide
spread aborts before submission; a live environment blocks submission; a
broken database blocks order creation; missing broker protection is
repaired.

### Reconciliation and crash recovery
An ambiguous intent that *did* fill is resolved to FILLED; one that never
landed is ABANDONED; an unknown broker position is adopted as ORPHANED
(idempotently); a trade the broker no longer has is closed with its real
P/L and the daily counter updated; a missing stop is restored from the
recorded plan; **a position survives a process restart** with its protection
and risk accounting intact, and the restarted process does not open a
duplicate.

### AI
Prose-wrapped and fence-wrapped JSON is extracted; unparseable output raises
rather than being guessed at; every contract violation (missing field, wrong
type, out-of-range confidence, unknown decision, empty reason) resolves to
NO TRADE; a contradicting direction is rejected; **AI-proposed levels never
replace the structural ones**; a nonsensical stop/target pair invalidates the
approval; weak setups never reach a provider; all providers failing means no
trade by default.

### Integration
The full pipeline to a persisted trade; the levels sent to the broker are the
engine's; a flat market produces NO TRADE with a reason; every decision is
journalled; the kill switch, pause flag and daily-loss auto-trip each stop
the scan; one symbol failing does not kill the scan; **several qualifying
setups still produce one trade**; management runs while scanning is paused;
health is honest about a broken database and an unverified environment; the
dashboard reports OFFLINE rather than inventing numbers, and has no method
that could open a trade.

### Backtest integrity
**Analysis on bar *i* cannot see bar *i+1*** — re-running on a truncated
series must produce the identical verdict; a bar touching both stop and
target resolves as the stop; costs make results worse; a stop fills worse
than the stop price; a trade cannot close on its own entry bar; walk-forward
folds never overlap; too little data is an error, not a result; a thin
out-of-sample sample is labelled inconclusive rather than claimed as an edge;
Monte Carlo needs a real sample, is reproducible for a seed, and always
carries its disclaimer.

### Transport and service
Reads retry on rate limiting; retries are bounded; **writes are never
retried**; a lost write response is ambiguous, not a failure; auth errors
surface immediately; the circuit opens and sheds load; backoff uses jitter;
`Retry-After` is respected; access tokens and configured secrets are redacted
from logs; `/healthz` returns 503 when a critical component is down; command
endpoints require the token when configured while reads stay open; the API
never leaks a secret; graceful shutdown does not touch positions.

## Strategy validation, honestly

Software correctness is proven by the suite above. **Strategy quality is a
separate question** and this repository does not claim to have answered it.

`bot/backtest/` provides the tools:

- `engine.py` — sequential, pessimistic fills (entry pays the spread, stops
  slip, a bar touching both levels resolves as the stop, commission per
  side);
- `walkforward.py` — train → validate → out-of-sample, with a deliberately
  tiny parameter grid, selecting on expectancy rather than total profit
  (which would just reward whichever setting traded more);
- `montecarlo.py` — drawdown dispersion, losing streaks, risk of ruin, always
  with the disclaimer that reordering history is not a forecast.

Running these against real broker history is the necessary next step before
any claim about edge. The synthetic fixtures prove the machinery is correct;
they cannot prove the strategy is profitable, and the code says so where it
reports results.

## End-to-end smoke test

`scripts/demo_service.py` boots the **real** `BotService` against the
in-memory fake broker. Only the network boundary is replaced — the
orchestrator, risk engine and executor all run for real — so it exercises the
full Node → Python → dashboard wiring without broker credentials:

```sh
# terminal 1
BOT_PORT=8799 python3 scripts/demo_service.py

# terminal 2
BOT_EXTERNAL=true BOT_PORT=8799 PORT=5055 node index.js

curl -s localhost:5055/healthz
curl -s localhost:5055/api/snapshot
curl -s -X POST localhost:5055/api/control/scan -d '{}' -H 'Content-Type: application/json'
```

Note that on a weekend the scan will correctly answer `NO TRADE — forex
market is closed`, and with data older than a few candles it will answer
`refusing to trade stale data`. Both are the guards working, not failures.

## Adding a test

1. Build the scenario explicitly in `tests/fakes.py` if the market shape is
   new.
2. Assert the specific correct answer, not just "something was returned".
3. If it fails, decide which side is wrong before changing either.
4. If you change strategy behaviour, bump the matching version in
   `bot/version.py` — performance analysis groups results by those strings.
