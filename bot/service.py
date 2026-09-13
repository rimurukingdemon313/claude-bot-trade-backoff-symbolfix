"""The long-running bot process.

Replaces the previous design where Node shelled out to a fresh `python3`
for every call. That approach re-authenticated with TradeLocker on every
single request (~8 HTTP calls per state read, 18+ per scan), which is what
was tripping Cloudflare's rate limiter — and it made shared state
impossible, so the scheduler flag, the session cache and any in-flight
execution intent died with each subprocess.

Here one process owns the broker session, the database, the scheduler and
all state, and exposes a small JSON API on localhost that the Node server
proxies. Only the standard library is used, so Railway needs no extra
Python dependencies.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .api import DashboardApi
from .broker.tradelocker import TradeLockerBroker
from .config import TradingConfig, load_config
from .errors import BotError, ConfigError
from .marketdata.provider import MarketDataProvider
from .observability import log_event
from .orchestrator import Orchestrator
from .scheduler import Scheduler
from .storage.db import open_database
from .storage.repositories import Repositories


class BotService:
    def __init__(self, config: TradingConfig | None = None, *, broker: Any = None) -> None:
        self.config = config or load_config()
        self.database = open_database(self.config.storage)
        self.repos = Repositories(self.database)
        self.broker = broker or TradeLockerBroker(self.config)
        self.market_data = MarketDataProvider(self.broker, self.config)
        self.orchestrator = Orchestrator(
            self.config,
            broker=self.broker,
            repositories=self.repos,
            market_data=self.market_data,
        )
        self.api = DashboardApi(self.config, self.orchestrator, self.repos)
        self.scheduler = Scheduler()
        self._shutting_down = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        result = self.orchestrator.startup()
        if not result.get("ok"):
            # Start the API anyway: the dashboard must be able to SHOW the
            # failure. Trading stays blocked because startup_complete is
            # False, which every scan checks first.
            log_event(
                "STARTUP",
                f"startup incomplete — API will serve health but trading is blocked: "
                f"{result.get('error')}",
                severity="critical",
            )

        scheduler_config = self.config.scheduler
        self.scheduler.add(
            "scan",
            scheduler_config.scan_interval_minutes * 60,
            self._safe(lambda: self.orchestrator.scan(source="scheduled")),
            aligned_to=scheduler_config.scan_interval_minutes,
            offset_seconds=scheduler_config.scan_offset_seconds,
        )
        self.scheduler.add(
            "positions",
            scheduler_config.position_poll_seconds,
            self._safe(self.orchestrator.manage_positions),
        )
        self.scheduler.add(
            "reconcile",
            scheduler_config.reconcile_interval_seconds,
            self._safe(self._reconcile_and_retry_startup),
        )
        self.scheduler.add("maintenance", 3600, self._safe(self._maintenance))
        self.scheduler.start()

    def _reconcile_and_retry_startup(self) -> None:
        """Periodic reconcile, and a retry of startup if it never completed.

        Without the retry, a transient broker outage at boot would leave
        the process permanently unable to trade until someone redeployed.
        """

        if not self.orchestrator.startup_complete:
            self.orchestrator.startup()
            return
        self.orchestrator.reconcile()

    def _maintenance(self) -> None:
        self.repos.equity.prune(keep=5000)

    @staticmethod
    def _safe(job: Callable[[], Any]) -> Callable[[], Any]:
        def wrapped() -> Any:
            try:
                return job()
            except BotError as exc:
                log_event("SCHEDULER", f"job error: {exc}", severity="error", **exc.as_dict())
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "SCHEDULER", f"unexpected job error: {exc}", severity="error"
                )
        return wrapped

    def shutdown(self) -> None:
        """Graceful stop. Positions are deliberately left untouched."""

        if self._shutting_down:
            return
        self._shutting_down = True
        log_event("SHUTDOWN", "stopping scheduler; open positions are left with the broker")
        self.scheduler.stop()
        try:
            self.database.close()
        except Exception:  # noqa: BLE001
            pass
        log_event("SHUTDOWN", "clean")


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def make_handler(service: BotService) -> type[BaseHTTPRequestHandler]:
    token = service.config.dashboard_token

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "TradeBot/2.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return  # structured logging only; no duplicate access log

        # -- helpers ---------------------------------------------------

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            """Commands require the token when one is configured.

            When no token is set the service binds to localhost only (see
            `serve`), so the command surface is not reachable from the
            internet either way.
            """

            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8")) or {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        # -- routes ----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            api = service.api
            routes: dict[str, Callable[[], Any]] = {
                "/healthz": lambda: {"status": "ok" if api.health()["ok"] else "degraded"},
                "/api/health": api.health,
                "/api/account": api.account,
                "/api/positions": api.open_positions,
                "/api/history": lambda: api.trade_history(
                    limit=int(query.get("limit", ["100"])[0])
                ),
                "/api/performance": api.performance,
                "/api/scan": lambda: api.latest_scan(
                    detail=query.get("detail", ["true"])[0] != "false"
                ),
                "/api/journal": lambda: api.journal(
                    limit=int(query.get("limit", ["100"])[0]),
                    symbol=query.get("symbol", [None])[0],
                ),
                "/api/risk": api.risk_state,
                "/api/snapshot": api.snapshot,
            }
            handler = routes.get(parsed.path)
            if handler is None:
                self._send(404, {"error": "not found"})
                return
            try:
                payload = handler()
            except Exception as exc:  # noqa: BLE001
                log_event("API", f"GET {parsed.path} failed: {exc}", severity="error")
                self._send(500, {"error": str(exc)})
                return
            status = 200
            if parsed.path == "/healthz" and payload.get("status") != "ok":
                status = 503
            self._send(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            body = self._body()
            api = service.api
            try:
                if parsed.path == "/api/control/scanning":
                    enabled = body.get("enabled")
                    if not isinstance(enabled, bool):
                        self._send(400, {"error": "enabled must be a boolean"})
                        return
                    self._send(200, api.set_scanning(enabled))
                elif parsed.path == "/api/control/kill-switch":
                    if body.get("active") is True:
                        self._send(200, api.trip_kill_switch(str(body.get("reason", "manual"))))
                    elif body.get("active") is False:
                        self._send(200, api.clear_kill_switch(force=bool(body.get("force"))))
                    else:
                        self._send(400, {"error": "active must be a boolean"})
                elif parsed.path == "/api/control/scan":
                    self._send(200, api.trigger_scan())
                elif parsed.path == "/api/control/reconcile":
                    self._send(200, api.trigger_reconcile())
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                log_event("API", f"POST {parsed.path} failed: {exc}", severity="error")
                self._send(500, {"error": str(exc)})

    return Handler


def serve(service: BotService, *, host: str = "127.0.0.1", port: int | None = None) -> ThreadingHTTPServer:
    """Bind the API. Defaults to localhost: the Node layer is the only
    intended client, and the broker-facing command surface should never be
    directly exposed."""

    # `port or ...` would treat an explicit 0 ("bind any free port") as unset.
    bind_port = port if port is not None else int(os.environ.get("BOT_PORT", "8787"))
    server = ThreadingHTTPServer((host, bind_port), make_handler(service))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="bot-api", daemon=True)
    thread.start()
    log_event("API", f"bot service listening on {host}:{bind_port}")
    return server


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        log_event("STARTUP", f"configuration is invalid: {exc}", severity="critical")
        raise SystemExit(2) from exc

    service = BotService(config)
    server = serve(service)
    service.start()

    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        log_event("SHUTDOWN", f"received signal {signum}")
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        stop.wait()
    finally:
        service.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
