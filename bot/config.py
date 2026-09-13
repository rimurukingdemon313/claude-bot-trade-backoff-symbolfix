"""Typed, validated configuration.

Rules encoded here, not just documented:

* DEMO is the only supported environment. `require_demo` cannot be turned
  off by an environment variable, a request body, or an AI response — it
  is a module constant, and `TradingConfig.broker.is_demo_url` must also
  agree before any order is sent.
* Every tunable has a conservative default. A missing/garbage value falls
  back to that default and is logged; it never silently disables a guard.
* Risk limits are hard ceilings. Dynamic risk (see bot.risk.engine) may
  move within them but the clamp lives here so there is exactly one place
  that defines what "too much" means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from .errors import ConfigError

# --------------------------------------------------------------------------
# Non-negotiable constants. Deliberately NOT environment-driven.
# --------------------------------------------------------------------------

#: The system refuses to trade unless the account is positively verified
#: as DEMO. There is no env var for this by design (MASTER_MISSION §4).
REQUIRE_DEMO = True

class ExecutionMode(str, Enum):
    """How an approved trade is executed.

    PAPER simulates the fill against LIVE broker prices and never sends a
    write to the broker. Everything upstream of the fill — market data, the
    SMC engine, scoring, the risk engine, the execution intent, the
    idempotency guard, position management — runs identically, so paper mode
    exercises the real decision and execution path without touching the
    account.

    DEMO_LIVE places real orders on the TradeLocker DEMO account.

    There is deliberately no third value. LIVE is not a mode this build has.
    """

    PAPER = "paper"
    DEMO_LIVE = "demo_live"


#: Hostname fragments that positively identify a TradeLocker demo API.
DEMO_URL_MARKERS = ("demo.tradelocker.com", "demo-api", "/demo")

#: Hostname fragments that positively identify a LIVE endpoint. Any match
#: is fatal regardless of what else the configuration says.
LIVE_URL_MARKERS = ("live.tradelocker.com", "live-api", "prod.tradelocker.com")


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not (low <= value <= high):
        return default
    return value


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    if not (low <= value <= high):
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    items = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    return items or default


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    email: str | None
    password: str | None
    server: str | None
    account_id: str | None
    base_url: str
    request_timeout: float = 20.0
    max_attempts: int = 4
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 60.0

    @property
    def is_demo_url(self) -> bool:
        url = self.base_url.lower()
        if any(marker in url for marker in LIVE_URL_MARKERS):
            return False
        return any(marker in url for marker in DEMO_URL_MARKERS)

    @property
    def is_live_url(self) -> bool:
        return any(marker in self.base_url.lower() for marker in LIVE_URL_MARKERS)

    @property
    def configured(self) -> bool:
        return all([self.email, self.password, self.server, self.account_id])


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Hard ceilings. `base_risk_pct` is the normal-setup risk; dynamic
    risk scales between `min_risk_pct` and `max_risk_pct` and is then
    clamped to those bounds unconditionally."""

    base_risk_pct: float = 0.005
    min_risk_pct: float = 0.0025
    max_risk_pct: float = 0.01
    max_portfolio_risk_pct: float = 0.03
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    max_open_positions: int = 3
    max_open_per_symbol: int = 1
    max_correlated_risk_pct: float = 0.015
    #: Exposure overlap above which two positions count as one risk.
    #: 0.5 is what two pairs sharing a single currency leg in the same
    #: direction score (EURUSD long + GBPUSD long), which is exactly the
    #: stacking this limit exists to prevent.
    correlation_threshold: float = 0.45
    max_consecutive_losses: int = 4
    max_trades_per_day: int = 6
    max_trades_per_session: int = 3
    min_risk_reward: float = 2.0
    loss_cooldown_minutes: int = 45
    execution_failure_cooldown_minutes: int = 20
    drawdown_derisk_pct: float = 0.05
    min_stop_distance_atr: float = 0.35
    max_stop_distance_atr: float = 3.5


