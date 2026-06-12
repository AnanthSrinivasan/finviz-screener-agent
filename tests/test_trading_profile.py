"""
Unit tests for agents/trading/trading_profile.py — the live-account profile
resolution + guard rails (sizing, circuit breakers, order sanity, idempotent
order ids, full-exit policy). Spec: docs/specs/live-alpaca-executor.md.
"""

import unittest

from agents.trading import trading_profile as tp


class ResolveProfileTests(unittest.TestCase):
    def test_default_is_paper(self):
        p = tp.resolve_profile(env={})
        self.assertEqual(p["name"], "paper")
        self.assertFalse(p["is_live"])
        self.assertEqual(p["stops_filename"], "paper_stops.json")
        self.assertEqual(p["state_filename"], "paper_trading_state.json")
        self.assertEqual(p["slack_tag"], "[PAPER]")
        self.assertFalse(p["dry_run"])

    def test_unknown_profile_falls_back_to_paper(self):
        p = tp.resolve_profile(env={"TRADING_PROFILE": "yolo"})
        self.assertEqual(p["name"], "paper")

    def test_paper_uses_paper_env_keys(self):
        env = {
            "ALPACA_API_KEY": "pk", "ALPACA_SECRET_KEY": "ps",
            "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
            "ALPACA_LIVE_API_KEY": "lk", "ALPACA_LIVE_SECRET_KEY": "ls",
        }
        p = tp.resolve_profile(env=env)
        self.assertEqual(p["api_key"], "pk")
        self.assertEqual(p["secret_key"], "ps")

    def test_live_profile_resolution(self):
        env = {
            "TRADING_PROFILE": "live",
            "ALPACA_API_KEY": "pk", "ALPACA_SECRET_KEY": "ps",
            "ALPACA_LIVE_API_KEY": "lk", "ALPACA_LIVE_SECRET_KEY": "ls",
            "ALPACA_LIVE_BASE_URL": "https://api.alpaca.markets/v2",
        }
        p = tp.resolve_profile(env=env)
        self.assertEqual(p["name"], "live")
        self.assertTrue(p["is_live"])
        self.assertEqual(p["api_key"], "lk")
        self.assertEqual(p["secret_key"], "ls")
        self.assertEqual(p["base_url"], "https://api.alpaca.markets/v2")
        self.assertEqual(p["stops_filename"], "live_alpaca_stops.json")
        self.assertEqual(p["state_filename"], "live_alpaca_trading_state.json")
        self.assertIn("LIVE", p["slack_tag"])
        self.assertFalse(p["dry_run"])

    def test_live_base_url_default(self):
        p = tp.resolve_profile(env={"TRADING_PROFILE": "live"})
        self.assertEqual(p["base_url"], "https://api.alpaca.markets/v2")

    def test_dry_run_flag(self):
        for val in ("1", "true", "YES"):
            p = tp.resolve_profile(env={"TRADING_PROFILE": "live", "LIVE_DRY_RUN": val})
            self.assertTrue(p["dry_run"], val)
        for val in ("", "0", "no"):
            p = tp.resolve_profile(env={"TRADING_PROFILE": "live", "LIVE_DRY_RUN": val})
            self.assertFalse(p["dry_run"], repr(val))

    def test_dry_run_never_applies_to_paper(self):
        p = tp.resolve_profile(env={"LIVE_DRY_RUN": "1"})
        self.assertFalse(p["dry_run"])


class ClientOrderIdTests(unittest.TestCase):
    def test_buy_format(self):
        self.assertEqual(tp.make_client_order_id("2026-06-12", "dave"),
                         "live-20260612-DAVE")

    def test_sell_format(self):
        self.assertEqual(tp.make_client_order_id("2026-06-12", "DAVE", side="sell"),
                         "live-sell-20260612-DAVE")

    def test_same_day_same_ticker_is_idempotent(self):
        a = tp.make_client_order_id("2026-06-12", "AAOI")
        b = tp.make_client_order_id("2026-06-12", "AAOI")
        self.assertEqual(a, b)


class LiveAllocationTests(unittest.TestCase):
    def test_base_size_is_equity_over_cap(self):
        # $5k equity / cap 3 × full size = $1666.67
        self.assertAlmostEqual(tp.compute_live_allocation(5000, 1.0, 85),
                               5000 / 3, places=2)

    def test_size_mul_applies(self):
        self.assertAlmostEqual(tp.compute_live_allocation(5000, 0.5, 85),
                               5000 / 3 * 0.5, places=2)

    def test_q_below_60_is_not_a_trade(self):
        self.assertEqual(tp.compute_live_allocation(5000, 1.0, 59.9), 0.0)

    def test_zero_equity_or_mul(self):
        self.assertEqual(tp.compute_live_allocation(0, 1.0, 85), 0.0)
        self.assertEqual(tp.compute_live_allocation(5000, 0.0, 85), 0.0)


class OrderSanityTests(unittest.TestCase):
    QUALIFIED = {"DAVE", "AAOI"}

    def test_ok_order_passes(self):
        ok, reason = tp.live_order_sanity(1500, 5000, "DAVE", self.QUALIFIED)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_below_10_dollar_floor_rejected(self):
        ok, reason = tp.live_order_sanity(9.99, 5000, "DAVE", self.QUALIFIED)
        self.assertFalse(ok)
        self.assertIn("minimum", reason)

    def test_over_60pct_of_equity_rejected(self):
        ok, reason = tp.live_order_sanity(3001, 5000, "DAVE", self.QUALIFIED)
        self.assertFalse(ok)
        self.assertIn("60%", reason)

    def test_exactly_60pct_allowed(self):
        ok, _ = tp.live_order_sanity(3000, 5000, "DAVE", self.QUALIFIED)
        self.assertTrue(ok)

    def test_unqualified_symbol_rejected(self):
        ok, reason = tp.live_order_sanity(1500, 5000, "GME", self.QUALIFIED)
        self.assertFalse(ok)
        self.assertIn("qualified", reason)

    def test_unknown_equity_rejected(self):
        ok, reason = tp.live_order_sanity(1500, 0, "DAVE", self.QUALIFIED)
        self.assertFalse(ok)


