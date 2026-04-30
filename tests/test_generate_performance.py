"""Unit tests for utils/generate_performance.py — FIFO P&L engine."""
import datetime
import io
import csv
import json
import os
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.generate_performance import compute_trades, compute_stats, load_system_closed, merge_trades


def _rows(*entries):
    """Build row dicts directly (bypass CSV parsing)."""
    result = []
    for date_str, ticker, side, qty, price, amount in entries:
        result.append({
            "date":   datetime.date.fromisoformat(date_str),
            "ticker": ticker,
            "side":   side,
            "qty":    float(qty),
            "price":  float(price),
            "amount": float(amount),
        })
    # Sort: buys before sells on same day
    result.sort(key=lambda r: (r["date"], 0 if r["side"] == "Buy" else 1))
    return result


class FifoMatchingTests(unittest.TestCase):
    def test_simple_round_trip(self):
        rows = _rows(
            ("2026-01-02", "AAPL", "Buy",  100, 150.0, -15000),
            ("2026-01-10", "AAPL", "Sell", 100, 160.0,  16000),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["ticker"], "AAPL")
        self.assertAlmostEqual(t["pnl"], 1000.0, places=0)
        self.assertFalse(t["prior_period"])

    def test_loss_trade(self):
        rows = _rows(
            ("2026-01-02", "SLV", "Buy",  100, 80.0, -8000),
            ("2026-02-01", "SLV", "Sell", 100, 65.0,  6500),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["pnl"], -1500.0, places=0)

    def test_fifo_partial_lots(self):
        # Two buy lots, one sell consuming both
        rows = _rows(
            ("2026-01-02", "X", "Buy",  50, 10.0, -500),
            ("2026-01-05", "X", "Buy",  50, 12.0, -600),
            ("2026-01-10", "X", "Sell", 100, 11.0, 1100),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 1)
        # FIFO: 50@$10 + 50@$12 = cost $1100; proceeds $1100 → P&L ≈ 0
        self.assertAlmostEqual(trades[0]["pnl"], 0.0, places=0)

    def test_prior_period_sell(self):
        # Sell with no matching buy → prior_period=True
        rows = _rows(
            ("2026-01-02", "TSLA", "Sell", 10, 400.0, 4000),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["prior_period"])
        # P&L should be ~0 (estimated cost = sell price)
        self.assertAlmostEqual(trades[0]["pnl"], 0.0, places=0)

    def test_same_day_buy_before_sell(self):
        # Buy and sell on the same day — buy should be processed first
        rows = _rows(
            ("2026-02-04", "LMND", "Sell", 50, 70.0,  3500),
            ("2026-02-04", "LMND", "Buy",  50, 82.0, -4100),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 1)
        # Buy processed first → sell consumes that lot → not prior_period
        self.assertFalse(trades[0]["prior_period"])
        self.assertAlmostEqual(trades[0]["pnl"], 3500 - 4100, places=0)

    def test_multiple_sells_split_lots(self):
        rows = _rows(
            ("2026-01-02", "AMD", "Buy",  25, 246.0, -6150),
            ("2026-01-03", "AMD", "Buy",  15, 252.0, -3780),
            ("2026-01-10", "AMD", "Sell", 10, 290.0,  2900),
            ("2026-01-10", "AMD", "Sell", 10, 295.0,  2950),
            ("2026-01-10", "AMD", "Sell", 20, 288.0,  5760),
        )
        trades = compute_trades(rows)
        self.assertEqual(len(trades), 3)
        total_pnl = sum(t["pnl"] for t in trades)
        # proceeds = 2900+2950+5760 = 11610; cost = 6150+3780 = 9930
        self.assertAlmostEqual(total_pnl, 11610 - 9930, places=0)


class StatsTests(unittest.TestCase):
    def _make_trades(self, pnls):
        return [{"pnl": p, "prior_period": False, "ticker": "X",
                 "sell_date": datetime.date(2026, 1, 1), "pnl_pct": 0} for p in pnls]

    def test_win_rate(self):
        stats = compute_stats(self._make_trades([100, 200, -50, -30]))
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertEqual(stats["n_wins"], 2)
        self.assertEqual(stats["n_losses"], 2)

    def test_profit_factor(self):
        stats = compute_stats(self._make_trades([300, -100]))
        self.assertAlmostEqual(stats["profit_factor"], 3.0, places=2)

    def test_prior_period_excluded_from_stats(self):
        trades = self._make_trades([100, 200])
        trades[0]["prior_period"] = True
        stats = compute_stats(trades)
        self.assertEqual(stats["n_trades"], 1)
        self.assertAlmostEqual(stats["total_pnl"], 200, places=0)
        self.assertAlmostEqual(stats["prior_pnl"], 100, places=0)


class SystemClosedSourceTests(unittest.TestCase):
    def _write_positions(self, closed):
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
        json.dump({"positions": [], "closed_positions": closed}, tf)
        tf.close()
        self.addCleanup(os.unlink, tf.name)
        return tf.name

    def test_load_system_closed_basic(self):
        path = self._write_positions([{
            "ticker": "INDV", "shares": 250,
            "entry_price": 34.0, "entry_date": "2026-04-22",
            "close_price": 36.0, "close_date": "2026-04-29",
            "result_pct": 5.88, "close_source": "snaptrade_fill",
        }])
        trades = load_system_closed(path)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["ticker"], "INDV")
        self.assertTrue(t["system_only"])
        self.assertAlmostEqual(t["pnl"], 500.0, places=1)
        self.assertEqual(t["sell_date"], datetime.date(2026, 4, 29))

    def test_load_system_closed_skips_incomplete(self):
        path = self._write_positions([
            {"ticker": "X", "shares": 10, "entry_price": 1.0, "entry_date": "2026-01-01"},  # no close
        ])
        self.assertEqual(load_system_closed(path), [])

    def test_load_system_closed_missing_file(self):
        self.assertEqual(load_system_closed("/tmp/__nope__.json"), [])

    def test_merge_broker_wins_over_system_match(self):
        broker = [{
            "ticker": "AMD", "sell_date": datetime.date(2026, 4, 22),
            "first_buy": datetime.date(2026, 4, 13), "qty": 25.0,
            "proceeds": 7327.25, "cost": 6151.50, "pnl": 1175.75,
            "pnl_pct": 19.11, "prior_period": False,
        }]
        system = [{
            "ticker": "AMD", "sell_date": datetime.date(2026, 4, 23),  # 1d off → match
            "first_buy": datetime.date(2026, 4, 13), "qty": 25.0,
            "proceeds": 7327.0, "cost": 6151.0, "pnl": 1176.0,
            "pnl_pct": 19.1, "prior_period": False, "system_only": True,
        }]
        merged = merge_trades(broker, system)
        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0]["system_only"])

    def test_merge_appends_system_only_when_no_broker_match(self):
        broker = []
        system = [{
            "ticker": "INDV", "sell_date": datetime.date(2026, 4, 29),
            "first_buy": datetime.date(2026, 4, 22), "qty": 250.0,
            "proceeds": 9000.0, "cost": 8500.0, "pnl": 500.0,
            "pnl_pct": 5.88, "prior_period": False, "system_only": True,
        }]
        merged = merge_trades(broker, system)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["system_only"])


if __name__ == "__main__":
    unittest.main()