@dataclass(frozen=True, slots=True)
class OpportunityConfig:
    """The profit objective. A FILTER, never a mandate: if a setup cannot
    reach it inside the risk limits, the answer is NO TRADE — the system
    never inflates size, widens leverage, or shrinks the stop to get there.

    Two numbers, not one:

      * `target_profit` is what the system aims for;
      * `minimum_profit` is an ABSOLUTE FLOOR. No tier, no tolerance and no
        configuration can take a trade whose expected profit at its
        structural target is below it.

    Note the arithmetic this imposes. With a 1:2 minimum R:R, a $40 floor
    means at least $20 of risk per trade, which at the 0.5% base risk needs
    roughly $4,000 of equity. Below that the floor is unreachable and the
    system would simply never trade — so `profit_floor_feasibility()`
    computes that explicitly and the health endpoint reports it, rather than
    leaving a silent do-nothing bot.
    """

    target_profit: float = 50.0
    #: Hard floor on expected profit at the structural target.
    minimum_profit: float = 40.0
    enabled: bool = True
    #: A top-tier setup may clear the target at this fraction — but never
    #: below `minimum_profit`.
    tolerance_fraction: float = 0.8

    def required_profit(self, tier: str) -> float:
        """The bar this tier must clear. Never below the absolute floor."""

        required = self.target_profit
        if tier == "A+":
            required = self.target_profit * self.tolerance_fraction
        return max(required, self.minimum_profit)


@dataclass(frozen=True, slots=True)
class PaperConfig:
    """Fill modelling for paper mode.

    Defaults are pessimistic on purpose: a paper run that fills at the mid
    with no costs would report a performance the demo account could never
    reproduce, which defeats the point of running it.
    """

    #: Starting equity. None = adopt the real broker balance on first boot,
    #: so paper results are scaled to the account actually being validated.
    starting_balance: float | None = None
    #: Extra adverse slippage on entry, in instrument ticks, on top of
    #: crossing the spread.
    entry_slippage_ticks: float = 2.0
    #: Adverse slippage when a stop is hit. Stops slip more than entries.
    stop_slippage_ticks: float = 4.0
    commission_per_lot: float = 7.0
    #: Treat a stop and target both touched inside one candle as the STOP.
    pessimistic_intrabar: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_spread_atr_fraction: float = 0.12
    max_spread_tp_fraction: float = 0.05
    max_slippage_atr_fraction: float = 0.25
    order_verify_attempts: int = 5
    order_verify_delay_seconds: float = 1.5
    breakeven_at_r: float = 1.0
    partial_tp_at_r: float = 1.5
    partial_tp_fraction: float = 0.5
    trail_after_r: float = 2.0
    enable_breakeven: bool = True
    enable_partial_tp: bool = False
    enable_trailing: bool = False
    enable_structure_exit: bool = True
    max_position_hours: float = 48.0


@dataclass(frozen=True, slots=True)
class SmcConfig:
    swing_window: int = 2
    htf_swing_window: int = 2
    equal_level_atr_fraction: float = 0.12
    displacement_atr_multiple: float = 1.3
    displacement_body_ratio: float = 0.55
    bos_atr_buffer: float = 0.08
    sweep_max_age_candles: int = 12
    sweep_reaction_candles: int = 4
    fvg_min_atr_fraction: float = 0.12
    fvg_max_age_candles: int = 60
    ob_max_age_candles: int = 60
    atr_period: int = 14
    min_candles: int = 60


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """UTC session windows. Times are approximations of the liquidity
    profile, not exchange hours; they shift by an hour across DST, which
    is acceptable for a session-quality filter."""

    asian: tuple[int, int] = (0, 8)
    london: tuple[int, int] = (7, 16)
    new_york: tuple[int, int] = (12, 21)
    tradeable_sessions: tuple[str, ...] = ("LONDON", "NEW_YORK", "OVERLAP")
    block_low_liquidity: bool = True


