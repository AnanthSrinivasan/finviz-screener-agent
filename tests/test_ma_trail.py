"""Unit tests for the regime-adaptive moving-average trail rule (shared rules.py)."""

import unittest

from agents.trading.rules import (
    _ema,
    check_ma_trail_alert,
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
    def test_red_regime_skips(self):
        self.assertIsNone(check_ma_trail_alert([100.0] * 30, "RED"))

    def test_danger_regime_skips(self):
        self.assertIsNone(check_ma_trail_alert([100.0] * 30, "DANGER"))

    def test_blackout_regime_skips(self):
        self.assertIsNone(check_ma_trail_alert([100.0] * 30, "BLACKOUT"))

    def test_unknown_regime_skips(self):
        self.assertIsNone(check_ma_trail_alert([100.0] * 30, "UNKNOWN"))


# --------------------------------------------------------------------------
# Violation detection per regime
# --------------------------------------------------------------------------

def _rising_closes(start: float = 100, n: int = 30) -> list:
    return [start + i * 0.5 for i in range(n)]


def _falling_below_ema_closes(start: float = 130, drop_tail: int = 5) -> list:
    head = [start] * 25
    tail = [start - 40 - i * 3 for i in range(drop_tail)]
    return head + tail


class TestViolations(unittest.TestCase):
    def test_green_rising_no_violation(self):
        # Low-vol path uses 21 EMA. Default atr_pct=0 → low-vol.
        self.assertIsNone(check_ma_trail_alert(_rising_closes(), "GREEN"))

    def test_green_2_closes_below_21ema(self):
        v = check_ma_trail_alert(_falling_below_ema_closes(drop_tail=5), "GREEN")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 2)
        self.assertLess(v["last_close"], v["last_ema"])

    def test_green_only_1_close_below_not_enough(self):
        closes = _rising_closes(n=30)
        closes[-1] = 10.0
        self.assertIsNone(check_ma_trail_alert(closes, "GREEN"))

    def test_thrust_uses_same_rule_as_green(self):
        v = check_ma_trail_alert(_falling_below_ema_closes(), "THRUST")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 2)

    def test_caution_1_close_below_21ema_triggers(self):
        closes = _rising_closes(n=30)
        closes[-1] = 10.0
        v = check_ma_trail_alert(closes, "CAUTION")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "21EMA")
        self.assertEqual(v["consecutive"], 1)

    def test_cooling_uses_8ema_1_close(self):
        closes = _rising_closes(n=30)
        closes[-1] = 10.0
        v = check_ma_trail_alert(closes, "COOLING")
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "8EMA")
        self.assertEqual(v["consecutive"], 1)

    def test_insufficient_bars_returns_none(self):
        self.assertIsNone(check_ma_trail_alert([100.0] * 3, "GREEN"))

    def test_empty_bars_returns_none(self):
        self.assertIsNone(check_ma_trail_alert([], "GREEN"))

    def test_high_vol_pct_trail_below_floor(self):
        # ATR% > 8 → 10% trail from highest_price_seen
        closes = [100.0] * 25 + [85.0]  # last close 15% below high 100 → trail floor 90
        v = check_ma_trail_alert(closes, "GREEN", atr_pct=10.0, highest_price_seen=100.0)
        self.assertIsNotNone(v)
        self.assertEqual(v["ma_type"], "10% trail")
        self.assertEqual(v["tier"], "high_vol")
        self.assertEqual(v["last_ema"], 90.0)

    def test_high_vol_pct_trail_above_floor(self):
        closes = [100.0] * 25 + [95.0]  # 5% off, above floor 90
        v = check_ma_trail_alert(closes, "GREEN", atr_pct=10.0, highest_price_seen=100.0)
        self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main()
