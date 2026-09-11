# Paper Trading Bot Dashboard (TradeLocker)

React + Vite dashboard, Express API server, and a Python trading/analysis engine
(TradeLocker client, SMC analysis, risk engine, market data, economic calendar).

## Stack

- Frontend: React 19, Vite 7, Tailwind CSS v4, TanStack Query, Recharts
- Server: Express 5 (TypeScript), pino logging
- Engine: Python 3 (TradeLocker client with retry/backoff and symbol resolution)

## Setup

```sh
npm install
pip install -r requirements.txt
cp .env.example .env   # then fill in your real values
```

## Development / production

```sh
npm run build      # build the frontend
npm start          # start the Express server (serves the built frontend)
npm run typecheck  # TypeScript check
```

## Configuration

All credentials come from environment variables — see `.env.example`.
Never commit real credentials; set them in your hosting provider's
environment variable settings.

## Diagnostics

```sh
python diagnose_tradelocker_connection.py
```

Checks credentials, authentication, account access, and symbol resolution.

## Tests

```sh
pytest
```

## Demo

```sh
python run_smc_demo.py
```

## Docker

```sh
docker build -t paper-trading-dashboard .
docker run --env-file .env -p 5000:5000 paper-trading-dashboard
```

## Layout

```
src/                 React dashboard
server/              Express API (routes, middlewares, lib)
analysis_engine/     SMC / analysis modules
tests/               Python test suite
*.py                 TradeLocker client, state, risk, market data, calendar
```
