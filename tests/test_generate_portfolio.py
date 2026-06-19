"""
Unit tests for utils/generators/generate_portfolio.py — the Claude Model
Portfolio HTML builder that runs inside alpaca_monitor.py.
"""

import json
import unittest

from utils.generators import generate_portfolio as gp


class FormatterTests(unittest.TestCase):
    def test_fmt_money(self):
        self.assertEqual(gp._fmt_money(1234), "$1,234")
        self.assertEqual(gp._fmt_money(-9876), "-$9,876")
        self.assertEqual(gp._fmt_money(0), "$0")

    def test_fmt_pct(self):
        self.assertEqual(gp._fmt_pct(1.23), "+1.23%")
        self.assertEqual(gp._fmt_pct(-4.56), "-4.56%")
        self.assertEqual(gp._fmt_pct(0), "+0.00%")


class HeatClassTests(unittest.TestCase):
    def test_bins(self):
        self.assertEqual(gp._heat_class(5.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(10.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(4.9), "heat-pos")
        self.assertEqual(gp._heat_class(0.01), "heat-pos")
        self.assertEqual(gp._heat_class(0), "heat-zero")
        self.assertEqual(gp._heat_class(-0.5), "heat-neg")
        self.assertEqual(gp._heat_class(-4.99), "heat-neg")
        self.assertEqual(gp._heat_class(-5.0), "heat-neg-strong")
        self.assertEqual(gp._heat_class(-12.0), "heat-neg-strong")


class EquityCurveTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(gp.build_equity_curve_js({}), "[]")
        self.assertEqual(gp.build_equity_curve_js(None), "[]")

    def test_valid(self):
        hist = {
            "timestamp": [1704067200, 1704153600],  # 2024-01-01, 2024-01-02
            "equity":    [100000.0, 101234.56],
        }
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["x"], "2024-01-01")
        self.assertEqual(out[0]["y"], 100000.0)
        self.assertEqual(out[1]["y"], 101234.56)

    def test_skips_none_equity(self):
        hist = {"timestamp": [1704067200, 1704153600], "equity": [None, 100.0]}
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["y"], 100.0)


class InceptionEquityTests(unittest.TestCase):
    def test_first_nonzero(self):
        self.assertEqual(gp.inception_equity({"equity": [100000.0, 105000.0]}), 100000.0)

    def test_skips_leading_none_and_zero(self):
        self.assertEqual(gp.inception_equity({"equity": [None, 0, 90000.0, 95000.0]}), 90000.0)

    def test_empty(self):
        self.assertEqual(gp.inception_equity({}), 0.0)
        self.assertEqual(gp.inception_equity(None), 0.0)
        self.assertEqual(gp.inception_equity({"equity": []}), 0.0)


class TotalReturnTests(unittest.TestCase):
    def test_gain(self):
        start, abs_ret, pct = gp.compute_total_return({"equity": [100000.0, 110000.0]}, 110000.0)
        self.assertEqual(start, 100000.0)
        self.assertEqual(abs_ret, 10000.0)
        self.assertAlmostEqual(pct, 10.0)

    def test_loss(self):
        start, abs_ret, pct = gp.compute_total_return({"equity": [100000.0]}, 92000.0)
        self.assertEqual(abs_ret, -8000.0)
        self.assertAlmostEqual(pct, -8.0)

    def test_no_history_zeroed(self):
        self.assertEqual(gp.compute_total_return({}, 50000.0), (0.0, 0.0, 0.0))


class MonthlyPerformanceTests(unittest.TestCase):
    # 2026-01-31, 2026-02-28, 2026-03-31 (UTC) daily-bar timestamps
    JAN = 1769817600  # 2026-01-31
    FEB = 1772236800  # 2026-02-28
    MAR = 1774915200  # 2026-03-31

    def test_month_over_month_pnl_and_pct(self):
        hist = {"timestamp": [self.JAN, self.FEB, self.MAR],
                "equity":    [100000.0, 110000.0, 104500.0]}
        out = gp.monthly_performance(hist)
        self.assertEqual([m["month"] for m in out], ["Jan 2026", "Feb 2026", "Mar 2026"])
        # First month: start = its own first point → pnl 0
        self.assertEqual(out[0]["pnl"], 0.0)
        # Feb: 110k vs Jan-end 100k = +10k / +10%
        self.assertEqual(out[1]["pnl"], 10000.0)
        self.assertAlmostEqual(out[1]["pct"], 10.0)
        # Mar: 104.5k vs Feb-end 110k = -5.5k / -5%
        self.assertEqual(out[2]["pnl"], -5500.0)
        self.assertAlmostEqual(out[2]["pct"], -5.0)

    def test_multiple_points_per_month_uses_month_end(self):
        # two Jan points + one Feb point → Jan end is the later Jan value
        hist = {"timestamp": [self.JAN - 86400, self.JAN, self.FEB],
                "equity":    [100000.0, 102000.0, 108000.0]}
        out = gp.monthly_performance(hist)
        self.assertEqual(out[0]["end"], 102000.0)
        self.assertEqual(out[1]["pnl"], 6000.0)  # 108k - 102k

    def test_empty(self):
        self.assertEqual(gp.monthly_performance({}), [])
        self.assertEqual(gp.monthly_performance({"equity": []}), [])


