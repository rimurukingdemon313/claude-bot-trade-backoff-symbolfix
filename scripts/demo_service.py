"""Boot the real BotService against the in-memory fake broker.

Used for end-to-end smoke testing of the Node ↔ Python ↔ dashboard wiring
without broker credentials. It runs the REAL orchestrator, risk engine and
executor — only the network boundary is replaced.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from bot.config import load_config  # noqa: E402
from bot.service import BotService, serve  # noqa: E402
from fakes import FakeBroker, aligned_htf, bullish_setup_m15  # noqa: E402


def main() -> None:
    os.environ.setdefault("SQLITE_PATH", "/tmp/smoke-bot.db")
    config = load_config()
    config = dataclasses.replace(
        config,
        symbols=("EURUSD",),
        ai=dataclasses.replace(config.ai, enabled=False),
        news=dataclasses.replace(config.news, enabled=False),
    )

    broker = FakeBroker()
    # Anchor the fixture to the current candle close so the staleness guard
    # is exercised realistically rather than always firing on old data.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    m15 = bullish_setup_m15(end=end)
    broker.set_series("EURUSD", "M15", m15)
    broker.set_series("EURUSD", "H1", aligned_htf(m15, timeframe="H1"))
    broker.set_series("EURUSD", "H4", aligned_htf(m15, timeframe="H4"))
    print(f"[smoke] fixture anchored to {end.isoformat()}", flush=True)

    service = BotService(config, broker=broker)
    serve(service, host="127.0.0.1", port=int(os.environ.get("BOT_PORT", "8787")))
    service.orchestrator.startup()
    print("[smoke] bot service ready", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
