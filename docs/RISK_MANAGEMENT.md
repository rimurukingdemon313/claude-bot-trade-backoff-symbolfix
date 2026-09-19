# Risk Management

There is exactly **one** risk engine: `bot/risk/engine.py`. No risk maths
exists in the frontend, the execution layer, the SMC engine, or the AI layer.
Every trade passes through `RiskEngine.evaluate()`, which is the only
function in the system that may approve one.

## Position sizing

This replaces the single most dangerous piece of the previous build, which
computed:

```python
lots = (risk_amount / stop_distance) / 100_000   # and / 100 for gold
```

That is only correct when the quote currency equals the account currency. On
USDJPY with a USD account it was wrong by roughly two orders of magnitude —
and it floored the result at `0.01` lots, which silently *exceeded* the
approved risk on small accounts.

### The correct chain

```
loss_per_lot(account ccy) = stop_distance(price)
                          × contract_size(units per lot)
                          × fx_rate(quote ccy → account ccy)

lots = risk_amount / loss_per_lot        then rounded DOWN to the lot step
```

Every input comes from the broker's own instrument specification: contract
size, tick size, lot step, minimum and maximum lot, digits.

### The conversion rate

Three cases, in order of reliability:

| Case | Rate | Example |
| --- | --- | --- |
| quote == account | `1.0` (exact) | EURUSD on a USD account |
| base == account | `1 / entry_price` (exact, from this instrument) | USDJPY on a USD account |
| a cross | ask the broker for the bridging pair | AUDJPY on a USD account |

**If the rate cannot be established, sizing fails.** It never defaults to
`1.0`. An assumed rate is an unbounded sizing error.

### Worked examples

| Instrument | Stop | Risk | Result | Why |
| --- | --- | --- | --- | --- |
| EURUSD | 50 pips | $100 | **0.20 lots** | 0.0050 × 100,000 = $500/lot |
| USDJPY @ 150 | 0.50 JPY | $100 | **0.30 lots** | 0.50 × 100,000 ÷ 150 = $333/lot |
| XAUUSD | $10 | $100 | **0.10 lots** | $10 × 100 oz = $1,000/lot |

The old formula sized that USDJPY trade at **0.002 lots**, rounded up to the
0.01 minimum — about 3% of the intended risk.

### Rounding down, always

Lots are rounded **down** to the broker's step. Rounding up would exceed the
risk the engine just approved. If the result falls below the broker's
minimum lot, the trade is **declined** — increasing size to reach the
minimum would breach the approved risk, so the only correct answer is not to
trade.

The engine also asserts after sizing that actual risk did not exceed the
budget. That invariant is what the whole risk model rests on.

## Dynamic risk

Base risk is 0.5% of equity. It is adjusted, then **clamped unconditionally**
to `[min_risk_pct, max_risk_pct]`.

| Condition | Adjustment |
| --- | --- |
| A+ setup | ×1.4 |
| A setup | ×1.15 |
| B setup | ×0.75 |
| Drawdown ≥ 5% | scales linearly down to ×0.4 at the hard drawdown limit |
| 2+ consecutive losses | ×0.8, ×0.6, floor ×0.5 |

**Upward adjustment comes only from setup quality.** Every downward
adjustment is applied afterwards, so adverse account state always wins.

There is no branch anywhere in the file that increases risk after a loss.
`tests/test_risk.py::test_risk_only_ever_decreases_after_losses` asserts this
directly — it is the anti-martingale invariant.

## Hard limits

All are evaluated *before* any sizing work, because they are cheap and
definitive:

| Limit | Default | Notes |
| --- | --- | --- |
| Risk per trade | 0.5% base / 1.0% ceiling | build refuses any config above 2% |
| Portfolio risk | 3% | build refuses any config above 6% |
| Daily loss | 3% | **realised + unrealised** |
| Maximum drawdown | 10% | from the recorded equity peak |
| Open positions | 3 | and 1 per symbol |
| Correlated exposure | 1.5% | see below |
| Consecutive losses | 4 | trips the kill switch |
| Trades per day | 6 | |
| Trades per session | 3 | |
| Minimum R:R | 1:2 | build refuses any config below 1.5 |
| Loss cooldown | 45 min | |
| Execution-failure cooldown | 20 min | |

