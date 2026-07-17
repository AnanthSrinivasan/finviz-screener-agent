"""
Tests for the Money Flow Dashboard theme layer
(docs/specs/money-flow-dashboard.md §8).

Covers: synthetic index math, combined-pool RS, flow_score rollup,
divergence, Money Line (name-ALL-groups regression), theme history,
render with/without themes, Slack additions, JSON additivity.
No network, no API keys.
"""

import json
import os
import tempfile
import unittest

from agents import sector_rotation as sr
from agents.utils import theme_flow as tf

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_THEME_MAP = os.path.join(REPO_ROOT, "data", "theme_map.json")


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------
def _metrics(**kw):
    m = {"last": 100.0, "atr_pct": 2.0, "mult50": 2.0, "pct50": 4.0, "dist52": -5.0,
         "s50_rising": True, "s200_rising": True, "range20": 8.0, "ret20": 5.0,
         "ema21d": 1.0, "rvol": 1.1}
    m.update(kw)
    return m


def _theme_payload():
    return {
        "themes": [
            {"theme_id": "T-A", "name": "Endpoint & Platform Security",
             "ecosystem": "E-CYBER", "ecosystem_name": "Cybersecurity",
             "sibling_etf": "HACK", "rs_score": 95, "rank": 1, "combined_rank": 2,
             "rank_delta_5d": -3, "divergence": 8,
             "index": {"base_date": "2026-01-05", "level": 174.8,
                       "spark": [100, 101, 103, 102, 105]},
             "tickers": ["CRWD", "PANW"]},
            {"theme_id": "T-B", "name": "Memory & Storage",
             "ecosystem": "E-AIHW", "ecosystem_name": "AI Hardware & Infrastructure",
             "sibling_etf": None, "rs_score": 88, "rank": 2, "combined_rank": 5,
             "rank_delta_5d": 0, "divergence": None,
             "index": {"base_date": "2026-01-05", "level": 150.0,
                       "spark": [100, 99, 104]},
             "tickers": ["MU", "WDC"]},
        ],
        "stock_flow": [
            {"ticker": "CRWD", "themes": ["T-A"], "theme_labels": ["Endpoint"],
             "flow_score": 0.9, "q_score": 84, "watchlist_tier": "focus", "held": False},
            {"ticker": "MU", "themes": ["T-B"], "theme_labels": ["Memory"],
             "flow_score": 0.76, "q_score": None, "watchlist_tier": None, "held": True},
        ],
        "money_line": {"in": [], "out": [],
                       "text": "Money is IN: Memory & Storage."},
    }


def _setups():
    return [
        {"ticker": "XLRE", "name": "Real Estate", "theme": "rate-sensitive",
         "kind": "sector", "bucket": "BASE", "metrics": _metrics()},
        {"ticker": "SMH", "name": "Semiconductors", "theme": "growth",
         "kind": "thematic", "bucket": "EXTENDED",
         "metrics": _metrics(mult50=8.0, dist52=-1.0)},
    ]


