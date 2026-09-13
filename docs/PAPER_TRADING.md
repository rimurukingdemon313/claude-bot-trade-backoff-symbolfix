# Paper Trading & Live Verification

Two tools bridge the gap between "the code is provably correct" and "the code
is correct against *your* broker":

1. **`python3 -m bot.doctor`** — read-only verification of the real account;
2. **`TRADING_MODE=paper`** — the real execution path with simulated fills.

## 1. The doctor

```sh
python3 -m bot.doctor
python3 -m bot.doctor --symbol XAUUSD --symbol GBPUSD --json
```

It connects to the real DEMO account and reports, per check, what it actually
found. It is **read-only**: it calls only read methods, and a test asserts
the module contains no reference to a write path.

| Check | What a failure means |
| --- | --- |
| `credentials`, `endpoint`, `mode` | configuration is wrong; fix before anything else |
| `authentication` | login/accNum resolution failed |
| `demo_guard` | the account is not positively DEMO — trading stays blocked |
| `account_state` | balance/equity cannot be read; every risk calculation depends on it |
| `trade_config` | a column map is missing, so positional responses cannot be decoded |
| `instruments` | the account exposes no instruments |
| `SYMBOL:specification` | no usable contract size or lot step → **that symbol will be skipped** |
| `SYMBOL:quote` | no usable quote, or a crossed quote |
| `SYMBOL:conversion` | a cross pair's bridging rate does not resolve → **that symbol cannot be sized** |
| `SYMBOL:M15/H1/H4` | candles do not arrive or fail validation |
| `SYMBOL:history_endpoint` | which endpoint shape works (see below) |
| `profit_objective` | the profit floor is unreachable at this equity |

Nothing it prints contains a credential — the report goes through the same
redaction sink as the structured logs.

### History endpoint discovery

TradeLocker deployments differ in the history endpoint's path, the time unit
of the `from`/`to` bounds, and the envelope key the bars arrive under. The
previous build hard-coded one guess, so a broker using any other shape
returned zero candles and the bot **silently never traded** — no error, no
signal, just permanent NO TRADE.

`bot/broker/history.py` probes a matrix of known shapes once, caches the one
that works, and reports it. Two deliberate behaviours:

- a **4xx** on one shape means "wrong shape here" → try the next;
- a **transport failure or 5xx aborts discovery entirely** rather than
  concluding "no endpoint works" and caching that. A broker outage is not
  evidence about the endpoint.

If the learned shape later stops returning bars, it re-probes once instead of
failing permanently.

## 2. Paper mode

```sh
TRADING_MODE=paper npm start
```

### What is real and what is simulated

| Stage | Paper mode |
| --- | --- |
| Broker session, instruments, quotes, candles | **real** (live TradeLocker DEMO) |
| Market-data validation, SMC engine, scoring | **real** |
| Risk engine, sizing, limits, correlation, profit floor | **real** |
| Execution intent, idempotency guard, DEMO guard, spread gate | **real** |
| Position management, reconciliation, journal, dashboard | **real** |
| **The fill itself** | **simulated** |

Paper mode is not a parallel code path — it is the real path with the last
inch replaced. That is what makes a paper run evidence about the live run
rather than a separate program that happens to look similar.

`PaperBroker` composes the live broker rather than subclassing it,
deliberately: inheritance would let an unoverridden method reach the network.

### Fill modelling — pessimistic on purpose

| Event | Modelled as |
| --- | --- |
| Entry | cross the spread (buy at ask / sell at bid) **plus** slippage |
| Stop hit | the stop price **plus adverse slippage** |
| Target hit | exactly the target, never better |
| Both touched in one window | **the stop** |
| Commission | per lot, half on entry and half on exit |

An optimistic simulator is worse than none: it manufactures confidence in an
execution path that has not been tested.

### Protection is evaluated from two sources

A quote-only check on a 30-second loop would miss a stop reached *inside* a
15-minute candle. So each poll checks both:

1. the **current quote**, and
2. the **high/low of closed candles since the position opened**.

A test asserts a stop reached inside a candle and recovered before the next
poll still triggers.

### Safety properties, all asserted

- **No write ever reaches the broker.** `test_paper_mode_never_writes_to_the_broker`
  asserts the live broker's submitted/modified/closed lists are all empty.
- **The DEMO guard still applies.** Verification runs against the *real*
  account, so paper-over-a-live-account is refused exactly as live trading
  would be.
- **Paper refuses what the broker would refuse** — a non-positive quantity, a
  missing stop or target, a stop already through the market. Accepting an
  order the real broker would reject would hide a bug instead of finding one.
- **State survives a restart.** Simulated positions live in the database, so a
  redeploy does not erase an open paper position.
- **The ledger always reconciles**: `balance == starting + realised − commission`.

### A real bug paper mode found

Break-even moved the stop to just beyond entry once 1R was reached. If price
retraced back to entry between polls, that level was **already through the
market** — a live broker rejects it, and the simulator filled it instantly at
a flattering "break-even". Both sides were fixed: `plan_actions` now only
proposes a stop that is still behind the market, and `PaperBroker.modify_position`
refuses protection through the market the way a broker does.

That is the entire point of the mode. It is covered by
`test_break_even_is_skipped_when_price_retraced_back_to_entry`.

### Resetting

```sh
curl -X POST localhost:5000/api/control/reset-paper -d '{"confirm":true}'
```

Wipes simulated positions and the paper account. Refused outside paper mode —
there is nothing to reset on a real account, and a command that silently did
nothing would be worse than one that says why.

## 3. The recommended path to live demo orders

1. `python3 -m bot.doctor` — every check PASS.
2. `python3 -m bot.backtest --symbol EURUSD --bars 3000 --monte-carlo` against
   your broker's own history.
3. `python3 -m bot.backtest --walk-forward --folds 3` — treat a verdict of
   *inconclusive* as inconclusive.
4. Run `TRADING_MODE=paper` for long enough to see real trades close. Watch
   the decision journal for what it is rejecting and whether you agree.
5. Only then set `TRADING_MODE=demo_live`.

Steps 2 and 3 are the ones this repository cannot do for you: the strategy's
edge is unproven until it has been measured on real history.

## 4. The profit floor and account size

The configured floor is **$40 minimum expected profit at the structural
target** (`OPPORTUNITY_MINIMUM_PROFIT`), with $50 as the target. No tier,
tolerance or configuration can take a trade below the floor.

That imposes arithmetic worth understanding:

```
minimum profit  = risk × R:R
$40             = $20   × 2        (the minimum R:R)
$20 of risk     = 1% of $2,000     (the risk ceiling)
```

So the floor is unreachable below roughly **$2,000 of equity** — and at the
0.5% base risk, comfortable headroom starts around **$4,000**.

Rather than returning NO TRADE forever with no explanation,
`profit_floor_feasibility()` computes this at startup and on every health
read. When it is unreachable you get an error-level log line, a failing
`profitObjective` health component, and a banner on the dashboard naming the
equity required.

Your options if that fires: fund the demo account higher, lower
`OPPORTUNITY_MINIMUM_PROFIT`, or raise `RISK_MAX_PCT` (the build refuses
anything above 2%).