def _fill(sym, side, qty, price, ts):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": ts}


class ClosedTradesTests(unittest.TestCase):
    def test_simple_round_trip(self):
        fills = [_fill("VIK", "buy", 100, 80.0, "2026-05-01T14:00:00Z"),
                 _fill("VIK", "sell", 100, 88.0, "2026-05-10T14:00:00Z")]
        out = gp.closed_trades(fills)
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual(t["symbol"], "VIK")
        self.assertEqual(t["qty"], 100)
        self.assertEqual(t["pnl"], 800.0)
        self.assertEqual(t["pct"], 10.0)
        self.assertEqual(t["hold_days"], 9)

    def test_partial_fills_aggregate_per_exit_date(self):
        # 3 partial sells on the same day collapse into one trade row
        fills = [_fill("SNDK", "buy", 24, 1000.0, "2026-04-28T14:00:00Z"),
                 _fill("SNDK", "sell", 18, 1400.0, "2026-05-06T14:00:00Z"),
                 _fill("SNDK", "sell", 2, 1400.0, "2026-05-06T14:01:00Z"),
                 _fill("SNDK", "sell", 4, 1400.0, "2026-05-06T14:02:00Z")]
        out = gp.closed_trades(fills)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["qty"], 24)
        self.assertEqual(out[0]["pnl"], 9600.0)

    def test_fifo_across_lots_and_weighted_entry(self):
        fills = [_fill("MU", "buy", 10, 100.0, "2026-05-01T14:00:00Z"),
                 _fill("MU", "buy", 10, 120.0, "2026-05-02T14:00:00Z"),
                 _fill("MU", "sell", 20, 130.0, "2026-05-08T14:00:00Z")]
        out = gp.closed_trades(fills)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["avg_entry"], 110.0)
        self.assertEqual(out[0]["pnl"], 400.0)
        self.assertEqual(out[0]["entry_date"], "2026-05-01")

    def test_since_filter_drops_old_exits(self):
        fills = [_fill("ALGM", "buy", 10, 40.0, "2023-02-08T14:00:00Z"),
                 _fill("ALGM", "sell", 10, 50.0, "2023-06-01T14:00:00Z"),
                 _fill("VIK", "buy", 10, 80.0, "2026-05-01T14:00:00Z"),
                 _fill("VIK", "sell", 10, 88.0, "2026-05-10T14:00:00Z")]
        out = gp.closed_trades(fills, since="2026-01-01")
        self.assertEqual([t["symbol"] for t in out], ["VIK"])

    def test_sell_without_lot_is_skipped(self):
        fills = [_fill("XYZ", "sell", 10, 50.0, "2026-05-01T14:00:00Z")]
        self.assertEqual(gp.closed_trades(fills), [])

    def test_newest_first(self):
        fills = [_fill("A", "buy", 1, 10.0, "2026-05-01T14:00:00Z"),
                 _fill("A", "sell", 1, 11.0, "2026-05-02T14:00:00Z"),
                 _fill("B", "buy", 1, 10.0, "2026-05-03T14:00:00Z"),
                 _fill("B", "sell", 1, 9.0, "2026-05-04T14:00:00Z")]
        out = gp.closed_trades(fills)
        self.assertEqual([t["symbol"] for t in out], ["B", "A"])


class TradeStatsTests(unittest.TestCase):
    def test_stats(self):
        trades = [{"pnl": 1000.0}, {"pnl": 500.0}, {"pnl": -300.0}]
        s = gp.trade_stats(trades)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["win_rate"], 66.7)
        self.assertEqual(s["net"], 1200.0)
        self.assertEqual(s["avg_win"], 750.0)
        self.assertEqual(s["avg_loss"], -300.0)
        self.assertEqual(s["payoff"], 2.5)

    def test_empty(self):
        s = gp.trade_stats([])
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["payoff"], 0.0)


