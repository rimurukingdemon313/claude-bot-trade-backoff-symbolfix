# Security

## Threat model

The system holds broker credentials and can move money in a DEMO account. The
realistic threats are: credential leakage through logs or the browser, an
unauthenticated party driving the trading controls, and the system being
induced to trade on a live account.

## What was hardened

### Credentials never reach the browser
All broker and AI credentials live in the bot process. The Node layer holds
no trading state and the React app receives only projected state. There is no
endpoint that returns a credential, and a test asserts the snapshot payload
contains no secret.

### Logs redact secrets at the sink
The previous build ran a connectivity diagnostic on every boot that printed
the first 400 characters of the login response body — which, on success,
contains the access token — straight into the platform logs. That script is
deleted.

Redaction now happens in `bot/observability.py` at the point of writing, not
at each call site: any configured secret value, and anything shaped like
`"accessToken": ...`, is replaced before a line is emitted.

### CORS is closed by default
The previous build called bare `cors()`, which sends
`Access-Control-Allow-Origin: *` — any website a user visited could have
driven the trading controls from their browser. The allowlist is now empty
unless `DASHBOARD_ORIGINS` is set explicitly.

### The command surface is authenticated and rate limited
`POST /api/control/*` requires `Authorization: Bearer $DASHBOARD_TOKEN` when
a token is configured. The token is read from the server's environment and
never sent to the browser. Commands are additionally rate limited per source
address. Read endpoints stay open so the dashboard can always display state.

The bot's own HTTP surface binds to `127.0.0.1` — it is not reachable from
outside the container at all.

### Commands can only make the system safer
The exposed controls are: pause scanning, trip/clear the kill switch,
trigger a scan, trigger a reconcile. None of them can change risk limits,
bypass the DEMO guard, alter a stop, or place an order directly. Clearing a
safety-class kill-switch trip requires an explicit force flag.

### No shell execution, no injection surface
Nothing in the bot shells out. The previous build's `execFile` calls that
passed JSON payloads to Python subprocesses are gone; the Node layer makes
HTTP calls to localhost instead. All SQL uses parameter binding — there is no
string interpolation of user input into a query anywhere.

### Security headers
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`, and a restrictive `Permissions-Policy`. Request
bodies are capped at 64 KB. `robots.txt` disallows everything — this
dashboard exposes a private account's state and must never be indexed.

### Third-party surface reduced
The dashboard no longer pulls webfonts from a CDN (a third-party dependency
and a referrer leak for a private page); it uses a system font stack. The
bot has **no** third-party Python dependencies on the trading path — only the
standard library — which keeps the supply-chain surface of the process that
holds broker credentials as small as it can be. `psycopg` is required only
when PostgreSQL is configured.

## Environment safety

`REQUIRE_DEMO` is a module constant, not an environment variable. Demo status
is verified at four points from two independent signals (the API URL and the
broker's own account metadata), and **both** must agree. A live URL is
rejected at config load. Failing verification trips the kill switch with a
reason that cannot be cleared without an explicit force.

## Dependencies

- Python trading path: **standard library only**.
- Optional: `psycopg` (PostgreSQL), `pytest` (tests only).
- Node: Express, pino, and the React/Vite/Tailwind frontend stack.

Run `npm audit` as part of routine maintenance. Keep the base image current;
the Dockerfile pins `node:20-bookworm-slim` and installs only `python3`,
`python3-pip` and `tini`.

## Reporting

If you find a vulnerability, do not open a public issue. Rotate the affected
credential in TradeLocker first, then in the deployment's environment
variables.

## Residual risks

- **Dashboard read endpoints are unauthenticated.** They expose account
  state to anyone who can reach the service URL. Put the deployment behind
  the platform's access control, or add auth in front of the read routes, if
  that matters for your use.
- **The bot trusts its own environment.** Anyone who can set environment
  variables on the deployment can point it at a different DEMO account.
  Treat the platform's variable settings as a secret store.
