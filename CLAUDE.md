# Project rules

Permanent rules for anyone — human or AI — changing this repository. They
are not style preferences; each one exists because violating it produces a
specific, known failure.

## 1. DEMO only. Always.

`REQUIRE_DEMO` in `bot/config.py` is a module constant with no environment
override, and that is deliberate. Do not add one.

Demo status is verified from two independent signals (the API URL and the
broker's own account metadata) at four points: startup, broker connection,
before order creation, before order submission. **Both signals must agree.**

- Never add a fallback that assumes demo when metadata is missing.
- Never let a retry, a config default, or an error path change the endpoint.
- If verification fails: no trade, trip the kill switch, say why.

## 2. One risk engine

`bot/risk/engine.py` is the only code that may approve a trade or decide a
size. If you find yourself computing a position size, a risk amount, or a
limit anywhere else — the frontend, the executor, a route handler, a
prompt — you are creating a second source of truth that will drift.

Risk may be reduced by adverse account state. **It may never be increased by
it.** There is no martingale, no revenge sizing, no "recover the loss"
branch. `test_risk_only_ever_decreases_after_losses` guards this.

## 3. Writes are never retried

TradeLocker documents no client-supplied idempotency key. A resent order can
become a second real position.

- Reads may retry with bounded exponential backoff and jitter.
- Writes raise `AmbiguousExecution` if their outcome is unknown, and the
  **only** recovery is to query the broker for the resulting state.
- Never add a retry loop around `place_market_order`, `close_position`, or
  `modify_position`.

## 4. No look-ahead

A detector working at bar *i* may use `candles[:i+1]` and nothing else.
Every `SwingPoint` carries `confirmed_index`; every consumer filters on it.

When adding a detector:

- if it needs *N* future bars to confirm, expose a `confirmed_index` and
  make consumers respect it;
- never evaluate "did price eventually come back" by inspecting the last bar;
  walk forward bar by bar;
- add a case to
  `test_analysis_on_bar_i_cannot_see_bar_i_plus_one`.

## 5. AI can only subtract

AI is a veto. It may cause the system to skip a trade. It may **never**:

- create a trade the deterministic pipeline did not produce;
- change direction, entry, stop, target, size, or risk;
- be consulted before the deterministic gates have passed.

Any malformed, missing, contradictory, or out-of-range field resolves to
NO TRADE. Never "repair" a model response.

## 6. Never fabricate a value

If balance, P/L, a broker state, or a statistic cannot be read, return
`None`/`OFFLINE`/`UNKNOWN` with a status. A plausible-looking zero is worse
than a visible gap, because it looks like information.

This applies to statistics too: a 100% win rate over two trades is reported
with `sample: "insufficient"`.

## 7. Fail closed on safety-critical state

- Database unavailable → no new orders.
- Kill switch unreadable → treat it as active.
- News feed unavailable with no cache → stand aside.
- Startup incomplete → no trading (but still serve health, so the failure is
  visible).

## 8. NO TRADE is the expected result

Nothing may force a trade to meet a quota, a daily target, or the profit
objective. The objective is a **filter**: if a setup cannot reach it within
the risk limits, the answer is no trade. Capital preservation is a feature.

## 9. Version every behaviour change

Bump the matching constant in `bot/version.py` when you change the SMC
engine, the scorer, the risk engine, the executor, or the AI prompt. Every
trade records the version stamp, and performance analysis groups by it. A
silent behaviour change under an unchanged version makes historical results
uninterpretable.

Change one meaningful thing at a time.

## 10. Tests assert behaviour, not shape

- Never weaken a production check to make a test pass. If a test fails,
  decide which side is wrong first. Several tests here found real bugs and
  the code changed.
- Never mock away the logic under test. The fake broker replaces the
  network, not the engine.
- Build market scenarios explicitly so the correct answer is known by
  construction. A random walk proves nothing.
- Pin the clock. Nothing calls `datetime.now()` directly; every module takes
  an injectable `now`.

## 11. The dashboard is presentation only

It displays state and can make the system safer (pause, kill switch) or ask
it to look again (scan, reconcile). It can never open, size, or close a
trade, and no control may bypass the demo guard, the risk limits, or the
execution guards.

Keep it usable on a phone: single column, ~38px tap targets, safe-area
insets, and horizontal scroll confined to the tables that genuinely need it.

## 12. Do not add indicators or complexity for their own sake

SMC is the strategy. An indicator earns its place only by adding independent
information *and* improving out-of-sample robustness. Every added parameter
is another degree of freedom for a backtest to curve-fit against.

Likewise for position management: break-even and the structural exit are on
because they are simple and tested. Partials and trailing are off by default
until evidence justifies them.

## 13. Never promise profitability

The backtester, walk-forward harness and Monte Carlo analysis exist to
measure honestly, including labelling small samples as inconclusive. Report
what the evidence supports and nothing more.