# ------------------------------------------------------------------
# Synthetic index math (spec §4.1)
# ------------------------------------------------------------------
class TestSynthThemeIndex(unittest.TestCase):
    def test_equal_weight_mean_of_returns(self):
        closes = {
            "T1": {"2026-01-01": 100.0, "2026-01-02": 110.0, "2026-01-03": 121.0},
            "T2": {"2026-01-01": 100.0, "2026-01-02": 90.0,  "2026-01-03": 99.0},
        }
        idx = tf.synth_theme_index(closes)
        # day1: mean(+10%, -10%) = 0 → 100 · day2: mean(+10%, +10%) → 110
        self.assertEqual(idx["levels"][0], 100.0)
        self.assertAlmostEqual(idx["levels"][1], 100.0, places=6)
        self.assertAlmostEqual(idx["levels"][2], 110.0, places=6)

    def test_base_100_and_base_date(self):
        closes = {
            "T1": {"2026-01-01": 50.0, "2026-01-02": 55.0},
            "T2": {"2026-01-01": 200.0, "2026-01-02": 220.0},
        }
        idx = tf.synth_theme_index(closes)
        self.assertEqual(idx["base_date"], "2026-01-01")
        self.assertEqual(idx["levels"][0], 100.0)

    def test_missing_ticker_day_excluded(self):
        # T3 halted on 01-02: day1 return uses only T1+T2
        closes = {
            "T1": {"2026-01-01": 100.0, "2026-01-02": 110.0},
            "T2": {"2026-01-01": 100.0, "2026-01-02": 120.0},
            "T3": {"2026-01-01": 100.0},
        }
        idx = tf.synth_theme_index(closes)
        self.assertAlmostEqual(idx["levels"][1], 115.0, places=6)

    def test_min_two_rule_carries_level(self):
        # only one constituent has both closes → index level carries flat
        closes = {
            "T1": {"2026-01-01": 100.0, "2026-01-02": 150.0},
            "T2": {"2026-01-01": 100.0},
        }
        idx = tf.synth_theme_index(closes)
        self.assertEqual(idx["levels"], [100.0, 100.0])

    def test_base_starts_where_min_valid_available(self):
        # T2 lists on 01-02 → base date is the first day with >= 2 closes
        closes = {
            "T1": {"2026-01-01": 100.0, "2026-01-02": 100.0, "2026-01-03": 110.0},
            "T2": {"2026-01-02": 100.0, "2026-01-03": 90.0},
        }
        idx = tf.synth_theme_index(closes)
        self.assertEqual(idx["base_date"], "2026-01-02")
        self.assertAlmostEqual(idx["levels"][-1], 100.0, places=6)

    def test_unbuildable_returns_none(self):
        self.assertIsNone(tf.synth_theme_index({}))
        self.assertIsNone(tf.synth_theme_index({"T1": {"2026-01-01": 100.0}}))

    def test_index_ret(self):
        self.assertAlmostEqual(tf.index_ret([100, 110], 1), 0.10, places=6)
        self.assertIsNone(tf.index_ret([100], 1))


# ------------------------------------------------------------------
# Combined-pool RS (spec §4.2)
# ------------------------------------------------------------------
class TestCombinedPoolRS(unittest.TestCase):
    def test_themes_and_etfs_ranked_together(self):
        themes = [{"theme_id": "T-A", "ret_vs_spy_20d": 0.30}]
        etfs = [{"etf": "HACK", "ret_vs_spy_20d": 0.10},
                {"etf": "XLE", "ret_vs_spy_20d": -0.05}]
        tf.combined_pool_rs(themes, etfs)
        # pool of 3: 0.30 has 2 below → 66; 0.10 has 1 → 33; -0.05 → 0
        self.assertEqual(themes[0]["rs_score"], 66)
        self.assertEqual(etfs[0]["rs_combined"], 33)
        self.assertEqual(etfs[1]["rs_combined"], 0)
        # combined rank spans BOTH lists
        self.assertEqual(themes[0]["combined_rank"], 1)
        self.assertEqual(etfs[0]["combined_rank"], 2)
        self.assertEqual(etfs[1]["combined_rank"], 3)
        # theme rank is among themes only
        self.assertEqual(themes[0]["rank"], 1)

    def test_etf_published_rs_score_not_overwritten(self):
        themes = [{"theme_id": "T-A", "ret_vs_spy_20d": 0.30}]
        etfs = [{"etf": "HACK", "ret_vs_spy_20d": 0.10, "rs_score": 92}]
        tf.combined_pool_rs(themes, etfs)
        self.assertEqual(etfs[0]["rs_score"], 92)  # untouched
        self.assertIn("rs_combined", etfs[0])

    def test_missing_ret_gets_zero(self):
        themes = [{"theme_id": "T-A", "ret_vs_spy_20d": None}]
        tf.combined_pool_rs(themes, [{"etf": "SPY-ish", "ret_vs_spy_20d": 0.01}])
        self.assertEqual(themes[0]["rs_score"], 0)