@dataclass(frozen=True, slots=True)
class NewsConfig:
    enabled: bool = True
    blackout_before_minutes: int = 30
    blackout_after_minutes: int = 20
    impact_levels: tuple[str, ...] = ("High",)
    cache_ttl_seconds: int = 900
    #: If the feed is unreachable AND no cache exists, fail closed for
    #: symbols whose currencies are news-heavy rather than trading blind.
    fail_closed_without_feed: bool = True


@dataclass(frozen=True, slots=True)
class AIConfig:
    enabled: bool = True
    gemini_key: str | None = None
    groq_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    groq_primary_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str = "qwen/qwen3.8-27b"
    timeout_seconds: float = 25.0
    min_confidence: int = 70
    #: AI is only consulted for candidates at or above this tier. Weak
    #: setups never reach the model, which is both the cost control and
    #: the hallucination-surface control.
    min_tier_for_ai: str = "B"
    #: When AI is enabled but every provider fails, the deterministic
    #: pipeline result stands only if this is True; otherwise NO TRADE.
    allow_trade_without_ai: bool = False


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database_url: str | None = None
    sqlite_path: str = "data/bot.db"
    #: Safety-critical writes must succeed. If the database is down the
    #: system refuses to create orders (MASTER_MISSION §56).
    fail_closed_on_db_error: bool = True


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    scan_interval_minutes: int = 15
    scan_offset_seconds: int = 20
    position_poll_seconds: int = 30
    reconcile_interval_seconds: int = 300
    max_scan_duration_seconds: float = 240.0


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    tier_a_plus: float = 80.0
    tier_a: float = 68.0
    tier_b: float = 56.0
    min_tradeable_score: float = 56.0


