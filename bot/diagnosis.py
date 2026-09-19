"""Why has it not traded?

A selective system standing aside is the expected result (project rule 8),
so silence is ambiguous by construction: it looks the same whether nothing
qualified or the pipeline is broken three stages upstream. That ambiguity
is what costs an operator days — the bot said NO TRADE 4,000 times and
every one of them was individually correct, while one gate nobody looked
at was refusing everything.

This module turns the journal's own record into one sentence and one
remedy. It invents nothing: every number comes from decisions already
written, and when there is not enough evidence it says so rather than
guessing (rule 6). It also never changes anything — it is a read.

The order below is deliberate. A configuration trap outranks a market
verdict, because "the calendar feed is down and the filter fails closed"
and "no setup qualified" produce the same silence, and only one of them
is the operator's to fix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from .clock import utc_now

#: Below this many evaluations the funnel shape is noise, not evidence.
MIN_EVALUATIONS_FOR_A_VERDICT = 40

#: Silence longer than this is worth a headline even when nothing is wrong.
QUIET_DAYS_BEFORE_CONCERN = 2.0

#: Stage -> what that stage means in words an operator can act on.
_STAGE_MEANING = {
    "CONFIG": "the symbol is not tradeable on this account",
    "NEWS": "the economic-calendar filter",
    "DATA": "market data (candles missing, stale, or failing validation)",
    "SMC": "the strategy found no setup",
    "SCORE": "setups formed but scored below the tradeable tier",
    "RISK": "the risk engine",
    "AI": "the AI veto",
    "ERROR": "errors during evaluation",
}


def _days_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def diagnose(
    *,
    funnel: Mapping[str, Any],
    feasibility: Mapping[str, Any] | None = None,
    last_trade_at: datetime | None = None,
    ai_required_but_unavailable: bool = False,
    news_failing_closed: bool = False,
    trading_paused: bool = False,
    kill_switch_active: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One headline, one remedy, and the evidence behind both.

    `now` is injected rather than read, like everything else in this
    codebase (project rule 10).
    """

    moment = now or utc_now()
    quiet_days = _days_since(last_trade_at, moment)
    evaluations = int(funnel.get("evaluations") or 0)
    candidates = int(funnel.get("candidates") or 0)
    stages: Sequence[Mapping[str, Any]] = funnel.get("stages") or ()
    reasons: Sequence[Mapping[str, Any]] = funnel.get("topReasons") or ()

    base: dict[str, Any] = {
        "quietDays": round(quiet_days, 2) if quiet_days is not None else None,
        "evaluations": evaluations,
        "candidates": candidates,
        "binding": None,
        "severity": "ok",
        "headline": "",
        "remedy": "",
        "evidence": [],
    }

    # -- 1. states that stop the pipeline before it can decide anything --

    if kill_switch_active:
        return {
            **base,
            "binding": "KILL_SWITCH",
            "severity": "warning",
            "headline": "The kill switch is active, so no new orders can be created.",
            "remedy": "Clear the kill switch from the dashboard once the cause is understood.",
        }
    if trading_paused:
        return {
            **base,
            "binding": "PAUSED",
            "severity": "warning",
            "headline": "Scanning is paused by the operator.",
            "remedy": "Resume scanning from the dashboard.",
        }

    # -- 2. configuration traps: silence that is not a market verdict ----

    if ai_required_but_unavailable:
        return {
            **base,
            "binding": "AI",
            "severity": "warning",
            "headline": (
                "AI validation is required but no provider is reachable, so every "
                "candidate that survives risk is refused at the last gate."
            ),
            "remedy": (
                "Set GEMINI_API_KEY or GROQ_API_KEY, or set AI_ENABLED=false to run the "
                "deterministic pipeline alone. AI_ALLOW_TRADE_WITHOUT_AI=true keeps the "
                "veto when a provider answers and skips it when none does."
            ),
        }

    if news_failing_closed:
        return {
            **base,
            "binding": "NEWS",
            "severity": "warning",
            "headline": (
                "The economic calendar is unreachable and the news filter fails closed, "
                "so every symbol with a mapped currency is being stood aside."
            ),
            "remedy": (
                "Allow outbound access to the calendar feed, or set "
                "NEWS_FAIL_CLOSED_WITHOUT_FEED=false to trade without it. "
                "NEWS_ENABLED=false turns the filter off entirely."
            ),
        }

    if feasibility and feasibility.get("feasible") is False:
        return {
            **base,
            "binding": "PROFIT_FLOOR",
            "severity": "warning",
            "headline": "The profit floor cannot be reached at this equity by any setup.",
            "remedy": str(feasibility.get("reason") or ""),
        }

    # -- 3. not enough evidence to name a constraint ---------------------

    if evaluations < MIN_EVALUATIONS_FOR_A_VERDICT:
        return {
            **base,
            "severity": "info",
            "headline": (
                f"Only {evaluations} evaluations recorded — too few to say what is "
                "binding. The journal needs a few scan cycles first."
            ),
            "remedy": "",
            "evidence": ["sample: insufficient"],
        }

    # -- 4. the funnel's own answer --------------------------------------

    worst = max(stages, key=lambda row: int(row.get("lost") or 0), default=None)
    binding = str(worst.get("stage")) if worst else None
    lost = int(worst.get("lost") or 0) if worst else 0
    share = float(worst.get("share") or 0.0) if worst else 0.0
    meaning = _STAGE_MEANING.get(binding or "", binding or "an unknown stage")

    evidence = [
        f"{row['stage']}: {row['reached']} reached, {row['lost']} stopped here"
        for row in stages
        if int(row.get("lost") or 0) or row.get("stage") == "CANDIDATE"
    ]
    top = reasons[0] if reasons else None
    if top:
        evidence.append(f"most common reason: {top.get('reason')} ({top.get('count')}x)")

    if feasibility and feasibility.get("demanding"):
        # Reachable but only by exceptional setups. This is the single
        # most common reason a correctly-working bot looks broken, and it
        # is arithmetic rather than opinion, so it is named even when the
        # funnel points elsewhere.
        return {
            **base,
            "binding": "PROFIT_FLOOR",
            "severity": "info",
            "headline": (
                "Nothing is broken: the profit floor is reachable only by exceptional "
                f"setups at this equity, and {meaning} is where most evaluations stop."
            ),
            "remedy": str(feasibility.get("reason") or ""),
            "evidence": evidence,
        }

    if candidates > 0:
        return {
            **base,
            "binding": binding,
            "severity": "ok",
            "headline": (
                f"{candidates} of {evaluations} evaluations produced a tradeable "
                f"candidate; the narrowest stage is {binding} ({meaning})."
            ),
            "remedy": "",
            "evidence": evidence,
        }

    severity = "warning" if (quiet_days or 0.0) >= QUIET_DAYS_BEFORE_CONCERN else "info"
    return {
        **base,
        "binding": binding,
        "severity": severity,
        "headline": (
            f"No candidate in {evaluations} evaluations. The narrowest stage is "
            f"{binding} — {meaning} — which stopped {lost} of them ({share:.0%})."
        ),
        "remedy": (
            "Read the reasons below before changing a limit: a stage that refuses "
            "everything for one repeated reason is a configuration problem, and a "
            "stage that refuses for many different reasons is the market."
        ),
        "evidence": evidence,
    }
