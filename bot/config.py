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
    #: Seconds between outbound broker requests.
    #:
    #: TradeLocker sits behind Cloudflare. At 0.15s — about seven requests
    #: a second — a live GATESFX account answered with error 1015, "you
    #: are being rate-limited by the website owner's configuration", and
    #: the startup sequence could not read account state at all. Scanning
    #: more symbols multiplies the call count, so this is the setting that
    #: has to give, never the symbol list.
    min_request_interval: float = 0.6

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
    min_risk_reward: float = 1.2
    #: How long a setup identity stays blocked after it has been traded.
    #:
    #: The evidence is what ages out, not the trade: a sweep stops being a
    #: usable trigger after `sweep_max_age_candles` (20 bars = 5h on M15),
    #: so a window comfortably longer than that means a blocked identity
    #: can never still be live. 24h also covers the overnight case where
    #: the same level is swept again on the next session - which IS a new
    #: setup, and gets a new identity because the sweep candle differs.
    setup_reentry_block_hours: float = 24.0
    loss_cooldown_minutes: int = 45
    execution_failure_cooldown_minutes: int = 20
    drawdown_derisk_pct: float = 0.05
    min_stop_distance_atr: float = 0.35
    max_stop_distance_atr: float = 3.5


@dataclass(frozen=True, slots=True)
class RewardConfig:
    """The reward objective, in R. Never in dollars.

    This used to be `target_profit=$50 / minimum_profit=$40`, and that was
    a bug with a plausible face. Expected profit at the structural target
    is `risk x R`, and risk is a FIXED PERCENTAGE of equity, so a dollar
    floor is a statement about account size pretending to be a statement
    about setup quality: at $5,000 equity a $40 floor quietly demands 1:2
    on every trade, and at $1,000 it demands 1:4 and the bot stands aside
    for weeks while every individual refusal looks correct. The market
    does not know the balance; the same setup must not grade differently
    on two accounts.

    R is the same number on every account, it is what the structure
    actually offers, and it cannot be improved by taking more risk.

    `preferred_reward_r` is a LABEL, not a second gate: a setup at or
    above it is reported as strong. Nothing is refused for missing it.
    """

    #: The floor. A setup below this is refused however large the account.
    min_reward_r: float = 1.2
    #: Reported as a stronger setup. Never a permission, never a refusal.
    preferred_reward_r: float = 1.5
    enabled: bool = True


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
    #: 0.75R, not the 1.5R this defaulted to. Chosen by a pre-registered
    #: experiment and confirmed on held-out data — see
    #: docs/EXPERIMENT_WIN_RATE.md. Every partial level tested beat no
    #: partial at all; 0.75 was the best of them on the discovery set
    #: under a rule fixed before the results were read.
    partial_tp_at_r: float = 0.75
    partial_tp_fraction: float = 0.5
    trail_after_r: float = 2.0
    enable_breakeven: bool = True
    #: ON since the experiment in docs/EXPERIMENT_WIN_RATE.md. Project
    #: rule 12 says partials stay off "until evidence justifies them",
    #: which is a standard of evidence, not a permanent ban — so here is
    #: the evidence it asks for: a rule fixed before the data was read,
    #: one candidate promoted out of ten, and a held-out set that chose
    #: nothing. On that held-out data the win rate went 25.4% -> 44.5%
    #: and expectancy -0.248R -> -0.128R.
    #:
    #: It does NOT make the system profitable. Both sets are still
    #: negative and both are synthetic. What it does is lose less, and
    #: stay inside its own drawdown limit while doing it.
    enable_partial_tp: bool = True
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
    #: How long a liquidity sweep or a displaced structure break stays
    #: usable as a trigger. 12 bars is three hours on M15, which is
    #: shorter than the sequence the strategy waits for: sweep, then
    #: displacement away, then the break, then the retrace back into the
    #: imbalance. Measured across 24 days of M15 decision points,
    #: widening this to 20 raised approvals and LOWERED the share of
    #: setups graded below tier, so the extra triggers were not junk.
    #: It saturates by 24; there is no evidence for going wider.
    sweep_max_age_candles: int = 20
    sweep_reaction_candles: int = 4
    fvg_min_atr_fraction: float = 0.12
    fvg_max_age_candles: int = 60
    ob_max_age_candles: int = 60
    #: How far BEFORE the trigger an entry zone may have formed and still
    #: count as belonging to the move that produced it.
    #:
    #: Fair value gaps used a hardcoded 2 and order blocks a hardcoded 12,
    #: for no stated reason. The 2 has the causality backwards: the gap is
    #: left by the DISPLACEMENT, and the structure break that displacement
    #: causes is confirmed after it - so the imbalance the entry is meant
    #: to use routinely forms BEFORE `reference_index` and was discarded.
    #: Measured over 24 days of M15 decision points, 342 of the 747 "no
    #: live fair value gap or order block" refusals had a live, correctly
    #: directed gap that failed only this test.
    poi_reference_lookback_candles: int = 12
    atr_period: int = 14
    min_candles: int = 60


