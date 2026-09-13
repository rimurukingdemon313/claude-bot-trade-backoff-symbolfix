# Architecture

## Process model

```
┌─────────────────────────── container ───────────────────────────┐
│                                                                 │
│  node index.js                                                  │
│    └── server/index.ts                                          │
│          ├── spawns  python3 -m bot.service   (supervised)      │
│          └── express on :PORT                                   │
│                ├── /            → dist/ (React dashboard)       │
│                ├── /api/*       → proxy → bot on :BOT_PORT      │
│                └── /healthz     → proxy → bot health            │
│                                                                 │
│  python3 -m bot.service                                         │
│    ├── http.server on 127.0.0.1:BOT_PORT   (localhost only)     │
│    ├── Orchestrator      — the scan pipeline                    │
│    ├── TradeLockerBroker — one long-lived authenticated session │
│    ├── Database          — Postgres (Railway) or SQLite         │
│    └── Scheduler         — scan / positions / reconcile timers  │
└─────────────────────────────────────────────────────────────────┘
```

### Why a long-running Python process

The previous build shelled out to a fresh `python3` for every call. Each
subprocess re-authenticated with TradeLocker from scratch — roughly eight
HTTP calls per state read, eighteen or more per scan — which was enough
burst volume to trip Cloudflare's rate limiter in front of the broker. It
also made shared state impossible: the session cache, the scheduler flag and
any in-flight execution intent died with each subprocess.

One process owns the session, the database and the scheduler. The Node layer
is a window onto it.

### Why Node still exists

The dashboard is a first-class deliverable and React + Vite is the right tool
for it. Express serves the built assets and proxies the API. It holds no
trading state, which makes "the dashboard is presentation only" structurally
true rather than a convention — there is no code path in `server/` that could
open, size or close a position.

## Module map

```
bot/
├── config.py            Typed config. Hard ceilings; REQUIRE_DEMO is a constant.
├── clock.py             One clock. Every module takes an injectable `now`.
├── errors.py            Error taxonomy: TRANSIENT / PERMANENT / SAFETY_CRITICAL.
├── observability.py     Structured JSON logging with secret redaction.
├── version.py           Component versions stamped onto every trade.
├── news.py              High-impact event filter (fails closed).
├── doctor.py            Read-only live verification of the real account.
├── scheduler.py         Candle-aligned timers with overlap protection.
├── orchestrator.py      The scan pipeline. Wires everything together.
├── api.py               Dashboard projections (read-only).
├── service.py           The daemon + its localhost HTTP surface.
│
├── safety/
│   ├── demo_guard.py    Two independent DEMO signals; fails closed.
│   └── kill_switch.py   Persistent stop. Survives restarts. Fails closed.
│
├── storage/
│   ├── db.py            Postgres/SQLite abstraction, schema, transactions.
│   └── repositories.py  Intents, trades, journal, daily stats, equity.
│
├── broker/
│   ├── http.py          Backoff + jitter + throttle + circuit breaker.
│   ├── models.py        InstrumentSpec, Quote, Position, Order.
│   ├── history.py       Candle endpoint DISCOVERY + permissive decoding.
│   ├── paper.py         Live prices in, simulated fills out. No broker writes.
│   └── tradelocker.py   Session, decoding, instruments, candles, writes.
│
├── marketdata/
│   ├── candles.py       Immutable Candle with close-time semantics.
│   ├── validation.py    Forming-candle removal, gaps, staleness, spread.
│   └── provider.py      Broker-native data with a sub-candle TTL cache.
│
├── smc/
│   ├── indicators.py    ATR and friends. Deliberately minimal.
│   ├── swings.py        Pivots with `confirmed_index` (the look-ahead guard).
│   ├── displacement.py  Energetic, directional, gap-creating movement.
│   ├── structure.py     BOS / CHoCH with ATR buffer and close confirmation.
│   ├── liquidity.py     Liquidity map + five-stage graded sweep detection.
│   ├── fvg.py           Fair value gaps with mitigation lifecycle.
│   ├── orderblocks.py   Order blocks that required a consequential impulse.
│   ├── dealing_range.py Premium / equilibrium / discount.
│   ├── regime.py        Trending / ranging, compressed / expanded / extreme.
│   ├── sessions.py      Asian / London / NY / overlap + the FX weekend.
│   └── engine.py        Multi-timeframe orchestration → SetupCandidate.
│
├── scoring/scorer.py    Deterministic 100-point score → A+ / A / B / NO TRADE.
│
├── risk/
│   ├── sizing.py        Lots from real broker specs and real FX conversion.
│   ├── correlation.py   Currency-exposure-based portfolio correlation.
│   ├── opportunity.py   The $50+ objective (a filter, never a target).
│   └── engine.py        THE risk authority. The only approver of a trade.
│
├── ai/
│   ├── schema.py        Strict contract. Anything invalid → NO TRADE.
│   ├── validator.py     Cross-checks AI output against the engine.
│   └── client.py        Gemini → Groq chain with compact prompts.
│
├── execution/
│   ├── plan.py          Immutable TradePlan + deterministic execution id.
│   ├── executor.py      The only place an order is created.
│   ├── reconciler.py    Broker ↔ database truth reconciliation.
│   └── manager.py       Break-even, partials, trailing, structure exits.
│
├── analytics/performance.py  Honest statistics; small samples are labelled.
└── backtest/
    ├── engine.py        Sequential, pessimistic-fill backtester.
    ├── walkforward.py   Train → validate → out-of-sample.
    └── montecarlo.py    Drawdown dispersion and risk of ruin.
```

## Source of truth

| Concern | Authority |
| --- | --- |
| Open positions, balance, equity, fills, broker-side SL/TP | **TradeLocker** |
| Trade lifecycle, intents, decisions, risk state, statistics | **Database** |
| Market structure | **SMC engine** |
| Whether a trade may happen and at what size | **Risk engine** |
| Whether a serious candidate should be skipped | **AI (veto only)** |
| Everything a human sees | **Dashboard (presentation only)** |

When the database and the broker disagree about whether a position exists,
the broker wins and the disagreement is recorded as a reconciliation event.

## The scan cycle

`Orchestrator.scan()` runs on the candle close plus a small offset:

1. **Gates** — startup complete? trading enabled? DEMO verified? kill switch
   clear? Any failure ends the cycle before any market data is fetched.
2. **Account state** — balance, equity, positions, daily counters, drawdown,
   losing streak. Auto-trips the kill switch if a limit is breached.
3. **Per symbol** — news → data → SMC → score → risk → AI. Each stage can
   reject, and every rejection is journalled with its reason.
4. **Rank** — surviving candidates are ordered by tier, then score, then
   R:R, then expected profit.
5. **Execute one** — only the single best opportunity. Several acceptable
   setups still produce one trade.

Two independent locks prevent overlapping scans: the scheduler will not
start a job that is running, and `scan()` holds a non-blocking lock of its
own (a manual trigger from the API can race a timer).

## Failure isolation

- One symbol raising does not end the scan; it is recorded as an ERROR
  outcome and the loop continues.
- A scheduler job that raises does not kill its thread.
- `health()` catches per-component failures, because the health endpoint is
  the one thing that has to work when everything else is broken.
- The kill switch fails **closed**: if its state cannot be read, it reports
  active.
