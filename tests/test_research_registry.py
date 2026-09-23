"""The experiment registry: counts, contamination, and the single-use holdout."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from bot.research.registry import (
    SEALED_SOURCE,
    SEALED_START,
    HoldoutSpent,
    Registry,
    RegistryError,
    Trial,
    Use,
    Verdict,
    bonferroni_t,
    holm,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def trial(tid, *, at=T0, uses=None, tests=1, configurations=1, status="PENDING",
          prereg="docs/X.md"):
    return Trial(
        id=tid, registered=at, title=tid, hypothesis="h",
        uses=uses if uses is not None else (Use("ds", date(2015, 1, 1), date(2016, 1, 1), "judge"),),
        tests=tests, configurations=configurations, status=status,
        preregistration=prereg,
    )


def sealed_use():
    return (Use(SEALED_SOURCE, SEALED_START, date(2026, 9, 1), "judge"),)


def test_bonferroni_reproduces_the_threshold_the_program_used():
    assert bonferroni_t(13) == pytest.approx(2.89, abs=0.005)
    assert bonferroni_t(1) == pytest.approx(1.96, abs=0.005)
    with pytest.raises(ValueError):
        bonferroni_t(0)


def test_holm_steps_down_and_stops_at_the_first_failure():
    # sorted: .005 <= .05/4, .01 <= .05/3, .03 > .05/2 -> stop; .04 not rejected
    assert holm([0.01, 0.04, 0.03, 0.005]) == [True, False, False, True]
    assert holm([]) == []


def test_round_trip_through_the_file(tmp_path):
    path = tmp_path / "reg.jsonl"
    reg = Registry(path)
    reg.register(trial("a", tests=2, configurations=5))
    reg.record_verdict(Verdict("a", T0.replace(hour=13), "FAILED", {"t": -1.2}))

    again = Registry.load(path)
    assert [t.id for t in again.trials] == ["a"]
    assert again.status_of("a") == "FAILED"
    assert again.tests_on("ds") == 2
    assert again.configurations_on("ds") == 5


def test_an_id_is_never_reused(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a"))
    with pytest.raises(RegistryError, match="already exists"):
        reg.register(trial("a", at=T0.replace(hour=13)))


def test_the_record_cannot_be_back_filled_out_of_order(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("late", at=T0.replace(hour=14)))
    with pytest.raises(RegistryError, match="before the last entry"):
        reg.register(trial("early"))


def test_times_must_carry_a_zone():
    with pytest.raises(RegistryError, match="timezone-aware"):
        trial("naive", at=datetime(2026, 9, 24, 12, 0))


def test_configurations_cannot_undercount_tests():
    with pytest.raises(RegistryError):
        trial("x", tests=3, configurations=2)


def test_a_refused_write_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "r.jsonl"
    reg = Registry(path)
    reg.register(trial("a"))
    before = path.read_text()
    with pytest.raises(RegistryError):
        reg.register(trial("a", at=T0.replace(hour=13)))
    assert path.read_text() == before


def test_a_hand_edited_file_that_breaks_a_rule_is_refused_on_load(tmp_path):
    path = tmp_path / "r.jsonl"
    reg = Registry(path)
    reg.register(trial("a"))
    line = path.read_text()
    path.write_text(line + line)  # the same trial twice
    with pytest.raises(RegistryError, match="already exists"):
        Registry.load(path)


def test_ranges_are_end_exclusive(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a", uses=(Use("ds", date(2015, 1, 1), date(2016, 1, 1), "judge"),)))
    assert not reg.is_unseen("ds", date(2015, 12, 31), date(2016, 6, 1))
    assert reg.is_unseen("ds", date(2016, 1, 1), date(2017, 1, 1))
    assert reg.is_unseen("other", date(2015, 1, 1), date(2016, 1, 1))


def test_every_role_contaminates_not_only_judging(tmp_path):
    # A design-period look chose something. Using that range as "unseen"
    # later would be the same mistake with a different label.
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("peek", tests=0, uses=(Use("ds", date(2018, 1, 1), date(2019, 1, 1), "select"),)))
    assert not reg.is_unseen("ds", date(2018, 6, 1), date(2018, 7, 1))
    assert reg.tests_on("ds") == 0


def test_exposure_by_year_does_not_leak_into_the_year_after_an_exclusive_end(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a", tests=3, configurations=3, uses=(
        Use("ds", date(2015, 1, 1), date(2017, 1, 1), "judge"),
        Use("ds", date(2016, 3, 1), date(2016, 4, 1), "fit"),
    )))
    # 2016 is touched by two uses of the same trial, and counted once.
    assert reg.exposure_by_year("ds") == {2015: 3, 2016: 3}


def test_threshold_for_next_counts_everything_before_it(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a", tests=12, configurations=12))
    assert reg.threshold_for_next("ds") == pytest.approx(bonferroni_t(13))


# ── verdicts ────────────────────────────────────────────────────────────


def test_a_verdict_is_recorded_once(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a"))
    reg.record_verdict(Verdict("a", T0.replace(hour=13), "FAILED"))
    with pytest.raises(RegistryError, match="final status"):
        reg.record_verdict(Verdict("a", T0.replace(hour=14), "PASSED"))


def test_a_verdict_needs_its_trial_and_must_be_final(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    with pytest.raises(KeyError):
        reg.record_verdict(Verdict("ghost", T0, "FAILED"))
    with pytest.raises(RegistryError, match="final"):
        Verdict("a", T0, "PENDING")


def test_a_backfilled_final_trial_takes_no_verdict(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("done", status="FAILED"))
    with pytest.raises(RegistryError, match="final status"):
        reg.record_verdict(Verdict("done", T0.replace(hour=13), "PASSED"))


def test_a_verdict_cannot_predate_its_trial(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("a"))
    with pytest.raises(RegistryError, match="predates"):
        reg.record_verdict(Verdict("a", T0.replace(hour=11), "FAILED"))


# ── the sealed holdout ──────────────────────────────────────────────────


def test_the_sealed_holdout_requires_a_preregistration(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    with pytest.raises(RegistryError, match="pre-registered"):
        reg.register(trial("casual", uses=sealed_use(), prereg=None))


def test_the_sealed_holdout_answers_one_question_once(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("final", uses=sealed_use()))
    with pytest.raises(HoldoutSpent):
        reg.register(trial("second-look", at=T0.replace(hour=13), uses=sealed_use()))


def test_any_role_spends_the_holdout_and_so_does_a_partial_overlap(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("peek", tests=0, uses=(
        Use(SEALED_SOURCE, date(2024, 1, 1), date(2024, 2, 1), "select"),)))
    with pytest.raises(HoldoutSpent):
        reg.register(trial("final", at=T0.replace(hour=13), uses=sealed_use()))


def test_the_holdout_stays_spent_across_a_reload(tmp_path):
    path = tmp_path / "r.jsonl"
    Registry(path).register(trial("final", uses=sealed_use()))
    with pytest.raises(HoldoutSpent):
        Registry.load(path).register(trial("again", at=T0.replace(hour=13), uses=sealed_use()))


def test_broker_data_before_the_seal_is_not_the_holdout(tmp_path):
    reg = Registry(tmp_path / "r.jsonl")
    reg.register(trial("recent", prereg=None, uses=(
        Use(SEALED_SOURCE, date(2021, 1, 1), SEALED_START, "fit"),)))
    assert reg.is_unseen(SEALED_SOURCE, SEALED_START, date(2026, 9, 1))


# ── the committed record ────────────────────────────────────────────────


def test_the_committed_registry_loads_and_says_what_we_know():
    reg = Registry.load(ROOT / "research" / "registry.jsonl")
    # Sixteen verdicts have been read from the public dataset: SMC H1, H2a,
    # H2b, reversion x2, three trend rules, eight families. Fewer would be
    # an undercount that lowers every future threshold.
    assert reg.tests_on("ejtraderlabs") >= 16
    assert reg.threshold_for_next("ejtraderlabs") > 2.89
    # Every year of it has been judged on.
    exposure = reg.exposure_by_year("ejtraderlabs")
    assert set(range(2012, 2023)) <= set(exposure)
    assert all(v > 0 for v in exposure.values())
    # And the sealed holdout is still sealed.
    assert reg.is_unseen(SEALED_SOURCE, SEALED_START, date.max)