# ------------------------------------------------------------------
# Stock flow rollup (spec §4.4)
# ------------------------------------------------------------------
class TestStockFlow(unittest.TestCase):
    def test_multi_theme_stacking_and_sub50_zero(self):
        themes = [
            {"theme_id": "A", "name": "Alpha & One", "rs_score": 95, "tickers": ["X", "Y"]},
            {"theme_id": "B", "name": "Beta", "rs_score": 75, "tickers": ["X"]},
            {"theme_id": "C", "name": "Gamma", "rs_score": 40, "tickers": ["Z", "X"]},
        ]
        rows = tf.stock_flow_rollup(themes)
        by_tk = {r["ticker"]: r for r in rows}
        self.assertAlmostEqual(by_tk["X"]["flow_score"], 1.4, places=2)  # 0.9 + 0.5 + 0
        self.assertAlmostEqual(by_tk["Y"]["flow_score"], 0.9, places=2)
        self.assertEqual(by_tk["Z"]["flow_score"], 0.0)  # sub-50 theme contributes 0
        self.assertEqual(rows[0]["ticker"], "X")  # sorted desc
        self.assertEqual(by_tk["X"]["themes"], ["A", "B", "C"])

    def test_enrich_stock_flow(self):
        rows = [{"ticker": "X", "themes": ["A"], "theme_labels": ["Alpha"], "flow_score": 0.9}]
        tf.enrich_stock_flow(rows, q_by_ticker={"X": 84},
                             tier_by_ticker={"X": "focus"}, held={"X"})
        self.assertEqual(rows[0]["q_score"], 84)
        self.assertEqual(rows[0]["watchlist_tier"], "focus")
        self.assertTrue(rows[0]["held"])

    def test_enrich_defaults(self):
        rows = [{"ticker": "X", "themes": ["A"], "theme_labels": ["Alpha"], "flow_score": 0.9}]
        tf.enrich_stock_flow(rows)
        self.assertIsNone(rows[0]["q_score"])
        self.assertIsNone(rows[0]["watchlist_tier"])
        self.assertFalse(rows[0]["held"])

    def test_short_label(self):
        self.assertEqual(tf.short_label("Endpoint & Platform Security"), "Endpoint")
        self.assertEqual(tf.short_label("Quantum"), "Quantum")
        self.assertEqual(tf.short_label("Vulnerability & Exposure Mgmt"), "Vulnerability")


# ------------------------------------------------------------------
# Divergence (spec §4.3)
# ------------------------------------------------------------------
class TestDivergence(unittest.TestCase):
    def test_positive_and_negative_sign(self):
        self.assertEqual(tf.divergence_for({"sibling_etf": "HACK", "rs_score": 80},
                                           {"HACK": 70}), 10)
        self.assertEqual(tf.divergence_for({"sibling_etf": "HACK", "rs_score": 60},
                                           {"HACK": 72}), -12)

    def test_none_without_sibling_or_data(self):
        self.assertIsNone(tf.divergence_for({"sibling_etf": None, "rs_score": 80}, {"HACK": 70}))
        self.assertIsNone(tf.divergence_for({"sibling_etf": "UFO", "rs_score": 80}, {"HACK": 70}))