### Why the daily limit counts open losses

A limit that only counts *realised* P/L is trivially evaded by not closing a
loser. `AccountRiskState.daily_pnl` is realised plus unrealised.

## Portfolio correlation

Two EUR-long positions are one bigger EUR position. Treating them as
independent is how a "1% per trade" system takes a 3% hit on a single ECB
headline.

Correlation is derived from **currency exposure**, not from a rolling price
correlation matrix. That is a deliberate trade-off: exposure is exact, needs
no history, cannot be distorted by a quiet sample window, and is the actual
mechanism by which correlated FX pairs move together.

```
long EURUSD  = +1 EUR, −1 USD
long GBPUSD  = +1 GBP, −1 USD      → overlap 0.50 (shared short USD)
long USDCHF  = +1 USD, −1 CHF      → overlap −0.50 (hedges the above)
long XAUUSD  = +1 XAU, −1 USD      → gold carries inverse dollar exposure
```

Two pairs sharing one leg in the same direction score 0.50, which is why the
threshold is 0.45 — that stacking is exactly what the limit exists to
prevent.

## The reward objective, in R

**A filter, never a mandate**, and a ratio rather than a sum.

It was a fixed dollar floor — $40 minimum expected profit, $50 target — and
that was a bug with a plausible face. Expected profit at the structural
target is

```
expected_profit = risk × R        risk = 0.5% of equity
```

so a dollar floor is a statement about **account size** wearing the costume
of a statement about setup quality. $40 silently demanded 1:2 on a $5,000
account and 1:4 on a $1,000 one; the market does not know the balance, and
the same chart must not grade differently on two accounts.

| | |
| --- | --- |
| `REWARD_MIN_R` | 1.2 — below this, NO TRADE, at any equity |
| `REWARD_PREFERRED_R` | 1.5 — reported as a stronger setup. A label, never a gate |

The three ways to inflate a dollar figure are each forbidden elsewhere and
would each be invisible here, which is the other reason the unit matters:

* **size** comes from `sizing.calculate_position_size`, which rounds *down*
  and declines rather than round up to a broker minimum;
* **the stop** is structural, from `smc/engine.py`;
* **the target** is the nearest meaningful opposing liquidity, also
  structural.

So `bot/risk/reward.py` judges a ratio that is already fixed by the time it
sees it. Expected profit is still computed and still reported — an operator
wants to know what a trade is worth — but it is **information, never a
gate**.

On a $5,000 account one R is $25, so 1.2R ≈ $30, 1.5R ≈ $37.50 and 2R = $50.
All three are takeable. None of them is a prediction.

## Kill switch

Persistent (in the database, not in memory), so it survives a redeploy.

Auto-trips on: daily loss limit, maximum drawdown, consecutive losses,
environment mismatch.

When active: **no new trades for any symbol.** Existing positions are still
managed — a kill switch must never strand a live position without a stop.

Operational trips (daily loss, drawdown, loss streak, manual) can be cleared
from the dashboard. Safety-class trips (environment mismatch, unreadable
state) require an explicit forced clear: a system that trips on a broker
inconsistency and then un-trips itself on the next scan has no kill switch
at all.

**It fails closed.** If the state cannot be read, the switch reports *active*.
A system that cannot tell whether it has been stopped must behave as if it
has been.

## State that survives a restart

Written to the database and reloaded on boot:

- daily realised P/L, trades opened/closed, wins, losses
- the consecutive-loss streak — which walks *backwards across day
  boundaries*, skipping days with no closed trades, because a weekend is not
  a win
- the equity peak, and therefore drawdown
- the kill switch and its reason
- every execution intent and trade
- the scanning-enabled flag