class GenerateHtmlTests(unittest.TestCase):
    def test_smoke_with_position(self):
        account = {"equity": "100000", "last_equity": "99500",
                   "cash": "20000", "buying_power": "20000"}
        positions = [{
            "symbol": "NVDA", "qty": "10",
            "avg_entry_price": "100", "current_price": "110",
            "market_value": "1100", "cost_basis": "1000",
            "unrealized_pl": "100", "unrealized_plpc": "0.10",
        }]
        history = {"timestamp": [1704067200, 1704153600], "equity": [90000.0, 100000.0]}
        trades = [{"symbol": "MU", "entry_date": "2026-04-30",
                   "exit_date": "2026-05-07", "qty": 47, "avg_entry": 504.19,
                   "avg_exit": 647.34, "pnl": 6728.0, "pct": 28.4,
                   "hold_days": 7}]
        html = gp.generate_html(account, positions, history, trades=trades)
        self.assertIn("Claude Model Portfolio", html)
        self.assertIn("Equity Curve", html)
        self.assertIn("Total Return", html)
        self.assertIn("since $90,000 start", html)
        self.assertIn("Month-over-Month Performance", html)
        self.assertIn("NVDA", html)
        self.assertIn("heat-pos", html)
        self.assertIn("1.1%", html)
        self.assertIn("$500", html)
        self.assertIn("Trade History", html)
        self.assertIn("1 closed trades", html)
        # open positions section renders before the MoM panel
        self.assertLess(html.index("Open Positions"),
                        html.index("Month-over-Month Performance"))

    def test_no_trades_renders_empty_state(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "100000", "buying_power": "100000"}
        html = gp.generate_html(account, [], {})
        self.assertIn("No closed trades yet", html)

    def test_total_return_unavailable_without_history(self):
        account = {"equity": "100000", "last_equity": "99500",
                   "cash": "20000", "buying_power": "20000"}
        html = gp.generate_html(account, [], {})
        self.assertIn("history unavailable", html)

    def test_empty_positions(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "100000", "buying_power": "100000"}
        html = gp.generate_html(account, [], {})
        self.assertIn("No open positions", html)


class OpenEntryDatesTests(unittest.TestCase):
    def _f(self, sym, side, qty, price, date):
        return {"symbol": sym, "side": side, "qty": str(qty),
                "price": str(price), "transaction_time": f"{date}T14:00:00Z"}

    def test_open_lot_reports_earliest_date(self):
        fills = [self._f("NVDA", "buy", 5, 200, "2026-05-10"),
                 self._f("NVDA", "buy", 5, 210, "2026-05-20")]
        self.assertEqual(gp.open_entry_dates(fills), {"NVDA": "2026-05-10"})

    def test_fully_closed_symbol_excluded(self):
        fills = [self._f("AAPL", "buy", 10, 100, "2026-06-02"),
                 self._f("AAPL", "sell", 10, 120, "2026-06-15")]
        self.assertNotIn("AAPL", gp.open_entry_dates(fills))

    def test_partial_close_keeps_remaining_lot_date(self):
        fills = [self._f("TSLA", "buy", 10, 100, "2026-04-01"),
                 self._f("TSLA", "buy", 10, 110, "2026-04-10"),
                 self._f("TSLA", "sell", 10, 130, "2026-05-01")]
        # first lot consumed FIFO → earliest remaining is the 04-10 lot
        self.assertEqual(gp.open_entry_dates(fills), {"TSLA": "2026-04-10"})


class TradeHistoryInteractivityTests(unittest.TestCase):
    def test_sort_and_filter_hooks_present(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "5000", "buying_power": "10000"}
        trades = [{"symbol": "AAA", "entry_date": "2026-06-02",
                   "exit_date": "2026-06-15", "qty": 10, "avg_entry": 100.0,
                   "avg_exit": 120.0, "pnl": 200.0, "pct": 20.0, "hold_days": 13}]
        html = gp.generate_html(account, [], {}, trades=trades)
        self.assertIn("sortTable", html)
        self.assertIn("filterMonth", html)
        self.assertIn("data-month='2026-06'", html)

    def test_open_position_entry_date_column(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "5000", "buying_power": "10000"}
        pos = [{"symbol": "NVDA", "qty": "5", "avg_entry_price": "200",
                "current_price": "210", "market_value": "1050",
                "cost_basis": "1000", "unrealized_pl": "50",
                "unrealized_plpc": "0.05"}]
        html = gp.generate_html(account, pos, {}, entry_dates={"NVDA": "2026-05-10"})
        self.assertIn("Entry Date", html)
        self.assertIn("2026-05-10", html)


if __name__ == "__main__":
    unittest.main()
