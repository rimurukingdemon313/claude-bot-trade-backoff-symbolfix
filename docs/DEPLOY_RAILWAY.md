# Deploying without a terminal

You do not need a local development environment, a terminal, or any commands
to run this. Everything can be done from a browser.

## 1. Create the service

1. Go to **railway.app** → sign in with GitHub → **New Project**.
2. **Deploy from GitHub repo** → pick this repository.
3. Choose the branch `claude/trading-bot-enhancement-zk153f`.

Railway builds from the `Dockerfile` automatically. The first build takes a
few minutes.

## 2. Add a database

In the same project: **New** → **Database** → **PostgreSQL**.

Railway sets `DATABASE_URL` for you. This matters — without it, the kill
switch, the daily-loss counter and the drawdown state reset on every
redeploy, because Railway's filesystem is ephemeral.

## 3. Set the variables

Open your service → **Variables** → paste this block into the raw editor,
then replace the four TradeLocker values with yours:

```
TRADELOCKER_EMAIL=your@email.com
TRADELOCKER_PASSWORD=your-password
TRADELOCKER_SERVER=YOURSERVER
TRADELOCKER_ACC_ID=1234567
TRADELOCKER_URL=https://demo.tradelocker.com/backend-api

TRADING_MODE=paper
TRADED_SYMBOLS=EURUSD,GBPUSD,USDJPY,AUDUSD,USDCHF,XAUUSD

OPPORTUNITY_MINIMUM_PROFIT=40
OPPORTUNITY_TARGET_PROFIT=50
```

Notes:

- `TRADING_MODE=paper` means orders are **simulated** against live prices.
  Nothing reaches your account until you change this to `demo_live`.
- `TRADED_SYMBOLS` accepts either bare pairs (`EURUSD`) or your broker's
  exact names (`EURUSD.R`). Both resolve.
- `DATABASE_URL` is already set by the PostgreSQL plugin — do not add it by
  hand.
- Add `DASHBOARD_TOKEN` (any long random string) to protect the pause and
  kill-switch controls.

## 4. Get a URL

Service → **Settings** → **Networking** → **Generate Domain**.

You now have something like `https://your-app.up.railway.app`.

## 5. Verify the account — from your phone

Two ways, no terminal:

**In the dashboard:** open your URL → **System** tab → **Run verification** →
**Copy report**.

**Or open the endpoint directly:**

```
https://your-app.up.railway.app/api/doctor
```

Either way the report is **masked**: balances, equity, margin and the account
number are replaced with `***`. What remains is the integration detail — which
history endpoint your broker uses, each instrument's contract size and lot
step, the symbol naming convention, whether candles validate. That is the part
worth sharing, and it is safe to share.

**It is read-only. It never places, modifies or closes an order.**

The same report is also printed into **Deployments → View Logs** on every
boot, so you can read it there without opening the dashboard at all. Set
`STARTUP_DOCTOR=false` to turn that off.

## 6. Read the result

| Verdict | Meaning |
| --- | --- |
| `PASS` | the account is ready |
| `WARN` | works, with something to review |
| `FAIL` | one or more checks block trading — the report says which |

A `FAIL` is useful information, not a dead end. Send the report and it can be
fixed.

## What to expect once it is running

It will mostly say **NO TRADE**, and that is correct behaviour. The system is
built to be selective: it wants multi-timeframe agreement, a liquidity sweep
or a displaced structure break, an unmitigated entry zone, an acceptable
spread, no news blackout, and at least $40 of expected profit at the
structural target — all at once. Days can pass without a trade.

The **System → Decision journal** shows why it stood aside each time. That is
the most useful screen in the whole dashboard.

## Common failures

| Symptom | Cause |
| --- | --- |
| Build fails immediately | usually the branch — confirm you selected `claude/trading-bot-enhancement-zk153f` |
| `/healthz` returns 503 | open `/api/health` and read `components` — one of database, broker, demo or startup will be false |
| `demo_guard` FAIL | `TRADELOCKER_URL` is not a demo endpoint, or the account does not self-identify as demo |
| `SYMBOL:specification` FAIL | the broker does not expose a contract size for that symbol; it will be skipped |
| `profit_objective` FAIL | the $40 floor is unreachable at your equity — see [PAPER_TRADING.md §5](PAPER_TRADING.md) |
| Never trades, no errors | expected. Read the decision journal. |
