# Execution Engine

The invariant this whole subsystem defends: **a network failure, a crash, or
a concurrent cycle must never produce two orders for the same setup.**

## Execution identity

Before anything leaves the process, a deterministic id is derived from what
makes this trade *this* trade:

```
sha256(symbol | direction | trigger_minute | entry | stop | target)[:32]
```

Two scans of the same unchanged setup produce the same id. The database has
a `UNIQUE` constraint on it, so a duplicate becomes a storage error rather
than a second live order. That is a structural guarantee, not a best-effort
check.

## The order sequence

Each step is persisted before the next begins, so a crash at any point
leaves a recoverable record:

```
1. database reachable?          → no  ⇒ ABORT (fail closed)
2. verify DEMO (creation)       → no  ⇒ ABORT
3. persist intent (CREATED)     → dup ⇒ DUPLICATE, no order
4. fresh broker position check  → hit ⇒ ABORT
5. spread check on a live quote → bad ⇒ ABORT
6. verify DEMO (submission)     → no  ⇒ ABORT
7. mark SUBMITTED → send
8. mark ACKNOWLEDGED (order id is not a fill)
9. verify the position exists at the broker
10. mark FILLED, record the trade, bump daily counters
11. verify SL/TP are attached; repair if the broker dropped them
```

### Why the database check comes first

An order whose intent cannot be persisted is unrecoverable after a restart:
nothing would know it existed. A missed opportunity is cheaper than an
untracked live position.

### Why step 4 is a live query

Seconds pass between analysis and submission — an AI call, risk evaluation,
sizing. A concurrent cycle or a manual trigger can open the same symbol in
that window, and a cached snapshot would not see it. This is the one call in
the path that must never be a shared snapshot.

Step 4 also blocks on any *unresolved intent* for the symbol: an in-flight
trade whose outcome is unknown must be reconciled before another order is
created.

### Why SL and TP are required

`place_market_order` refuses a missing or non-positive stop or target. An
unprotected position is an unbounded loss, and there is no path in this
system that opens one. If the broker accepts the order but drops the
protection, the executor reattaches it and — if that fails — records an
`UNPROTECTED_POSITION` reconciliation event at critical severity.

## Failure handling

| Failure | Classification | Response |
| --- | --- | --- |
| Broker rejects (4xx) | PERMANENT | mark FAILED, no retry |
| Transport dies after the request left | **SAFETY_CRITICAL** | mark AMBIGUOUS, **never retry**, hand to the reconciler |
| Acknowledged but no position appears | SAFETY_CRITICAL | mark AMBIGUOUS, reconcile |
| Anything before the request leaves | safe | abort cleanly |

### The ambiguous case

This is the most dangerous state in the system. TradeLocker documents no
client-supplied idempotency key, so a resent order can become a second real
position. The rule at the transport layer is absolute:

- **reads** retry on 429/5xx/timeout with exponential backoff and full
  jitter, bounded by `max_attempts`;
- **writes never retry.** A write whose response is lost raises
  `AmbiguousExecution`, and the only recovery is to ask the broker what
  actually happened.

Full jitter (uniform in `[0, base]`) rather than fixed sleeps keeps several
clients from re-colliding in lockstep after a shared rate limit.

A circuit breaker sheds load after repeated failures so a broker outage
degrades into "we stop calling for a minute" instead of "every scan spends
its whole budget timing out".

## Reconciliation

Runs at **startup** — before any new trade is permitted — and periodically
thereafter. Webhooks do not exist here, so a position can close between
scans and nothing would know.

Four disagreements are handled:

1. **An unresolved intent** → ask the broker. A matching position means the
   order *did* fill (mark FILLED, adopt the position). Nothing matching, and
   nothing in order history, means it never landed (mark ABANDONED). Either
   way the ambiguity is settled by evidence, not by a retry.
2. **A broker position the database has never seen** → adopt it as
   `ORPHANED`. This is the crash-between-send-and-record case. It is
   recorded as ORPHANED rather than as a normal trade so the distinction
   stays visible in analysis and on the dashboard.
3. **A database OPEN trade the broker does not have** → close it out, taking
   the realised result from broker order history. If that cannot be read the
   P/L is recorded as `0.0` rather than invented — a fabricated number would
   corrupt the daily loss counter, which is a safety control.
4. **A position with no stop loss** → restore it from the recorded plan, or
   escalate to critical severity if no plan exists.

## Crash recovery

The scenario from the specification, exercised by
`tests/test_orchestrator.py::test_a_position_survives_a_process_restart`:

```
bot running → position open → container restarts → process memory lost
→ bot returns → TradeLocker still holds the position
```

On boot: verify DEMO → connect broker → connect database → read broker
account/positions/orders → read local state → reconcile → restore management
→ **only then** allow new trades.

Afterwards the position is still tracked with its original stop, target,
risk amount and setup grade; the daily counters and losing streak are intact;
and a second scan does not open a duplicate.

If startup fails, `startup_complete` stays false and every scan refuses to
run. The API still serves health, because the dashboard must be able to
*show* the failure. A periodic job retries startup, so a transient broker
outage at boot does not leave the process permanently unable to trade.

## Position management

Each feature is a pure decision function (`plan_actions`) that returns what
*should* happen; applying it is a separate step. That is what makes them
testable without a broker.

| Feature | Default | Trigger |
| --- | --- | --- |
| Break-even | **on** | 1.0R, moved slightly beyond entry to cover the spread |
| Structure exit | **on** | a confirmed close beyond the recorded stop, before 1R |
| Time stop | **on** | 48h open without reaching 0.5R |
| Partial take-profit | off | 1.5R, 50% off |
| Trailing | off | after 2.0R, locking `R−1` |

Partials and trailing are off by default because management must be earned
by testing, not added because it sounds sophisticated.

R comparisons use a small epsilon: floating-point price arithmetic puts an
exact 1.0R at `0.9999999999999556`, which would silently skip the break-even
move at precisely the level it exists to fire at.

Exits are deterministic: the stop, the target, a structural invalidation
confirmed by a close, a risk emergency, or the broker. A profitable position
is never panic-closed because one candle looked wrong.

## Graceful shutdown

On SIGTERM: stop scheduling new scans, let in-flight work finish, persist
state, close the database cleanly — and **never close broker positions**. A
deploy must not liquidate the book. On restart, reconciliation picks
everything back up.
