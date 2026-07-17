"""
Unit tests for agents/market/cohort_health.py — the Cohort Health Index
(spec docs/specs/cohort-health-index.md). No network: bars are synthetic and
the fetch layer is injected/mocked.
"""

import json
import os
import tempfile
import unittest

from agents.market import cohort_health as ch


# ------------------------------------------------------------ bar builders

def bars_from_closes(closes, highs=None):
    highs = highs or closes
    return [{"c": c, "h": h, "l": c, "o": c, "v": 1000}
            for c, h in zip(closes, highs)]


def flat_bars(n=60, price=100.0):
    """Healthy name: flat at price → above 20/50MA, at its window high."""
    return bars_from_closes([price] * n)


def crash_bars(n=60, price=100.0, last=50.0):
    """Carnage name: flat then −50% today → down-4, below MAs, far off high."""
    closes = [price] * (n - 1) + [last]
    return bars_from_closes(closes)


# ------------------------------------------------------------ universe

class UniverseBuildTests(unittest.TestCase):
    def _write(self, d, name, obj):
        with open(os.path.join(d, name), "w") as f:
            json.dump(obj, f)

    def _fixture_dir(self, tmp, theme_map=None):
        self._write(tmp, "positions.json", {
            "open_positions": [
                {"ticker": "aapl", "status": "active"},
                {"ticker": "GONE", "status": "closed"},
            ],
            "closed_positions": [{"ticker": "OLDPOS", "status": "closed"}],
        })
        self._write(tmp, "paper_stops.json", {
            "BTSG": {"stop_price": 10}, "AAPL": {"stop_price": 5}})
        self._write(tmp, "live_alpaca_stops.json", {"NVDA": {"stop_price": 9}})
        self._write(tmp, "watchlist.json", {"watchlist": [
            {"ticker": "DAVE", "priority": "focus", "status": "watching"},
            {"ticker": "WCH", "priority": "watching", "status": "watching"},
            {"ticker": "ER", "priority": "entry-ready", "status": "watching"},
            {"ticker": "ARCH", "priority": "archived", "status": "archived"},
            {"ticker": "ARC2", "priority": "focus", "status": "archived"},
        ]})
        if theme_map is not None:
            self._write(tmp, "theme_map.json", theme_map)

    def test_three_book_union_dedup_and_archived_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)
            universe = ch.build_cohort_universe(tmp)
        # AAPL appears in positions AND paper_stops — deduped, uppercased.
        self.assertEqual(
            universe, ["AAPL", "BTSG", "DAVE", "ER", "NVDA", "WCH"])
        self.assertNotIn("GONE", universe)    # closed position excluded
        self.assertNotIn("ARCH", universe)    # archived priority excluded
        self.assertNotIn("ARC2", universe)    # archived status excluded

    def test_missing_theme_map_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)  # no theme_map.json written
            universe = ch.build_cohort_universe(tmp)
        self.assertEqual(len(universe), 6)

    def test_theme_map_constituents_included(self):
        tm = {"version": "2026-07-15", "themes": {
            "T-CYBEND": {"name": "Endpoint Security", "ecosystem": "E-CYBER",
                         "tickers": ["CRWD", "PANW", "AAPL"]},
        }}
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp, theme_map=tm)
            universe = ch.build_cohort_universe(tmp)
        self.assertIn("CRWD", universe)
        self.assertIn("PANW", universe)
        self.assertEqual(universe.count("AAPL"), 1)  # still deduped

    def test_malformed_theme_map_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)
            with open(os.path.join(tmp, "theme_map.json"), "w") as f:
                f.write("{not json")
            universe = ch.build_cohort_universe(tmp)
        self.assertEqual(len(universe), 6)

    def test_empty_dir_gives_empty_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ch.build_cohort_universe(tmp), [])


# ------------------------------------------------------------ metrics/score

