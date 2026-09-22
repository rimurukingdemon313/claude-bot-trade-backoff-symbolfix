"""Write-outcome classification, and benching one symbol instead of all.

Both findings came from reading how other systems solve the same
problems — NautilusTrader's command-outcome taxonomy and Freqtrade's
per-pair protection locks. Neither is a copied implementation; what
transferred is the distinction each project draws that this one did not.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from bot.clock import utc_now
from bot.errors import AmbiguousExecution, BrokerError
from bot.execution.manager import ManagementAction, PositionManager
from bot.risk.engine import AccountRiskState, RiskEngine
from fakes import DEFAULT_SPEC, SETUP_END


# -- a 5xx on a write is unknown, not failed -------------------------------


def _raising_transport(monkeypatch, code: int):
    """Make every urlopen raise an HTTPError with `code`."""

    import io
    import urllib.error
    import urllib.request

    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, code, "boom", {}, io.BytesIO(b"gateway said no")
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_a_server_error_on_a_write_is_ambiguous_not_a_clean_failure(monkeypatch, code):
    """A 5xx says the gateway failed, never that the order did.

    NautilusTrader states the rule precisely: a status code is definitive
    only when the venue's own semantics prove the command was not
    accepted. A 4xx does. A 5xx does not — the request reached the
    server, it answered, and the matching engine behind it may well have
    taken the order. A 504 in particular is the classic shape of "it
    worked, the reply was lost".

    This branch used to raise a plain BrokerError. The executor's
    catch-all routed that to the ambiguous path anyway, so no live order
    was ever lost — but every OTHER caller of a write inherited the wrong
    classification, and `PositionManager` is one of them.
    """

    from bot.broker.http import HttpTransport

    _raising_transport(monkeypatch, code)
    client = HttpTransport(sleeper=lambda _seconds: None)

    with pytest.raises(AmbiguousExecution):
        client.request("POST", "https://example.invalid/trade/orders", body={"qty": 1})


@pytest.mark.parametrize("code", [500, 503])
def test_the_same_server_error_on_a_READ_stays_an_ordinary_failure(monkeypatch, code):
    """A read has no side effect to be uncertain about. It may retry and
    then fail plainly; turning it ambiguous would be noise."""

    from bot.broker.http import HttpTransport

    _raising_transport(monkeypatch, code)
    client = HttpTransport(max_attempts=2, sleeper=lambda _seconds: None)

    with pytest.raises(BrokerError) as caught:
        client.request("GET", "https://example.invalid/trade/accounts")
    assert not isinstance(caught.value, AmbiguousExecution)


def test_a_client_error_on_a_write_stays_a_definitive_rejection(monkeypatch):
    """4xx is the case where the venue's semantics DO prove it was not
    accepted. Treating it as ambiguous would strand a trade in the
    reconciler for an order that certainly never existed."""

    from bot.broker.http import HttpTransport
    from bot.errors import BrokerRejected

    _raising_transport(monkeypatch, 400)
    client = HttpTransport(sleeper=lambda _seconds: None)

    with pytest.raises(BrokerRejected):
        client.request("POST", "https://example.invalid/trade/orders", body={"qty": 1})


# -- an ambiguous management action must not repeat ------------------------


class _AmbiguousBroker:
    """Closes raise as ambiguous; everything else behaves."""

    def __init__(self, inner):
        self._inner = inner
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close_position(self, position_id, quantity=None):
        self.close_calls += 1
        raise AmbiguousExecution("transport died after the request left this process")


def test_an_ambiguous_partial_close_is_never_repeated(config, broker, repos):
    """The bug that cost a live position, reached by a second route.

    Nothing retries in code. But `_apply` runs on EVERY poll, so a
    partial close whose outcome was unknown simply came back thirty
    seconds later and took another slice — the same descending stack the
    missing column produced: 0.17, 0.09, 0.04, 0.03, 0.01, 0.01.

    The guard is `mark_partial_taken`, and it sat AFTER the broker call,
    so an ambiguous outcome skipped it.
    """

    position = broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.2, entry=1.1000, stop_loss=1.0950
    )
    repos.trades.create_pending(
        execution_id="exec-ambiguous",
        plan={
            "execution_id": "exec-ambiguous",
            "symbol": "EURUSD",
            "direction": "BUY",
            "quantity": 0.2,
            "entry": 1.1000,
            "stop_loss": 1.0950,
            "take_profit": 1.1150,
        },
    )
    repos.trades.mark_open(
        "exec-ambiguous",
        broker_position_id=position.position_id,
        broker_order_id="order-1",
        actual_entry=1.1000,
        quantity=0.2,
    )

    unreliable = _AmbiguousBroker(broker)
    manager = PositionManager(config, unreliable, repos)
    action = ManagementAction(
        kind="PARTIAL_CLOSE",
        position_id=position.position_id,
        symbol="EURUSD",
        reason="partial at 0.75R",
        quantity=0.1,
    )

    applied = manager.apply([action])
    assert applied[0]["ok"] is False
    assert "ambiguous" in applied[0], "an unknown outcome must be reported as unknown"

    trade = repos.trades.by_position_id(position.position_id)
    assert trade["partial_taken"], (
        "an ambiguous partial must still set the guard — the safe asymmetry is a "
        "runner left too large, never a position sliced again every poll"
    )


def test_an_ambiguous_action_is_handed_to_the_reconciler(config, broker, repos):
    """Rule 3: the only recovery is to ask the broker."""

    position = broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.2, entry=1.1000, stop_loss=1.0950
    )
    manager = PositionManager(config, _AmbiguousBroker(broker), repos)
    manager.apply(
        [
            ManagementAction(
                kind="CLOSE",
                position_id=position.position_id,
                symbol="EURUSD",
                reason="structural exit",
            )
        ]
    )
    records = repos.reconciliations.recent(limit=10)
    assert any(row["kind"] == "AMBIGUOUS_MANAGEMENT" for row in records), (
        "an unknown write outcome must reach the reconciler, not just a log line"
    )


def test_an_ordinary_failure_still_reads_as_a_failure(config, broker, repos):
    """The ambiguous path must not swallow definitive errors."""

    class _Rejecting(_AmbiguousBroker):
        def close_position(self, position_id, quantity=None):
            raise BrokerError("instrument closed for trading")

    position = broker.add_position(
        symbol="EURUSD", direction="BUY", quantity=0.2, entry=1.1000, stop_loss=1.0950
    )
    manager = PositionManager(config, _Rejecting(broker), repos)
    applied = manager.apply(
        [
            ManagementAction(
                kind="PARTIAL_CLOSE",
                position_id=position.position_id,
                symbol="EURUSD",
                reason="partial",
                quantity=0.1,
            )
        ]
    )
    assert applied[0]["ok"] is False
    assert "error" in applied[0] and "ambiguous" not in applied[0]


# -- benching one symbol, not the whole book -------------------------------


def _account(**overrides):
    base = dict(
        balance=10_000.0,
        equity=10_000.0,
        available_margin=10_000.0,
        peak_equity=10_000.0,
        daily_realized_pnl=0.0,
        open_pnl=0.0,
        trades_today=0,
        trades_this_session=0,
        consecutive_losses=0,
        open_positions=[],
    )
    base.update(overrides)
    return AccountRiskState(**base)


@pytest.fixture()
def candidate(config, broker):
    from bot.marketdata.provider import MarketDataProvider
    from bot.smc.engine import SmcEngine

    series = MarketDataProvider(broker, config).multi_timeframe(DEFAULT_SPEC, now=SETUP_END)
    result = SmcEngine(config).analyze("EURUSD", series, now=SETUP_END)
    assert result.candidate is not None, result.rejection
    return result.candidate


def _locked_config(config, losses=2):
    return dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, symbol_lock_losses=losses)
    )


def test_the_symbol_guard_is_off_unless_configured(config, candidate):
    """Rule 12: it ships ready and stays off until evidence earns it."""

    assert config.risk.symbol_lock_losses == 0
    decision = RiskEngine(config).evaluate(
        candidate=candidate,
        tier="A",
        account=_account(recent_by_symbol={"EURUSD": {"closed": 9, "losses": 9, "net": -900.0}}),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not any("benched" in reason for reason in decision.reasons)


def test_a_losing_symbol_is_benched(config, candidate):
    decision = RiskEngine(_locked_config(config)).evaluate(
        candidate=candidate,
        tier="A",
        account=_account(
            recent_by_symbol={"EURUSD": {"closed": 3, "losses": 3, "net": -150.0}}
        ),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not decision.approved
    assert any("EURUSD is benched" in reason for reason in decision.reasons)


def test_one_symbols_losses_do_not_bench_another(config, candidate):
    """The whole point. Every other guard here is global; this one is not."""

    decision = RiskEngine(_locked_config(config)).evaluate(
        candidate=candidate,
        tier="A",
        account=_account(
            recent_by_symbol={"USDCHF": {"closed": 5, "losses": 5, "net": -400.0}}
        ),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not any("benched" in reason for reason in decision.reasons), (
        "USDCHF losing must not stop EURUSD trading"
    )


def test_an_unreadable_record_stands_down_rather_than_benching_everything(config, candidate):
    """It may only ever refuse a trade, so silence must mean "no evidence
    to refuse on" — not "refuse everything"."""

    decision = RiskEngine(_locked_config(config)).evaluate(
        candidate=candidate,
        tier="A",
        account=_account(recent_by_symbol={}),
        spec=DEFAULT_SPEC,
        now=SETUP_END,
    )
    assert not any("benched" in reason for reason in decision.reasons)


