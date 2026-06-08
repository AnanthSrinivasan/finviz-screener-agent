import unittest

from utils.generators import generate_daily_cockpit as cp


class TestGateDecision(unittest.TestCase):
    def test_full_size_green(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "normal")
        self.assertEqual(g["action"], "FULL SIZE")
        self.assertEqual(g["color"], "green")
        self.assertEqual(g["cap"], 10)

    def test_half_size_caution(self):
        g = cp.gate_decision("CAUTION", "early-rotation", "normal")
        self.assertEqual(g["action"], "HALF SIZE")
        self.assertEqual(g["cap"], 7)

    def test_no_new_extended(self):
        g = cp.gate_decision("EXTENDED", "mid-rotation", "normal")
        self.assertEqual(g["action"], "NO NEW ENTRIES")
        self.assertEqual(g["color"], "red")
        self.assertEqual(g["cap"], 5)

    def test_regime_riskoff_overrides_full(self):
        g = cp.gate_decision("GREEN", "blow-off-risk", "normal")
        self.assertEqual(g["action"], "NO NEW ENTRIES")
        self.assertIn("blow-off-risk", g["detail"])

    def test_suspended_forces_paper(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "suspended")
        self.assertEqual(g["action"], "PAPER ONLY")

    def test_reduced_caps_full_to_half(self):
        g = cp.gate_decision("THRUST", "mid-rotation", "reduced")
        self.assertEqual(g["action"], "HALF SIZE")
        self.assertIn("reduced", g["detail"])

    def test_reduced_keeps_no_new_when_already_closed(self):
        g = cp.gate_decision("RED", "correlation_phase", "reduced")
        self.assertEqual(g["action"], "NO NEW ENTRIES")


class TestDisciplineLine(unittest.TestCase):
    def test_capital_preservation_when_gate_closed(self):
        g = cp.gate_decision("EXTENDED", "blow-off-risk", "reduced")
        line = cp.discipline_line({"current_sizing_mode": "reduced", "consecutive_losses": 2}, g)
        self.assertIn("Capital preservation", line)

    def test_winning_streak_warns_roundtrip(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "aggressive")
        line = cp.discipline_line({"consecutive_wins": 3}, g)
        self.assertIn("round-trip", line.lower())


class TestQualify(unittest.TestCase):
    def _row(self, **kw):
        base = {"Ticker": "AAA", "Quality Score": "85", "ATR%": "5", "Dist From High%": "-6",
                "Rel Volume": "1.0", "VCP": "80", "SMA20%": "3", "SMA50%": "8", "SMA200%": "20",
                "Company": "Co", "Sector": "Tech"}
        base.update(kw)
        return base

    def test_clean_setup_passes(self):
        out = cp.qualify_setups([self._row()], held=set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ticker"], "AAA")
        self.assertGreaterEqual(out[0]["stop_pct"], 8.0)  # -8% MAE floor

    def test_held_excluded(self):
        out = cp.qualify_setups([self._row()], held={"AAA"})
        self.assertEqual(out, [])

    def test_low_q_rejected(self):
        out = cp.qualify_setups([self._row(**{"Quality Score": "70"})], held=set())
        self.assertEqual(out, [])

    def test_extended_dist_rejected(self):
        out = cp.qualify_setups([self._row(**{"Dist From High%": "0.5"})], held=set())
        self.assertEqual(out, [])

    def test_not_stage2_perfect_rejected(self):
        # 50MA below 200MA (s200 < s50) => not perfect
        out = cp.qualify_setups([self._row(**{"SMA50%": "25", "SMA200%": "10"})], held=set())
        self.assertEqual(out, [])

    def test_peel_unsafe_rejected(self):
        # low-vol tier warn=3; s50/atr = 20/4 = 5 > 3 => extended
        out = cp.qualify_setups([self._row(**{"ATR%": "4", "SMA50%": "20"})], held=set())
        self.assertEqual(out, [])

    def test_top_n_cap(self):
        rows = [self._row(Ticker=f"T{i}", **{"Quality Score": str(80 + i)}) for i in range(6)]
        out = cp.qualify_setups(rows, held=set(), top_n=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["ticker"], "T5")  # highest Q first


class TestRecordStats(unittest.TestCase):
    def test_payoff_and_winrate(self):
        ts = {"total_wins": 16, "total_losses": 24, "current_sizing_mode": "reduced"}
        closed = [{"result_pct": 30}, {"result_pct": 20}, {"result_pct": -10}, {"result_pct": -5}]
        s = cp.record_stats(ts, closed)
        self.assertAlmostEqual(s["win_rate"], 40.0)
        self.assertAlmostEqual(s["avg_win"], 25.0)
        self.assertAlmostEqual(s["avg_loss"], -7.5)
        self.assertGreater(s["payoff"], 3.0)

    def test_empty_closed_safe(self):
        s = cp.record_stats({"total_wins": 0, "total_losses": 0}, [])
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["payoff"], 0.0)


class TestRenderSmoke(unittest.TestCase):
    def test_empty_book_and_qualified_render(self):
        g = cp.gate_decision("EXTENDED", "blow-off-risk", "reduced")
        self.assertIn("100% cash", cp.render_book([], {}))
        self.assertIn("patience", cp.render_qualified([], g).lower())
        self.assertIn("rebuilding", cp.render_ondeck([]).lower())

    def test_leadership_groups_by_bucket(self):
        rotation = {"regime": "blow-off-risk", "etfs": [
            {"ticker": "XBI", "name": "Biotech", "bucket": "BASE", "rs_rank": 5},
            {"ticker": "COPX", "name": "Copper Miners", "bucket": "PRE-BREAKOUT", "rs_rank": 2},
            {"ticker": "XLK", "name": "Technology", "bucket": "EXTENDED", "rs_rank": 1},
            {"ticker": "QQQJUNK", "name": "x", "bucket": "NEUTRAL", "rs_rank": 9},
        ]}
        html = cp.render_leadership(rotation)
        self.assertIn("BASE", html)
        self.assertIn("XBI", html)
        self.assertIn("lchip-pre", html)        # pre-breakout styled
        self.assertNotIn("QQQJUNK", html)       # NEUTRAL bucket dropped
        self.assertIn("blow-off-risk", html)

    def test_leadership_empty_safe(self):
        self.assertIn("unavailable", cp.render_leadership({}))


if __name__ == "__main__":
    unittest.main()
