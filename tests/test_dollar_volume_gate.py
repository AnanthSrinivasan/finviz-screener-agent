import unittest

from agents.screener.finviz_agent import passes_dollar_volume_gate, MIN_DOLLAR_VOL


class TestDollarVolumeGate(unittest.TestCase):
    """Quality-screen liquidity gate: keep high-priced liquid names (DAVE-class),
    drop genuinely illiquid quality-screen names, never touch mover screens."""

    def test_dave_class_high_priced_low_share_passes(self):
        # DAVE: ~573K shares @ ~$270 = ~$155M/day — must KEEP.
        self.assertTrue(passes_dollar_volume_gate("Base / Near-High", 572_710, "270.29"))

    def test_illiquid_quality_name_dropped(self):
        # 100K shares @ $12 = $1.2M/day — below $30M, quality screen → DROP.
        self.assertFalse(passes_dollar_volume_gate("Growth", 100_000, "12.00"))

    def test_mover_screen_exempt_penny_rocket(self):
        # HYLN-class: $2 × 1M shares = $2M/day, but a mover screen → KEEP (exempt).
        self.assertTrue(passes_dollar_volume_gate("10% Change", 1_000_000, "2.00"))
        self.assertTrue(passes_dollar_volume_gate("Power Move", 500_000, "1.50"))

    def test_mover_plus_quality_membership_keeps(self):
        # If ANY source screen is a mover, exempt even when also a quality screen.
        self.assertTrue(passes_dollar_volume_gate("Growth, 10% Change", 100_000, "12.00"))

    def test_missing_data_keeps(self):
        # Incomplete price/volume must not drop the row.
        self.assertTrue(passes_dollar_volume_gate("Growth", 0, "12.00"))
        self.assertTrue(passes_dollar_volume_gate("Growth", 500_000, ""))
        self.assertTrue(passes_dollar_volume_gate("Growth", None, None))

    def test_exact_threshold_inclusive(self):
        # avg_vol * price == floor → KEEP (>=).
        self.assertTrue(passes_dollar_volume_gate("Growth", MIN_DOLLAR_VOL // 10, "10.00"))

    def test_price_with_formatting(self):
        self.assertTrue(passes_dollar_volume_gate("52 Week High", 600_000, "$1,234.50"))


if __name__ == "__main__":
    unittest.main()
