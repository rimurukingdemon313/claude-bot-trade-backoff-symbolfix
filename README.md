# SMC Trading Bot — TradeLocker DEMO

An autonomous Smart Money Concepts trading system for a **TradeLocker DEMO
account**, with a mobile-first dashboard.

The system is built to be *selective and survivable* rather than busy. It
spends most of its time answering **NO TRADE**, and it is designed so that a
crash, a broker timeout, a redeploy, a bad AI response, or a losing streak
cannot turn into an unrecoverable position or a duplicate order.

> **DEMO only.** This build refuses to start against a live endpoint and
> refuses to submit an order unless the account positively identifies itself
> as DEMO, verified at four separate points. There is no configuration flag,
> API call, dashboard control, or AI response that can disable that check.

---

## What it does

```
TradeLocker candles
  → validation (closed candles only, no gaps, not stale)
  → H4 context → H1 bias → M15 structure
  → liquidity map → sweep → displacement → BOS/CHoCH
  → FVG / order block retracement → premium/discount
  → deterministic setup score → tier (A+ / A / B / NO TRADE)
  → session · news · spread
  → RISK ENGINE (single authority: sizing, limits, correlation)
  → $50+ opportunity check
  → AI validation (veto only)
  → final execution guard → TradeLocker DEMO
  → broker verification → position management → exit
  → database / journal → dashboard → performance analysis
```

## Architecture

Two processes in one container:

| Process | Responsibility |
| --- | --- |
| **`bot/` (Python)** | Everything that matters: broker session, market data, SMC engine, risk engine, execution, reconciliation, scheduler, persistence. Exposes a JSON API on localhost. |
| **`server/` (Node/Express)** | Serves the dashboard and proxies to the bot. Holds no trading state and performs no trading logic. |

The Node layer supervises the Python process and exits if it dies, so the
platform restarts the whole container rather than leaving a dashboard
serving a dead trading process.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full map.

## Broker symbol naming

If your broker decorates instrument names (`EURUSD.R`, `EURUSD_i`,
`EURUSDm`), set `TRADED_SYMBOLS` to **either** the bare pairs or your
broker's exact names — both resolve. The canonical pair is used as the
identity for duplicate detection, the per-symbol limit, news blackouts and
correlation, while the API is called with the broker's own name. See
[PAPER_TRADING.md §4](docs/PAPER_TRADING.md) for why this is a safety
property rather than a convenience.

## Execution modes

| `TRADING_MODE` | Behaviour |
| --- | --- |
| `paper` (default) | Orders are **simulated** against live broker prices. Nothing is sent to TradeLocker. |
| `demo_live` | Real orders on your TradeLocker **DEMO** account. |

Paper mode is not a separate code path with its own bugs — it is the real
path with the last inch replaced. Market data, the SMC engine, scoring, the
risk engine, execution intents, the idempotency guard, position management
and reconciliation all run identically, so a paper run is evidence about the
live path. Fills are modelled pessimistically (cross the spread, pay
slippage, stops slip further, both-levels-touched resolves as the stop,
commission per lot).

There is no third value. LIVE is not a mode this build has.

## Quick start

One command does everything — installs, prompts for your credentials
locally, builds, tests, and verifies your broker account read-only:

```sh
bash scripts/setup.sh
```

Then:

```sh
npm start                 # bot + dashboard on :5000
```

It starts in **paper** mode, so nothing reaches your account until you set
`TRADING_MODE=demo_live`.

<details>
<summary>Or do it manually</summary>

```sh
npm install
cp .env.example .env      # then fill in your TradeLocker DEMO credentials
python3 -m bot.doctor     # verify the account, read-only
npm run build
npm start
```
</details>

### Verify your account first

`python3 -m bot.doctor` connects to your real DEMO account and reports, per
check, exactly what it found: whether DEMO verification passes, which
history endpoint shape your broker uses, whether each instrument exposes a
usable contract size and lot step, whether the currency conversion path
resolves, and whether the profit floor is reachable at your equity. It is
read-only — it never places, modifies or closes an order.

Run it before every deployment and after any broker-side change. Add
`--safe` to mask balances and account identifiers so the report can be
shared — the integration details that matter for diagnosis are preserved.

Run the bot alone (no dashboard):

```sh
python3 -m bot.service
```

Run the test suite:

```sh
python3 -m pytest         # 425 tests, no network required
npm run typecheck
```

Backtest and validate against your broker's own history:

```sh
python3 -m bot.backtest --symbol EURUSD --bars 3000 --monte-carlo
python3 -m bot.backtest --symbol EURUSD --walk-forward --folds 3
```

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, data flow, process model |
| [SMC_ENGINE.md](docs/SMC_ENGINE.md) | Every detector, and why it rejects what it rejects |
| [RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) | Position sizing maths, limits, the $50 objective |
| [EXECUTION_ENGINE.md](docs/EXECUTION_ENGINE.md) | Idempotency, reconciliation, crash recovery |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Railway setup, environment, persistence |
| [PAPER_TRADING.md](docs/PAPER_TRADING.md) | Paper mode, the doctor, and the path to live demo orders |
| [TESTING.md](docs/TESTING.md) | What is tested and how to extend it |
| [SECURITY.md](docs/SECURITY.md) | Threat model and hardening |
| [CLAUDE.md](CLAUDE.md) | Permanent project rules for anyone (human or AI) changing this code |

## The core invariants

These are enforced in code and covered by tests. Breaking one should break
the build:

1. **DEMO only** — verified at startup, at broker connection, before order
   creation, and before order submission. Fails closed.
2. **One risk engine** — no risk maths in the frontend, the executor, the
   SMC engine, or the AI layer.
3. **No duplicate orders** — a deterministic execution id plus a UNIQUE
   database constraint make a second order for the same setup impossible,
   and a write whose outcome is unknown is *never* retried.
4. **No look-ahead** — a detector on bar *i* is handed `candles[:i+1]` and
   nothing else; every swing carries the index at which it became knowable.
5. **AI can only subtract** — it can veto a trade, never create one, change
   direction, move a level, or alter risk.
6. **Real data only** — an unavailable value renders as OFFLINE/UNKNOWN,
   never as a plausible zero.
7. **NO TRADE is a valid result** — nothing in the system forces a trade to
   meet a quota, a daily target, or a profit objective.

## What this is not

It is not a promise of profitability. The backtester, walk-forward harness
and Monte Carlo analysis exist to *measure* the strategy honestly, including
labelling small samples as inconclusive. Software correctness is proven by
tests; strategy quality is a separate, ongoing question that the decision
journal and performance breakdowns are designed to answer with data.