def test_an_unmeasured_close_is_not_counted_as_a_loss(repos):
    """Rule 6 again: a close we could not price is not a break-even close."""

    moment = utc_now()
    for index, pnl in enumerate([-10.0, None, 5.0]):
        execution_id = f"exec-{index}"
        repos.trades.create_pending(
            execution_id=execution_id,
            plan={
                "execution_id": execution_id,
                "symbol": "EURUSD",
                "direction": "BUY",
                "quantity": 0.1,
                "entry": 1.1,
                "stop_loss": 1.09,
                "take_profit": 1.12,
            },
        )
        repos.trades.mark_open(
            execution_id,
            broker_position_id=f"pos-{index}",
            broker_order_id=f"ord-{index}",
            actual_entry=1.1,
            quantity=0.1,
        )
        repos.trades.mark_closed(
            broker_position_id=f"pos-{index}",
            exit_price=1.09 if pnl is not None else None,
            realized_pnl=pnl,
            exit_reason="STOP" if pnl is not None else "BROKER_CLOSED_PNL_UNKNOWN",
        )

    summary = repos.trades.losses_by_symbol_since(moment - timedelta(hours=1))["EURUSD"]
    assert summary["closed"] == 3
    assert summary["losses"] == 1, "only the measured negative counts"
    assert summary["unmeasured"] == 1
    assert summary["net"] == pytest.approx(-5.0), "the unmeasured close is excluded, not zeroed"
