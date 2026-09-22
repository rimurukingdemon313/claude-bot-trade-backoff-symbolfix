# What ten well-known trading repositories taught this one

Read in full (shallow clones, September 2026):
freqtrade, jesse, nautilus_trader, hummingbot, lumibot, backtrader,
OctoBot, ccxt, FinRL, FinRL-Trading.

The brief was "find anything they do better and take it." What follows is
what was actually taken, what was rejected and why, and one thing that was
independently confirmed rather than learned.

No code was copied from any of these projects. What transferred is a
**distinction** each of them draws that this codebase did not.

---

## The honest framing first

None of these repositories contains a profitable strategy, and none
claims to. They are infrastructure: backtest engines, exchange adapters,
order routers, research frameworks. FinRL says so about itself in its own
README ("educational and research framework").

That is not a criticism — it is where the value is. A strategy that
reliably made money would not be on GitHub. Execution engineering is
published precisely because it is not the edge, and it is exactly what
this bot was weakest at.

So: three defects found, one confirmation, several deliberate rejections.

---

## 1. NautilusTrader — a write can fail without failing

`docs/concepts/execution/policies.md` draws a three-way distinction where
this codebase drew two:

| Evidence | Meaning |
|---|---|
| Definitive local failure | Proven the command was never sent |
| Definitive venue result | The venue explicitly confirms the outcome |
| **Unknown live outcome** | It may have reached the venue; no result known |

And the rule that follows from it:

> HTTP status codes and rate limits are definitive only when
> venue-specific semantics prove that the command was not accepted.

### The defect this found

`bot/broker/http.py` classified a **5xx on a write** as
`BrokerError` — a clean failure:

```python
elif 500 <= exc.code < 600:
    last_error = BrokerError(f"... server error ({exc.code}) ...")
```

But a 502/503/504 proves nothing about the order. The request reached the
server — it answered — and the matching engine behind that gateway may
well have accepted it. A 504 is the textbook shape of "it worked, the
reply was lost."

**Severity, stated honestly:** no live order was ever lost to this. The
executor has a catch-all (`except BotError` → AMBIGUOUS) that happened to
route it correctly. But the classification was wrong *at the source*, so
every other caller of a write inherited the wrong answer — and
`PositionManager` is one of them, where it mattered (§2).

Now raises `AmbiguousExecution`. A 4xx still raises `BrokerRejected`,
because there the venue's semantics *do* prove non-acceptance — treating
that as ambiguous would strand a trade in the reconciler for an order
that certainly never existed.

Our rule 3 and their policy were already the same conclusion reached
independently. The gap was in applying it consistently.

---

## 2. The same insight, applied to position management — and this one was live

`PositionManager.apply` ran:

```python
self.broker.close_position(action.position_id, quantity=action.quantity)
self.repos.trades.mark_partial_taken(action.position_id)   # ← after
```

with a single `except BotError` that logged and moved on.

Nothing retries in code. **But this method runs on every poll.** So a
partial close whose outcome was unknown skipped the guard that prevents
it repeating, and came back thirty seconds later to take another slice.

That is the same descending stack of slices that cost a live position —
0.17, 0.09, 0.04, 0.03, 0.01, 0.01 lots — reached by a different route
than the missing column that was fixed earlier. The column fix closed one
door; this was the other one.

Now `AmbiguousExecution` is caught separately: the guard is written
anyway, the action is recorded for the reconciler, and it is never
repeated. The asymmetry is deliberate and stated in the code — recording
a partial that did not happen leaves one runner larger than intended and
the reconciler corrects it; *not* recording one that did happen slices
the position away, every poll, until nothing is left.

**This is the most valuable thing in this document.** It was found by
reading someone else's taxonomy and asking where ours disagreed.

---

## 3. Freqtrade — punish the symbol, not the book

`freqtrade/plugins/protections/` holds four guards, and the architectural
point is not what they measure but their **scope**: each can lock a
single pair (`stop_per_pair`) or everything (`global_stop`).

Every guard in `bot/risk/engine.py` is global — losing streak, cooldown,
drawdown, daily loss. So the symbol actually doing the damage keeps its
turn in the scan, while the symbols that did nothing wrong serve its
sentence. Given our evidence points at one particular instrument, that is
the wrong shape.

Added: `symbol_lock_losses` / `symbol_lock_lookback_hours` /
`symbol_lock_hours`. N losing closes on one symbol benches that symbol;
everything else keeps trading.

**Shipped OFF by default (`0` = disabled).** Turning it on in the same
fortnight as the spread-aware stop would make the result unreadable — we
would not know which change produced it. It ships ready and waits for the
trade record (rules 9 and 12).

Two details that are ours, not theirs: an unmeasured close is not counted
as a loss (rule 6 — a close nobody could price is not a break-even
close), and an unreadable record makes the guard *stand down* rather than
bench everything, because it may only ever refuse a trade.

---

## 4. Confirmed, not learned: the stop triggers on the other side

Yesterday's fix padded the stop by the spread, on the reasoning that a
long is closed by selling (bid) and a short by buying (ask), so the
candle series is the wrong price to place the level against.

NautilusTrader models this explicitly as a first-class order property
(`docs/concepts/orders/index.md`):

> `BID_ASK`: Uses the ask for BUY orders and the bid for SELL orders.

That is the same asymmetry, stated by the most rigorously engineered
project of the ten. It does not make our fix right — the live trade
already did that — but it does mean the reasoning is not idiosyncratic.

Surveying all ten for the same distinction: **nautilus_trader models it
properly; the rest largely do not.** That is worth knowing, because it
means "a popular bot doesn't do X" is weak evidence that X doesn't matter.

---

## 5. What was deliberately NOT taken

| Available | Rejected because |
|---|---|
| Grid / DCA engines (OctoBot, hummingbot) | Averaging into a loser. Rule 2 forbids risk increasing with adverse state, and this is that idea's purest form |
| Hyperparameter optimization (freqtrade `hyperopt`, jesse) | Fitting parameters on the data you then report results from. Rule 12 calls every added parameter a degree of freedom for a backtest to curve-fit against — an optimizer is a machine for spending them |
| Deep reinforcement learning (FinRL, FinRL-Trading) | Needs vastly more data than we have, produces a policy nobody can inspect, and rule 5 makes AI a veto that may never create or alter a trade. An RL policy *is* the trade |
| Indicator libraries | Rule 12: an indicator earns its place by adding independent information AND improving out-of-sample robustness. None was demonstrated |
| CCXT-style multi-exchange abstraction | Solves a problem we do not have. One broker, already adapted |
| Telegram/Discord control surfaces | More public control surface. Rule 15 was written because a control surface on the internet is an attack surface |

The pattern: what was taken is all **execution correctness**. What was
rejected is all **strategy generation**. That split is not an accident —
it is where published code is and is not trustworthy.

---

## 6. What none of them could give us

The thing we most need is not in any repository: **whether this strategy
has an edge on this broker's prices.** That is answered by our own trade
record and by nothing else.

Reading ten codebases made the execution layer more correct. It did not,
and could not, make the strategy profitable. Anyone claiming otherwise
about a public repository is selling something.

---

## Status

- 3 defects fixed, 15 new behavioural tests, all suites green.
- 1 feature added and left disabled pending evidence.
- No strategy change. No promise of profitability.

The plan is unchanged: change nothing else, let ~20 trades close, read
the record.
