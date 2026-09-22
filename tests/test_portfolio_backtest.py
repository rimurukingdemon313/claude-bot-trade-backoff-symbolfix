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


# -- position management, simulated by the live rules ---------------------


def _bar(open_, high, low, close):
    return series_from_path([(open_, high, low, close)], timeframe="M15", end=SETUP_END)[0]


def _long(config, **overrides):
    defaults = dict(
        symbol="EURUSD", direction="BUY", entry_index=0, entry_time=SETUP_END,
        entry=1.1000, stop_loss=1.0980, take_profit=1.1060, lots=1.0,
        risk_amount=100.0, setup_grade="A", setup_score=70.0,
    )
    defaults.update(overrides)
    return SimulatedTrade(**defaults)


def test_break_even_cannot_rescue_a_trade_the_same_bar_stopped_out():
    """Management runs on a timer live, not intrabar.

    A simulator that moved the stop first would let a bar which touched
    1R and then ran to the original stop come out flat — turning a loss
    into a scratch out of nothing, on the one bar where the sequence is
    unknowable.
    """

    config = load_test_config()
    engine = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))
    trade = _long(config)
    assert config.execution.enable_breakeven

    # Reaches 1R (1.1020) AND the original stop (1.0980) in one bar.
    assert engine._resolve_exit(trade, _bar(1.1000, 1.1030, 1.0975, 1.0985), index=1)
    assert trade.exit_reason == "STOP"
    assert (trade.pnl or 0) < 0, "the stop must be honoured, not the break-even"


def test_break_even_protects_the_trade_from_the_next_bar():
    config = load_test_config()
    engine = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))
    trade = _long(config)
    original = trade.stop_loss

    # Bar 1 reaches 1R without touching either level.
    assert not engine._resolve_exit(trade, _bar(1.1000, 1.1030, 1.0995, 1.1025), index=1)
    assert trade.stop_loss > original, "1R should have moved the stop up"
    assert trade.stop_loss >= trade.entry
    assert any("break-even" in note for note in trade.managed)

    # Bar 2 falls back through entry: closed at break-even, not at -1R.
    assert engine._resolve_exit(trade, _bar(1.1025, 1.1028, 1.0900, 1.0950), index=2)
    assert (trade.pnl or 0) > 0, "a break-even stop books a touch above entry"


def test_r_is_measured_against_the_risk_the_trade_was_opened_with():
    """Once the stop has moved, `stop_loss` is no longer the risk.

    Measuring R against the CURRENT stop would report a break-even trade
    as infinite R, and a trailing one as nonsense.
    """

    config = load_test_config()
    engine = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))
    trade = _long(config)
    engine._resolve_exit(trade, _bar(1.1000, 1.1030, 1.0995, 1.1025), index=1)
    engine._resolve_exit(trade, _bar(1.1025, 1.1065, 1.1020, 1.1060), index=2)

    assert trade.initial_stop == pytest.approx(1.0980)
    assert trade.exit_reason == "TARGET"
    assert trade.r_multiple == pytest.approx((trade.pnl or 0) / trade.risk_amount)
    assert 1.0 < (trade.r_multiple or 0) < 10.0, "R must stay a sane multiple"


def test_a_partial_banks_profit_and_leaves_the_rest_running():
    """The lever that converts a would-be scratch into a win.

    A trade that reaches 1.5R and then reverses to break-even books
    nothing today. With half off at 1.5R it books 0.75R — and that is a
    genuine win-rate improvement rather than a cosmetic one, because the
    money was actually taken at a level price actually reached.
    """

    config = dataclasses.replace(
        load_test_config(),
        execution=dataclasses.replace(load_test_config().execution, enable_partial_tp=True),
    )
    engine = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))
    trade = _long(config)

    # 1.5R for this trade is 1.1030. Reach it without hitting the target.
    assert not engine._resolve_exit(trade, _bar(1.1000, 1.1035, 1.0995, 1.1032), index=1)
    assert trade.partial_taken
    assert trade.open_lots == pytest.approx(trade.lots * config.execution.partial_tp_fraction)
    assert trade.banked_pnl > 0

    # Now it reverses to the break-even stop, which 1R also set.
    assert engine._resolve_exit(trade, _bar(1.1032, 1.1033, 1.0900, 1.0950), index=2)
    assert (trade.pnl or 0) > 0, "banked profit must survive the reversal"
    # The total is the banked half plus whatever the runner did, and
    # nothing else — no double counting of the closed lots.
    runner_move = (trade.exit_price or 0) - trade.entry
    runner = runner_move * DEFAULT_SPEC.contract_size * (trade.lots * 0.5)
    assert trade.pnl == pytest.approx(trade.banked_pnl + runner, rel=1e-6)


