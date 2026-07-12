import unittest

from agents.screener.finviz_agent import (
    classify_screener_tail,
    passes_dollar_volume_gate,
    passes_dollar_volume_prefilter,
    MIN_DOLLAR_VOL,
    PREFILTER_MIN_DOLLAR_VOL,
)


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

    def test_comma_formatted_volume_string_parses(self):
        # Screener-table Volume arrives comma-formatted ("1,234,567"). It must
        # parse, not silently fall back to 0 (which would keep every name and
        # make the pre-filter a no-op). 100K @ $12 = $1.2M → DROP.
        self.assertFalse(passes_dollar_volume_gate("Growth", "100,000", "12.00"))
        # 2M shares @ $20 = $40M → KEEP.
        self.assertTrue(passes_dollar_volume_gate("Growth", "2,000,000", "20.00"))


class TestDollarVolumePrefilter(unittest.TestCase):
    """Cheap pre-snapshot gate on raw screener Volume × Price. Looser threshold
    than the final gate so quiet-volume days never drop genuine DAVE-class names."""

    def test_prefilter_threshold_looser_than_final(self):
        # Pre-filter floor must sit below the precise final cut.
        self.assertLess(PREFILTER_MIN_DOLLAR_VOL, MIN_DOLLAR_VOL)

    def test_dave_class_passes_prefilter(self):
        # 573K @ $270 ≈ $155M raw — keep.
        self.assertTrue(passes_dollar_volume_prefilter("Base / Near-High", 572_710, "270.29"))

    def test_obviously_illiquid_dropped(self):
        # 100K @ $12 = $1.2M/day raw, quality screen → drop before snapshot.
        self.assertFalse(passes_dollar_volume_prefilter("Growth", 100_000, "12.00"))

    def test_borderline_below_final_above_prefilter_kept(self):
        # $25M raw: below the $30M final gate but above the $20M pre-filter →
        # KEEP so the precise avg-volume gate is still the final decider.
        self.assertTrue(passes_dollar_volume_prefilter("Growth", 1_000_000, "25.00"))

    def test_mover_screen_exempt(self):
        self.assertTrue(passes_dollar_volume_prefilter("10% Change", 1_000_000, "2.00"))

    def test_missing_data_keeps(self):
        self.assertTrue(passes_dollar_volume_prefilter("Growth", 0, "12.00"))
        self.assertTrue(passes_dollar_volume_prefilter("Growth", None, None))


class TestClassifyScreenerTail(unittest.TestCase):
    """Regression for the 2026-07 Finviz screener-table reorder: Price/Change/
    Volume must be identified by format, never by column position. The fixed
    indexes had Change% landing in Price and the price in Volume — which
    fail-opened both dollar-volume gates and zeroed the Big Movers 9M gate."""

    def test_new_layout_price_change_volume(self):
        # Live layout as of 2026-07-12: Price@8, Change@9, Volume@10.
        vol, price, change = classify_screener_tail(["16.95", "-0.64%", "82,036,295"])
        self.assertEqual(vol, "82,036,295")
        self.assertEqual(price, "16.95")
        self.assertEqual(change, "-0.64%")

    def test_old_layout_volume_price_change(self):
        # Pre-reorder layout: Volume@8, Price@9, Change@10.
        vol, price, change = classify_screener_tail(["82,036,295", "16.95", "-0.64%"])
        self.assertEqual(vol, "82,036,295")
        self.assertEqual(price, "16.95")
        self.assertEqual(change, "-0.64%")

    def test_high_priced_name_with_comma_and_decimal(self):
        # BRK.A-class: price has commas AND a decimal — still classified as price.
        vol, price, change = classify_screener_tail(["1,712.00", "0.20%", "24,096"])
        self.assertEqual(price, "1,712.00")
        self.assertEqual(vol, "24,096")

    def test_dash_and_empty_cells_yield_blank(self):
        vol, price, change = classify_screener_tail(["-", "", None])
        self.assertEqual((vol, price, change), ("", "", ""))
        # Blank output keeps the row via the gates' missing-data rule.
        self.assertTrue(passes_dollar_volume_prefilter("Growth", vol, price))


if __name__ == "__main__":
    unittest.main()
