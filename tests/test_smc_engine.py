from __future__ import annotations

import json
import unittest
from pathlib import Path

from analysis_engine import Candle, SMCAnalyzer


FIXTURE_PATH = Path(__file__).parents[1] / "analysis_engine" / "fixtures" / "m15_sample.json"


def load_fixture() -> list[Candle]:
    rows = json.loads(FIXTURE_PATH.read_text())
    return [Candle.from_mapping(row) for row in rows]


class SMCAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candles = load_fixture()
        cls.analysis = SMCAnalyzer().analyze(cls.candles)

    def test_fixture_is_chronological_m15_data(self) -> None:
        self.assertEqual(self.analysis.timeframe, "M15")
        self.assertEqual(self.analysis.candle_count, 29)
        self.assertEqual(self.analysis.latest_candle.close, 113.2)

    def test_detects_swings_previous_levels_and_equal_levels(self) -> None:
        self.assertIn(6, [point.index for point in self.analysis.swing_highs])
        self.assertIn(10, [point.index for point in self.analysis.swing_highs])
        self.assertIn(8, [point.index for point in self.analysis.swing_lows])
        self.assertGreaterEqual(len(self.analysis.equal_highs), 1)
        self.assertGreaterEqual(len(self.analysis.equal_lows), 1)
        self.assertEqual(self.analysis.equal_lows[0].first_index, 23)
        self.assertEqual(self.analysis.equal_lows[0].second_index, 26)
        self.assertIsNotNone(self.analysis.previous_high)
        self.assertIsNotNone(self.analysis.previous_low)

    def test_detects_both_liquidity_sweep_directions(self) -> None:
        sweeps_by_index = {event.index: event.direction for event in self.analysis.liquidity_sweeps}
        self.assertEqual(sweeps_by_index.get(13), "bearish")
        self.assertEqual(sweeps_by_index.get(16), "bullish")

    def test_detects_choch_and_bos(self) -> None:
        event_types = {event.event_type for event in self.analysis.structure_breaks}
        directions = {event.direction for event in self.analysis.structure_breaks}
        self.assertIn("CHoCH", event_types)
        self.assertIn("BOS", event_types)
        self.assertIn("bullish", directions)
        self.assertIn("bearish", directions)
        self.assertEqual(self.analysis.market_structure.bias, "bullish")

    def test_detects_fvgs_order_blocks_and_displacement(self) -> None:
        self.assertIn("bullish", {gap.direction for gap in self.analysis.fair_value_gaps})
        self.assertIn("bearish", {gap.direction for gap in self.analysis.fair_value_gaps})
        self.assertIn("bullish", {block.direction for block in self.analysis.order_blocks})
        self.assertIn("bearish", {block.direction for block in self.analysis.order_blocks})
        displacement_indexes = {move.index for move in self.analysis.displacement}
        self.assertIn(14, displacement_indexes)
        self.assertIn(16, displacement_indexes)

    def test_returns_momentum_volatility_and_context(self) -> None:
        self.assertEqual(self.analysis.momentum.direction, "bullish")
        self.assertIn(self.analysis.volatility.regime, {"low", "normal", "high"})
        self.assertIn(self.analysis.overall_context.direction, {"bullish", "bearish", "mixed"})
        self.assertIsInstance(self.analysis.overall_context.rationale, tuple)

    def test_analysis_is_deterministic_and_serializable(self) -> None:
        analyzer = SMCAnalyzer()
        first = analyzer.analyze(self.candles).to_dict()
        second = analyzer.analyze(self.candles).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(first["timeframe"], "M15")
        self.assertEqual(first["latest_candle"]["close"], 113.2)

    def test_accepts_supported_non_m15_timeframes(self) -> None:
        row = {
            "timestamp": "2026-01-02T09:00:00+00:00",
            "timeframe": "H1",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
        }
        candle = Candle.from_mapping(row)
        self.assertEqual(candle.timeframe, "H1")

    def test_rejects_unsupported_timeframe(self) -> None:
        row = {
            "timestamp": "2026-01-02T09:00:00+00:00",
            "timeframe": "D1",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
        }
        with self.assertRaises(ValueError):
            Candle.from_mapping(row)

    def test_rejects_mixed_timeframes_in_one_analysis(self) -> None:
        candles = load_fixture()[:6]
        rows = [
            {
                "timestamp": candle.timestamp.isoformat(),
                "timeframe": candle.timeframe,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
            }
            for candle in candles
        ]
        h1_row = dict(rows[-1])
        h1_row["timeframe"] = "H1"
        rows.append(h1_row)
        with self.assertRaises(ValueError):
            SMCAnalyzer().analyze(Candle.from_mapping(row) for row in rows)


if __name__ == "__main__":
    unittest.main()