@dataclass(frozen=True, slots=True)
class TradingConfig:
    symbols: tuple[str, ...]
    broker: BrokerConfig
    risk: RiskConfig = field(default_factory=RiskConfig)
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    smc: SmcConfig = field(default_factory=SmcConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    mode: ExecutionMode = ExecutionMode.PAPER
    require_demo: bool = REQUIRE_DEMO
    dashboard_token: str | None = None
    trading_enabled_default: bool = True

    @property
    def is_paper(self) -> bool:
        return self.mode is ExecutionMode.PAPER

    def validate(self) -> None:
        """Structural validation. Raises on anything that would make a
        guard meaningless; returns quietly on a merely-incomplete config
        (missing broker credentials is a "cannot trade yet" state that the
        health endpoint reports, not a crash)."""

        risk = self.risk
        if not (0 < risk.min_risk_pct <= risk.base_risk_pct <= risk.max_risk_pct):
            raise ConfigError(
                "risk percentages must satisfy 0 < min <= base <= max "
                f"(got min={risk.min_risk_pct}, base={risk.base_risk_pct}, max={risk.max_risk_pct})"
            )
        if risk.max_risk_pct > 0.02:
            raise ConfigError("max_risk_pct above 2% is refused by this build")
        if risk.max_portfolio_risk_pct > 0.06:
            raise ConfigError("max_portfolio_risk_pct above 6% is refused by this build")
        if risk.max_daily_loss_pct <= 0 or risk.max_daily_loss_pct > 0.06:
            raise ConfigError("max_daily_loss_pct must be in (0, 6%]")
        if risk.max_drawdown_pct <= 0 or risk.max_drawdown_pct > 0.25:
            raise ConfigError("max_drawdown_pct must be in (0, 25%]")
        if risk.min_risk_reward < 1.5:
            raise ConfigError("min_risk_reward below 1.5 is refused by this build")
        if risk.max_open_positions < 1:
            raise ConfigError("max_open_positions must be at least 1")
        if not self.symbols:
            raise ConfigError("at least one symbol must be configured")
        if self.require_demo is not True:
            raise ConfigError("require_demo cannot be disabled")
        if self.broker.is_live_url:
            raise ConfigError(
                f"TRADELOCKER_URL points at a LIVE endpoint ({self.broker.base_url}); "
                "this build only runs against TradeLocker DEMO"
            )
        scoring = self.scoring
        if not (scoring.tier_b <= scoring.tier_a <= scoring.tier_a_plus):
            raise ConfigError("score tiers must be ordered B <= A <= A+")
        if not isinstance(self.mode, ExecutionMode):
            raise ConfigError(f"unknown execution mode {self.mode!r}")
        if self.paper.starting_balance is not None and self.paper.starting_balance <= 0:
            raise ConfigError("paper starting balance must be positive when set")
        opportunity = self.opportunity
        if opportunity.minimum_profit < 0:
            raise ConfigError("minimum_profit cannot be negative")
        if opportunity.enabled and opportunity.minimum_profit > opportunity.target_profit:
            raise ConfigError(
                f"minimum_profit (${opportunity.minimum_profit:g}) cannot exceed "
                f"target_profit (${opportunity.target_profit:g}) — the floor would make the "
                "target unreachable by definition"
            )

    def with_overrides(self, **changes: Any) -> "TradingConfig":
        return replace(self, **changes)


def profit_floor_feasibility(config: "TradingConfig", equity: float) -> dict[str, Any]:
    """Can the profit floor be reached at all, at this equity?

    `min_risk_reward` is a floor, not a ceiling — targets are structural,
    so a setup's R:R is whatever the liquidity above it happens to be
    worth. An earlier version of this function multiplied the risk ceiling
    by the *minimum* R:R and called the shortfall impossible, which was
    simply wrong: it declared "no setup can pass" on an account where any
    setup reaching 1:4 would have passed comfortably.

    So the question is split in two, because they have different answers
    and different remedies:

    * **Infeasible** — not even an exceptional setup clears the floor.
      Nothing but more equity (or a lower floor) changes that.
    * **Demanding** — reachable, but only by setups whose R:R exceeds the
      configured minimum. The bot will trade; it will just say NO TRADE
      far more often, which is the profit objective working as a filter
      (project rule 8), not a fault.

    Risk is never raised to close the gap. That direction is forbidden.
    """

    opportunity = config.opportunity
    if not opportunity.enabled:
        return {"feasible": True, "reason": "profit objective disabled"}

    max_risk = max(0.0, equity) * config.risk.max_risk_pct
    floor = opportunity.minimum_profit
    min_rr = config.risk.min_risk_reward

    # The largest R:R this build will entertain as realistic. It is the
    # same ceiling `RISK_MIN_RR` is clamped to, and structural targets
    # beyond it are rare enough that promising them would be dishonest.
    ceiling_rr = ATTAINABLE_RISK_REWARD_CEILING

    comfortable_profit = max_risk * min_rr
    best_case_profit = max_risk * ceiling_rr
    feasible = best_case_profit >= floor
    demanding = feasible and comfortable_profit < floor

    required_rr = floor / max_risk if max_risk > 0 else None
    required_equity = (
        floor / (config.risk.max_risk_pct * ceiling_rr)
        if config.risk.max_risk_pct > 0
        else None
    )
    comfortable_equity = (
        floor / (config.risk.max_risk_pct * min_rr)
        if config.risk.max_risk_pct > 0 and min_rr > 0
        else None
    )

    if not feasible:
        reason = (
            f"at ${equity:,.2f} equity the maximum allowed risk is ${max_risk:,.2f}, which even at "
            f"an exceptional 1:{ceiling_rr:g} R:R yields at best ${best_case_profit:,.2f} — below "
            f"the ${floor:,.2f} profit floor. No setup can pass this filter until equity reaches "
            f"about ${required_equity:,.2f}, or the floor is lowered. Risk is never raised to "
            f"close this gap."
        )
    elif demanding:
        # Name the lever. An operator told only that setups are being
        # filtered cannot tell whether that is the strategy or the
        # configuration — and here it is the configuration, by arithmetic.
        affordable = max_risk * min_rr
        reason = (
            f"reachable but demanding: at ${equity:,.2f} equity the ${max_risk:,.2f} risk ceiling "
            f"needs a setup worth 1:{required_rr:.1f} R:R to clear the ${floor:,.2f} floor, above "
            f"the 1:{min_rr:g} minimum. Expect NO TRADE most days. Three honest ways out, and "
            f"raising risk is not one of them: grow equity to about ${comfortable_equity:,.2f}, "
            f"set OPPORTUNITY_MINIMUM_PROFIT to ${affordable:,.0f} or less to accept what this "
            f"account can actually produce, or accept the low frequency as the cost of the floor."
        )
    else:
        reason = (
            f"reachable: ${comfortable_profit:,.2f} at the ${max_risk:,.2f} risk ceiling and the "
            f"1:{min_rr:g} minimum R:R, versus a ${floor:,.2f} floor"
        )

    return {
        "feasible": feasible,
        "demanding": demanding,
        "equity": round(equity, 2),
        "maxRiskPerTrade": round(max_risk, 2),
        "comfortableProfit": round(comfortable_profit, 2),
        "bestCaseProfit": round(best_case_profit, 2),
        "minimumProfit": floor,
        "requiredRiskReward": round(required_rr, 2) if required_rr else None,
        "requiredEquity": round(required_equity, 2) if required_equity else None,
        "comfortableEquity": round(comfortable_equity, 2) if comfortable_equity else None,
        "reason": reason,
    }


#: The largest risk:reward this build treats as attainable when judging
#: whether the profit floor is reachable. Matches the upper clamp on
#: `RISK_MIN_RR`; structural targets beyond it exist but are too rare to
#: base a feasibility promise on.
ATTAINABLE_RISK_REWARD_CEILING = 10.0

#: The instruments scanned when TRADED_SYMBOLS is not set.
#:
#: Breadth is the one honest way to see more setups: each symbol is an
#: independent chance for structure to line up, and none of it touches
#: risk. Per-trade risk, the portfolio cap, the concurrent-position limit
#: and the correlation check are unchanged and still bound total exposure
#: — twenty symbols do not mean twenty positions.
#:
#: Everything here is liquid enough for the spread check to pass during
#: London and New York. Exotics are deliberately absent: a wide spread
#: eats a 1:2 setup before structure gets a say.
DEFAULT_SYMBOLS = (
    # Majors.
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD",
    # EUR crosses.
    "EURGBP", "EURJPY", "EURAUD", "EURCHF", "EURCAD",
    # GBP crosses.
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPCHF",
    # Commodity and JPY crosses.
    "AUDJPY", "AUDCAD", "AUDNZD", "NZDJPY", "CADJPY", "CHFJPY",
    # Metals.
    "XAUUSD",
)


def load_config(env: Mapping[str, str] | None = None) -> TradingConfig:
    """Build the config from the process environment.

    Note the deliberate asymmetry: numeric tunables silently fall back to
    their safe default when malformed, but anything that would weaken a
    safety guard (a live URL, a disabled demo requirement, a risk ceiling
    above the build limit) raises from `validate()`.
    """

    if env is not None:  # test injection
        previous = dict(os.environ)
        os.environ.update(env)
        try:
            return load_config()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    broker = BrokerConfig(
        email=_env_str("TRADELOCKER_EMAIL"),
        password=_env_str("TRADELOCKER_PASSWORD"),
        server=_env_str("TRADELOCKER_SERVER"),
        account_id=_env_str("TRADELOCKER_ACC_ID"),
        base_url=(_env_str("TRADELOCKER_URL") or "https://demo.tradelocker.com/backend-api").rstrip("/"),
        request_timeout=_env_float("TL_TIMEOUT_SECONDS", 20.0, low=5.0, high=60.0),
        max_attempts=_env_int("TL_MAX_ATTEMPTS", 4, low=1, high=6),
    )
    risk = RiskConfig(
        base_risk_pct=_env_float("RISK_BASE_PCT", 0.005, low=0.0005, high=0.02),
        min_risk_pct=_env_float("RISK_MIN_PCT", 0.0025, low=0.0005, high=0.02),
        max_risk_pct=_env_float("RISK_MAX_PCT", 0.01, low=0.0005, high=0.02),
        max_portfolio_risk_pct=_env_float("RISK_MAX_PORTFOLIO_PCT", 0.03, low=0.005, high=0.06),
        max_daily_loss_pct=_env_float("RISK_MAX_DAILY_LOSS_PCT", 0.03, low=0.005, high=0.06),
        max_drawdown_pct=_env_float("RISK_MAX_DRAWDOWN_PCT", 0.10, low=0.02, high=0.25),
        max_open_positions=_env_int("RISK_MAX_OPEN_POSITIONS", 3, low=1, high=10),
        max_trades_per_day=_env_int("RISK_MAX_TRADES_PER_DAY", 6, low=1, high=30),
        min_risk_reward=_env_float("RISK_MIN_RR", 2.0, low=1.5, high=10.0),
    )
    opportunity = OpportunityConfig(
        target_profit=_env_float("OPPORTUNITY_TARGET_PROFIT", 50.0, low=0.0, high=100000.0),
        minimum_profit=_env_float("OPPORTUNITY_MINIMUM_PROFIT", 40.0, low=0.0, high=100000.0),
        enabled=_env_bool("OPPORTUNITY_ENABLED", True),
    )
    ai = AIConfig(
        enabled=_env_bool("AI_ENABLED", True),
        gemini_key=_env_str("GEMINI_API_KEY"),
        groq_key=_env_str("GROQ_API_KEY"),
        timeout_seconds=_env_float("AI_TIMEOUT_SECONDS", 25.0, low=5.0, high=90.0),
        min_confidence=_env_int("AI_MIN_CONFIDENCE", 70, low=50, high=100),
        allow_trade_without_ai=_env_bool("AI_ALLOW_TRADE_WITHOUT_AI", False),
    )
    storage = StorageConfig(
        database_url=_env_str("DATABASE_URL"),
        sqlite_path=_env_str("SQLITE_PATH", "data/bot.db") or "data/bot.db",
    )
    scheduler = SchedulerConfig(
        scan_interval_minutes=_env_int("SCAN_INTERVAL_MINUTES", 15, low=1, high=240),
        position_poll_seconds=_env_int("POSITION_POLL_SECONDS", 30, low=5, high=600),
        reconcile_interval_seconds=_env_int("RECONCILE_INTERVAL_SECONDS", 300, low=30, high=3600),
    )
    raw_mode = (_env_str("TRADING_MODE", "paper") or "paper").lower()
    try:
        mode = ExecutionMode(raw_mode)
    except ValueError as exc:
        # An unrecognised mode must not fall through to placing real orders.
        raise ConfigError(
            f"TRADING_MODE={raw_mode!r} is not recognised. Use 'paper' or 'demo_live'."
        ) from exc

    paper_balance = os.environ.get("PAPER_STARTING_BALANCE")
    config = TradingConfig(
        symbols=_env_list("TRADED_SYMBOLS", DEFAULT_SYMBOLS),
        mode=mode,
        paper=PaperConfig(
            starting_balance=(
                _env_float("PAPER_STARTING_BALANCE", 10_000.0, low=1.0, high=10_000_000.0)
                if paper_balance
                else None
            ),
            entry_slippage_ticks=_env_float("PAPER_ENTRY_SLIPPAGE_TICKS", 2.0, low=0.0, high=100.0),
            stop_slippage_ticks=_env_float("PAPER_STOP_SLIPPAGE_TICKS", 4.0, low=0.0, high=200.0),
            commission_per_lot=_env_float("PAPER_COMMISSION_PER_LOT", 7.0, low=0.0, high=200.0),
        ),
        broker=broker,
        risk=risk,
        opportunity=opportunity,
        ai=ai,
        storage=storage,
        scheduler=scheduler,
        news=NewsConfig(enabled=_env_bool("NEWS_FILTER_ENABLED", True)),
        dashboard_token=_env_str("DASHBOARD_TOKEN"),
        trading_enabled_default=_env_bool("TRADING_ENABLED_DEFAULT", True),
    )
    config.validate()
    return config