def test_a_partial_is_filled_at_its_trigger_not_at_the_bar_extreme():
    """A poll sees the level, not the wick's best tick."""

    config = dataclasses.replace(
        load_test_config(),
        execution=dataclasses.replace(load_test_config().execution, enable_partial_tp=True),
    )
    engine = Backtester(config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0))
    trade = _long(config)

    # A wick well beyond 1.5R (1.1030) but short of the target (1.1060),
    # so the trade is still open and the partial is the only thing that
    # fires on this bar.
    engine._resolve_exit(trade, _bar(1.1000, 1.1050, 1.0995, 1.1032), index=1)
    assert trade.partial_taken

    risk = trade.entry - trade.initial_stop
    at_trigger = risk * config.execution.partial_tp_at_r * DEFAULT_SPEC.contract_size * (
        trade.lots * config.execution.partial_tp_fraction
    )
    assert trade.banked_pnl == pytest.approx(at_trigger, rel=1e-6), (
        "a partial filled at the wick would invent profit the poll never saw"
    )


def test_management_uses_the_live_planner_rather_than_its_own_rules():
    """A reimplementation would drift from live the first time either moved."""

    import inspect

    from bot.backtest import engine as module

    source = inspect.getsource(module.Backtester._manage)
    assert "plan_actions(" in source
    assert "enable_breakeven" not in source, "the backtest must not re-decide management"
    assert "breakeven_at_r" not in source


def test_management_can_be_changed_without_a_code_deploy(monkeypatch):
    """"Earned by testing" needs testing to be possible.

    Trailing is off because the project says it must be earned rather
    than switched on for sounding sophisticated. That rule only means
    something if an operator can actually run the experiment — on paper,
    for a fortnight, and switch back — without shipping code.

    Partials went through exactly that procedure and are now ON at
    0.75R; see docs/EXPERIMENT_WIN_RATE.md. The rule said "until
    evidence justifies them", which is a standard, not a ban.
    """

    from bot.config import load_config

    default = load_config({}).execution
    assert default.enable_breakeven is True
    assert default.enable_structure_exit is True
    assert default.enable_partial_tp is True
    assert default.partial_tp_at_r == pytest.approx(0.75)
    assert default.enable_trailing is False, "trailing measured no better than nothing"

    monkeypatch.setenv("EXEC_ENABLE_PARTIAL_TP", "true")
    monkeypatch.setenv("EXEC_PARTIAL_TP_AT_R", "1.25")
    monkeypatch.setenv("EXEC_ENABLE_TRAILING", "true")
    tuned = load_config({}).execution
    assert tuned.enable_partial_tp is True
    assert tuned.partial_tp_at_r == pytest.approx(1.25)
    assert tuned.enable_trailing is True

    # A garbage value falls back to the default rather than disabling a
    # guard — the same asymmetry the rest of the config uses.
    monkeypatch.setenv("EXEC_PARTIAL_TP_AT_R", "not-a-number")
    assert load_config({}).execution.partial_tp_at_r == pytest.approx(0.75)

    # And it can be turned back off from the environment, which a default
    # moved on synthetic evidence had better allow.
    monkeypatch.setenv("EXEC_ENABLE_PARTIAL_TP", "false")
    assert load_config({}).execution.enable_partial_tp is False

    # And the execution GUARDS are untouched by any of this.
    assert load_config({}).execution.max_spread_atr_fraction == pytest.approx(0.12)
    assert load_config({}).execution.order_verify_attempts == 5


def test_a_short_banks_its_partial_at_the_mirror_level():
    """The sign convention, asserted rather than assumed.

    A short's stop is ABOVE entry and its 1.5R is BELOW, so every term
    flips. This is where a copy-pasted long branch books a loss as a
    profit and the backtest reports a win rate that never existed.
    """

    config = dataclasses.replace(
        load_test_config(),
        execution=dataclasses.replace(load_test_config().execution, enable_partial_tp=True),
    )
    engine = Backtester(
        config, DEFAULT_SPEC, costs=BacktestCosts(slippage_points=0.0, commission_per_lot=0.0)
    )
    trade = _long(
        config, direction="SELL", entry=1.1000, stop_loss=1.1020, take_profit=1.0940
    )
    assert trade.initial_stop > trade.entry, "a short's stop sits above entry"

    # 1.5R for a short risking 0.0020 is 1.1000 - 0.0030 = 1.0970.
    assert not engine._resolve_exit(trade, _bar(1.1000, 1.1005, 1.0965, 1.0972), index=1)
    assert trade.partial_taken
    assert trade.banked_pnl > 0, "a short that fell 1.5R banked a PROFIT"

    risk = trade.initial_stop - trade.entry
    expected = risk * config.execution.partial_tp_at_r * DEFAULT_SPEC.contract_size * (
        trade.lots * config.execution.partial_tp_fraction
    )
    assert trade.banked_pnl == pytest.approx(expected, rel=1e-6)

    # And its break-even stop moved DOWN, not up.
    assert trade.stop_loss < trade.entry
    assert trade.stop_loss < trade.initial_stop


# -- the property every number in this session rests on --------------------