class DailyHaltTests(unittest.TestCase):
    def test_minus_3pct_triggers(self):
        self.assertTrue(tp.daily_halt_triggered(4850, 5000))   # -3.0%

    def test_worse_than_minus_3pct_triggers(self):
        self.assertTrue(tp.daily_halt_triggered(4700, 5000))   # -6.0%

    def test_minus_2_9pct_does_not_trigger(self):
        self.assertFalse(tp.daily_halt_triggered(4855.1, 5000))

    def test_flat_or_up_does_not_trigger(self):
        self.assertFalse(tp.daily_halt_triggered(5000, 5000))
        self.assertFalse(tp.daily_halt_triggered(5200, 5000))

    def test_unknown_last_equity_is_safe(self):
        self.assertFalse(tp.daily_halt_triggered(5000, 0))


class DrawdownSuspendTests(unittest.TestCase):
    def test_below_85pct_of_high_water_triggers(self):
        self.assertTrue(tp.drawdown_suspend_triggered(4249, 5000))

    def test_exactly_85pct_does_not_trigger(self):
        self.assertFalse(tp.drawdown_suspend_triggered(4250, 5000))

    def test_no_high_water_yet_is_safe(self):
        self.assertFalse(tp.drawdown_suspend_triggered(5000, 0))

    def test_update_high_water_ratchets(self):
        state = {"high_water_equity": 5000}
        self.assertFalse(tp.update_high_water(state, 4900))
        self.assertEqual(state["high_water_equity"], 5000)
        self.assertTrue(tp.update_high_water(state, 5100))
        self.assertEqual(state["high_water_equity"], 5100)

    def test_update_high_water_seeds_from_zero(self):
        state = {}
        self.assertTrue(tp.update_high_water(state, 5000))
        self.assertEqual(state["high_water_equity"], 5000)


class FullTakeProfitTests(unittest.TestCase):
    def test_plus_30pct_triggers(self):
        self.assertTrue(tp.should_full_take_profit(100.0, 130.0))

    def test_above_30pct_triggers(self):
        self.assertTrue(tp.should_full_take_profit(100.0, 145.0))

    def test_plus_29_9pct_holds(self):
        self.assertFalse(tp.should_full_take_profit(100.0, 129.9))

    def test_bad_prices_are_safe(self):
        self.assertFalse(tp.should_full_take_profit(0, 130.0))
        self.assertFalse(tp.should_full_take_profit(100.0, 0))


class MarketableLimitTests(unittest.TestCase):
    def test_half_percent_markup(self):
        self.assertEqual(tp.marketable_limit(100.0), 100.5)

    def test_rounded_to_cents(self):
        self.assertEqual(tp.marketable_limit(123.45), round(123.45 * 1.005, 2))


class FilterExpiredUnfilledTests(unittest.TestCase):
    def _order(self, **over):
        o = {
            "side": "buy", "symbol": "DAVE", "status": "expired",
            "client_order_id": "live-20260612-DAVE", "filled_qty": "0",
            "expired_at": "2026-06-12T20:00:00Z", "limit_price": "282.50",
        }
        o.update(over)
        return o

    def test_expired_unfilled_live_buy_is_reported(self):
        self.assertEqual(len(tp.filter_expired_unfilled([self._order()])), 1)

    def test_canceled_also_reported(self):
        out = tp.filter_expired_unfilled([self._order(status="canceled",
                                                      canceled_at="2026-06-12T20:00:00Z")])
        self.assertEqual(len(out), 1)

    def test_filled_orders_skipped(self):
        self.assertEqual(tp.filter_expired_unfilled([self._order(status="filled")]), [])

    def test_partial_fill_not_reported_as_unfilled(self):
        self.assertEqual(tp.filter_expired_unfilled([self._order(filled_qty="2")]), [])

    def test_sells_skipped(self):
        self.assertEqual(tp.filter_expired_unfilled([self._order(side="sell")]), [])

    def test_non_agent_orders_skipped(self):
        # Manual order in the account — not ours, never touched
        self.assertEqual(
            tp.filter_expired_unfilled([self._order(client_order_id="abc123")]), [])

    def test_since_dedup(self):
        o = self._order()
        self.assertEqual(
            tp.filter_expired_unfilled([o], since_iso="2026-06-12T21:00:00Z"), [])
        self.assertEqual(
            len(tp.filter_expired_unfilled([o], since_iso="2026-06-12T19:00:00Z")), 1)


class ExecutorLiveWiringTests(unittest.TestCase):
    """The executor module must resolve to paper defaults when TRADING_PROFILE
    is unset — guards against accidentally pointing paper runs at live."""

    def test_executor_defaults_to_paper(self):
        from agents.trading import alpaca_executor as ae
        self.assertFalse(ae.IS_LIVE)
        self.assertIn("paper_stops.json", ae.PAPER_STOPS_FILE)
        self.assertIn("paper-api", ae.ALPACA_BASE_URL)

    def test_monitor_defaults_to_paper(self):
        from agents.trading import alpaca_monitor as am
        self.assertFalse(am.IS_LIVE)
        self.assertIn("paper_stops.json", am.PAPER_STOPS_FILE)


if __name__ == "__main__":
    unittest.main()
