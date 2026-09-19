"""Why has it not traded? — the funnel, and the sentence built from it.

A selective system standing aside is the expected result (project rule 8),
so silence is ambiguous by construction. These tests pin the two halves
that remove the ambiguity: the journal's own survivor counts, and the
judgement made from them.

Every scenario is constructed, so the correct answer is known before the
code runs (rule 10), and the clock is injected everywhere (rule 10 again).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.diagnosis import MIN_EVALUATIONS_FOR_A_VERDICT, diagnose
from bot.storage.db import in_memory_database
from bot.storage.repositories import Repositories

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repos() -> Repositories:
    return Repositories(in_memory_database())


def journal(repos: Repositories, stage: str, outcome: str, times: int, reason: str | None = None):
    for index in range(times):
        repos.journal.record(
            scan_id=f"scan{index}",
            symbol="EURUSD",
            stage=stage,
            outcome=outcome,
            reason=reason,
        )


# -- the funnel -----------------------------------------------------------


def test_the_funnel_reports_survivors_not_just_deaths(repos):
    """The shape is the point: 'reached' is what makes a cliff visible."""

    journal(repos, "NEWS", "REJECTED", 10, "news blackout")
    journal(repos, "SMC", "NO_SETUP", 60, "no live fair value gap")
    journal(repos, "SCORE", "BELOW_TIER", 5, "score 41.0 graded NO_TRADE")
    journal(repos, "CANDIDATE", "CANDIDATE", 2)

    funnel = repos.journal.funnel(days=7)
    assert funnel["evaluations"] == 77
    assert funnel["candidates"] == 2

    stages = {row["stage"]: row for row in funnel["stages"]}
    assert stages["NEWS"]["reached"] == 77
    assert stages["NEWS"]["lost"] == 10
    # Everything that did not die at or before NEWS reached DATA.
    assert stages["DATA"]["reached"] == 67
    assert stages["SMC"]["reached"] == 67
    assert stages["SMC"]["lost"] == 60
    assert stages["SCORE"]["reached"] == 7
    assert stages["CANDIDATE"]["reached"] == 2
    assert funnel["narrowest"] == "SMC"


def test_an_empty_journal_produces_no_invented_shape(repos):
    funnel = repos.journal.funnel(days=7)
    assert funnel["evaluations"] == 0
    assert funnel["candidates"] == 0
    assert funnel["narrowest"] is None
    assert all(row["reached"] == 0 for row in funnel["stages"])


def test_an_unrecognised_stage_is_reported_rather_than_dropped(repos):
    """Project rule 6: a gap is better than a number that quietly lies.

    A stage this build does not know about would otherwise vanish from
    the arithmetic and every survivor count downstream would be wrong.
    """

    journal(repos, "SMC", "NO_SETUP", 5)
    journal(repos, "SOMETHING_NEW", "REJECTED", 3)
    funnel = repos.journal.funnel(days=7)
    assert funnel["evaluations"] == 8
    assert funnel["unknownStages"] == {"SOMETHING_NEW": 3}


def test_the_scan_wide_journal_rows_are_not_counted_as_evaluations(repos):
    """A kill-switch row is written against '*', not against a symbol."""

    journal(repos, "SMC", "NO_SETUP", 4)
    repos.journal.record(
        scan_id="s", symbol="*", stage="RISK", outcome="KILL_SWITCH", reason="manual"
    )
    assert repos.journal.funnel(days=7)["evaluations"] == 4


# -- the judgement --------------------------------------------------------


def _funnel(evaluations: int, *, binding: str, lost: int, candidates: int = 0):
    stages = []
    remaining = evaluations
    for stage in ("CONFIG", "NEWS", "DATA", "SMC", "SCORE", "RISK", "AI"):
        died = lost if stage == binding else 0
        stages.append(
            {
                "stage": stage,
                "reached": remaining,
                "lost": died,
                "share": died / evaluations if evaluations else 0.0,
            }
        )
        remaining -= died
    stages.append({"stage": "CANDIDATE", "reached": remaining, "lost": 0, "share": 0.0})
    return {
        "days": 7,
        "evaluations": evaluations,
        "candidates": candidates,
        "stages": stages,
        "narrowest": binding,
        "topReasons": [{"stage": binding, "reason": "a repeated cause", "count": lost}],
    }


def test_a_configuration_trap_outranks_a_market_verdict():
    """Both produce silence; only one is the operator's to fix."""

    verdict = diagnose(
        funnel=_funnel(900, binding="SMC", lost=880),
        ai_required_but_unavailable=True,
        now=NOW,
    )
    assert verdict["binding"] == "AI"
    assert verdict["severity"] == "warning"
    assert "AI_ENABLED" in verdict["remedy"]


def test_a_news_feed_failing_closed_is_named_with_its_variable():
    verdict = diagnose(
        funnel=_funnel(900, binding="NEWS", lost=900), news_failing_closed=True, now=NOW
    )
    assert verdict["binding"] == "NEWS"
    assert "NEWS_FAIL_CLOSED_WITHOUT_FEED" in verdict["remedy"]


