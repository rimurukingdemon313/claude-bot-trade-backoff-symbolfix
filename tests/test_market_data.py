from __future__ import annotations

import unittest

from market_data import MarketDataError, parse_yahoo_chart_payload


def yahoo_payload() -> dict:
    timestamps = [1788303600 + (index * 900) for index in range(8)]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [1.1 + index * 0.001 for index in range(8)],
                                "high": [1.102 + index * 0.001 for index in range(8)],
                                "low": [1.098 + index * 0.001 for index in range(8)],
                                "close": [1.101 + index * 0.001 for index in range(8)],
                                "volume": [0] * 8,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class MarketDataTests(unittest.TestCase):
    def test_converts_yahoo_payload_to_m15_ohlcv(self) -> None:
        candles = parse_yahoo_chart_payload(yahoo_payload())
        self.assertEqual(len(candles), 8)
        self.assertEqual(candles[0]["timeframe"], "M15")
        self.assertAlmostEqual(candles[-1]["close"], 1.108, places=9)
        self.assertEqual(candles[0]["volume"], 0.0)

    def test_skips_incomplete_bars_but_requires_seven(self) -> None:
        payload = yahoo_payload()
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
        candles = parse_yahoo_chart_payload(payload)
        self.assertEqual(len(candles), 7)

    def test_rejects_empty_chart(self) -> None:
        with self.assertRaises(MarketDataError):
            parse_yahoo_chart_payload({"chart": {"result": [], "error": None}})


if __name__ == "__main__":
    unittest.main()