# ------------------------------------------------------------------
# Money Line (spec §4.5) — must name ALL qualifying groups
# ------------------------------------------------------------------
class TestMoneyLine(unittest.TestCase):
    def _rows(self):
        themes = [
            {"theme_id": "T-MEM", "name": "Memory & Storage",
             "ecosystem_name": "AI Hardware", "rs_score": 97,
             "combined_rank": 1, "rank_delta_5d": 0},
            {"theme_id": "T-END", "name": "Endpoint & Platform Security",
             "ecosystem_name": "Cybersecurity", "rs_score": 96,
             "combined_rank": 2, "rank_delta_5d": -1},
            {"theme_id": "T-VULN", "name": "Vulnerability & Exposure Mgmt",
             "ecosystem_name": "Cybersecurity", "rs_score": 90,
             "combined_rank": 4, "rank_delta_5d": 0},
        ]
        etfs = [
            {"etf": "XBI", "name": "Biotech", "rs_combined": 85,
             "combined_rank": 3, "rank_delta_5d": -2},
            {"etf": "KBE", "name": "Banks", "rs_combined": 75,
             "combined_rank": 8, "rank_delta_5d": -7},   # IN via 5d climb
            {"etf": "XLE", "name": "Energy", "rs_combined": 55,
             "combined_rank": 20, "rank_delta_5d": 0},   # qualifies nowhere
            {"etf": "IGV", "name": "Cloud software", "rs_combined": 30,
             "combined_rank": 40, "rank_delta_5d": 12},  # OUT
            {"etf": "ARKF", "name": "Fintech", "rs_combined": 45,
             "combined_rank": 30, "rank_delta_5d": 15},  # OUT
        ]
        return themes, etfs

    def test_names_all_qualifying_groups(self):
        themes, etfs = self._rows()
        ml = tf.money_line(themes, etfs)
        text = ml["text"]
        # ALL five IN groups named (regression for the name-all-groups rule)
        self.assertIn("Memory & Storage", text)
        self.assertIn("Cybersecurity (", text)
        self.assertIn("Endpoint #2", text)
        self.assertIn("Vulnerability #4", text)
        self.assertIn("Biotech", text)
        self.assertIn("Banks", text)
        self.assertEqual(len(ml["in"]), 5)
        # OUT thresholds honored
        self.assertIn("LEAVING:", text)
        self.assertIn("Cloud software", text)
        self.assertIn("Fintech", text)
        self.assertNotIn("Energy", text)
        self.assertEqual(len(ml["out"]), 2)

    def test_out_threshold_edges(self):
        # rs exactly 50 is NOT out; delta 9 is NOT out
        etfs = [
            {"etf": "A", "name": "A-grp", "rs_combined": 50, "combined_rank": 30, "rank_delta_5d": 12},
            {"etf": "B", "name": "B-grp", "rs_combined": 40, "combined_rank": 31, "rank_delta_5d": 9},
            {"etf": "C", "name": "C-grp", "rs_combined": 40, "combined_rank": 32, "rank_delta_5d": 10},
        ]
        ml = tf.money_line([], etfs)
        self.assertEqual([e["name"] for e in ml["out"]], ["C-grp"])

    def test_in_requires_rs_70(self):
        # top-rank but weak RS does not qualify
        etfs = [{"etf": "A", "name": "A-grp", "rs_combined": 65,
                 "combined_rank": 1, "rank_delta_5d": -20}]
        ml = tf.money_line([], etfs)
        self.assertEqual(ml["in"], [])
        self.assertIn("No group clears the money-line bar", ml["text"])

    def test_empty_pool(self):
        ml = tf.money_line([], [])
        self.assertIn("No group clears the money-line bar", ml["text"])


