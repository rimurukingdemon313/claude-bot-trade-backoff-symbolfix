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

## Quick start

```sh
npm install
cp .env.example .env      # then fill in your TradeLocker DEMO credentials
npm run build             # build the dashboard
npm start                 # starts the bot + dashboard on :5000
```

Run the bot alone (no dashboard):

```sh
python3 -m bot.service
```

Run the test suite:

```sh
python3 -m pytest         # 280+ tests, no network required
npm run typecheck
```

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, data flow, process model |
| [SMC_ENGINE.md](docs/SMC_ENGINE.md) | Every detector, and why it rejects what it rejects |
| [RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) | Position sizing maths, limits, the $50 objective |
| [EXECUTION_ENGINE.md](docs/EXECUTION_ENGINE.md) | Idempotency, reconciliation, crash recovery |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Railway setup, environment, persistence |
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
