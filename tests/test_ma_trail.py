"""Unit tests for the regime-adaptive moving-average trail rule."""

import unittest
from unittest.mock import patch

from agents.trading.position_monitor import (
    _ema,
    check_ma_trail_violation,
)


# --------------------------------------------------------------------------
# EMA implementation vs pandas reference
# --------------------------------------------------------------------------

class TestEMA(unittest.TestCase):
    def test_ema_matches_pandas_reference(self):
        import pandas as pd
        values = [100.0, 101.0, 99.5, 102.0, 103.5, 102.8, 104.2, 105.0, 104.5, 106.0] * 3
        ours = _ema(values, span=21)
        ref  = pd.Series(values).ewm(span=21, adjust=False).mean().tolist()
        for o, r in zip(ours, ref):
            self.assertAlmostEqual(o, r, places=6)

    def test_ema_empty_input(self):
        self.assertEqual(_ema([], span=21), [])

    def test_ema_single_value(self):
        self.assertEqual(_ema([100.0], span=21), [100.0])


# --------------------------------------------------------------------------
# Regime gating — which states skip the rule
# --------------------------------------------------------------------------

class TestRegimeGating(unittest.TestCase):
    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_red_regime_skips(self, mock_bars):
        self.assertIsNone(check_ma_trail_violation("XXX", "RED"))
        mock_bars.assert_not_called()

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_danger_regime_skips(self, mock_bars):
        self.assertIsNone(check_ma_trail_violation("XXX", "DANGER"))
        mock_bars.assert_not_called()

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_blackout_regime_skips(self, mock_bars):
        self.assertIsNone(check_ma_trail_violation("XXX", "BLACKOUT"))
        mock_bars.assert_not_called()

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_unknown_regime_skips(self, mock_bars):
        self.assertIsNone(check_ma_trail_violation("XXX", "UNKNOWN"))
        mock_bars.assert_not_called()


# --------------------------------------------------------------------------
# Violation detection per regime
# --------------------------------------------------------------------------

def _rising_bars(start: float = 100, n: int = 30) -> list:
    """Bars where close rises monotonically — price always above EMAs, no violation."""
    return [{"c": start + i * 0.5} for i in range(n)]


def _falling_below_ema_bars(start: float = 130, drop_tail: int = 5) -> list:
    """Start high, drop sharply below what the EMA will settle at."""
    # 25 stable bars, then drop_tail bars collapsing to ~80
    head = [{"c": start} for _ in range(25)]
    tail = [{"c": start - 40 - i * 3} for i in range(drop_tail)]
    return head + tail


class TestViolations(unittest.TestCase):
    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_green_rising_no_violation(self, mock_bars):
        mock_bars.return_value = _rising_bars()
        self.assertIsNone(check_ma_trail_violation("XXX", "GREEN"))

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_green_2_closes_below_21ema(self, mock_bars):
        """GREEN regime requires 2 consecutive closes below 21 EMA."""
        mock_bars.return_value = _falling_below_ema_bars(drop_tail=5)
        v = check_ma_trail_violation("XXX", "GREEN")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 2)
        self.assertLess(v["last_close"], v["last_ema"])

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_green_only_1_close_below_not_enough(self, mock_bars):
        """GREEN requires 2 — single close below should not trigger."""
        bars = _rising_bars(n=30)
        # Drop ONLY the last close below EMA — only 1 below
        bars[-1] = {"c": 10.0}
        self.assertIsNone(check_ma_trail_violation("XXX", "GREEN"))

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_thrust_uses_same_rule_as_green(self, mock_bars):
        mock_bars.return_value = _falling_below_ema_bars()
        v = check_ma_trail_violation("XXX", "THRUST")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 2)

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_caution_1_close_below_21ema_triggers(self, mock_bars):
        """CAUTION: tighter — 1 close below 21 EMA = violation."""
        bars = _rising_bars(n=30)
        bars[-1] = {"c": 10.0}  # 1 close well below
        mock_bars.return_value = bars
        v = check_ma_trail_violation("XXX", "CAUTION")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 1)

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_cooling_uses_8ema_1_close(self, mock_bars):
        """COOLING: 1 close below 8 EMA (faster, tighter)."""
        bars = _rising_bars(n=30)
        bars[-1] = {"c": 10.0}
        mock_bars.return_value = bars
        v = check_ma_trail_violation("XXX", "COOLING")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "8EMA")
        self.assertEqual(v["consecutive"], 1)

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_insufficient_bars_returns_none(self, mock_bars):
        mock_bars.return_value = [{"c": 100.0}] * 3  # way fewer than 21
        self.assertIsNone(check_ma_trail_violation("XXX", "GREEN"))

    @patch("agents.trading.position_monitor.fetch_alpaca_daily_bars")
    def test_empty_bars_returns_none(self, mock_bars):
        mock_bars.return_value = []
        self.assertIsNone(check_ma_trail_violation("XXX", "GREEN"))


if __name__ == "__main__":
    unittest.main()
