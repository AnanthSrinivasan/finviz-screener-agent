"""Unit tests for compute_pnl_from_events + paginated fetch_position_history."""
import unittest
from unittest.mock import patch

from utils.pnl_walk import compute_pnl_from_events


class ComputePnlFromEventsTests(unittest.TestCase):
    def test_empty_events(self):
        r = compute_pnl_from_events([], current_price=100, current_shares=0)
        self.assertEqual(r["realized"], 0)
        self.assertEqual(r["unrealized"], 0)
        self.assertEqual(r["final_shares"], 0)

    def test_pure_buy_unrealized_only(self):
        events = [{"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 100}]
        r = compute_pnl_from_events(events, current_price=110, current_shares=10)
        self.assertEqual(r["realized"], 0)
        self.assertAlmostEqual(r["unrealized"], 100.0)
        self.assertAlmostEqual(r["avg_cost"], 100.0)

    def test_avg_up_then_partial_close(self):
        events = [
            {"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 100},
            {"date": "2026-05-02", "action": "BUY", "shares": 10, "price": 120},  # avg=110
            {"date": "2026-05-03", "action": "SELL", "shares": 5, "price": 130},  # realized = 5*(130-110)=100
        ]
        r = compute_pnl_from_events(events, current_price=130, current_shares=15)
        self.assertAlmostEqual(r["realized"], 100.0)
        self.assertAlmostEqual(r["avg_cost"], 110.0)
        self.assertAlmostEqual(r["final_shares"], 15.0)
        # unrealized: 15*(130-110)=300
        self.assertAlmostEqual(r["unrealized"], 300.0)

    def test_full_close(self):
        events = [
            {"date": "2026-05-01", "action": "BUY", "shares": 80, "price": 50},
            {"date": "2026-05-10", "action": "SELL", "shares": 30, "price": 60},  # realized=30*(60-50)=300
            {"date": "2026-05-12", "action": "SELL", "shares": 50, "price": 70},  # realized+=50*(70-50)=1000 -> 1300
        ]
        r = compute_pnl_from_events(events)
        self.assertAlmostEqual(r["realized"], 1300.0)
        self.assertEqual(r["final_shares"], 0)
        self.assertEqual(r["total_bought_units"], 80)
        self.assertEqual(r["total_sold_units"], 80)

    def test_aaoi_class_recovery(self):
        # 80 bought across two adds, sold down to 10 — realized P/L should be substantial.
        events = [
            {"date": "2026-04-01", "action": "BUY", "shares": 40, "price": 20},
            {"date": "2026-04-08", "action": "BUY", "shares": 40, "price": 25},  # avg=22.5
            {"date": "2026-04-20", "action": "SELL", "shares": 35, "price": 35},  # realized=35*(35-22.5)=437.5
            {"date": "2026-05-01", "action": "SELL", "shares": 35, "price": 40},  # realized+=35*(40-22.5)=612.5 -> 1050
        ]
        r = compute_pnl_from_events(events, current_price=42, current_shares=10)
        self.assertAlmostEqual(r["realized"], 1050.0)
        self.assertEqual(r["final_shares"], 10)
        # unrealized = 10*(42-22.5)=195
        self.assertAlmostEqual(r["unrealized"], 195.0)
        self.assertAlmostEqual(r["realized"] + r["unrealized"], 1245.0)


class WalkProceedsCostBasisTests(unittest.TestCase):
    def test_proceeds_and_cost_basis_tracked(self):
        events = [
            {"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 100},
            {"date": "2026-05-02", "action": "BUY", "shares": 10, "price": 120},  # avg=110
            {"date": "2026-05-03", "action": "SELL", "shares": 15, "price": 130},
        ]
        r = compute_pnl_from_events(events)
        # 15 sold at avg 110, proceeds 15*130=1950, cost_basis 15*110=1650, realized 300
        self.assertAlmostEqual(r["proceeds_sold"], 1950.0)
        self.assertAlmostEqual(r["cost_basis_sold"], 1650.0)
        self.assertAlmostEqual(r["realized"], 300.0)

    def test_full_close_cost_basis_nonzero(self):
        # Reproduces the FLY bug: fully-closed position must report cost > 0.
        events = [
            {"date": "2026-04-01", "action": "BUY", "shares": 100, "price": 50},
            {"date": "2026-04-10", "action": "SELL", "shares": 100, "price": 40},
        ]
        r = compute_pnl_from_events(events)
        self.assertEqual(r["final_shares"], 0)
        self.assertAlmostEqual(r["cost_basis_sold"], 5000.0)
        self.assertAlmostEqual(r["proceeds_sold"], 4000.0)
        self.assertAlmostEqual(r["realized"], -1000.0)


class CycleSplittingTests(unittest.TestCase):
    def test_two_cycles_separated(self):
        from utils.generate_performance import _split_into_cycles
        events = [
            # Cycle 1
            {"date": "2026-03-16", "action": "BUY", "shares": 400, "price": 24},
            {"date": "2026-03-19", "action": "SELL", "shares": 400, "price": 22},
            # Cycle 2
            {"date": "2026-04-06", "action": "BUY", "shares": 100, "price": 33},
            {"date": "2026-04-24", "action": "SELL", "shares": 100, "price": 35},
        ]
        cycles = _split_into_cycles(events)
        self.assertEqual(len(cycles), 2)
        self.assertEqual(len(cycles[0]), 2)
        self.assertEqual(len(cycles[1]), 2)

    def test_open_position_one_cycle(self):
        from utils.generate_performance import _split_into_cycles
        events = [
            {"date": "2026-04-07", "action": "BUY", "shares": 50, "price": 100},
            {"date": "2026-04-30", "action": "BUY", "shares": 20, "price": 110},
            {"date": "2026-05-11", "action": "SELL", "shares": 25, "price": 130},
        ]
        cycles = _split_into_cycles(events)
        self.assertEqual(len(cycles), 1)


class FetchPositionHistoryPaginationTests(unittest.TestCase):
    def test_paginates_through_multiple_pages(self):
        from agents.trading import position_monitor as pm

        def make_act(i, action="BUY", ticker="AAOI"):
            return {
                "id": f"id-{i}",
                "type": action,
                "symbol": {"symbol": {"symbol": ticker}},
                "price": 10.0 + i,
                "units": 1,
                "trade_date": f"2026-05-{(i % 28) + 1:02d}",
            }

        page1 = [make_act(i) for i in range(200)]
        page2 = [make_act(i) for i in range(200, 350)]
        page3 = []
        responses = [page1, page2, page3]
        calls = []

        def fake_get(path, params=None):
            calls.append(params)
            if responses:
                return responses.pop(0)
            return []

        with patch.object(pm, "snaptrade_get", side_effect=fake_get):
            hist = pm.fetch_position_history(["acct-1"])
        # 350 unique events captured
        self.assertEqual(len(hist.get("AAOI", [])), 350)
        # offsets walked
        offsets = [c.get("offset") for c in calls]
        self.assertEqual(offsets[0], 0)
        self.assertEqual(offsets[1], 200)

    def test_dedups_by_id_across_pages(self):
        from agents.trading import position_monitor as pm

        def make_act(i):
            return {
                "id": f"id-{i}",
                "type": "BUY",
                "symbol": {"symbol": {"symbol": "AAOI"}},
                "price": 10.0,
                "units": 1,
                "trade_date": "2026-05-01",
            }

        # First page is exactly PAGE_LIMIT (200) so loop continues; second page
        # repeats half the same ids — those must be deduped.
        page1 = [make_act(i) for i in range(200)]
        page2 = [make_act(i) for i in range(100, 250)]  # 100 dups + 50 new
        responses = [page1, page2, []]

        def fake_get(path, params=None):
            return responses.pop(0) if responses else []

        with patch.object(pm, "snaptrade_get", side_effect=fake_get):
            hist = pm.fetch_position_history(["acct-1"])
        self.assertEqual(len(hist.get("AAOI", [])), 250)


if __name__ == "__main__":
    unittest.main()
