"""Tests for agents/utils/etf_rotation_summary.py — pure functions only."""
import os
import json
import tempfile
import unittest

from agents.utils.etf_rotation_summary import (
    REGIME_ADVICE,
    load_etf_rotation,
    summarize_etf_rotation,
    render_sector_setup_html,
    render_sector_setup_slack,
)


def _etf(ticker, bucket, mult50=1.0, dist52=-5.0, ret20=2.0, range20=4.0, rvol=1.0, name=None):
    return {
        "ticker": ticker,
        "name":   name or ticker,
        "kind":   "sector",
        "bucket": bucket,
        "metrics": {
            "mult50":  mult50,
            "dist52":  dist52,
            "ret20":   ret20,
            "range20": range20,
            "rvol":    rvol,
        },
    }


class TestLoadEtfRotation(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(load_etf_rotation(d))

    def test_loads_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"date": "2026-05-17", "regime": "mid-rotation", "etfs": []}
            with open(os.path.join(d, "etf_rotation.json"), "w") as f:
                json.dump(payload, f)
            self.assertEqual(load_etf_rotation(d), payload)

    def test_invalid_json_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "etf_rotation.json"), "w") as f:
                f.write("not json{")
            self.assertIsNone(load_etf_rotation(d))


class TestSummarizeEtfRotation(unittest.TestCase):
    def test_groups_into_buckets_top_n(self):
        rotation = {"regime": "mid-rotation", "etfs": [
            _etf("XLRE", "BASE", ret20=-2.8),
            _etf("KRE",  "BASE", ret20=1.0),
            _etf("XBI",  "BASE", ret20=5.0),
            _etf("FCG",  "BASE", ret20=3.0),
            _etf("XLE",  "BASE", ret20=4.0),
            _etf("EXTRA","BASE", ret20=-99.0),  # 6th — should fall off when top_n=5
            _etf("XLK",  "EXTENDED", mult50=8.0),
            _etf("SMH",  "EXTENDED", mult50=9.0),
            _etf("UFO",  "PRE-BREAKOUT", dist52=-1.0),
            _etf("HACK", "BROKEN", dist52=-30.0),
            _etf("XLU",  "NEUTRAL"),  # filtered out
        ]}
        out = summarize_etf_rotation(rotation, top_n=5)
        self.assertEqual(out["regime"], "mid-rotation")
        self.assertEqual(len(out["buckets"]["BASE"]), 5)
        base_tickers = [r["ticker"] for r in out["buckets"]["BASE"]]
        self.assertNotIn("EXTRA", base_tickers)
        # BASE sorted by ret20 desc → XBI first
        self.assertEqual(base_tickers[0], "XBI")
        # EXTENDED sorted by mult50 desc → SMH first
        self.assertEqual([r["ticker"] for r in out["buckets"]["EXTENDED"]], ["SMH", "XLK"])
        # NEUTRAL not surfaced
        self.assertNotIn("NEUTRAL", out["buckets"])

    def test_regime_paragraph_uses_advice(self):
        out = summarize_etf_rotation({"regime": "mid-rotation", "etfs": []})
        self.assertIn("best-entry", out["regime_paragraph"].lower())
        self.assertEqual(out["regime_advice"], REGIME_ADVICE["mid-rotation"])

    def test_unknown_regime_falls_back(self):
        out = summarize_etf_rotation({"regime": "weird-tag", "etfs": []})
        # Falls back to bootstrapping advice
        self.assertEqual(out["regime_advice"], REGIME_ADVICE["bootstrapping"])

    def test_empty_bucket_omitted(self):
        rotation = {"regime": "mid-rotation", "etfs": [
            _etf("XLRE", "BASE"),
        ]}
        out = summarize_etf_rotation(rotation)
        self.assertIn("BASE", out["buckets"])
        # Empty BROKEN/EXTENDED/PRE-BREAKOUT omitted
        self.assertNotIn("BROKEN", out["buckets"])
        self.assertNotIn("EXTENDED", out["buckets"])
        self.assertNotIn("PRE-BREAKOUT", out["buckets"])

    def test_partial_bucket_surfaced(self):
        rotation = {"regime": "early-rotation", "etfs": [
            _etf("XLRE", "BASE"),
            _etf("KRE",  "BASE"),
        ]}
        out = summarize_etf_rotation(rotation, top_n=5)
        self.assertEqual(len(out["buckets"]["BASE"]), 2)

    def test_each_regime_has_nonempty_advice(self):
        for regime in REGIME_ADVICE:
            out = summarize_etf_rotation({"regime": regime, "etfs": []})
            self.assertTrue(out["regime_advice"])
            self.assertIn(regime, out["regime_paragraph"])

    def test_table_rows_include_actionable_only(self):
        rotation = {"regime": "mid-rotation", "etfs": [
            _etf("XLRE", "BASE"),
            _etf("XLK",  "EXTENDED"),
            _etf("HACK", "BROKEN"),
        ]}
        out = summarize_etf_rotation(rotation)
        buckets_in_table = {r["bucket"] for r in out["table_rows"]}
        self.assertIn("BASE", buckets_in_table)
        self.assertIn("EXTENDED", buckets_in_table)
        # BROKEN excluded from active-opportunity table
        self.assertNotIn("BROKEN", buckets_in_table)

    def test_regime_actions_lookup_wired(self):
        rotation = {"regime": "mid-rotation", "etfs": []}
        out = summarize_etf_rotation(
            rotation,
            regime_actions_lookup=lambda r: {"headline": "TEST-HEAD", "sizing": "S", "entries": "E", "held": "H"},
        )
        self.assertIn("TEST-HEAD", out["regime_paragraph"])


class TestRenderers(unittest.TestCase):
    def test_html_empty_when_no_summary(self):
        self.assertEqual(render_sector_setup_html(None), "")
        self.assertEqual(render_sector_setup_html({"buckets": {}}), "")

    def test_html_contains_section_markers(self):
        out = summarize_etf_rotation({"regime": "mid-rotation", "etfs": [
            _etf("XLRE", "BASE"),
            _etf("XLK",  "EXTENDED"),
        ]})
        html = render_sector_setup_html(out)
        self.assertIn("📊 Sector Setup This Week", html)
        self.assertIn("sector-setup-section", html)
        self.assertIn("XLRE", html)
        self.assertIn("XLK", html)
        self.assertIn("etf_rotation.html", html)
        # Markdown bold converted to <strong>
        self.assertNotIn("**", html)

    def test_slack_empty_when_no_summary(self):
        self.assertEqual(render_sector_setup_slack(None), "")
        self.assertEqual(render_sector_setup_slack({"buckets": {}}), "")

    def test_slack_contains_buckets(self):
        out = summarize_etf_rotation({"regime": "mid-rotation", "etfs": [
            _etf("XLRE", "BASE"),
            _etf("XLK",  "EXTENDED"),
        ]})
        text = render_sector_setup_slack(out)
        self.assertIn("Sector Setup This Week", text)
        self.assertIn("mid-rotation", text)
        self.assertIn("`XLRE`", text)
        self.assertIn("`XLK`", text)


if __name__ == "__main__":
    unittest.main()
