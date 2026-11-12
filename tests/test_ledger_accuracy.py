"""Ledger-vs-broker accuracy: dividend dedupe, fill parsing, commission floor.

Regression tests for the 2026-11 live cash drift (local cash above the
broker's): duplicate dividend credits, BUY fills recorded at signal price,
and an under-modeled commission. No network.
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from kabu_trader import corporate_actions as ca
from kabu_trader.brokers import ibkr_webapi
from kabu_trader.paper_trader import PaperTrader, Position


def _jp_dividends():
    # yfinance stamps JP events at 09:00 Asia/Tokyo — the exact shape that
    # defeated the old datetime compare.
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-29 09:00", tz="Asia/Tokyo")])
    return pd.Series([23.75], index=idx)


class EventsAfterTests(unittest.TestCase):
    def test_bought_before_ex_date_gets_dividend(self):
        out = ca._events_after(_jp_dividends(), datetime(2026, 9, 8, 10, 23), "amount")
        self.assertEqual(out, [{"date": "2026-09-29", "amount": 23.75}])

    def test_watermark_on_ex_date_does_not_rematch(self):
        # last_adjusted_at is stored as a bare date after applying.
        out = ca._events_after(_jp_dividends(), datetime(2026, 9, 29), "amount")
        self.assertEqual(out, [])

    def test_bought_on_ex_date_not_entitled(self):
        out = ca._events_after(_jp_dividends(), datetime(2026, 9, 29, 10, 0), "amount")
        self.assertEqual(out, [])


def _trader(tmp, **overrides):
    cfg = {"initial_capital": 500000, "commission_rate": 0.00088,
           "commission_min": 88, "dividend_tax_rate": 0.20315,
           "position_size_pct": 0.1, "max_positions": 3}
    cfg.update(overrides)
    t = PaperTrader(cfg, state_dir=Path(tmp))
    t.cash = 500000.0
    return t


class DividendTests(unittest.TestCase):
    def test_dividend_applied_once_across_daily_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = _trader(tmp)
            t.positions["8309.T"] = Position(
                "8309.T", "SMTH", 1781.0, 100, "2026-09-08 10:23:13", 4, [])
            tracker = ca.CorporateActionsTracker()
            with mock.patch.object(ca.yf, "Ticker") as tk, \
                    mock.patch.object(ca.rate_limit, "is_cooling_down", return_value=False):
                tk.return_value.splits = pd.Series(dtype=float)
                tk.return_value.dividends = _jp_dividends()
                credited = 0
                for _ in range(4):  # four daily checks while still held
                    pos = t.positions["8309.T"]
                    res = tracker.get_actions_since("8309.T", pos.last_adjusted_at)
                    if res and res["dividends"]:
                        credited += len(t.apply_corporate_actions({"8309.T": res}))
            self.assertEqual(credited, 1)
            # Net of 20.315% withholding: 2375 × 0.79685
            self.assertAlmostEqual(t.cash - 500000.0, 2375 * (1 - 0.20315), places=6)
            self.assertAlmostEqual(t.positions["8309.T"].entry_price, 1781.0 - 23.75)


class CommissionTests(unittest.TestCase):
    def test_floor_applies_to_small_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = _trader(tmp)
            self.assertEqual(t._commission(66180), 88)          # 58.2 → floor
            self.assertAlmostEqual(t._commission(326900), 287.672)  # rate wins
            self.assertAlmostEqual(t._commission(100000), 88.0)  # boundary
            self.assertEqual(t._commission(0), 0.0)

    def test_default_config_keeps_flat_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = PaperTrader({"commission_rate": 0.0005}, state_dir=Path(tmp))
            self.assertAlmostEqual(t._commission(66180), 33.09)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class FillParseTests(unittest.TestCase):
    def _broker(self, place_reply, status_replies):
        b = ibkr_webapi.IBKRWebAPIBroker.__new__(ibkr_webapi.IBKRWebAPIBroker)
        b.account_id = "U0"
        b._lock = mock.MagicMock()
        client = mock.MagicMock()
        client.place_order.return_value = _FakeResult([place_reply])
        client.order_status.side_effect = [_FakeResult(r) for r in status_replies]
        b._ensure = lambda: client
        b._ensure_brokerage_session = lambda c=None: None
        b._resolve_conid = lambda t: 123
        b._answers = lambda: {"": True}
        return b

    def _place(self, b):
        fake_ibind = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"ibind": fake_ibind}), \
                mock.patch.object(ibkr_webapi.time, "sleep"):
            return b.place_order("6762.T", "BUY", 100)

    def test_cum_fill_is_read(self):
        # Shape from the IBKR docs for /iserver/account/order/status/{id}.
        b = self._broker({"order_id": "1", "order_status": "Submitted"},
                         [{"order_status": "Filled", "cum_fill": "100.0",
                           "average_price": "3269.0"}])
        r = self._place(b)
        self.assertEqual((r["filled_shares"], r["avg_fill_price"]), (100, 3269.0))

    def test_terminal_status_in_place_reply_still_polls(self):
        b = self._broker({"order_id": "1", "order_status": "Filled"},
                         [{"order_status": "Filled", "cum_fill": "100.0",
                           "average_price": "3269.0"}])
        r = self._place(b)
        self.assertEqual((r["filled_shares"], r["avg_fill_price"]), (100, 3269.0))

    def test_filled_without_quantity_assumes_full(self):
        b = self._broker({"order_id": "1", "order_status": "Submitted"},
                         [{"order_status": "Filled", "average_price": "3269.0"}])
        r = self._place(b)
        self.assertEqual(r["filled_shares"], 100)


class LiveBuyRecordsFillTests(unittest.TestCase):
    def _buy(self, order_result):
        with tempfile.TemporaryDirectory() as tmp:
            broker = mock.MagicMock()
            broker.place_order.return_value = order_result
            t = _trader(tmp)
            t.live_broker = broker
            trade = t._buy("6762.T", "TDK", 3353.0, 4, [], "2026-10-09 09:17:33")
            return t, trade

    def test_records_actual_fill_not_signal(self):
        t, trade = self._buy({"status": "Filled", "filled_shares": 100,
                              "avg_fill_price": 3269.0})
        self.assertEqual(trade["price"], 3269.0)
        self.assertEqual(t.positions["6762.T"].entry_price, 3269.0)
        self.assertAlmostEqual(trade["cost"], 326900 + 287.672)

    def test_missing_fill_price_never_books_zero_entry(self):
        t, trade = self._buy({"status": "Filled", "filled_shares": 100,
                              "avg_fill_price": 0})
        self.assertEqual(t.positions["6762.T"].entry_price, 3353.0)


if __name__ == "__main__":
    unittest.main()
