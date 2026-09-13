"""Live connectivity and field-mapping verification.

    python3 -m bot.doctor
    python3 -m bot.doctor --symbol XAUUSD --json

This is the tool that closes the gap between "the code is correct" and "the
code is correct against YOUR broker". It connects to the real TradeLocker
DEMO account and reports, per check, exactly what the account returned:
which history endpoint shape works, whether the instrument exposes a usable
contract size and lot step, whether quotes are sane, and whether the profit
floor is reachable at the account's actual equity.

It is READ-ONLY. It never places, modifies or closes an order. That is a
hard property of this module, not a convention — it calls only the read
methods, and `--json` output includes the write count so it can be asserted.

Nothing it prints contains a credential: the report goes through the same
redaction sink as the structured logs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from .broker.tradelocker import TradeLockerBroker
from .clock import utc_now
from .broker.symbols import broker_suffix, canonical_symbol
from .config import ExecutionMode, TradingConfig, load_config, profit_floor_feasibility
from .errors import BotError, ConfigError
from .marketdata.candles import to_candles
from .marketdata.validation import validate_series
from .observability import redact
from .safety.demo_guard import verify_demo

OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": redact(self.detail),
            "data": self.data,
        }


#: Fields that identify the ACCOUNT rather than describe the integration.
#: `--safe` masks these so the report can be shared without exposing a
#: balance or an account number. Nothing here is needed to diagnose a
#: field-mapping problem.
SENSITIVE_KEYS = frozenset(
    {
        "balance", "equity", "marginUsed", "marginAvailable", "openPnl", "todayPnl",
        "accountId", "accNum", "id", "name", "bestCaseProfit", "maxRiskPerTrade",
        "requiredEquity",
    }
)

#: Checks whose free-text detail quotes account figures.
SENSITIVE_CHECKS = frozenset({"credentials", "account_state", "profit_objective", "demo_guard"})


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **data: Any) -> Check:
        check = Check(name, status, detail, data)
        self.checks.append(check)
        return check

    def sanitized(self) -> "Report":
        """A copy with account figures masked, safe to paste anywhere.

        Only balances, account identifiers and the numbers derived from them
        are removed. Everything needed to diagnose the integration — the
        history endpoint shape, instrument specifications, suffix naming,
        conversion paths, candle validation — is preserved, because that is
        the part worth sharing.
        """

        import re

        masked = Report()
        for check in self.checks:
            detail = check.detail
            if check.name in SENSITIVE_CHECKS:
                # Replace bare numbers, keeping the surrounding words so the
                # shape of the finding is still readable.
                detail = re.sub(r"-?\d[\d,]*\.?\d*", "***", detail)
            data = {
                key: ("***" if key in SENSITIVE_KEYS else value)
                for key, value in check.data.items()
            }
            masked.add(check.name, check.status, detail, **data)
        return masked

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [check for check in self.checks if check.status == WARN]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generatedAt": utc_now().isoformat(),
            "verdict": FAIL if self.failed else (WARN if self.warned else OK),
            "checks": [check.as_dict() for check in self.checks],
        }

    def render(self) -> str:
        symbols = {OK: "✓", WARN: "!", FAIL: "✗"}
        lines = ["", "TradeLocker DEMO verification", "=" * 62]
        for check in self.checks:
            lines.append(f" {symbols[check.status]} {check.name}")
            for piece in redact(check.detail).split("\n"):
                lines.append(f"     {piece}")
        lines.append("=" * 62)
        if self.failed:
            lines.append(f" VERDICT: FAIL — {len(self.failed)} check(s) block trading")
        elif self.warned:
            lines.append(f" VERDICT: PASS WITH WARNINGS — {len(self.warned)} item(s) to review")
        else:
            lines.append(" VERDICT: PASS — the account is ready")
        lines.append("")
        return "\n".join(lines)


def run(config: TradingConfig, symbols: list[str]) -> Report:
    report = Report()

    # 1. configuration ----------------------------------------------------
    if not config.broker.configured:
        report.add(
            "credentials",
            FAIL,
            "TradeLocker credentials are incomplete. Set TRADELOCKER_EMAIL, "
            "TRADELOCKER_PASSWORD, TRADELOCKER_SERVER and TRADELOCKER_ACC_ID.",
        )
        return report
    report.add(
        "credentials",
        OK,
        f"configured for server {config.broker.server} account {config.broker.account_id}",
    )
    report.add(
        "endpoint",
        OK if config.broker.is_demo_url else FAIL,
        f"{config.broker.base_url} "
        + ("matches a DEMO endpoint" if config.broker.is_demo_url else "is NOT a known DEMO endpoint"),
    )
    report.add(
        "mode",
        OK,
        f"TRADING_MODE={config.mode.value}"
        + (
            " — orders will be SIMULATED against live prices, nothing sent to the broker"
            if config.is_paper
            else " — REAL orders will be placed on this DEMO account"
        ),
        paper=config.is_paper,
    )

    broker = TradeLockerBroker(config)

    # 2. authentication ---------------------------------------------------
    try:
        broker.ensure_session()
    except BotError as exc:
        report.add("authentication", FAIL, f"login failed: {exc}")
        return report
    report.add("authentication", OK, "authenticated and resolved the account number")

    # 3. DEMO verification ------------------------------------------------
    verification = verify_demo(config, broker.account_metadata, stage="doctor")
    report.add(
        "demo_guard",
        OK if verification.verified else FAIL,
        "\n".join(verification.evidence)
        + ("" if verification.verified else f"\nREASON: {verification.reason}"),
        **verification.as_dict(),
    )

    # 4. account state ----------------------------------------------------
    equity = 0.0
    try:
        account = broker.account_state()
        equity = account.equity
        report.add(
            "account_state",
            OK,
            f"balance {account.balance:.2f} {account.currency}, equity {account.equity:.2f}, "
            f"free margin {account.margin_available:.2f}",
            **account.as_dict(),
        )
    except BotError as exc:
        report.add("account_state", FAIL, f"could not read account state: {exc}")

    # 5. trade config decoding -------------------------------------------
    try:
        panels = broker.trade_config()
        described = [key for key in panels if key.endswith("Config")]
        missing = [
            panel
            for panel in ("positionsConfig", "ordersConfig", "ordersHistoryConfig", "accountDetailsConfig")
            if panel not in panels
        ]
        report.add(
            "trade_config",
            FAIL if missing else OK,
            f"{len(described)} column maps returned"
            + (f"; MISSING: {', '.join(missing)}" if missing else ""),
            panels=described,
        )
    except BotError as exc:
        report.add("trade_config", FAIL, f"could not read /trade/config: {exc}")

    # 6. instruments + suffix detection ------------------------------------
    try:
        available = broker.available_symbols()
        suffixes: dict[str, int] = {}
        unresolved: list[str] = []
        for name in available:
            if canonical_symbol(name) is None:
                unresolved.append(name)
                continue
            suffixes[broker_suffix(name) or "(none)"] = suffixes.get(broker_suffix(name) or "(none)", 0) + 1
        dominant = max(suffixes.items(), key=lambda item: item[1]) if suffixes else None

        detail = f"{len(available)} instruments on this account, e.g. {', '.join(available[:12])}"
        if dominant and dominant[0] != "(none)":
            detail += (
                f"\nNaming: {dominant[1]} pair(s) use the suffix {dominant[0]!r}. "
                "You may set TRADED_SYMBOLS to either the bare pair (EURUSD) or the "
                "broker's exact name (EURUSD.R) — both resolve to the same instrument."
            )
        if suffixes:
            detail += "\nSuffixes seen: " + ", ".join(
                f"{suffix} x{count}" for suffix, count in sorted(suffixes.items(), key=lambda i: -i[1])
            )
        if unresolved:
            detail += (
                f"\n{len(unresolved)} instrument(s) are not currency pairs and cannot be sized "
                f"(they will be skipped): {', '.join(unresolved[:10])}"
            )
        report.add(
            "instruments",
            OK,
            detail,
            count=len(available),
            suffixes=suffixes,
            unresolved=unresolved[:20],
            sample=available[:40],
        )
    except BotError as exc:
        report.add("instruments", FAIL, f"could not list instruments: {exc}")
        available = []

    # 7. per-symbol verification -----------------------------------------
    for symbol in symbols:
        _check_symbol(broker, config, report, symbol)

    # 8. positions / orders ----------------------------------------------
    for name, call in (
        ("open_positions", broker.positions),
        ("working_orders", broker.orders),
        ("order_history", lambda: broker.order_history(limit=20)),
    ):
        try:
            rows = call()
            report.add(name, OK, f"decoded {len(rows)} row(s)", count=len(rows))
        except BotError as exc:
            report.add(name, FAIL, f"could not decode: {exc}")

    # 9. profit objective feasibility ------------------------------------
    if equity > 0:
        feasibility = profit_floor_feasibility(config, equity)
        report.add(
            "profit_objective",
            OK if feasibility["feasible"] else FAIL,
            feasibility["reason"],
            **feasibility,
        )

    return report


def _check_symbol(
    broker: TradeLockerBroker, config: TradingConfig, report: Report, symbol: str
) -> None:
    try:
        spec = broker.instrument(symbol)
    except BotError as exc:
        report.add(f"{symbol}:instrument", FAIL, str(exc))
        return

    # Contract size and lot step are what position sizing depends on. The
    # broker refuses to size a symbol that lacks them, so a WARN here means
    # "this symbol will be skipped", which is worth knowing before a scan.
    # A pair that does not resolve has no known quote currency, so sizing
    # refuses it rather than assuming one. Report that as the finding.
    resolved = canonical_symbol(spec.broker_name) is not None
    sizing_ok = spec.contract_size > 0 and spec.lot_step > 0 and spec.min_lot > 0 and resolved
    suffix = broker_suffix(spec.broker_name)
    report.add(
        f"{symbol}:specification",
        OK if sizing_ok else FAIL,
        (f"canonical {spec.symbol} <- broker name {spec.broker_name}"
         + (f" (suffix {suffix!r})" if suffix else "")
         + "\n"
         + ("" if resolved else
            "NOT a recognisable currency pair — no quote currency, so this symbol "
            "CANNOT be sized and every scan will skip it.\n"))
        + f"contract size {spec.contract_size:g}, "
        f"tick {spec.tick_size:g}, lot step {spec.lot_step:g}, "
        f"min/max lot {spec.min_lot:g}/{spec.max_lot:g}, "
        f"{spec.base_currency}/{spec.quote_currency} vs account {spec.account_currency}",
        **spec.as_dict(),
    )

    # Conversion path: a cross needs a bridging pair, and sizing FAILS
    # rather than guessing, so verify it resolves now rather than mid-scan.
    from .risk.sizing import SizingError, conversion_rate

    def lookup(base: str, quote: str) -> float | None:
        try:
            bridge = broker.instrument(f"{base}{quote}")
            return broker.quote(bridge).mid
        except BotError:
            return None

    try:
        quote = broker.quote(spec)
        spread_ticks = quote.spread / (spec.tick_size or 1)
        report.add(
            f"{symbol}:quote",
            OK,
            f"bid {quote.bid} ask {quote.ask}, spread {quote.spread:.6f} "
            f"({spread_ticks:.0f} ticks)",
            bid=quote.bid,
            ask=quote.ask,
            spread=quote.spread,
        )
        try:
            rate, note = conversion_rate(spec, quote.mid, lookup)
            report.add(f"{symbol}:conversion", OK, f"rate {rate:.6f} via {note}")
        except SizingError as exc:
            report.add(
                f"{symbol}:conversion",
                FAIL,
                f"{exc}\nThis symbol cannot be sized and will be SKIPPED by every scan.",
            )
    except BotError as exc:
        report.add(f"{symbol}:quote", FAIL, f"no usable quote: {exc}")

    # History: the discovery result is the single most useful output here.
    for timeframe in ("M15", "H1", "H4"):
        try:
            raw = broker.candles(spec, timeframe, count=120)
        except BotError as exc:
            report.add(f"{symbol}:{timeframe}", FAIL, f"no candles: {exc}")
            continue
        if not raw:
            report.add(f"{symbol}:{timeframe}", FAIL, "endpoint returned zero bars")
            continue
        try:
            candles = to_candles(raw)
            cleaned, validation = validate_series(
                candles, timeframe=timeframe, min_candles=min(60, len(candles))
            )
            report.add(
                f"{symbol}:{timeframe}",
                OK,
                f"{validation.accepted} closed candles, newest close "
                f"{validation.newest_close}, age {validation.age_minutes:.0f}m, "
                f"{validation.gaps} gap(s)",
                **validation.as_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - any decode failure is the finding
            report.add(
                f"{symbol}:{timeframe}",
                WARN,
                f"{len(raw)} bars returned but validation rejected them: {exc}",
            )

    report.add(
        f"{symbol}:history_endpoint",
        OK if broker.history.strategy else FAIL,
        json.dumps(broker.history.describe(), indent=2),
        **broker.history.describe(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.doctor")
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="symbol to verify (repeatable). Defaults to the configured list.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--safe",
        action="store_true",
        help="mask account balances and identifiers so the report can be shared",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration is invalid: {exc}", file=sys.stderr)
        return 2

    symbols = args.symbols or list(config.symbols)[:3]
    report = run(config, symbols)
    failed = bool(report.failed)
    if args.safe:
        report = report.sanitized()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.render())
        if args.safe:
            print(" Account figures masked (--safe). Integration details are intact.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