class MetricsScoreTests(unittest.TestCase):
    def test_all_healthy_scores_100(self):
        bars = {f"T{i}": flat_bars() for i in range(5)}
        m = ch.compute_cohort_metrics(bars)
        self.assertEqual(m["cohort_score"], 100)
        self.assertEqual(m["label"], "HEALTHY")
        self.assertEqual(m["pct_down4_today"], 0.0)
        self.assertEqual(m["pct_above_20ma"], 100.0)
        self.assertEqual(m["pct_above_50ma"], 100.0)
        self.assertEqual(m["pct_within_10_of_52wk_high"], 100.0)
        self.assertEqual(m["universe_size"], 5)

    def test_weighted_blend_math(self):
        # 4 healthy + 1 crash: every share is 0.8 (the crash name fails all
        # four components) → score = 100 × 0.8 = 80.
        bars = {f"OK{i}": flat_bars() for i in range(4)}
        bars["CRSH"] = crash_bars()
        m = ch.compute_cohort_metrics(bars)
        self.assertEqual(m["cohort_score"], 80)
        self.assertEqual(m["pct_down4_today"], 20.0)
        self.assertEqual(m["label"], "HEALTHY")

    def test_carnage_down4_override(self):
        # 6 healthy + 2 crashing = 25% down-4 → CARNAGE even though the
        # blended score (75) would otherwise read HEALTHY.
        bars = {f"OK{i}": flat_bars() for i in range(6)}
        bars["CR1"] = crash_bars()
        bars["CR2"] = crash_bars()
        m = ch.compute_cohort_metrics(bars)
        self.assertEqual(m["pct_down4_today"], 25.0)
        self.assertEqual(m["cohort_score"], 75)
        self.assertEqual(m["label"], "CARNAGE")

    def test_up4_counted(self):
        bars = {f"OK{i}": flat_bars() for i in range(4)}
        bars["UP"] = bars_from_closes([100.0] * 59 + [110.0])
        m = ch.compute_cohort_metrics(bars)
        self.assertEqual(m["pct_up4_today"], 20.0)

    def test_worst_names_with_dollar_moves(self):
        bars = {f"OK{i}": flat_bars() for i in range(5)}
        bars["CRSH"] = crash_bars(price=100.0, last=50.0)
        m = ch.compute_cohort_metrics(bars)
        worst = m["worst"]
        self.assertEqual(len(worst), 3)
        self.assertEqual(worst[0]["ticker"], "CRSH")
        self.assertEqual(worst[0]["chg_pct"], -50.0)
        self.assertEqual(worst[0]["dollar_move"], -50.0)
        self.assertEqual(worst[0]["close"], 50.0)

    def test_label_thresholds(self):
        self.assertEqual(ch.cohort_label(65, 0), "HEALTHY")
        self.assertEqual(ch.cohort_label(64, 0), "MIXED")
        self.assertEqual(ch.cohort_label(40, 0), "MIXED")
        self.assertEqual(ch.cohort_label(39, 0), "STRESS")
        self.assertEqual(ch.cohort_label(25, 0), "STRESS")
        self.assertEqual(ch.cohort_label(24, 0), "CARNAGE")
        self.assertEqual(ch.cohort_label(90, 25.0), "CARNAGE")  # override
        self.assertEqual(ch.cohort_label(90, 24.9), "HEALTHY")

    def test_symbols_with_too_few_bars_skipped(self):
        bars = {f"OK{i}": flat_bars() for i in range(5)}
        bars["IPO"] = bars_from_closes([10.0])  # 1 bar — skipped
        m = ch.compute_cohort_metrics(bars)
        self.assertEqual(m["universe_size"], 5)

    def test_thin_universe_returns_none(self):
        bars = {f"OK{i}": flat_bars() for i in range(4)}  # < 5 names
        self.assertIsNone(ch.compute_cohort_metrics(bars))
        self.assertIsNone(ch.compute_cohort_metrics({}))
        self.assertIsNone(ch.compute_cohort_metrics(None))

    def test_no_ma_history_returns_none(self):
        # 5 names but nobody has 20 closes → per-metric denominators empty.
        bars = {f"N{i}": bars_from_closes([100.0] * 5) for i in range(5)}
        self.assertIsNone(ch.compute_cohort_metrics(bars))


# ------------------------------------------------------------ pipeline

class ComputeCohortHealthTests(unittest.TestCase):
    def _fixture_dir(self, tmp):
        with open(os.path.join(tmp, "paper_stops.json"), "w") as f:
            json.dump({f"T{i}": {"stop_price": 1} for i in range(6)}, f)

    def test_happy_path_with_injected_fetch(self):
        def fake_fetch(symbols):
            return {s: flat_bars() for s in symbols}
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)
            m = ch.compute_cohort_health(data_dir=tmp, fetch_fn=fake_fetch)
        self.assertIsNotNone(m)
        self.assertEqual(m["label"], "HEALTHY")
        self.assertEqual(m["universe_size"], 6)

    def test_non_fatal_on_bars_failure(self):
        def boom(symbols):
            raise RuntimeError("alpaca down")
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)
            self.assertIsNone(
                ch.compute_cohort_health(data_dir=tmp, fetch_fn=boom))

    def test_non_fatal_on_empty_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture_dir(tmp)
            self.assertIsNone(ch.compute_cohort_health(
                data_dir=tmp, fetch_fn=lambda s: None))

    def test_empty_universe_skips_fetch(self):
        calls = []
        def spy_fetch(symbols):
            calls.append(symbols)
            return None
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ch.compute_cohort_health(
                data_dir=tmp, fetch_fn=spy_fetch))
        self.assertEqual(calls, [])

    def test_fetch_without_keys_returns_none(self):
        old_key = os.environ.pop("ALPACA_API_KEY", None)
        old_sec = os.environ.pop("ALPACA_SECRET_KEY", None)
        try:
            self.assertIsNone(ch.fetch_cohort_bars(["AAPL"]))
        finally:
            if old_key is not None:
                os.environ["ALPACA_API_KEY"] = old_key
            if old_sec is not None:
                os.environ["ALPACA_SECRET_KEY"] = old_sec


