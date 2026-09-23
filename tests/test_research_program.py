"""The six gates of docs/EXPERIMENT_EDGE_PROGRAM.md, on trade lists whose
correct verdict is known by construction (rule 10)."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "research_program", Path(__file__).resolve().parent.parent / "scripts" / "research_program.py"
)
program = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(program)

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]


def trades(r_for, per_month=2, start=2013, end=2022):
    """Trades on every symbol, `per_month` a month, R chosen by r_for(symbol, when, k)."""

    out = []
    k = 0
    for year in range(start, end + 1):
        for month in range(1, 13):
            if year == 2022 and month > 3:
                break
            for symbol in SYMBOLS:
                for j in range(per_month):
                    when = datetime(year, month, 3 + 7 * j, 10, tzinfo=timezone.utc)
                    k += 1
                    out.append({"symbol": symbol, "entryTime": when.isoformat(),
                                "exitTime": (when + timedelta(hours=5)).isoformat(),
                                "r": r_for(symbol, when, k), "costR": 0.05,
                                "exitReason": "TARGET", "tag": ""})
    return out


def edge(symbol, when, k):
    """A clear, broad edge: +1.2R wins, -1R losses, 55% winners."""

    return 1.2 if k % 20 < 11 else -1.0


def by_variant(base, **overrides):
    labels = ["base"] + [f"v{i}" for i in range(8)] + ["spread_x2"]
    return {label: overrides.get(label, base) for label in labels}


def test_a_broad_durable_edge_passes_every_gate():
    result = program.judge("FX", by_variant(trades(edge)))
    assert result["gates"] == {f"G{i}": True for i in range(1, 7)}
    assert result["passed"]


def test_an_edge_living_on_one_pair_fails_breadth():
    def one_pair(symbol, when, k):
        return 2.5 if symbol == "EURUSD" else (0.3 if k % 2 else -0.35)

    result = program.judge("FX", by_variant(trades(one_pair)))
    assert result["gates"]["G3"] is False
    assert not result["passed"]


def test_an_edge_that_existed_only_before_2019_fails():
    def faded(symbol, when, k):
        return edge(symbol, when, k) if when.year < 2019 else (-edge(symbol, when, k) * 0.9)

    result = program.judge("FX", by_variant(trades(faded)))
    assert result["gates"]["G1"] is False
    assert not result["passed"]


def test_a_tiny_positive_edge_fails_significance():
    def barely(symbol, when, k):
        return 1.0 if k % 2 else -0.98

    result = program.judge("FX", by_variant(trades(barely)))
    assert result["segments"]["OOS"]["avgR"] > 0
    assert result["gates"]["G2"] is False


def test_a_spike_in_parameter_space_fails_the_plateau():
    base = trades(edge)
    losing = trades(lambda s, w, k: -edge(s, w, k))
    result = program.judge("FX", by_variant(base, v0=losing, v1=losing, v2=losing))
    assert result["detail"]["plateauPositive"] == 6
    assert result["gates"]["G5"] is False


def test_an_edge_the_doubled_spread_erases_fails_robustness():
    thin = trades(lambda s, w, k: -0.5)
    result = program.judge("FX", by_variant(trades(edge), spread_x2=thin))
    assert result["gates"]["G6"] is False
    assert not result["passed"]


def test_losing_in_two_of_four_oos_years_fails_time_breadth():
    def patchy(symbol, when, k):
        return -edge(symbol, when, k) if when.year in (2020, 2021) else edge(symbol, when, k) * 2

    result = program.judge("FX", by_variant(trades(patchy)))
    assert result["detail"]["positiveYears"] == 2
    assert result["gates"]["G4"] is False


def test_the_plateau_grid_is_nine_variants_around_the_base_plus_the_spread_run():
    for key in program.FAMILIES:
        labels = [v[0] for v in program.variants(key)]
        assert labels[0] == "base" and labels[-1] == "spread_x2"
        assert len(labels) == 10
