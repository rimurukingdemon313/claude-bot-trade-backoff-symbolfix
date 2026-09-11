# M15 SMC Analysis Engine

This module analyzes chronological OHLC candles and returns structured Smart
Money Concepts features. It is intentionally analysis-only:

- No MT5 or broker dependency
- No Gemini or external AI dependency
- No trade execution or order-routing code
- No claims about profitability

## Run the analysis

```bash
# Live EURUSD M15 candles from Yahoo Finance
python3 run_smc_demo.py

# Deterministic fixture for offline verification
python3 run_smc_demo.py --fixture
```

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

The fixture in `analysis_engine/fixtures/m15_sample.json` contains M15 candles
with deliberately observable pivots, equal levels, liquidity sweeps, structure
breaks, gaps, displacement, and opposing candles for basic order-block
detection. It is synthetic verification data, not market history. The default
runner uses Yahoo Finance's free chart endpoint for current EURUSD M15 candles
and fails explicitly if live data is unavailable; it does not fall back to the
fixture.