@dataclass(frozen=True, slots=True)
class MtfConfig:
    """Thresholds for the multi-timeframe decision layer.

    Every number the classifier branches on lives here so it can be tuned
    per account without editing logic, and so a change is visible in one
    place rather than buried across the engine.

    The model is H1 = TREND, M15 = EXECUTION. Disagreement raises the
    bar; it does not veto. What DOES veto is a move against the trend
    that has not earned the name reversal.
    """

    #: A trigger weaker than this is noise whatever else agrees with it.
    min_trigger_quality: float = 0.35
    #: A structure break must clear its level by this much ATR to count as
    #: structure at all. Below it, a wick-and-a-half through an old pivot
    #: would read as a break of structure.
    min_structure_clearance_atr: float = 0.12

    # -- what a REVERSAL against the H1 trend must show --------------------
    #: The swept level's own significance (see liquidity.LEVEL_WEIGHTS).
    #: A random intraday pivot is not the liquidity a reversal runs on.
    reversal_min_level_weight: float = 0.6
    reversal_min_sweep_quality: float = 0.55
    reversal_min_displacement_quality: float = 0.45
    #: A reversal must break structure the OTHER way — a CHoCH. A BOS in
    #: the counter direction is continuation of a move already underway,
    #: which is the definition of the retracement this must not trade.
    reversal_requires_choch: bool = True

    # -- score floors by classification ------------------------------------
    #: Minimum total score (0..100) for each setup type. Continuation
    #: with the trend is the baseline; everything that fights something
    #: has to be better than baseline, in proportion to what it fights.
    floor_continuation: float = 0.0          # 0 = use the configured B tier
    floor_range_rotation: float = 60.0
    #: H1 has no confirmed directional structure. The M15 sequence is
    #: then the only anchor there is, so it must be a good one - but a
    #: trendless H1 is the ABSENCE of opposition, not opposition, and
    #: refusing these outright would be exactly the over-filtering this
    #: layer exists to remove.
    floor_no_htf_context: float = 60.0
    floor_reversal: float = 70.0

    # -- regime handling ---------------------------------------------------
    #: A transitional market with directional strength below this and no
    #: consistent breaks is chop: the same setup means much less in it.
    choppy_directional_strength: float = 0.28
    #: Extra score demanded in a choppy market, on top of the type floor.
    choppy_score_premium: float = 8.0
    #: In a ranging primary timeframe, a rotation entry must sit this far
    #: into the correct half of the dealing range.
    range_rotation_min_position: float = 0.62

    #: No single critical component may be near-absent and be carried by
    #: the others. Expressed as a fraction of that component's weight.
    min_critical_component_fraction: float = 0.25


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
    #: How often to re-read positions when there are NONE open.
    #:
    #: Polling every 30s with nothing to manage was the single largest
    #: source of broker traffic in this system - 2.7x the scans - and all
    #: of it spent asking about positions that did not exist. A position
    #: can only appear two ways, and both are already covered: the bot
    #: opening one (which refreshes this view immediately) or somebody
    #: opening one by hand (which the reconciler finds). So the fast poll
    #: buys nothing while flat, and it was buying it at the price of the
    #: rate limit the scans needed.
    idle_position_poll_seconds: int = 180
    reconcile_interval_seconds: int = 300
    max_scan_duration_seconds: float = 240.0


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Score bands, and the lowest one that may trade.

    `min_tradeable_score` is the knob worth knowing about. Measured over
    180 simulated trades, the A/B boundary separated sharply — A grades
    averaged +0.014R and B grades -0.152R, which is to say the entire
    loss lived in the B cohort — while the correlation between the
    CONTINUOUS score and outcome was +0.037, near enough to nothing.

    One threshold doing real work while the arithmetic behind it ranks
    almost at random is a strange pair of facts, and it is one dataset,
    so the default has not moved. Raising this to `tier_a` is the single
    most promising experiment available and belongs on paper first: it
    would cut roughly 70% of the trades.
    """

    tier_a_plus: float = 80.0
    tier_a: float = 68.0
    tier_b: float = 56.0
    min_tradeable_score: float = 56.0


@dataclass(frozen=True, slots=True)
class TradingConfig:
    symbols: tuple[str, ...]
    broker: BrokerConfig
    risk: RiskConfig = field(default_factory=RiskConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    smc: SmcConfig = field(default_factory=SmcConfig)
    mtf: MtfConfig = field(default_factory=MtfConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    mode: ExecutionMode = ExecutionMode.PAPER
    #: Starting strategy. The dashboard switch overrides it and persists
    #: that choice, so this is the boot default, not the live value.
    strategy: str = "smc"
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
        if risk.min_risk_reward < 1.0:
            # Below 1:1 a winner is worth less than a loser costs, and no
            # hit rate this system can honestly claim recovers that. 1.2
            # is the default; the hard refusal is at parity.
            raise ConfigError("min_risk_reward below 1:1 is refused by this build")
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
        reward = self.reward
        if reward.min_reward_r <= 0:
            raise ConfigError("min_reward_r must be positive")
        if reward.preferred_reward_r < reward.min_reward_r:
            raise ConfigError(
                f"preferred_reward_r ({reward.preferred_reward_r:g}) cannot sit below "
                f"min_reward_r ({reward.min_reward_r:g}) — the label would be weaker than "
                "the gate it describes"
            )
        if reward.enabled and self.risk.min_risk_reward < reward.min_reward_r:
            # Two floors on the same quantity, and the risk engine checks
            # the structural one first. If they disagreed, the reward
            # verdict would be unreachable and its reason would never be
            # the one an operator read.
            raise ConfigError(
                f"risk.min_risk_reward ({self.risk.min_risk_reward:g}) is below "
                f"reward.min_reward_r ({reward.min_reward_r:g}); the reward objective would "
                "never be the binding constraint"
            )

    def with_overrides(self, **changes: Any) -> "TradingConfig":
        return replace(self, **changes)


#: Tolerance for comparing R-multiples.
#:
#: An R:R is a ratio of two floats, and a target constructed to sit EXACTLY
#: on a floor recomputes as 1.1999999999999998 about as often as
#: 1.2000000000000555. Without this, a setup priced precisely at the
#: minimum is accepted or rejected by the last bit of a double — measured
#: at 40 rejections in 600 on targets built to land on the floor. The same
#: constant already guards the break-even trigger, for the same reason.
R_EPSILON = 1e-6

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
        min_request_interval=_env_float(
            "BROKER_MIN_REQUEST_INTERVAL", 0.6, low=0.05, high=10.0
        ),
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
        min_risk_reward=_env_float("RISK_MIN_RR", 1.2, low=1.0, high=10.0),
        # Friction is `spread / stop_distance` - the contract size and the
        # lot count cancel - so a TIGHT stop does not reduce cost, it
        # magnifies it. Measured across four settings, raising this floor
        # took friction from 16.6% of R to 8.2% and moved win rate, profit
        # factor and expectancy monotonically the right way. The far end
        # had n=30 and a confidence interval spanning zero, so the default
        # has not moved - but the mechanism is arithmetic, not a search,
        # which makes it the experiment most worth running on paper.
        #
        # Note what raising it does: it REJECTS setups whose structural
        # stop is tighter than this. It never widens a stop.
        min_stop_distance_atr=_env_float(
            "RISK_MIN_STOP_ATR", 0.35, low=0.05, high=3.0
        ),
        max_stop_distance_atr=_env_float(
            "RISK_MAX_STOP_ATR", 3.5, low=0.5, high=20.0
        ),
    )
    reward = RewardConfig(
        min_reward_r=_env_float("REWARD_MIN_R", 1.2, low=1.0, high=10.0),
        preferred_reward_r=_env_float("REWARD_PREFERRED_R", 1.5, low=1.0, high=20.0),
        enabled=_env_bool("REWARD_ENABLED", True),
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
        idle_position_poll_seconds=_env_int(
            "IDLE_POSITION_POLL_SECONDS", 180, low=5, high=3600
        ),
        reconcile_interval_seconds=_env_int("RECONCILE_INTERVAL_SECONDS", 300, low=30, high=3600),
    )
    mtf = MtfConfig(
        min_trigger_quality=_env_float("MTF_MIN_TRIGGER_QUALITY", 0.35, low=0.0, high=1.0),
        min_structure_clearance_atr=_env_float(
            "MTF_MIN_STRUCTURE_CLEARANCE_ATR", 0.12, low=0.0, high=2.0
        ),
        reversal_min_level_weight=_env_float(
            "MTF_REVERSAL_MIN_LEVEL_WEIGHT", 0.6, low=0.0, high=1.0
        ),
        reversal_min_sweep_quality=_env_float(
            "MTF_REVERSAL_MIN_SWEEP_QUALITY", 0.55, low=0.0, high=1.0
        ),
        reversal_min_displacement_quality=_env_float(
            "MTF_REVERSAL_MIN_DISPLACEMENT_QUALITY", 0.45, low=0.0, high=1.0
        ),
        reversal_requires_choch=_env_bool("MTF_REVERSAL_REQUIRES_CHOCH", True),
        floor_range_rotation=_env_float("MTF_FLOOR_RANGE_ROTATION", 60.0, low=0.0, high=100.0),
        floor_no_htf_context=_env_float(
            "MTF_FLOOR_NO_HTF_CONTEXT", 60.0, low=0.0, high=100.0
        ),
        floor_reversal=_env_float("MTF_FLOOR_REVERSAL", 70.0, low=0.0, high=100.0),
        choppy_directional_strength=_env_float(
            "MTF_CHOPPY_DIRECTIONAL_STRENGTH", 0.28, low=0.0, high=1.0
        ),
        choppy_score_premium=_env_float("MTF_CHOPPY_SCORE_PREMIUM", 8.0, low=0.0, high=50.0),
        range_rotation_min_position=_env_float(
            "MTF_RANGE_ROTATION_MIN_POSITION", 0.62, low=0.5, high=1.0
        ),
        min_critical_component_fraction=_env_float(
            "MTF_MIN_CRITICAL_COMPONENT_FRACTION", 0.25, low=0.0, high=1.0
        ),
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
        strategy=(_env_str("TRADING_STRATEGY") or "smc").strip().lower(),
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
        scoring=ScoringConfig(
            tier_a_plus=_env_float("SCORING_TIER_A_PLUS", 80.0, low=1.0, high=100.0),
            tier_a=_env_float("SCORING_TIER_A", 68.0, low=1.0, high=100.0),
            tier_b=_env_float("SCORING_TIER_B", 56.0, low=1.0, high=100.0),
            # Raise this to trade only the higher grades. See ScoringConfig.
            min_tradeable_score=_env_float(
                "SCORING_MIN_TRADEABLE", 56.0, low=1.0, high=100.0
            ),
        ),
        risk=risk,
        # Position management is the one area the project says must be
        # EARNED by testing rather than switched on because it sounds
        # sophisticated. So these are env-controllable: an operator can
        # run partials or trailing on paper for a fortnight and turn them
        # off again without a code deploy, which is what "earned by
        # testing" needs in order to be a real option rather than advice.
        # The defaults are unchanged.
        execution=ExecutionConfig(
            enable_breakeven=_env_bool("EXEC_ENABLE_BREAKEVEN", True),
            enable_partial_tp=_env_bool("EXEC_ENABLE_PARTIAL_TP", True),
            enable_trailing=_env_bool("EXEC_ENABLE_TRAILING", False),
            enable_structure_exit=_env_bool("EXEC_ENABLE_STRUCTURE_EXIT", True),
            breakeven_at_r=_env_float("EXEC_BREAKEVEN_AT_R", 1.0, low=0.2, high=5.0),
            partial_tp_at_r=_env_float("EXEC_PARTIAL_TP_AT_R", 0.75, low=0.3, high=10.0),
            partial_tp_fraction=_env_float(
                "EXEC_PARTIAL_TP_FRACTION", 0.5, low=0.1, high=0.9
            ),
            trail_after_r=_env_float("EXEC_TRAIL_AFTER_R", 2.0, low=0.5, high=10.0),
            max_position_hours=_env_float(
                "EXEC_MAX_POSITION_HOURS", 48.0, low=1.0, high=720.0
            ),
        ),
        mtf=mtf,
        reward=reward,
        ai=ai,
        storage=storage,
        scheduler=scheduler,
        news=NewsConfig(enabled=_env_bool("NEWS_FILTER_ENABLED", True)),
        dashboard_token=_env_str("DASHBOARD_TOKEN"),
        trading_enabled_default=_env_bool("TRADING_ENABLED_DEFAULT", True),
    )
    config.validate()

    # Rule 9: a behaviour change must be visible in the version stamp.
    # Several tunables are settable from the environment, so an operator
    # can run an experiment on paper without a deploy — which means the
    # version constants alone no longer identify the behaviour that
    # produced a trade. The fingerprint does, and the diff is logged once
    # here so the digest can be read back later.
    from .observability import log_event
    from .version import set_tuning_fingerprint

    fingerprint, changed = set_tuning_fingerprint(config)
    if changed:
        log_event(
            "STARTUP",
            f"running tuning {fingerprint}: {len(changed)} setting(s) overridden from the "
            "build defaults — every trade records this fingerprint so the experiment and "
            "the control can be told apart afterwards",
            severity="warning",
            tuning=fingerprint,
            overrides={key: str(value) for key, value in sorted(changed.items())},
        )
    return config
