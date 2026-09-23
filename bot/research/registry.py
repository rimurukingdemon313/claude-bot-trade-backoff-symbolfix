"""The experiment registry: every hypothesis ever judged on a dataset.

A significance claim is a claim about a denominator. "t = 3.1" means
nothing until you know how many things were tried before this one looked
good, and that number lives nowhere unless it is written down. This module
is where it is written down.

Each trial records which data it LOOKED AT and in which role:

- ``fit``    parameters were estimated from it;
- ``select`` it influenced a choice (which variant, which rule, whether to
             continue) — a design-period look is a selection;
- ``judge``  the pass/fail verdict was read from it.

Any of the three contaminates the range for a later trial that wants to
use it as an unseen test. The registry cannot stop a person from looking
at data off the books; what it does is make the honest count the easy one
to produce, and make the sealed holdout mechanically single-use.

The file is append-only JSON lines, committed to the repository, so that
its history is part of the evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Iterable

ROLES = ("fit", "select", "judge")
STATUSES = ("PENDING", "PASSED", "FAILED", "INVALID", "ABANDONED")

# The broker's own history after the public dataset ends. Nobody in this
# project has used it for research; it is the only data left that can
# answer "does this work on data that chose nothing?".
SEALED_SOURCE = "tradelocker"
SEALED_START = date(2022, 4, 1)


class RegistryError(ValueError):
    """The registry refused a write because it would corrupt the record."""


class HoldoutSpent(RegistryError):
    """The sealed holdout has already been opened; it cannot be reused."""


@dataclass(frozen=True)
class Use:
    """One contiguous range of one data source, used in one role."""

    source: str
    start: date
    end: date  # exclusive
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise RegistryError(f"unknown role {self.role!r}; expected one of {ROLES}")
        if not self.start < self.end:
            raise RegistryError(f"empty range {self.start} → {self.end}")

    def overlaps(self, source: str, start: date, end: date) -> bool:
        return self.source == source and self.start < end and start < self.end

    def to_json(self) -> dict:
        return {"source": self.source, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "role": self.role}

    @classmethod
    def from_json(cls, raw: dict) -> "Use":
        return cls(raw["source"], date.fromisoformat(raw["start"]),
                   date.fromisoformat(raw["end"]), raw["role"])


@dataclass(frozen=True)
class Trial:
    """A registered hypothesis.

    ``tests`` is the number of pass/fail verdicts this trial reads — the
    multiple-testing denominator. ``configurations`` is the number of
    parameter sets actually evaluated, including ones that gated nothing
    (a plateau, a doubled-spread run); a deflated Sharpe ratio needs it.
    """

    id: str
    registered: datetime
    title: str
    hypothesis: str
    uses: tuple[Use, ...]
    tests: int
    configurations: int
    status: str
    preregistration: str | None = None
    commit: str | None = None
    result: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("-", "").replace("_", "").isalnum():
            raise RegistryError(f"trial id {self.id!r} must be a plain slug")
        if self.status not in STATUSES:
            raise RegistryError(f"unknown status {self.status!r}")
        if self.tests < 0 or self.configurations < max(self.tests, 1):
            raise RegistryError(
                f"{self.id}: configurations ({self.configurations}) must be at "
                f"least tests ({self.tests}) and at least one"
            )
        if self.registered.tzinfo is None:
            raise RegistryError(f"{self.id}: registration time must be timezone-aware")

    def to_json(self) -> dict:
        raw = asdict(self)
        raw["kind"] = "trial"
        raw["registered"] = self.registered.isoformat()
        raw["uses"] = [u.to_json() for u in self.uses]
        return raw

    @classmethod
    def from_json(cls, raw: dict) -> "Trial":
        return cls(
            id=raw["id"],
            registered=datetime.fromisoformat(raw["registered"]),
            title=raw["title"],
            hypothesis=raw["hypothesis"],
            uses=tuple(Use.from_json(u) for u in raw["uses"]),
            tests=int(raw["tests"]),
            configurations=int(raw["configurations"]),
            status=raw["status"],
            preregistration=raw.get("preregistration"),
            commit=raw.get("commit"),
            result=dict(raw.get("result") or {}),
        )


@dataclass(frozen=True)
class Verdict:
    """The outcome of a PENDING trial, appended once it has run.

    Kept as its own line so that registering a trial — which is what
    contaminates the data — happens before the result exists, and the
    result can never be edited into the registration afterwards.
    """

    trial: str
    recorded: datetime
    status: str
    result: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status in ("PENDING",) or self.status not in STATUSES:
            raise RegistryError(f"a verdict must be final, not {self.status!r}")
        if self.recorded.tzinfo is None:
            raise RegistryError("verdict time must be timezone-aware")

    def to_json(self) -> dict:
        return {"kind": "verdict", "trial": self.trial,
                "recorded": self.recorded.isoformat(), "status": self.status,
                "result": self.result}

    @classmethod
    def from_json(cls, raw: dict) -> "Verdict":
        return cls(raw["trial"], datetime.fromisoformat(raw["recorded"]),
                   raw["status"], dict(raw.get("result") or {}))


def bonferroni_t(tests: int, alpha: float = 0.05) -> float:
    """Two-sided |t| (normal approximation) a result must exceed.

    Thirteen tests at 5% gives 2.89 — the threshold the eight-family
    program used, reproduced here rather than restated.
    """
    if tests < 1:
        raise ValueError("at least one test is needed to set a threshold")
    return NormalDist().inv_cdf(1 - alpha / (2 * tests))


def holm(pvalues: Iterable[float], alpha: float = 0.05) -> list[bool]:
    """Holm–Bonferroni step-down. Returns, per p-value, whether it is rejected.

    Uniformly more powerful than plain Bonferroni and still controls the
    family-wise error rate, so there is no reason to use the weaker one
    when the p-values are all in hand.
    """
    ps = list(pvalues)
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    rejected = [False] * len(ps)
    m = len(ps)
    for rank, i in enumerate(order):
        if ps[i] > alpha / (m - rank):
            break
        rejected[i] = True
    return rejected


class Registry:
    """Append-only record of trials, backed by a JSON-lines file."""

    def __init__(self, path: Path | str,
                 entries: Iterable[Trial | Verdict] = ()) -> None:
        self.path = Path(path)
        self._trials: list[Trial] = []
        self._verdicts: dict[str, Verdict] = {}
        for entry in entries:
            self._accept(entry)

    @classmethod
    def load(cls, path: Path | str) -> "Registry":
        path = Path(path)
        entries: list[Trial | Verdict] = []
        if path.exists():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    kind = raw.get("kind", "trial")
                    if kind == "trial":
                        entries.append(Trial.from_json(raw))
                    elif kind == "verdict":
                        entries.append(Verdict.from_json(raw))
                    else:
                        raise ValueError(f"unknown kind {kind!r}")
                except (KeyError, ValueError, TypeError) as exc:
                    raise RegistryError(f"{path}:{n}: unreadable trial ({exc})") from exc
        # Loading re-applies every rule, so a hand-edited file that breaks
        # one is refused on read rather than trusted.
        return cls(path, entries)

    @property
    def trials(self) -> tuple[Trial, ...]:
        return tuple(self._trials)

    def get(self, trial_id: str) -> Trial:
        for trial in self._trials:
            if trial.id == trial_id:
                return trial
        raise KeyError(trial_id)

    def status_of(self, trial_id: str) -> str:
        verdict = self._verdicts.get(trial_id)
        return verdict.status if verdict else self.get(trial_id).status

    def register(self, trial: Trial) -> Trial:
        """Validate, then append to the file. Nothing is ever rewritten."""
        self._accept(trial)
        self._append(trial)
        return trial

    def record_verdict(self, verdict: Verdict) -> Verdict:
        self._accept(verdict)
        self._append(verdict)
        return verdict

    def _append(self, entry: Trial | Verdict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_json(), sort_keys=True) + "\n")

    def _accept(self, entry: Trial | Verdict) -> None:
        if isinstance(entry, Verdict):
            self._accept_verdict(entry)
        else:
            self._accept_trial(entry)

    def _accept_verdict(self, verdict: Verdict) -> None:
        trial = self.get(verdict.trial)  # KeyError: no verdict without a trial
        if self.status_of(trial.id) != "PENDING":
            raise RegistryError(
                f"{trial.id} already has a final status "
                f"({self.status_of(trial.id)}); a verdict is recorded once"
            )
        if verdict.recorded < trial.registered:
            raise RegistryError(f"{trial.id}: verdict predates its registration")
        self._verdicts[trial.id] = verdict

    def _accept_trial(self, trial: Trial) -> None:
        if any(t.id == trial.id for t in self._trials):
            raise RegistryError(
                f"trial {trial.id!r} already exists; a result is recorded as a new "
                f"trial that references it, never by editing the old one"
            )
        if self._trials and trial.registered < self._trials[-1].registered:
            raise RegistryError(
                f"trial {trial.id!r} is dated before the last entry; the record is "
                f"chronological and cannot be back-filled out of order"
            )
        sealed = [u for u in trial.uses
                  if u.overlaps(SEALED_SOURCE, SEALED_START, date.max)]
        if sealed:
            if trial.preregistration is None:
                raise RegistryError(
                    f"{trial.id}: the sealed holdout is used only by a pre-registered trial"
                )
            prior = self.touching(SEALED_SOURCE, SEALED_START, date.max)
            if prior:
                raise HoldoutSpent(
                    f"the sealed holdout ({SEALED_SOURCE} from {SEALED_START}) was "
                    f"already used by {', '.join(t.id for t in prior)}; it answers "
                    f"one question, once"
                )
        self._trials.append(trial)

    # ── queries ──────────────────────────────────────────────────────────

    def touching(self, source: str, start: date, end: date,
                 roles: tuple[str, ...] = ROLES) -> list[Trial]:
        """Trials that used any part of ``[start, end)`` of ``source``."""
        return [
            t for t in self._trials
            if any(u.role in roles and u.overlaps(source, start, end) for u in t.uses)
        ]

    def tests_on(self, source: str, start: date = date.min, end: date = date.max) -> int:
        """Verdicts read from this data: the multiple-testing denominator."""
        return sum(t.tests for t in self.touching(source, start, end))

    def configurations_on(self, source: str, start: date = date.min,
                          end: date = date.max) -> int:
        return sum(t.configurations for t in self.touching(source, start, end))

    def is_unseen(self, source: str, start: date, end: date) -> bool:
        """True only if no trial has used this range in any role."""
        return not self.touching(source, start, end)

    def threshold_for_next(self, source: str, start: date = date.min,
                           end: date = date.max, new_tests: int = 1,
                           alpha: float = 0.05) -> float:
        """|t| the next trial on this data must beat, counting everything before it."""
        return bonferroni_t(self.tests_on(source, start, end) + new_tests, alpha)

    def exposure_by_year(self, source: str) -> dict[int, int]:
        """Per calendar year, how many verdicts have been read from it."""
        years: dict[int, int] = {}
        for trial in self._trials:
            touched = {
                year
                for u in trial.uses if u.source == source
                for year in range(u.start.year, (u.end - timedelta(days=1)).year + 1)
            }
            for year in touched:
                years[year] = years.get(year, 0) + trial.tests
        return dict(sorted(years.items()))
