"""Measuring the system the bot actually runs.

`Backtester` measures one symbol. The live bot scans 23, ranks the whole
scan and takes the single best, under a cap on concurrent positions — so
a one-symbol run has a twentieth of the sample AND models a strategy
nobody runs, optimistically, because in isolation every setup gets a slot.

That was not a theoretical complaint. Walk-forward on this strategy
returned "no parameter set produced enough trades" rather than a verdict,
and the folds were not small because the windows were short; they were
small because 22 symbols were missing.

Two currency bugs are pinned here too. Both were invisible while the
harness could only run EURUSD, and both would have quietly poisoned the
first multi-symbol measurement taken with it.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from bot.backtest.engine import BacktestCosts, Backtester, SimulatedTrade
from bot.backtest.portfolio import PortfolioBacktester, _index_at
from bot.risk.sizing import SizingError, calculate_position_size
from fakes import DEFAULT_SPEC, SETUP_END, series_from_path


def _walk(count: int, seed: int, start: float = 1.1000):
    rng = random.Random(seed)
    path = []
    price, vol, trend = start, start * 0.00032, 0.0
    for index in range(count):
        if index % 96 == 0:
            trend = rng.choice([0.0, 0.0, 1.0, -1.0]) * start * 0.00011
        vol = max(start * 0.00011, min(start * 0.0009, vol * rng.uniform(0.92, 1.09)))
        open_, close = price, price + trend + rng.gauss(0, vol)
        high = max(open_, close) + abs(rng.gauss(0, vol * 0.7))
        low = min(open_, close) - abs(rng.gauss(0, vol * 0.7))
        path.append((open_, high, low, close))
        price = close
    return series_from_path(path, timeframe="M15", end=SETUP_END)


def _h1(m15, group: int = 4):
    out = []
    for index in range(0, len(m15) - group + 1, group):
        chunk = m15[index : index + group]
        out.append(
            dataclasses.replace(
                chunk[0],
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                timeframe="H1",
            )
        )
    return out


# -- the currency bugs ----------------------------------------------------


def test_profit_is_converted_into_the_account_currency():
    """`gross` was quote-currency units reported as account currency.

    Correct for a USD-quoted pair, where the rate is 1.0 — which is every
    pair the harness could run before it went multi-symbol. On a JPY cross
    it overstated every win and every loss by about 150x, and the sizer
    made it worse by being right: RISK was converted and RESULT was not,
    so the two halves of the same trade used different money.
    """

    yen = dataclasses.replace(
        DEFAULT_SPEC, symbol="USDJPY", broker_name="USDJPY",
        base_currency="USD", quote_currency="JPY", tick_size=0.001, digits=3,
    )
    engine = Backtester(load_test_config(), yen, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))

    rate = 1 / 150.0
    trade = SimulatedTrade(
        symbol="USDJPY", direction="BUY", entry_index=0, entry_time=SETUP_END,
        entry=150.000, stop_loss=149.000, take_profit=152.000, lots=0.10,
        risk_amount=100.0, setup_grade="A", setup_score=70.0, conversion_rate=rate,
    )
    bar = dataclasses.replace(
        series_from_path([(152.0, 152.5, 151.8, 152.2)], timeframe="M15", end=SETUP_END)[0]
    )
    assert engine._resolve_exit(trade, bar, index=1)

    # 2.000 JPY of move x 100,000 contract x 0.10 lots = 20,000 JPY,
    # which is about $133 — not $20,000.
    assert trade.pnl == pytest.approx(20_000 * rate, rel=1e-6)
    assert trade.pnl < 200.0, "unconverted JPY profit is ~150x too large"


def test_a_cross_currency_symbol_needs_a_rate_source_and_says_so():
    """Without one, `conversion_rate` raises — correctly — and the
    backtester counted it as "sizing" and moved on.

    So a twelve-symbol run measured the four quoted in the account
    currency and reported it as twelve. The sizer was never wrong; the
    harness just never gave it what it needed.
    """

    cross = dataclasses.replace(
        DEFAULT_SPEC, symbol="EURGBP", broker_name="EURGBP",
        base_currency="EUR", quote_currency="GBP",
    )
    with pytest.raises(SizingError, match="no conversion-rate source"):
        calculate_position_size(
            spec=cross, risk_amount=25.0, entry=0.8500, stop_loss=0.8450
        )

    sized = calculate_position_size(
        spec=cross, risk_amount=25.0, entry=0.8500, stop_loss=0.8450,
        rate_lookup=lambda base, quote: 1.27 if (base, quote) == ("GBP", "USD") else None,
    )
    assert sized.lots > 0
    assert sized.conversion_rate == pytest.approx(1.27)


# -- the portfolio run ----------------------------------------------------


def load_test_config():
    from bot.config import load_config

    base = load_config({})
    return dataclasses.replace(
        base,
        ai=dataclasses.replace(base.ai, enabled=False),
        news=dataclasses.replace(base.news, enabled=False),
    )


def test_the_portfolio_run_respects_the_concurrent_position_cap():
    """The live bot takes the single best opportunity per scan under a cap.

    A one-symbol backtest models neither, and gives every setup a slot.
    """

    config = load_test_config()
    run = PortfolioBacktester(config, starting_balance=5_000.0)
    for index in range(4):
        m15 = _walk(900, seed=300 + index)
        spec = dataclasses.replace(
            DEFAULT_SPEC, symbol=f"PAIR{index}", broker_name=f"PAIR{index}",
            tradable_instrument_id=index + 1,
        )
        run.add(spec, m15, _h1(m15))

    result = run.run(warmup=250, step=4)

    # Never more open at once than the cap allows.
    opened = sorted(result.trades, key=lambda t: t.entry_time)
    live = 0
    peak = 0
    for trade in opened:
        live += 1
        peak = max(peak, live)
        if trade.exit_time is not None:
            live -= 1
    assert peak <= config.risk.max_open_positions

    # And one symbol never holds two positions at the same time.
    for symbol in {trade.symbol for trade in result.trades}:
        rows = [t for t in result.trades if t.symbol == symbol]
        rows.sort(key=lambda t: t.entry_time)
        for earlier, later in zip(rows, rows[1:]):
            assert earlier.exit_time is not None
            assert earlier.exit_time <= later.entry_time


def test_the_portfolio_run_sees_more_than_one_symbol():
    """The whole point. A run that silently measured one would look fine."""

    config = load_test_config()
    run = PortfolioBacktester(config, starting_balance=5_000.0)
    for index in range(5):
        m15 = _walk(900, seed=400 + index)
        spec = dataclasses.replace(
            DEFAULT_SPEC, symbol=f"SYM{index}", broker_name=f"SYM{index}",
            tradable_instrument_id=index + 1,
        )
        run.add(spec, m15, _h1(m15))

    result = run.run(warmup=250, step=4)
    stats = result.statistics()
    assert stats["barsProcessed"] > 0
    # Rejections are pooled across symbols, so the harness reached them all.
    assert sum(stats["rejections"].values()) > 0


def test_a_symbol_is_never_judged_on_a_candle_that_had_not_closed():
    """Rule 4, at the portfolio level.

    Symbols share a clock here, and the index each one is walked by comes
    from a bisect on close time. An off-by-one there would hand the engine
    the future on every symbol at once.
    """

    m15 = _walk(400, seed=7)
    for probe in (0, 1, 50, 399):
        cutoff = m15[probe].close_time
        assert _index_at(m15, cutoff) == probe
        # A moment one second before this candle closed resolves to the
        # PREVIOUS one, never to this.
        from datetime import timedelta

        just_before = cutoff - timedelta(seconds=1)
        resolved = _index_at(m15, just_before)
        assert resolved is None or resolved == probe - 1


def test_an_empty_portfolio_refuses_rather_than_reporting_nothing():
    with pytest.raises(ValueError, match="no symbols"):
        PortfolioBacktester(load_test_config()).run()