def test_a_paused_bot_says_so_rather_than_blaming_the_market():
    verdict = diagnose(funnel=_funnel(900, binding="SMC", lost=900), trading_paused=True, now=NOW)
    assert verdict["binding"] == "PAUSED"


def test_the_kill_switch_outranks_everything_including_a_paused_scanner():
    verdict = diagnose(
        funnel=_funnel(900, binding="SMC", lost=900),
        trading_paused=True,
        kill_switch_active=True,
        now=NOW,
    )
    assert verdict["binding"] == "KILL_SWITCH"


def test_too_little_evidence_says_so_instead_of_naming_a_constraint():
    """Rule 6 again: an insufficient sample is labelled, never extrapolated."""

    verdict = diagnose(funnel=_funnel(MIN_EVALUATIONS_FOR_A_VERDICT - 1, binding="SMC", lost=10), now=NOW)
    assert verdict["binding"] is None
    assert verdict["severity"] == "info"
    assert "too few" in verdict["headline"]
    assert verdict["evidence"] == ["sample: insufficient"]


def test_no_verdict_still_talks_about_a_dollar_profit_floor():
    """The floor is gone, so the verdict that named it must be gone too.

    It was the most useful verdict this module had - a fixed dollar floor
    makes a small account silently untradeable - but the objective is
    measured in R now, so nothing can reach that branch. A verdict that
    can never fire is worse than no verdict.
    """

    import inspect

    import bot.diagnosis as module

    source = inspect.getsource(module)
    assert "PROFIT_FLOOR" not in source.split('"""', 2)[2], "a dead verdict is still reachable"
    assert "feasibility" not in inspect.signature(diagnose).parameters


def test_the_narrowest_stage_is_named_with_what_it_means():
    verdict = diagnose(funnel=_funnel(900, binding="SMC", lost=880), now=NOW)
    assert verdict["binding"] == "SMC"
    assert "the strategy found no setup" in verdict["headline"]
    assert any("most common reason" in line for line in verdict["evidence"])


def test_a_long_silence_is_a_warning_and_a_short_one_is_not():
    quiet = diagnose(
        funnel=_funnel(900, binding="SMC", lost=900),
        last_trade_at=NOW - timedelta(days=5),
        now=NOW,
    )
    assert quiet["severity"] == "warning"
    assert quiet["quietDays"] == pytest.approx(5.0)

    fresh = diagnose(
        funnel=_funnel(900, binding="SMC", lost=900),
        last_trade_at=NOW - timedelta(hours=6),
        now=NOW,
    )
    assert fresh["severity"] == "info"


def test_trades_happening_is_not_reported_as_a_problem():
    verdict = diagnose(funnel=_funnel(900, binding="SMC", lost=800, candidates=12), now=NOW)
    assert verdict["severity"] == "ok"
    assert verdict["candidates"] == 12
    assert verdict["remedy"] == ""


def test_the_diagnosis_never_proposes_raising_risk():
    """Rule 2 and rule 8: frequency is never bought with exposure.

    Every remedy this module can emit is checked, because a suggestion to
    'increase risk' would be the one piece of advice the whole design
    exists to refuse - and it would arrive on the dashboard looking
    official.
    """

    verdicts = [
        diagnose(funnel=_funnel(900, binding="SMC", lost=900), ai_required_but_unavailable=True, now=NOW),
        diagnose(funnel=_funnel(900, binding="NEWS", lost=900), news_failing_closed=True, now=NOW),
        diagnose(funnel=_funnel(900, binding="SMC", lost=900), trading_paused=True, now=NOW),
        diagnose(funnel=_funnel(900, binding="SMC", lost=900), kill_switch_active=True, now=NOW),
        diagnose(funnel=_funnel(900, binding="SMC", lost=900), now=NOW),
        diagnose(funnel=_funnel(20, binding="SMC", lost=20), now=NOW),
        diagnose(funnel=_funnel(900, binding="RISK", lost=900, candidates=3), now=NOW),
    ]
    for verdict in verdicts:
        text = f"{verdict['headline']} {verdict['remedy']}".lower()
        for forbidden in ("raise the risk", "increase risk", "more leverage", "risk_max_pct"):
            assert forbidden not in text, verdict


def test_the_diagnosis_reads_the_clock_it_is_given():
    """Rule 10: nothing here calls datetime.now()."""

    early = diagnose(
        funnel=_funnel(900, binding="SMC", lost=900),
        last_trade_at=NOW - timedelta(days=1),
        now=NOW,
    )
    later = diagnose(
        funnel=_funnel(900, binding="SMC", lost=900),
        last_trade_at=NOW - timedelta(days=1),
        now=NOW + timedelta(days=4),
    )
    assert early["quietDays"] == pytest.approx(1.0)
    assert later["quietDays"] == pytest.approx(5.0)