# ------------------------------------------------------------------
# Theme map loading / validation
# ------------------------------------------------------------------
class TestLoadThemeMap(unittest.TestCase):
    def test_real_repo_theme_map_loads(self):
        tm = tf.load_theme_map(REAL_THEME_MAP)
        self.assertIsNotNone(tm)
        self.assertEqual(len(tm["themes"]), 14)
        self.assertEqual(len(tm["ecosystems"]), 6)
        # spot-check the approved Appendix A baskets
        self.assertEqual(tm["themes"]["T-MEMSTOR"]["tickers"], ["MU", "WDC", "STX", "SNDK"])
        self.assertIsNone(tm["themes"]["T-MEMSTOR"]["sibling_etf"])
        self.assertEqual(tm["themes"]["T-CYBEND"]["sibling_etf"], "HACK")
        # every theme's ecosystem exists
        for t in tm["themes"].values():
            self.assertIn(t["ecosystem"], tm["ecosystems"])

    def test_missing_file_returns_none(self):
        self.assertIsNone(tf.load_theme_map("/nonexistent/theme_map.json"))

    def test_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            path = f.name
        try:
            self.assertIsNone(tf.load_theme_map(path))
        finally:
            os.unlink(path)

    def test_malformed_schema_returns_none(self):
        cases = [
            {},                                    # no themes
            {"themes": {}, "ecosystems": {}},      # empty themes
            {"themes": {"T": {"name": "X", "tickers": ["A"]}}, "ecosystems": {}},  # 1 ticker
            {"themes": {"T": {"tickers": ["A", "B"]}}, "ecosystems": {}},          # no name
            {"themes": {"T": {"name": "X", "tickers": ["A", "B"]}}},               # no ecosystems key
        ]
        for tm in cases:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(tm, f)
                path = f.name
            try:
                self.assertIsNone(tf.load_theme_map(path), f"should reject: {tm}")
            finally:
                os.unlink(path)

    def test_theme_ticker_union(self):
        tm = {"themes": {"A": {"tickers": ["X", "Y"]}, "B": {"tickers": ["Y", "Z"]}}}
        self.assertEqual(tf.theme_ticker_union(tm), ["X", "Y", "Z"])


# ------------------------------------------------------------------
# Theme history (spec §3.2)
# ------------------------------------------------------------------
class TestThemeHistory(unittest.TestCase):
    def test_annotate_rank_delta_5d(self):
        history = []
        dates = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13"]
        # rank was 4 five trading entries back (2026-07-07), 1 most recently
        ranks = [5, 4, 3, 2, 1, 1]
        for d, rk in zip(dates, ranks):
            history.append({"date": d, "theme": "T-A", "rs_score": 90, "rank": rk, "ret_1d": 0.01})
        rows = [{"theme_id": "T-A", "rank": 1, "rs_score": 95}]
        tf.annotate_theme_history(rows, history, "2026-07-14")
        self.assertEqual(rows[0]["rank_5d_ago"], 4)
        self.assertEqual(rows[0]["rank_delta_5d"], -3)  # climbed 3 spots

    def test_annotate_no_history(self):
        rows = [{"theme_id": "T-A", "rank": 1, "rs_score": 95}]
        tf.annotate_theme_history(rows, [], "2026-07-14")
        self.assertIsNone(rows[0]["rank_5d_ago"])
        self.assertEqual(rows[0]["rank_delta_5d"], 0)

    def test_append_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "theme_rotation_history.json")
            rows = [{"theme_id": "T-A", "rs_score": 90, "rank": 1, "ret_1d": 0.01},
                    {"theme_id": "T-B", "rs_score": 60, "rank": 2, "ret_1d": -0.002}]
            today = "2026-07-14"
            tf.append_theme_history(rows, path, today)
            tf.append_theme_history(rows, path, today)  # second run same day
            hist = tf.load_theme_history(path)
            self.assertEqual(len(hist), 2)  # not 4
            self.assertEqual({h["theme"] for h in hist}, {"T-A", "T-B"})
            self.assertEqual(hist[0]["date"], today)


# ------------------------------------------------------------------
# Sparkline
# ------------------------------------------------------------------
class TestSparkline(unittest.TestCase):
    def test_svg_output(self):
        svg = tf.sparkline_svg([100, 101, 99, 105])
        self.assertIn("<svg", svg)
        self.assertIn("polyline", svg)
        self.assertNotIn("http", svg)  # strict-CSP: no external refs

    def test_too_few_values(self):
        self.assertEqual(tf.sparkline_svg([100]), "")
        self.assertEqual(tf.sparkline_svg([]), "")
        self.assertEqual(tf.sparkline_svg(None), "")


