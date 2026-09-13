# Deployment

Railway is the **runtime**, not the broker. Its job is to keep the process
running, expose the dashboard, provide environment variables and networking,
and restart on failure. TradeLocker is the broker; the dashboard is the UI.

## What runs

One service, one container, two processes:

- `node index.js` → builds the frontend if needed, bundles the TypeScript
  server, starts it;
- `server/index.ts` → spawns `python3 -m bot.service` and supervises it,
  then serves the dashboard on `$PORT`.

If the bot process exits, the container exits so the platform restarts it. A
dashboard that keeps serving a dead trading process is a lie.

## Persistence — read this before deploying

Railway's filesystem is **ephemeral**. Without a database, the kill switch,
daily-loss counters, drawdown state, losing streak and execution intents
reset on every redeploy.

```
DATABASE_URL=postgresql://user:password@host:5432/database
```

Attach a Railway PostgreSQL plugin and set `DATABASE_URL`. The bot creates
its own schema on boot (idempotent, safe to re-run).

If `DATABASE_URL` is set but the `psycopg` driver is missing, the bot
**refuses to start** rather than silently downgrading to SQLite — a silent
downgrade would look fine until the next deploy wiped the risk state.

Without `DATABASE_URL` the bot uses SQLite at `SQLITE_PATH`. That is correct
for local development and acceptable in production **only** if you mount a
persistent volume at that path.

## Environment

See `.env.example` for the annotated list. The minimum:

| Variable | Required | Notes |
| --- | --- | --- |
| `TRADELOCKER_EMAIL` / `_PASSWORD` / `_SERVER` / `_ACC_ID` | yes | DEMO account |
| `TRADELOCKER_URL` | yes | must be a demo endpoint; a live URL is refused at config load |
| `DATABASE_URL` | strongly recommended | see above |
| `DASHBOARD_TOKEN` | recommended | protects the command endpoints |
| `GEMINI_API_KEY` / `GROQ_API_KEY` | optional | AI validation; omit to run fully deterministically |
| `PORT` | provided by Railway | |

Never commit real values. `.env` is gitignored; set them in Railway's
variable settings.

## Health checks

`GET /healthz` reports the **bot's** health, not merely "Express is up":

- `200 {"status":"ok"}` — database, broker, DEMO verification and startup are
  all good;
- `503 {"status":"degraded"}` — a trading-critical component is down;
- `503 {"status":"unavailable"}` — the bot process is not answering.

Point Railway's healthcheck at `/healthz`. A green check on a process that
cannot trade would be worse than no check at all.

Richer detail is at `GET /api/health`.

## Startup timing

Boot performs the full startup sequence: authenticate, verify DEMO, connect
the database, read broker account/positions/orders, reconcile. Give it a
generous `start-period` (the Dockerfile uses 90s) so the platform does not
kill it mid-reconcile.

If startup fails, the API still serves health so the dashboard can show why,
and a periodic job retries it. Trading stays blocked until it succeeds.

## Graceful shutdown

On SIGTERM the Node process signals the bot and waits up to 15 seconds. The
bot stops its scheduler, finishes in-flight work, and closes the database.
It **does not close broker positions** — a deploy must never liquidate the
book. On restart, reconciliation picks everything back up.

`tini` is the container entrypoint so the Python child is reaped correctly
and SIGTERM is forwarded rather than the process being killed outright.

## Resource profile

Designed for a small Railway instance:

- one scan per candle close (default 15 minutes), not a polling loop;
- a market-data cache with a TTL shorter than the timeframe it serves, so a
  cache hit can never hide a new closed candle for long;
- a request throttle plus a circuit breaker in front of the broker;
- AI called only for candidates that already passed every deterministic
  gate — token spend scales with genuine opportunities, not scan frequency;
- the equity-snapshot table pruned hourly, since it is the only one that
  grows on a timer rather than per trade;
- bounded retries everywhere. Nothing in this system retries forever.

## Operating it

| Action | Where |
| --- | --- |
| Pause new trades | Dashboard → *Pause scanning* (persists across restarts) |
| Emergency stop | Dashboard → *Emergency stop* (kill switch, persists) |
| Force a scan now | Dashboard → *Scan now* |
| Re-sync with the broker | Dashboard → *Reconcile* |
| See why it did not trade | Dashboard → *System* → Decision journal |

Pausing stops new entries only. Open positions continue to be managed.

## Logs

One JSON object per line, with an event id, timestamp, stage, severity,
symbol and the strategy version stamp. Stages trace the pipeline: `SCAN`,
`DATA`, `SMC`, `SCORE`, `AI`, `RISK`, `ORDER`, `FILL`, `POSITION`, `EXIT`,
`RECONCILE`, `SAFETY`, `HEALTH`.

Secrets are redacted at the sink, not at each call site: configured secret
values and anything shaped like an access token are replaced before a line
is ever written.
