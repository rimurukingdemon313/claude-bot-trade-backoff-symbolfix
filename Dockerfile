# ---------------------------------------------------------------------------
# One container, two processes: the Python trading bot (the system) and the
# Node dashboard server (its window). Node supervises the bot and exits if
# it dies, so the platform restarts the whole thing rather than leaving a
# dashboard serving a dead trading process.
# ---------------------------------------------------------------------------

FROM node:20-bookworm-slim AS build

WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.js index.html ./
COPY src ./src
COPY public ./public
RUN npm run build


FROM node:20-bookworm-slim AS runtime

# python3 only — the bot uses nothing outside the standard library. The
# optional psycopg install below is what enables PostgreSQL persistence on
# Railway; without DATABASE_URL set, the bot uses SQLite and ignores it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BOT_PORT=8787 \
    SQLITE_PATH=/app/data/bot.db

COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY requirements.txt ./
# PEP 668 requires --break-system-packages on Debian's system Python.
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY server ./server
COPY bot ./bot
COPY index.js tsconfig.json ./
COPY --from=build /app/dist ./dist

# A writable directory for the SQLite fallback and the news cache. On
# Railway, mount a volume here or set DATABASE_URL so risk state survives
# a redeploy.
RUN mkdir -p /app/data && chown -R node:node /app
USER node

EXPOSE 5000

# tini reaps the Python child and forwards SIGTERM, so a deploy triggers
# the graceful shutdown path instead of killing a scan mid-flight.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||5000)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "index.js"]