# ------------------------------------------------------------------
# Render (spec §8 — with and without themes; import-safe, no network)
# ------------------------------------------------------------------
class TestRenderWithThemes(unittest.TestCase):
    def test_render_without_themes_is_todays_page(self):
        snapshot = {"date": "2026-07-14", "regime": "mid-rotation"}
        html = sr.render_etf_rotation_html(snapshot, _setups())
        self.assertIn("ETF Rotation Dashboard", html)
        self.assertNotIn("Flow Map", html)
        self.assertNotIn('<div class="money-line">', html)
        # NOTE: the shared nav (cx-rehaul) always carries a "💰 Flow" link, so
        # assert on the money-line section itself, not the bare emoji.
        self.assertNotIn("Stock Flow Leaderboard", html)
        # full table still present (inside collapsed details)
        self.assertIn("Full metrics — all 2 ETFs", html)
        self.assertIn("<details>", html)

    def test_render_with_themes_has_new_sections_in_order(self):
        snapshot = {"date": "2026-07-14", "regime": "mid-rotation"}
        html = sr.render_etf_rotation_html(snapshot, _setups(), themes=_theme_payload())
        self.assertIn("💰", html)
        self.assertIn("Money is IN: Memory &amp; Storage." if "&amp;" in html
                      else "Money is IN: Memory & Storage.", html)
        self.assertIn("Flow Map", html)
        self.assertIn("Stock Flow Leaderboard", html)
        self.assertIn("<svg", html)  # sparkline rendered inline
        self.assertIn("Cybersecurity", html)  # ecosystem header
        self.assertIn("⚠ basket +8 vs HACK", html)  # divergence chip
        self.assertIn("CRWD", html)
        self.assertIn("📌 held", html)
        self.assertIn("focus", html)
        # plain-English delta, and rank climbing renders green "up N"
        self.assertIn("up 3", html)
        # user-rejected category badges must NOT appear on this page
        self.assertNotIn("FADING", html)
        self.assertNotIn("↘", html)
        # section order: money line before flow map before stock flow before sweet spot zone
        self.assertLess(html.index("💰"), html.index("Flow Map"))
        self.assertLess(html.index("Flow Map"), html.index("Stock Flow Leaderboard"))
        self.assertLess(html.index("Stock Flow Leaderboard"), html.index("Full metrics"))

    def test_render_theme_without_sibling_has_no_divergence_chip(self):
        snapshot = {"date": "2026-07-14", "regime": "mid-rotation"}
        payload = _theme_payload()
        payload["themes"] = [payload["themes"][1]]  # Memory & Storage, no sibling
        payload["money_line"] = {"in": [], "out": [], "text": "x"}
        html = sr.render_etf_rotation_html(snapshot, _setups(), themes=payload)
        self.assertNotIn("⚠ basket", html)

    def test_full_table_wrapped_in_collapsed_details(self):
        snapshot = {"date": "2026-07-14", "regime": "mid-rotation"}
        html = sr.render_etf_rotation_html(snapshot, _setups(), themes=_theme_payload())
        d = html.index("<details>")
        self.assertLess(d, html.index("full-table"))
        self.assertNotIn("<details open>", html)


# ------------------------------------------------------------------
# Slack additions (spec §6)
# ------------------------------------------------------------------
class TestSlackAdditions(unittest.TestCase):
    def _snap(self):
        return {"date": "2026-07-13", "regime": "mid-rotation",
                "dispersion_percentile_180d": 0.50, "etfs": []}

    def _sig(self):
        return {"in": [], "out": [], "anticipation": [], "decay": []}

    def test_money_line_directly_after_phase(self):
        text = sr.format_slack(self._snap(), self._sig(), themes=_theme_payload())
        lines = text.split("\n")
        phase_i = next(i for i, ln in enumerate(lines) if ln.startswith("Phase:"))
        self.assertTrue(lines[phase_i + 1].startswith("💰"))
        self.assertIn("Money is IN: Memory & Storage.", lines[phase_i + 1])

    def test_stock_flow_block_top5(self):
        text = sr.format_slack(self._snap(), self._sig(), themes=_theme_payload())
        self.assertIn("*🏆 Stock flow:*", text)
        self.assertIn("CRWD (Endpoint, Q84, focus)", text)
        self.assertIn("MU (Memory, held)", text)

    def test_without_themes_unchanged(self):
        text = sr.format_slack(self._snap(), self._sig())
        self.assertNotIn("💰", text)
        self.assertNotIn("🏆", text)
        self.assertIn("Best entry tape", text)  # existing action block intact


