"""Unit tests for tradelocker_state.py's trade-accounting logic.

These use synthetic TradeLocker-shaped dicts (no network calls), so they
run anywhere, including CI, via:
    python3 -m unittest discover -s tests -v

They lock in the position-grouping fix: a completed round-trip trade
must be counted once, not once per FILLED order row (open fill + close
fill) — see the module docstring in tradelocker_state.py for the
TradeLocker API behavior this guards against.
"""

from __future__ import annotations

import unittest

import tradelocker_state as ts


def _order_row(
    *,
    order_id: str,
    position_id: str,
    instrument_id: str = "1",
    side: str = "buy",
    realized_pl: float | None = None,
    created_date: float = 1_700_000_000_000,
    last_modified: float | None = None,
    status: str = "FILLED",
) -> dict:
    return {
        "id": order_id,
        "positionId": position_id,
        "tradableInstrumentId": instrument_id,
        "side": side,
        "status": status,
        "realizedPl": realized_pl,
        "createdDate": created_date,
        "lastModified": last_modified if last_modified is not None else created_date,
        "avgPrice": 1.1000,
    }


class TradeGroupingTests(unittest.TestCase):
    """The core bug this module exists to prevent: TradeLocker reports one
    completed trade as two FILLED order rows (open + close fill) sharing a
    positionId. These must merge into a single logical trade."""

    def test_single_round_trip_counts_as_one_trade(self):
        history = [
            _order_row(
                order_id="order-open-1",
                position_id="pos-1",
                realized_pl=None,
                created_date=1_700_000_000_000,
                last_modified=1_700_000_000_000,
            ),
            _order_row(
                order_id="order-close-1",
                position_id="pos-1",
                realized_pl=42.50,
                created_date=1_700_000_000_000,
                last_modified=1_700_000_060_000,
            ),
        ]
        merged = ts._group_closed_trades_by_position(history)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["realizedPl"], 42.50)
        self.assertEqual(merged[0]["_fillCount"], 2)

    def test_two_separate_positions_count_as_two_trades(self):
        history = [
            _order_row(order_id="a-open", position_id="pos-A", realized_pl=None, created_date=1000),
            _order_row(
                order_id="a-close", position_id="pos-A", realized_pl=10.0,
                created_date=1000, last_modified=2000,
            ),
            _order_row(order_id="b-open", position_id="pos-B", realized_pl=None, created_date=3000),
            _order_row(
                order_id="b-close", position_id="pos-B", realized_pl=-5.0,
                created_date=3000, last_modified=4000,
            ),
        ]
        merged = ts._group_closed_trades_by_position(history)
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(t["realizedPl"] for t in merged), [-5.0, 10.0])

    def test_breakeven_close_with_zero_pnl_still_counts_once(self):
        """A position closed at exactly break-even (realizedPl == 0 on
        every row) must not be dropped, and must still merge into one
        trade rather than falling back to double-counting."""

        history = [
            _order_row(order_id="be-open", position_id="pos-BE", realized_pl=None, created_date=1000),
            _order_row(
                order_id="be-close", position_id="pos-BE", realized_pl=0.0,
                created_date=1000, last_modified=2000,
            ),
        ]
        merged = ts._group_closed_trades_by_position(history)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["realizedPl"], 0.0)

    def test_partial_fills_on_same_position_still_one_trade(self):
        """A position closed via multiple partial fills (3+ FILLED rows
        sharing one positionId) must still merge into a single trade."""

        history = [
            _order_row(order_id="p-open", position_id="pos-P", realized_pl=None, created_date=1000),
            _order_row(
                order_id="p-partial-close-1", position_id="pos-P", realized_pl=5.0,
                created_date=1000, last_modified=2000,
            ),
            _order_row(
                order_id="p-partial-close-2", position_id="pos-P", realized_pl=7.0,
                created_date=1000, last_modified=3000,
            ),
        ]
        merged = ts._group_closed_trades_by_position(history)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["_fillCount"], 3)
        # The most-recently-modified nonzero-PnL row is the canonical
        # closing fill.
        self.assertEqual(merged[0]["realizedPl"], 7.0)

    def test_non_filled_rows_are_excluded(self):
        history = [
            _order_row(order_id="c-1", position_id="pos-C", status="CANCELLED", realized_pl=None),
            _order_row(order_id="r-1", position_id="pos-R", status="REJECTED", realized_pl=None),
        ]
        merged = ts._group_closed_trades_by_position(history)
        self.assertEqual(merged, [])


class NumericParsingTests(unittest.TestCase):
    def test_num_or_none_distinguishes_real_zero_from_missing(self):
        self.assertEqual(ts._num_or_none(0), 0.0)
        self.assertEqual(ts._num_or_none("0"), 0.0)
        self.assertIsNone(ts._num_or_none(None))
        self.assertIsNone(ts._num_or_none(""))
        self.assertIsNone(ts._num_or_none("not-a-number"))


class MissingBalanceTests(unittest.TestCase):
    """Anti-regression test for the removed silent 1000.0 balance
    fallback: build_account must raise, not fabricate a balance, when
    TradeLocker's state response has no usable balance field.

    build_account is now a pure function of already-fetched data (no
    config or HTTP calls involved), so these tests need no mocking —
    that simplification is itself part of the fix that eliminated
    build_account's duplicate get_positions()/get_order_history() calls.
    """

    def test_missing_balance_raises_incomplete_data(self):
        with self.assertRaises(ts.IncompleteAccountData):
            ts.build_account({}, positions=[], history=[])

    def test_unparseable_balance_raises_incomplete_data(self):
        with self.assertRaises(ts.IncompleteAccountData):
            ts.build_account({"balance": "not-a-number"}, positions=[], history=[])

    def test_valid_balance_is_used_directly(self):
        account = ts.build_account({"balance": 5000.0}, positions=[], history=[])
        self.assertEqual(account["balance"], 5000.0)


if __name__ == "__main__":
    unittest.main()
