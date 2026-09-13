"""TradeLocker DEMO broker adapter.

Responsibilities:
  * session lifecycle (login, refresh, accNum resolution) held in ONE
    long-lived process rather than re-authenticating per subprocess;
  * column-mapped decoding of TradeLocker's positional array responses;
  * instrument specification extraction for correct position sizing;
  * broker-native candles, so the strategy analyses the same prices it
    will trade on (the previous build analysed Yahoo Finance data and
    submitted the resulting levels to TradeLocker);
  * writes that never auto-retry.

Every method is small and pure-ish around the transport so the fake in
tests/fakes.py can stand in for the whole class.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from typing import Any, Mapping, Sequence

from ..clock import from_epoch, utc_now
from ..config import TradingConfig
from ..errors import BotError, BrokerAuthError, BrokerError, BrokerRejected, ConfigError
from ..observability import log_event
from .history import HistoryFetcher, TIMEFRAME_MINUTES
from .symbols import (
    alphanumeric,
    broker_suffix,
    canonical_symbol,
    same_instrument,
    split_currencies,
)
from .http import CircuitBreaker, HttpTransport, Throttle
from .models import (
    AccountState,
    BrokerOrder,
    BrokerPosition,
    InstrumentSpec,
    OrderResult,
    Quote,
)


def _num(value: Any, default: float | None = 0.0) -> float | None:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """Brokers rename fields between versions; try each candidate name."""

    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def normalize_symbol(raw: str) -> str:
    """Alphanumeric uppercase form, for exact-name comparison only.

    NOT for identity: `EURUSD` and `EURUSD.R` normalize differently. Use
    bot.broker.symbols.same_instrument() whenever two names need to be
    judged as the same tradable instrument — that distinction is what kept
    the duplicate-order check from seeing an existing position on a broker
    whose names carry a suffix.
    """

    return alphanumeric(raw)


#: Claim values larger than this are not environment markers; refusing
#: them keeps a hostile or malformed token from becoming a memory or log
#: problem on a path that runs before every order.
_MAX_CLAIM_CHARS = 256


def decode_token_claims(token: str | None) -> dict[str, Any] | None:
    """Read the payload of a broker-issued JWT.

    The DEMO guard needs a second, broker-controlled statement of which
    environment this session belongs to, and TradeLocker brands that omit
    a type field on the account record still sign one into the token.

    The signature is deliberately NOT verified: nothing here grants
    access, and the token already reached us over TLS from the host we
    authenticated against. It is read as evidence, never as authority —
    and because it is untrusted input, only scalar claims within a sane
    length survive, so no nested structure or unbounded blob is handed to
    the guard. Returns None when the token is absent or not a JWT, which
    the guard treats as "no evidence" (and therefore as failure).
    """

    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload)
        decoded = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None

    claims: dict[str, Any] = {}
    for key, value in decoded.items():
        if not isinstance(value, (str, bool, int, float)):
            continue
        if isinstance(value, str) and len(value) > _MAX_CLAIM_CHARS:
            continue
        claims[str(key)] = value
    return claims


class TradeLockerBroker:
    """Thread-safe TradeLocker client for a single DEMO account."""

    def __init__(self, config: TradingConfig, transport: HttpTransport | None = None) -> None:
        self.config = config
        self.broker_config = config.broker
        self.transport = transport or HttpTransport(
            timeout=config.broker.request_timeout,
            max_attempts=config.broker.max_attempts,
            circuit=CircuitBreaker(
                failure_threshold=config.broker.circuit_failure_threshold,
                reset_seconds=config.broker.circuit_reset_seconds,
            ),
            throttle=Throttle(min_interval=0.15),
        )
        self._lock = threading.RLock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0
        self._acc_num: str | None = None
        self._trade_config: dict[str, Any] | None = None
        self._instrument_cache: dict[str, InstrumentSpec] = {}
        self._instrument_cache_at = 0.0
        self._instrument_detail_error: str | None = None
        self._instruments_raw: list[dict[str, Any]] = []
        self._account_meta: dict[str, Any] | None = None
        self.instrument_cache_ttl = 3600.0
        self.history = HistoryFetcher(
            lambda path, query: self.get(path, query=query)
        )

    # -- session ---------------------------------------------------------

    def _require_credentials(self) -> None:
        if not self.broker_config.configured:
            missing = [
                name
                for name, value in (
                    ("TRADELOCKER_EMAIL", self.broker_config.email),
                    ("TRADELOCKER_PASSWORD", self.broker_config.password),
                    ("TRADELOCKER_SERVER", self.broker_config.server),
                    ("TRADELOCKER_ACC_ID", self.broker_config.account_id),
                )
                if not value
            ]
            raise ConfigError(f"TradeLocker credentials are incomplete; missing: {', '.join(missing)}")

    def login(self) -> None:
        self._require_credentials()
        result = self.transport.request(
            "POST",
            f"{self.broker_config.base_url}/auth/jwt/token",
            body={
                "email": self.broker_config.email,
                "password": self.broker_config.password,
                "server": self.broker_config.server,
            },
            # Login is the one write-shaped call that is genuinely
            # idempotent: authenticating twice creates no order and no
            # financial side effect, and it is the call most likely to be
            # rate limited on a cold start.
            idempotent=True,
        )
        token = result.get("accessToken")
        if not token:
            raise BrokerAuthError("TradeLocker login returned no accessToken")
        with self._lock:
            self._access_token = token
            self._refresh_token = result.get("refreshToken")
            self._expires_at = time.time() + 50 * 60
            self._acc_num = None
            self._trade_config = None
        log_event("BROKER", "authenticated with TradeLocker")

    def _try_refresh(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            result = self.transport.request(
                "POST",
                f"{self.broker_config.base_url}/auth/jwt/refresh",
                body={"refreshToken": self._refresh_token},
                idempotent=True,
            )
        except BrokerError:
            return False
        token = result.get("accessToken")
        if not token:
            return False
        with self._lock:
            self._access_token = token
            if result.get("refreshToken"):
                self._refresh_token = result["refreshToken"]
            self._expires_at = time.time() + 50 * 60
        return True

    def ensure_session(self) -> None:
        with self._lock:
            needs_login = self._access_token is None
            expired = time.time() >= self._expires_at
        if needs_login:
            self.login()
        elif expired and not self._try_refresh():
            self.login()
        if self._acc_num is None:
            self._resolve_account()

    def _resolve_account(self) -> None:
        """Resolve accNum (header) from accountId (path) and capture the
        account metadata the DEMO guard verifies against."""

        result = self._raw("GET", "/auth/jwt/all-accounts", authed_only=True)
        accounts = result.get("accounts", []) if isinstance(result, Mapping) else []
        match = next(
            (a for a in accounts if str(a.get("id")) == str(self.broker_config.account_id)), None
        )
        if match is None:
            match = next(
                (a for a in accounts if str(a.get("accNum")) == str(self.broker_config.account_id)),
                None,
            )
        if match is None:
            raise BrokerAuthError(
                f"account {self.broker_config.account_id} was not found in this TradeLocker login. "
                f"Available account ids: {[a.get('id') for a in accounts]}"
            )
        with self._lock:
            self._acc_num = str(match.get("accNum"))
            self._account_meta = dict(match)

    @property
    def account_metadata(self) -> dict[str, Any] | None:
        """Raw account record used by the DEMO guard. None until connected."""

        return self._account_meta

    @property
    def session_claims(self) -> dict[str, Any] | None:
        """Environment claims from the broker-signed access token.

        The DEMO guard's second source. Returns the claims only — the
        token itself never leaves this class, so nothing downstream can
        log or persist a live credential while reading the evidence.
        """

        with self._lock:
            token = self._access_token
        return decode_token_claims(token)

    # -- request helpers -------------------------------------------------

    def _raw(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        authed_only: bool = False,
        idempotent: bool | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if not authed_only:
            headers["accNum"] = str(self._acc_num)
        return self.transport.request(
            method,
            f"{self.broker_config.base_url}{path}",
            headers=headers,
            body=body,
            query=query,
            idempotent=idempotent,
        )

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, Mapping) and "s" in payload and "d" in payload:
            if payload.get("s") != "ok":
                raise BrokerRejected(f"TradeLocker returned status {payload.get('s')}: {payload}")
            return payload["d"]
        return payload

    def get(self, path: str, query: Mapping[str, Any] | None = None) -> Any:
        self.ensure_session()
        try:
            return self._unwrap(self._raw("GET", path, query=query))
        except BrokerAuthError:
            # One re-login then one resend: the token was invalidated
            # server-side rather than merely expired. Reads only.
            self.login()
            self._resolve_account()
            return self._unwrap(self._raw("GET", path, query=query))

    def write(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        """Writes never auto-retry and never auto-re-login-and-resend."""

        self.ensure_session()
        return self._unwrap(self._raw(method, path, body=body, idempotent=False))

    # -- config / decoding ----------------------------------------------

    def trade_config(self) -> dict[str, Any]:
        if self._trade_config is None:
            self._trade_config = self.get("/trade/config") or {}
        return self._trade_config

    def _columns(self, panel: str) -> list[str]:
        config = self.trade_config().get(panel, {})
        return [str(column.get("id")) for column in config.get("columns", [])]

    def _decode(self, rows: Sequence[Sequence[Any]], panel: str) -> list[dict[str, Any]]:
        columns = self._columns(panel)
        if not columns:
            raise BrokerError(f"TradeLocker /trade/config did not describe {panel}")
        return [dict(zip(columns, row)) for row in rows]

    # -- account ---------------------------------------------------------

    def account_state(self) -> AccountState:
        columns = self._columns("accountDetailsConfig")
        result = self.get(f"/trade/accounts/{self.broker_config.account_id}/state") or {}
        values = result.get("accountDetailsData", [])
        data = dict(zip(columns, values)) if columns else {}
        balance = _num(_first(data, "balance", "accountBalance"), None)
        if balance is None:
            raise BrokerError(
                "TradeLocker account state did not include a usable balance. "
                "Refusing to substitute a placeholder — every risk calculation depends on it."
            )
        equity = _num(_first(data, "projectedBalance", "equity", "balance"), balance) or balance
        return AccountState(
            balance=float(balance),
            equity=float(equity),
            margin_used=float(_num(_first(data, "marginUsed", "usedMargin", "blockedBalance"), 0.0) or 0.0),
            margin_available=float(_num(_first(data, "marginAvailable", "availableFunds", "freeFunds"), 0.0) or 0.0),
            open_pnl=float(_num(_first(data, "openNetPnL", "openPnL", "unrealizedPl"), 0.0) or 0.0),
            today_pnl=float(_num(_first(data, "todayNet", "todayNetPnL", "dailyPnL"), 0.0) or 0.0),
            currency=str(
                _first(
                    self._account_meta or {}, "currency", "accountCurrency", default="USD"
                )
            ),
            raw=data,
        )

    # -- instruments -----------------------------------------------------

    def _load_instruments(self, *, force: bool = False) -> list[dict[str, Any]]:
        fresh = (time.monotonic() - self._instrument_cache_at) < self.instrument_cache_ttl
        if self._instruments_raw and fresh and not force:
            return self._instruments_raw
        result = self.get(f"/trade/accounts/{self.broker_config.account_id}/instruments") or {}
        instruments = result.get("instruments", [])
        if not instruments:
            raise BrokerError("TradeLocker returned no instruments for this account")
        self._instruments_raw = list(instruments)
        self._instrument_cache_at = time.monotonic()
        self._instrument_cache.clear()
        return self._instruments_raw

    def available_symbols(self) -> list[str]:
        return sorted({str(item.get("name", "")) for item in self._load_instruments() if item.get("name")})

    def instrument(self, symbol: str, *, force: bool = False) -> InstrumentSpec:
        """Resolve a symbol to a full specification.

        Ambiguity is fatal: if 'EURUSD' prefix-matches two broker
        instruments, this raises rather than picking one, because a wrong
        pick places a real order on the wrong instrument.
        """

        key = canonical_symbol(symbol) or normalize_symbol(symbol)
        if not force and key in self._instrument_cache:
            return self._instrument_cache[key]

        instruments = self._load_instruments(force=force)
        requested = normalize_symbol(symbol)

        # 1. The broker's exact name, so TRADED_SYMBOLS=EURUSD.R works.
        candidates = [i for i in instruments if normalize_symbol(str(i.get("name", ""))) == requested]
        # 2. Same instrument by canonical pair, so TRADED_SYMBOLS=EURUSD finds
        #    EURUSD.R on a suffixed broker without guessing at the suffix.
        if not candidates:
            candidates = [
                i for i in instruments if same_instrument(str(i.get("name", "")), symbol)
            ]
        # 3. Last resort: prefix. Kept for names this module cannot resolve to
        #    a currency pair at all (indices, commodities).
        if not candidates:
            candidates = [
                i for i in instruments if normalize_symbol(str(i.get("name", ""))).startswith(requested)
            ]
        if len(candidates) > 1:
            names = ", ".join(sorted(str(i.get("name")) for i in candidates))
            raise BrokerRejected(
                f"symbol {symbol!r} matches multiple broker instruments ({names}); "
                "set TRADED_SYMBOLS to the exact broker name to disambiguate"
            )
        if not candidates:
            available = self.available_symbols()
            raise BrokerRejected(
                f"symbol {symbol!r} not available on this account. "
                f"{len(available)} instruments exist, e.g. {', '.join(available[:20])}"
            )

        spec = self._build_spec(symbol, candidates[0])
        self._instrument_cache[key] = spec
        return spec

    def _instrument_details(
        self, raw: Mapping[str, Any], route_id: int | None
    ) -> dict[str, Any]:
        """The instrument's full specification.

        The account's instrument LIST is a directory — id, name, type,
        routes — and on TradeLocker it carries no contract size at all.
        Sizing read from it alone found nothing and skipped every symbol,
        which is how a correctly configured account produced "no symbol
        produced an executable candidate" on every scan.

        The size lives behind a second call, per instrument. It is fetched
        once and cached with the spec, so this costs one request per symbol
        per cache period rather than one per scan.

        A failure here is never swallowed: a guessed contract size
        mis-sizes every order on the instrument, which is worse than
        skipping it.
        """

        self._instrument_detail_error = None
        instrument_id = int(_num(_first(raw, "tradableInstrumentId", "id", default=0), 0) or 0)
        if not instrument_id:
            return dict(raw.get("details") or {})

        query: dict[str, Any] = {"locale": "en"}
        if route_id:
            query["routeId"] = route_id
        try:
            response = self.get(f"/trade/instruments/{instrument_id}", query=query) or {}
        except BotError as exc:
            # Not swallowed: the caller reports "no contract size" and the
            # reason the lookup failed travels with it, so this never
            # becomes a symbol that is silently skipped for an unrelated
            # cause. Broad on purpose — a missing credential and a rejected
            # request both leave sizing unknowable, and unknowable is the
            # thing the caller must refuse to guess past.
            self._instrument_detail_error = str(exc)[:200]
            log_event(
                "BROKER",
                f"instrument details unavailable for {raw.get('name')}: {exc}",
                severity="warning",
            )
            return dict(raw.get("details") or {})

        # TradeLocker wraps a single record in "d"; some brands do not.
        payload = response.get("d") if isinstance(response.get("d"), Mapping) else response
        if not isinstance(payload, Mapping):
            return dict(raw.get("details") or {})
        merged = dict(payload)
        nested = merged.get("details")
        if isinstance(nested, Mapping):
            merged = {**merged, **nested}
        return merged

    def _build_spec(self, symbol: str, raw: Mapping[str, Any]) -> InstrumentSpec:
        routes = raw.get("routes", []) or []
        trade_route = next((r for r in routes if str(r.get("type")).upper() == "TRADE"), None)
        info_route = next((r for r in routes if str(r.get("type")).upper() in ("INFO", "QUOTE")), None)
        if trade_route is None:
            raise BrokerRejected(f"instrument {symbol!r} exposes no TRADE route")

        details: dict[str, Any] = {
            **{k: v for k, v in raw.items() if k != "routes"},
            **(raw.get("details") or {}),
        }

        def read_contract_size(source: Mapping[str, Any]) -> float | None:
            return _num(
                _first(
                    source,
                    "contractSize",
                    "lotSize",
                    "unitsPerLot",
                    "contract_size",
                    "lotsize",
                    "unitSize",
                    "notionalPerLot",
                    default=None,
                ),
                None,
            )

        contract_size = read_contract_size(details)
        if contract_size is None or contract_size <= 0:
            # Only now is the second call worth making. Brands that put the
            # size in the directory cost nothing extra; GATESFX does not,
            # and without this every symbol was skipped for want of a field
            # that exists one request away.
            info_route_id = int(_num((info_route or {}).get("id"), 0) or 0) or None
            details.update(self._instrument_details(raw, info_route_id))
            contract_size = read_contract_size(details)
        tick_size = _num(_first(details, "tickSize", "minPriceIncrement", "pipSize", default=None), None)
        digits_raw = _first(details, "digits", "precision", "pricePrecision", default=None)
        digits = int(_num(digits_raw, 5) or 5)

        if tick_size is None or tick_size <= 0:
            tick_size = 10 ** (-digits)
        if contract_size is None or contract_size <= 0:
            # Refusing to guess is the whole point: an assumed contract
            # size silently mis-sizes every order on this instrument.
            # Name what WAS returned. The previous message said only that
            # the field was absent, which cost a full deploy cycle to
            # diagnose — exactly as the DEMO guard's did.
            seen = ", ".join(sorted(str(key) for key in details)) or "no fields"
            why = getattr(self, "_instrument_detail_error", None)
            raise BrokerRejected(
                f"instrument {symbol!r} does not expose a contract size; position sizing "
                f"cannot be computed safely and this symbol will be skipped. "
                + (
                    f"The detail lookup failed: {why}. "
                    if why
                    else ""
                )
                + f"Fields the broker returned: {seen}"
            )

        broker_name = str(raw.get("name", symbol))
        base, quote = split_currencies(broker_name)
        account_currency = str(_first(self._account_meta or {}, "currency", default="USD"))
        # spec.symbol is the CANONICAL pair, never the decorated broker name:
        # it is the join key for duplicate detection, the per-symbol limit,
        # news blackouts and correlation. spec.broker_name keeps the exact
        # name the API expects.
        canonical = canonical_symbol(broker_name) or normalize_symbol(broker_name)
        suffix = broker_suffix(broker_name)
        if suffix:
            log_event(
                "BROKER",
                f"resolved {symbol} to broker instrument {broker_name} (suffix {suffix!r})",
                symbol=canonical,
                broker_name=broker_name,
            )
        return InstrumentSpec(
            symbol=canonical,
            broker_name=broker_name,
            tradable_instrument_id=int(
                _num(_first(raw, "tradableInstrumentId", "id", default=0), 0) or 0
            ),
            route_id=int(_num(trade_route.get("id"), 0) or 0),
            quote_route_id=int(_num(info_route.get("id"), 0) or 0) if info_route else None,
            contract_size=float(contract_size),
            tick_size=float(tick_size),
            tick_value=_num(_first(details, "tickValue", "pointValue", default=None), None),
            lot_step=float(_num(_first(details, "lotStep", "lotSizeStep", "qtyStep", default=0.01), 0.01) or 0.01),
            min_lot=float(_num(_first(details, "minLotSize", "minQty", "minLot", default=0.01), 0.01) or 0.01),
            max_lot=float(_num(_first(details, "maxLotSize", "maxQty", "maxLot", default=100.0), 100.0) or 100.0),
            base_currency=base,
            quote_currency=quote,
            account_currency=account_currency,
            digits=digits,
            raw=dict(raw),
        )

    # -- market data -----------------------------------------------------

    def quote(self, spec: InstrumentSpec) -> Quote:
        route = spec.quote_route_id or spec.route_id
        result = self.get(
            f"/trade/accounts/{self.broker_config.account_id}/instruments/"
            f"{spec.tradable_instrument_id}/quotes",
            query={"routeId": route},
        ) or {}
        bid = _num(_first(result, "bp", "bid", "bidPrice"), None)
        ask = _num(_first(result, "ap", "ask", "askPrice"), None)
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            raise BrokerError(f"TradeLocker returned no usable quote for {spec.symbol}: {result}")
        if ask < bid:
            raise BrokerError(f"TradeLocker returned a crossed quote for {spec.symbol} (bid {bid} > ask {ask})")
        return Quote(symbol=spec.symbol, bid=float(bid), ask=float(ask), timestamp=utc_now())

    def candles(
        self, spec: InstrumentSpec, timeframe: str, *, count: int = 300
    ) -> list[dict[str, Any]]:
        """Broker-native OHLC history.

        The endpoint shape is DISCOVERED rather than assumed (see
        bot/broker/history.py): TradeLocker deployments differ in path, in
        the time unit of the range bounds, and in the envelope key the bars
        arrive under. Hard-coding one guess is why the previous version
        could silently return nothing on a broker that used another.

        Returned rows are raw dicts; validation (ordering, gaps, staleness,
        forming-candle removal) happens in bot.marketdata.validation so the
        rules stay testable without a broker.
        """

        if timeframe.upper() not in TIMEFRAME_MINUTES:
            raise BrokerError(f"unsupported timeframe {timeframe!r}")
        return self.history.fetch(
            instrument_id=spec.tradable_instrument_id,
            route_id=spec.quote_route_id or spec.route_id,
            timeframe=timeframe,
            count=count,
        )

    # -- positions / orders ----------------------------------------------

    def positions(self) -> list[BrokerPosition]:
        result = self.get(f"/trade/accounts/{self.broker_config.account_id}/positions") or {}
        rows = self._decode(result.get("positions", []), "positionsConfig")
        names = {
            str(item.get("tradableInstrumentId") or item.get("id")): str(item.get("name", ""))
            for item in self._load_instruments()
        }
        orders = {order.order_id: order for order in self.orders()}
        positions: list[BrokerPosition] = []
        for row in rows:
            instrument_id = str(_first(row, "tradableInstrumentId", "instrumentId", default=""))
            stop_order = orders.get(str(_first(row, "stopLossId", default="")))
            tp_order = orders.get(str(_first(row, "takeProfitId", default="")))
            opened = _first(row, "openDate", "openTime")
            positions.append(
                BrokerPosition(
                    position_id=str(_first(row, "id", "positionId", default="")),
                    # Canonical, so this matches the plan's symbol. Reporting the
                    # decorated broker name here is what broke the executor's
                    # duplicate check on a suffixed account.
                    symbol=(
                        canonical_symbol(names.get(instrument_id, instrument_id))
                        or normalize_symbol(names.get(instrument_id, instrument_id))
                    ),
                    instrument_id=int(_num(instrument_id, 0) or 0),
                    direction="BUY" if str(_first(row, "side", default="buy")).lower() == "buy" else "SELL",
                    quantity=float(_num(_first(row, "qty", "quantity"), 0.0) or 0.0),
                    entry_price=float(_num(_first(row, "avgPrice", "openPrice", "price"), 0.0) or 0.0),
                    stop_loss=(stop_order.stop_price or stop_order.price) if stop_order else _num(_first(row, "stopLoss", default=None), None),
                    take_profit=(tp_order.price or tp_order.stop_price) if tp_order else _num(_first(row, "takeProfit", default=None), None),
                    unrealized_pnl=float(_num(_first(row, "unrealizedPl", "unrealizedPnL"), 0.0) or 0.0),
                    opened_at=from_epoch(float(opened)) if opened not in (None, "") else None,
                    raw=row,
                )
            )
        return positions

    def orders(self) -> list[BrokerOrder]:
        result = self.get(f"/trade/accounts/{self.broker_config.account_id}/orders") or {}
        rows = self._decode(result.get("orders", []), "ordersConfig")
        return [self._to_order(row) for row in rows]

    def order_history(self, limit: int = 200) -> list[BrokerOrder]:
        result = self.get(f"/trade/accounts/{self.broker_config.account_id}/ordersHistory") or {}
        rows = self._decode(result.get("ordersHistory", []), "ordersHistoryConfig")
        rows.sort(
            key=lambda row: _num(_first(row, "lastModified", "createdDate"), 0.0) or 0.0,
            reverse=True,
        )
        return [self._to_order(row) for row in rows[:limit]]

    def _to_order(self, row: Mapping[str, Any]) -> BrokerOrder:
        created = _first(row, "createdDate", "lastModified")
        return BrokerOrder(
            order_id=str(_first(row, "id", "orderId", default="")),
            position_id=(str(row["positionId"]) if row.get("positionId") not in (None, "") else None),
            symbol=str(_first(row, "symbol", default="")),
            instrument_id=int(_num(_first(row, "tradableInstrumentId", "instrumentId"), 0) or 0),
            direction="BUY" if str(_first(row, "side", default="buy")).lower() == "buy" else "SELL",
            quantity=float(_num(_first(row, "qty", "quantity"), 0.0) or 0.0),
            status=str(_first(row, "status", default="")).upper(),
            price=_num(_first(row, "price", "avgPrice", default=None), None),
            stop_price=_num(_first(row, "stopPrice", default=None), None),
            order_type=str(_first(row, "type", default="")).lower(),
            created_at=from_epoch(float(created)) if created not in (None, "") else None,
            raw=dict(row),
        )

    # -- writes ----------------------------------------------------------

    def place_market_order(
        self,
        spec: InstrumentSpec,
        *,
        direction: str,
        quantity: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        """Submit a market order with attached protection.

        SL and TP are REQUIRED, not optional. An unprotected position is
        an unbounded loss, and there is no code path in this system that
        opens one.
        """

        if quantity <= 0:
            raise BrokerRejected(f"refusing to submit a non-positive quantity ({quantity})")
        if not stop_loss or not take_profit or stop_loss <= 0 or take_profit <= 0:
            raise BrokerRejected("refusing to submit an order without a positive stop loss and take profit")
        if direction.upper() not in ("BUY", "SELL"):
            raise BrokerRejected(f"unknown order direction {direction!r}")

        body = {
            "tradableInstrumentId": spec.tradable_instrument_id,
            "routeId": spec.route_id,
            "qty": quantity,
            "side": direction.lower(),
            "type": "market",
            "validity": "IOC",
            "price": 0,
            "stopLoss": spec.round_price(stop_loss),
            "stopLossType": "absolute",
            "takeProfit": spec.round_price(take_profit),
            "takeProfitType": "absolute",
        }
        result = self.write("POST", f"/trade/accounts/{self.broker_config.account_id}/orders", body)
        order_id = None
        if isinstance(result, Mapping):
            order_id = _first(result, "orderId", "id", default=None)
        return OrderResult(order_id=str(order_id) if order_id is not None else None, raw=dict(result or {}))

    def modify_position(
        self, position_id: str, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
        if take_profit is not None:
            body["takeProfit"] = take_profit
        if not body:
            return {}
        return self.write("PATCH", f"/trade/positions/{position_id}", body) or {}

    def close_position(self, position_id: str, quantity: float | None = None) -> dict[str, Any]:
        body = {"qty": quantity if quantity is not None else 0}
        return self.write("DELETE", f"/trade/positions/{position_id}", body) or {}

    def health(self) -> dict[str, Any]:
        return {
            "connected": self._access_token is not None,
            "accountResolved": self._acc_num is not None,
            "instrumentsCached": len(self._instrument_cache),
            "history": self.history.describe(),
            **self.transport.health(),
        }