# ------------------------------------------------------------------
# JSON additivity (spec §3.3) + enrichment loaders
# ------------------------------------------------------------------
class TestJsonAndEnrichmentLoaders(unittest.TestCase):
    def test_etf_rotation_json_additive(self):
        snapshot = {"date": "2026-07-14", "regime": "mid-rotation"}
        old_dir = sr.DATA_DIR
        with tempfile.TemporaryDirectory() as td:
            sr.DATA_DIR = td
            try:
                sr.write_etf_rotation_json(snapshot, _setups(), themes=_theme_payload())
                with open(os.path.join(td, "etf_rotation.json")) as f:
                    payload = json.load(f)
                # existing keys untouched
                self.assertEqual(payload["date"], "2026-07-14")
                self.assertEqual(payload["regime"], "mid-rotation")
                self.assertEqual(len(payload["etfs"]), 2)
                # additive keys present
                self.assertEqual(len(payload["themes"]), 2)
                self.assertEqual(payload["themes"][0]["theme_id"], "T-A")
                self.assertIn("stock_flow", payload)
                self.assertIn("money_line", payload)

                # without themes: no new keys (today's file exactly)
                sr.write_etf_rotation_json(snapshot, _setups())
                with open(os.path.join(td, "etf_rotation.json")) as f:
                    payload = json.load(f)
                self.assertNotIn("themes", payload)
                self.assertNotIn("stock_flow", payload)
                self.assertNotIn("money_line", payload)
            finally:
                sr.DATA_DIR = old_dir

    def test_enrichment_loaders_from_local_files(self):
        old_dir = sr.DATA_DIR
        with tempfile.TemporaryDirectory() as td:
            sr.DATA_DIR = td
            try:
                with open(os.path.join(td, "finviz_screeners_2026-07-13.csv"), "w") as f:
                    f.write("Ticker,Quality Score\nCRWD,98.0\nMU,77.5\n")
                with open(os.path.join(td, "watchlist.json"), "w") as f:
                    json.dump({"watchlist": [
                        {"ticker": "CRWD", "priority": "focus"},
                        {"ticker": "OLD", "priority": "archived"},
                    ]}, f)
                with open(os.path.join(td, "positions.json"), "w") as f:
                    json.dump({"open_positions": [{"ticker": "DAVE"}],
                               "closed_positions": []}, f)
                with open(os.path.join(td, "paper_stops.json"), "w") as f:
                    json.dump({"MU": {"stop_price": 1.0}}, f)

                q = sr._load_q_scores("2026-07-14")  # resolver: latest <= today
                self.assertEqual(q["CRWD"], 98)
                self.assertEqual(q["MU"], 78)
                tiers = sr._load_watchlist_tiers()
                self.assertEqual(tiers.get("CRWD"), "focus")
                self.assertNotIn("OLD", tiers)  # archived excluded
                held = sr._load_held_tickers()
                self.assertIn("DAVE", held)   # manual book
                self.assertIn("MU", held)     # paper stops
            finally:
                sr.DATA_DIR = old_dir

    def test_loaders_missing_files_graceful(self):
        old_dir = sr.DATA_DIR
        with tempfile.TemporaryDirectory() as td:
            sr.DATA_DIR = td
            try:
                self.assertEqual(sr._load_q_scores("2026-07-14"), {})
                self.assertEqual(sr._load_watchlist_tiers(), {})
                self.assertEqual(sr._load_held_tickers(), set())
            finally:
                sr.DATA_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
