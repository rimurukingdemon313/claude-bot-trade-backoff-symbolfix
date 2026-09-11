FROM node:20-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY requirements.txt ./
# --break-system-packages is required on Debian's system Python 3.11+
# (PEP 668 externally-managed-environment guard). requests and the
# official tradelocker package are used only by
# diagnose_tradelocker_connection.py (a one-time startup diagnostic, see
# server/index.ts) — the live trading path (tradelocker_client.py,
# tradelocker_state.py) still uses only the standard library, per
# requirements.txt.
RUN pip install --break-system-packages --no-cache-dir requests tradelocker

COPY . .

RUN npm run build

EXPOSE 3000

CMD ["node", "index.js"]
