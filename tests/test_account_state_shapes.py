"""The read that only demo_live makes.

`account_state()` is the one broker call paper mode never reaches when
PAPER_STARTING_BALANCE is set: paper takes its balance from the simulated
ledger instead. So every way this call can fail is invisible until the
operator flips TRADING_MODE to demo_live, at which point the very first
scan aborts with "could not build account state" and the bot looks dead
while paper mode had run for days.

It used to zip `/trade/config` columns over the response itself instead of
going through `_decode` like every other table, which gave it two failure
modes the other tables did not have:

  * a brand that renames `accountDetailsConfig` yielded no columns, an
    empty row, and an error blaming the BALANCE rather than the panel;
  * a brand that returns an already-keyed object made `zip` iterate the
    row's KEYS, so `balance` was read as the string "balance".

Each test below pins one shape.
"""

from __future__ import annotations

import pytest

from bot.broker.tradelocker import TradeLockerBroker
from bot.errors import BrokerError

COLUMNS = {
    "accountDetailsConfig": {
        "columns": [
            {"id": "balance"},
            {"id": "projectedBalance"},
            {"id": "openNetPnL"},
            {"id": "marginAvailable"},
        ]
    }
}


def _broker(config, *, state_payload, trade_config=COLUMNS):
    broker = TradeLockerBroker(config)
    broker._account_meta = {"currency": "USD"}
    broker.trade_config = lambda: trade_config  # type: ignore[assignment]
    broker.get = lambda path, query=None: state_payload  # type: ignore[assignment]
    return broker


def test_a_positional_row_is_zipped_over_the_configured_columns(config):
    broker = _broker(
        config, state_payload={"accountDetailsData": [5000.0, 5012.5, 12.5, 4800.0]}
    )
    state = broker.account_state()
    assert state.balance == 5000.0
    assert state.equity == 5012.5
    assert state.open_pnl == 12.5
    assert state.margin_available == 4800.0


def test_a_row_already_keyed_by_the_broker_is_read_as_named_fields(config):
    """Zipping column names over a mapping iterates its KEYS.

    Before the fix this produced balance == "balance" — a string that
    `_num` refuses, so the account looked unreadable on a brand whose
    response was in fact perfectly well formed.
    """

    broker = _broker(
        config,
        state_payload={
            "accountDetailsData": {
                "balance": 5000.0,
                "projectedBalance": 4990.0,
                "openNetPnL": -10.0,
                "marginAvailable": 4700.0,
            }
        },
    )
    state = broker.account_state()
    assert state.balance == 5000.0
    assert state.equity == 4990.0
    assert state.open_pnl == -10.0


def test_a_single_row_wrapped_in_a_list_of_rows_is_unwrapped(config):
    broker = _broker(
        config, state_payload={"accountDetailsData": [[5000.0, 5000.0, 0.0, 5000.0]]}
    )
    assert broker.account_state().balance == 5000.0


def test_a_renamed_panel_names_the_panel_not_the_balance(config):
    """The failure message must point at the actual culprit.

    CLAUDE.md rule 1: a message that names the wrong field costs the next
    operator a deploy cycle. "no usable balance" sent one person looking at
    their account funding when the real problem was a config panel name.
    """

    broker = _broker(
        config,
        state_payload={"accountDetailsData": [5000.0, 5000.0, 0.0, 5000.0]},
        trade_config={"accountSummaryConfig": {"columns": [{"id": "balance"}]}},
    )
    with pytest.raises(BrokerError) as excinfo:
        broker.account_state()
    message = str(excinfo.value)
    assert "accountDetailsConfig" in message
    assert "accountSummaryConfig" in message


def test_a_balance_that_is_genuinely_absent_still_refuses_a_placeholder(config):
    broker = _broker(
        config,
        state_payload={"accountDetailsData": {"marginAvailable": 4700.0}},
    )
    with pytest.raises(BrokerError) as excinfo:
        broker.account_state()
    message = str(excinfo.value)
    # Rule 6: never fabricate. And say which fields were looked at.
    assert "balance" in message and "accountBalance" in message
    assert "marginAvailable" in message


def test_an_empty_response_does_not_report_a_zero_balance(config):
    broker = _broker(config, state_payload={"accountDetailsData": []})
    with pytest.raises(BrokerError, match="no fields at all"):
        broker.account_state()
