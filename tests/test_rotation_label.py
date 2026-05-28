import unittest

from agents.utils.rotation_label import (
    rotation_label, HOT, RISING, STABLE, FADING, COLD,
)


class TestRotationLabel(unittest.TestCase):

    def test_hot_top_rank_holding(self):
        # UFO-class: rank 1, holding/improving
        self.assertEqual(rotation_label(rank=1, delta=-7, rs=96), HOT)
        # Boundary: rank=5, delta=0 still HOT
        self.assertEqual(rotation_label(rank=5, delta=0, rs=82), HOT)

    def test_rising_climbing_from_mid(self):
        # IGV-class: rank 6-15, delta ≤ -3
        self.assertEqual(rotation_label(rank=6, delta=-3, rs=79), RISING)
        self.assertEqual(rotation_label(rank=12, delta=-8, rs=70), RISING)

    def test_stable_no_meaningful_move(self):
        self.assertEqual(rotation_label(rank=10, delta=0, rs=65), STABLE)
        self.assertEqual(rotation_label(rank=18, delta=-2, rs=55), STABLE)
        self.assertEqual(rotation_label(rank=18, delta=2, rs=55), STABLE)

    def test_fading_worsening_low_rs(self):
        # delta ≥ +5 AND rs < 60
        self.assertEqual(rotation_label(rank=12, delta=6, rs=50), FADING)
        self.assertEqual(rotation_label(rank=8, delta=10, rs=45), FADING)

    def test_cold_deep_bottom_worsening(self):
        # rank ≥ 20 AND delta ≥ +3 but rs ≥ 60 (so not FADING) → COLD
        self.assertEqual(rotation_label(rank=25, delta=5, rs=75), COLD)
        # rank=20 delta=3 rs=40 — delta<5 so not FADING; rank≥20 delta≥3 → COLD
        self.assertEqual(rotation_label(rank=20, delta=3, rs=40), COLD)

    def test_boundary_hot_vs_rising(self):
        # rank=5 delta=-3 → HOT (HOT wins on rank≤5)
        self.assertEqual(rotation_label(rank=5, delta=-3, rs=80), HOT)
        # rank=6 delta=-3 → RISING (just left HOT band)
        self.assertEqual(rotation_label(rank=6, delta=-3, rs=80), RISING)
        # rank=4 delta=1 → not HOT (delta>0), not RISING (rank<6) → STABLE
        self.assertEqual(rotation_label(rank=4, delta=1, rs=80), STABLE)


if __name__ == "__main__":
    unittest.main()