# ------------------------------------------------------------ divergence

class DivergenceTests(unittest.TestCase):
    def test_fires_on_index_bullish_plus_cohort_stress(self):
        for state in ("GREEN", "THRUST", "TREND-FOLLOW", "STEADY-UPTREND"):
            self.assertTrue(ch.is_divergent(state, "STRESS"))
            self.assertTrue(ch.is_divergent(state, "CARNAGE"))

    def test_no_divergence_when_index_bearish_or_cohort_ok(self):
        self.assertFalse(ch.is_divergent("RED", "STRESS"))
        self.assertFalse(ch.is_divergent("CAUTION", "CARNAGE"))
        self.assertFalse(ch.is_divergent("GREEN", "MIXED"))
        self.assertFalse(ch.is_divergent("GREEN", "HEALTHY"))

    def test_alert_dedup_once_per_label_change(self):
        # First divergence → fire.
        self.assertTrue(ch.should_alert_divergence("GREEN", "STRESS", None))
        # Same label next run → deduped.
        self.assertFalse(
            ch.should_alert_divergence("GREEN", "STRESS", "STRESS"))
        # Escalation STRESS → CARNAGE → fire again.
        self.assertTrue(
            ch.should_alert_divergence("GREEN", "CARNAGE", "STRESS"))
        # Not divergent → never fires regardless of stored label.
        self.assertFalse(ch.should_alert_divergence("RED", "STRESS", None))

    def test_inverse_path_is_resilient_note_not_alert(self):
        self.assertTrue(ch.is_resilient("RED", "HEALTHY"))
        self.assertFalse(ch.is_resilient("GREEN", "HEALTHY"))
        self.assertFalse(ch.is_resilient("RED", "MIXED"))
        # Inverse case must NOT route through the divergence alert.
        self.assertFalse(ch.should_alert_divergence("RED", "HEALTHY", None))

    def test_divergence_text_cites_worst_names(self):
        cohort = {
            "label": "STRESS", "cohort_score": 32, "universe_size": 120,
            "pct_down4_today": 18.0, "pct_above_20ma": 31.0,
            "pct_above_50ma": 40.0,
            "worst": [
                {"ticker": "AAOI", "chg_pct": -14.8,
                 "dollar_move": -2.70, "close": 15.50},
                {"ticker": "TEM", "chg_pct": -9.1,
                 "dollar_move": -5.10, "close": 51.00},
                {"ticker": "DAVE", "chg_pct": -6.2,
                 "dollar_move": -17.30, "close": 262.00},
            ],
        }
        text = ch.build_divergence_text("GREEN", cohort)
        self.assertIn("COHORT DIVERGENCE", text)
        self.assertIn("index says GREEN", text)
        self.assertIn("your cohort says STRESS", text)
        self.assertIn("AAOI", text)
        self.assertIn("-2.70", text)
        self.assertIn("-14.8%", text)
        self.assertIn("informational only", text)

    def test_cohort_slack_line_format(self):
        cohort = {"cohort_score": 61, "label": "MIXED",
                  "pct_down4_today": 12.0, "pct_above_20ma": 48.0}
        self.assertEqual(
            ch.format_cohort_line(cohort),
            "Cohort: 61 MIXED · 12% down-4 · 48% above 20MA")


# ------------------------------------------------------------ theme map

class ThemeMapExtractionTests(unittest.TestCase):
    def test_spec_shape(self):
        tm = {"themes": {"T-A": {"tickers": ["CRWD", "panw "]},
                         "T-B": {"tickers": ["FTNT"]}}}
        self.assertEqual(ch._theme_map_tickers(tm),
                         {"CRWD", "PANW", "FTNT"})

    def test_constituents_key_and_nesting(self):
        tm = {"groups": [{"constituents": ["UMAC", "ONDS"]}]}
        self.assertEqual(ch._theme_map_tickers(tm), {"UMAC", "ONDS"})

    def test_none_and_junk(self):
        self.assertEqual(ch._theme_map_tickers(None), set())
        self.assertEqual(ch._theme_map_tickers({"themes": "oops"}), set())
        self.assertEqual(ch._theme_map_tickers({"tickers": [1, None, ""]}),
                         set())


if __name__ == "__main__":
    unittest.main()
