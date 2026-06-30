"""Unit tests for the shared portfolio rendering/analytics module."""

import unittest

from utils.generators import portfolio_common as pc


def _ev(sym, side, qty, price, date):
    return {"symbol": sym, "side": side, "qty": qty, "price": price, "date": date}


class ClosedTradesTests(unittest.TestCase):
    def test_round_trip(self):
        ev = [_ev("AAA", "buy", 10, 100, "2026-06-02"),
              _ev("AAA", "sell", 10, 120, "2026-06-15")]
        t = pc.closed_trades(ev)
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["symbol"], "AAA")
        self.assertEqual(t[0]["pnl"], 200.0)
        self.assertEqual(t[0]["pct"], 20.0)
        self.assertEqual(t[0]["hold_days"], 13)

    def test_fifo_weighted_entry(self):
        ev = [_ev("X", "buy", 10, 100, "2026-01-01"),
              _ev("X", "buy", 10, 200, "2026-01-05"),
              _ev("X", "sell", 20, 300, "2026-02-01")]
        t = pc.closed_trades(ev)
        self.assertEqual(t[0]["avg_entry"], 150.0)

    def test_since_filter(self):
        ev = [_ev("X", "buy", 1, 10, "2025-12-01"),
              _ev("X", "sell", 1, 12, "2025-12-15")]
        self.assertEqual(pc.closed_trades(ev, since="2026-01-01"), [])

    def test_sell_without_lot_skipped(self):
        ev = [_ev("X", "sell", 5, 10, "2026-01-01")]
        self.assertEqual(pc.closed_trades(ev), [])


class OpenEntryDatesTests(unittest.TestCase):
    def test_open_lot_earliest_date(self):
        ev = [_ev("N", "buy", 5, 200, "2026-05-10"),
              _ev("N", "buy", 5, 210, "2026-05-20")]
        self.assertEqual(pc.open_entry_dates(ev), {"N": "2026-05-10"})

    def test_closed_excluded(self):
        ev = [_ev("A", "buy", 10, 100, "2026-06-02"),
              _ev("A", "sell", 10, 120, "2026-06-15")]
        self.assertNotIn("A", pc.open_entry_dates(ev))


class ClassifyActionTests(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(pc.classify_action(-6, 3), "cut")
        self.assertEqual(pc.classify_action(22, 3), "peel")
        self.assertEqual(pc.classify_action(11, 8), "trail")   # NOT peel
        self.assertEqual(pc.classify_action(8, 8), "peel")     # high-vol peel ⅓
        self.assertEqual(pc.classify_action(0.4, 3, held=5), "dead")
        self.assertEqual(pc.classify_action(0.4, 3, held=0), "hold")  # new buy, not dead
        self.assertEqual(pc.classify_action(5, 3), "hold")


class MonthlyRealizedTests(unittest.TestCase):
    def test_groups_by_exit_month(self):
        trades = [{"exit_date": "2026-06-15", "pnl": 100.0},
                  {"exit_date": "2026-06-20", "pnl": -40.0},
                  {"exit_date": "2026-05-10", "pnl": 50.0}]
        out = pc.monthly_realized(trades)
        self.assertEqual([m["key"] for m in out], ["2026-05", "2026-06"])
        jun = next(m for m in out if m["key"] == "2026-06")
        self.assertEqual(jun["pnl"], 60.0)
        self.assertEqual(jun["count"], 2)


class RenderTests(unittest.TestCase):
    def _row(self, t, gain, atr=3.0, mv=1000.0):
        return {"ticker": t, "shares": 100, "avg": 10.0, "live": 11.0,
                "gain": gain, "pl": 100.0, "mv": mv, "entry_date": "2026-06-01",
                "held": "18d", "atr": atr, "s20": 1.0, "stage": "2P"}

    def test_positions_summary_matches_rows(self):
        rows = [self._row("P", 25), self._row("C", -6), self._row("D", 0.4)]
        html = pc.render_positions_section(rows, 100000)
        self.assertIn("PEEL: 1", html)
        self.assertIn("CUT: 1", html)
        self.assertIn("dead weight: 1", html)
        self.assertIn("data-action='peel'", html)
        self.assertIn("price vs 20-day moving average", html)

    def test_high_gain_high_vol_is_trail(self):
        html = pc.render_positions_section([self._row("DAVE", 11.0, atr=8.0)], 100000)
        self.assertIn("PEEL: 0", html)
        self.assertIn("data-action='trail'", html)
        self.assertIn("trail tighter", html)

    def test_trade_history_sortable_and_filterable(self):
        trades = [{"symbol": "AAA", "entry_date": "2026-06-02",
                   "exit_date": "2026-06-15", "qty": 10, "avg_entry": 100.0,
                   "avg_exit": 120.0, "pnl": 200.0, "pct": 20.0, "hold_days": 13}]
        html = pc.render_trade_history(trades)
        self.assertIn("data-month='2026-06'", html)
        self.assertIn("sortTable", html)
        self.assertIn("1 closed trades", html)

    def test_empty_states(self):
        self.assertIn("No open positions", pc.render_positions_section([], 100000))
        self.assertIn("No closed trades", pc.render_trade_history([]))


if __name__ == "__main__":
    unittest.main()
