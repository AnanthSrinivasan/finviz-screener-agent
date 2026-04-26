"""Unit tests for utils/generate_performance.py — FIFO P&L engine."""
import datetime
import io
import csv
import os
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.generate_performance import compute_trades, compute_stats


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


if __name__ == "__main__":
    unittest.main()