def test_the_portfolio_run_is_identical_when_the_future_is_deleted():
    """Rule 4, applied to the harness rather than to the detectors.

    `test_analysis_on_bar_i_cannot_see_bar_i_plus_one` proves the
    DETECTORS are honest. It says nothing about the runner I wrote today,
    which now carries every measured number in this session — the control
    against a coin flip, the stop-width table, the win-rate table. If this
    runner peeked, all of them would be fiction.

    So: run the whole thing on a series, then run it again on a series
    truncated before the end, and every trade opened inside the shorter
    window must be identical. A single lookahead anywhere — the bisect,
    the H1 filter, the exit loop, the ranking — changes one of them.
    """

    config = load_test_config()

    def run(bars: int):
        engine = PortfolioBacktester(config, starting_balance=5_000.0)
        for index in range(3):
            m15 = _walk(900, seed=700 + index)[:bars]
            spec = dataclasses.replace(
                DEFAULT_SPEC, symbol=f"LOOK{index}", broker_name=f"LOOK{index}",
                tradable_instrument_id=index + 1,
            )
            engine.add(spec, m15, _h1(m15))
        return engine.run(warmup=250, step=4)

    full = run(900)
    truncated = run(700)

    # Trades opened before the truncation point must match exactly.
    shortened = {(t.symbol, t.entry_time.isoformat()): t for t in truncated.trades}
    compared = 0
    for trade in full.trades:
        key = (trade.symbol, trade.entry_time.isoformat())
        if key not in shortened:
            continue
        other = shortened[key]
        compared += 1
        assert trade.direction == other.direction
        assert trade.entry == pytest.approx(other.entry)
        assert trade.initial_stop == pytest.approx(other.initial_stop)
        assert trade.take_profit == pytest.approx(other.take_profit)
        assert trade.lots == pytest.approx(other.lots)
        assert trade.setup_score == pytest.approx(other.setup_score)

    assert compared > 0, "the two runs shared no trades — the test proved nothing"


def test_a_symbol_never_proposes_from_a_candle_that_had_not_closed():
    """The bisect is the single point where the future could leak in.

    Every symbol shares one clock here, and each is walked by its own
    index into it. An off-by-one in `_index_at` would hand the engine one
    unclosed candle on every symbol at once, which is the kind of bug that
    improves every backtest and explains nothing.
    """

    from datetime import timedelta

    m15 = _walk(500, seed=42)
    for probe in (250, 300, 499):
        cutoff = m15[probe].close_time
        assert _index_at(m15, cutoff) == probe

        # One second before this candle closed resolves to the previous.
        assert _index_at(m15, cutoff - timedelta(seconds=1)) == probe - 1
        # And one second after still resolves to this one, never the next.
        assert _index_at(m15, cutoff + timedelta(seconds=1)) == probe

    # Before the first close there is nothing to see.
    assert _index_at(m15, m15[0].close_time - timedelta(minutes=1)) is None


def _spread_run(spread_points: float):
    """The same four symbols and seeds, differing only in spread."""

    from bot.backtest.engine import BacktestCosts

    config = load_test_config()
    run = PortfolioBacktester(
        config,
        starting_balance=5_000.0,
        costs=BacktestCosts(spread_points=spread_points),
    )
    for index in range(4):
        m15 = _walk(900, seed=300 + index)
        spec = dataclasses.replace(
            DEFAULT_SPEC, symbol=f"PAIR{index}", broker_name=f"PAIR{index}",
            tradable_instrument_id=index + 1,
        )
        run.add(spec, m15, _h1(m15))
    return run.run(warmup=250, step=4)


def test_the_backtest_refuses_the_spreads_the_live_executor_refuses():
    """The harness paid the spread and never asked the executor's question.

    `Executor._spread_check` runs `validate_spread` last, before the
    order goes out, and aborts when the spread is too large relative to
    THIS setup's stop and target. The backtest charged the spread as a
    cost and skipped that gate entirely, so it counted trades the live
    bot refuses at submission.

    That is not a rounding difference. It is the difference between "the
    strategy is too selective" and "the strategy proposes setups it
    cannot execute" — opposite diagnoses with opposite fixes, and acting
    on the wrong one makes the real problem worse. It is also exactly
    the gap an operator was living: a backtest reporting 180 trades
    beside a live bot reporting none.

    A wide spread must now cost TRADES, not merely money.
    """

    tight = _spread_run(2)
    wide = _spread_run(90)

    assert wide.setups_rejected.get("spread guard", 0) > 0, (
        "a 90-tick spread has to be refused by the guard, not silently paid"
    )
    assert len(wide.trades) < len(tight.trades), (
        "and the refusal must cost trades — the whole point of the gate"
    )


def test_a_workable_spread_still_lets_the_setups_through():
    """The control: the guard must not simply refuse everything.

    Without this, the test above would also pass on a harness broken
    into taking no trades at all.
    """

    result = _spread_run(2)
    assert result.setups_rejected.get("spread guard", 0) == 0
    assert result.trades, "a workable spread must still produce trades"
