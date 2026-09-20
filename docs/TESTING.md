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
  the upper quartile equalled the current value;
- **break-even could place a stop already through the market** — found by the
  paper simulator, which filled it instantly at a flattering price. A live
  broker would have rejected it;
- **a sustained volatility spike contaminated its own baseline**: because
  "extreme" was judged against the upper percentiles of a window that already
  contained the spike, a flash crash was reclassified as merely "expanded"
  within a few bars, and the engine would have resumed trading structure
  breaks inside a crash. Now judged against the window's median;
- **a concurrency race escaped as a raw `sqlite3.IntegrityError`**: two
  threads could both pass the intent existence check. The UNIQUE constraint
  still prevented the duplicate order, but the losing thread crashed instead
  of reporting DUPLICATE;
- **a broker suffix (`EURUSD.R`) silently disabled three safety controls.**
  Symbol identity is the join key for the duplicate-order check, the
  per-symbol limit, news blackouts and correlation — and all four compared
  decorated names as strings, so every one of them failed OPEN. Reported by
  the operator, reproduced, and closed by canonical resolution.

**Scenarios are hand-built, not random.** A random walk proves nothing about
whether a detector found the *right* swing. `tests/fakes.py` constructs each
market stage by stage — trend, pullback, equal lows, sweep, displacement,
retracement — so the correct answer is known by construction.

**The important logic is not mocked away.** The fake broker replaces the
network, not the engine. Risk, sizing, scoring, structure detection and the
executor all run for real.

## What is covered (425 tests)

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
carries its disclaimer; reordering cannot produce a range for the total (a
sum does not care about order, so the three "return percentiles" it used to
print were one constant under three names) and the bootstrap, which draws
with replacement, is the part that can.

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
- `montecarlo.py` — two separate questions kept separate. REORDERING the
  same trades answers "how bad could the drawdown have been?" and says
  nothing about the total, because every shuffle ends on the same figure.
  BOOTSTRAPPING (drawing the same count with replacement) answers "how wide
  is the total, given a sample this small?". Both carry the disclaimer that
  neither is a forecast.

Running these against real broker history is the necessary next step before
any claim about edge. The synthetic fixtures prove the machinery is correct;
they cannot prove the strategy is profitable, and the code says so where it
reports results.

### Live data integration (33 tests)
Object bars, positional bars, every known envelope key, nested envelopes and
bare-list responses all decode; a malformed bar is dropped rather than
defaulted; a wick inconsistent by a rounding tick is clamped rather than
discarded; overlapping pages deduplicate; bars come back chronological.
Discovery finds the working endpoint shape and caches it; a 4xx skips to the
next shape; **a transport failure aborts discovery instead of caching a wrong
conclusion**; a learned shape that stops working is re-probed; the requested
window over-fetches to survive weekends. The doctor renders, redacts secrets,
and is structurally read-only (asserted by source inspection).

### Paper trading (28 tests)
**No write ever reaches the broker** — submitted, modified and closed lists
all empty. The DEMO guard still applies, so paper-over-a-live-account is
refused. A buy crosses the spread and pays slippage; a sell crosses the other
way; commission reduces the balance. A fill landing outside the plan's levels
is refused. Paper refuses what the live broker would refuse (non-positive
quantity, missing stop or target, a stop already through the market). A stop
closes worse than its price; a target closes exactly at the target; **the
configured minimum R is actually booked at target**; both levels in
one window resolves as the stop; a level reached between polls is not missed;
an unreadable price leaves the position open rather than closing it. Equity
marks to live prices while balance moves only on a realised close; positions
survive a restart; the starting balance adopts the real account when unset;
partial closes reduce the position; order history is shaped like the broker's
so the reconciler can read it. The whole pipeline runs in paper mode without
touching the broker, the mode is visible in health, and reset is refused
outside paper mode.

### Suffixed broker symbols (46 tests)
Every decoration (`.R`, `_i`, `m`, `.pro`, `EUR/USD`) resolves to the same
pair; a non-pair (`US500`, `WTI`) does not resolve and is not guessed;
`XAUUSD.R` yields quote `USD`, not `USDR`. A bare symbol finds the suffixed
instrument and vice versa; a genuinely ambiguous account (`EURUSD.R` +
`EURUSD.RAW`) still refuses to guess, and an exact name disambiguates it;
`positions()` reports the canonical symbol. Then the fail-open holes:
**the duplicate check sees an existing suffixed position**, the per-symbol
limit fires, a decorated name already in the database still matches, the
reconciler settles an intent against a suffixed position, news currencies and
correlation both resolve, and a suffixed metal sizes correctly. Finally the
whole pipeline trades a suffix-only account, a second scan does not
duplicate, `TRADED_SYMBOLS` accepts either form, and five different suffixes
all behave identically.

### Stress and extreme conditions (39 tests)
**Market:** a 20% flash crash makes the regime untradeable and produces no
setup; a weekend gap is tolerated while a mid-week gap is not; a frozen
market of identical candles yields no setup; zero and non-finite prices never
enter the engine; a series of one repeated timestamp is refused; **a feed that
stops updating goes stale rather than looking calm**.

**Execution conditions:** a spread wider than the target is rejected; a news
spread spike aborts before submission; a crossed quote is refused rather than
averaged; a zero quote is refused.

**Risk under extremes:** a nearly-wiped and a negative-balance account never
trade; an enormous equity is still capped by the broker's max lot; **risk
taken never exceeds risk approved across a sweep of five equities × three
tiers**; an absurd conversion rate cannot produce an absurd position; a
one-tick stop is refused; compound breaches all report their reasons; the
reward objective is a ratio, so no equity can make it unreachable.

**Infrastructure:** a rate-limit storm is bounded and never duplicates a
write; the circuit breaker sheds load instead of burning the scan budget; a
database failure mid-flight blocks the order; a broker reporting no position
after a fill is ambiguous, not success; **a storm of ambiguous submissions
never produces a second order**; every symbol failing leaves the scan intact
and journalled; health stays answerable with everything broken and the kill
switch fails closed.

**Paper under stress:** a gap straight through the stop books the real loss
(larger than planned risk, recorded honestly); a spread explosion prevents a
fill; the paper ledger never diverges from `starting + realised − commission`;
a broker outage mid-run does not lose the position or its protection.

**Concurrency:** four simultaneous scans produce at most one order; five
threads executing the same plan produce exactly one order.

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
