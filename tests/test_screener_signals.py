"""
Unit tests for Hidden Growth scoring and Ready-to-Enter Slack block.
"""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import (
    _score_hidden_growth,
    _HIDDEN_GROWTH_EXCLUDED_SECTORS,
    send_slack_notification,
)


def _row(**over):
    base = {
        "Ticker": "X",
        "Appearances": 0,
        "EPS Y/Y TTM": 0.0,
        "EPS Q/Q": 0.0,
        "Inst Trans": 0.0,
        "Screeners": "",
        "Stage": {"stage": 0, "perfect": False},
    }
    base.update(over)
    return pd.Series(base)


# --------------------------------------------------------------------------
# Hidden Growth scoring
# --------------------------------------------------------------------------

class TestHiddenGrowthScoring(unittest.TestCase):
    def test_mu_not_flagged_clean_growth_no_persistence(self):
        """MU (today): clean TTM, strong Q/Q, Stage 2 perfect — but no persistence, no inst.
        Should score 3/6 → not flagged (correct: belongs to Ready-to-Enter tier)."""
        criteria = _score_hidden_growth(_row(
            Ticker="MU",
            Appearances=1,
            **{"EPS Y/Y TTM": 410.18, "EPS Q/Q": 756.34, "Inst Trans": -0.25},
            Screeners="Growth",
            Stage={"stage": 2, "perfect": True},
        ))
        self.assertTrue(criteria["eps_yy_strong"])
        self.assertTrue(criteria["eps_qq_strong"])
        self.assertTrue(criteria["stage2_perfect"])
        self.assertFalse(criteria["persistence"])
        self.assertFalse(criteria["inst_buying"])
        self.assertFalse(criteria["ipo_lifecycle"])
        self.assertEqual(sum(criteria.values()), 3)

    def test_nvts_apr16_flagged_deep_base_breakout(self):
        """NVTS Apr 16: persistence + eps_qq(recovering) + inst + stage2 — even though
        TTM negative and no IPO tag. Should score 4/6 → flagged.
        This is the case where the old SNDK pattern scan missed it (10% excluded)."""
        criteria = _score_hidden_growth(_row(
            Ticker="NVTS",
            Appearances=3,
            **{"EPS Y/Y TTM": -24.76, "EPS Q/Q": 32.79, "Inst Trans": 11.07},
            Screeners="10% Change, Week 20%+ Gain, Power Move",
            Stage={"stage": 2, "perfect": True},
        ))
        self.assertTrue(criteria["persistence"])
        self.assertFalse(criteria["eps_yy_strong"])  # -24 < 50
        self.assertTrue(criteria["eps_qq_strong"])   # eps_yy<0 and qq=32>20
        self.assertTrue(criteria["inst_buying"])
        self.assertTrue(criteria["stage2_perfect"])
        self.assertFalse(criteria["ipo_lifecycle"])
        self.assertEqual(sum(criteria.values()), 4)

    def test_sndk_flagged_ipo_spin_off(self):
        """SNDK (today): stage 2 perfect, strong Q/Q, IPO tag, strong inst."""
        criteria = _score_hidden_growth(_row(
            Ticker="SNDK",
            Appearances=2,
            **{"EPS Y/Y TTM": -327.99, "EPS Q/Q": 617.71, "Inst Trans": -9.11},
            Screeners="Growth, IPO",
            Stage={"stage": 2, "perfect": True},
        ))
        self.assertFalse(criteria["persistence"])
        self.assertFalse(criteria["eps_yy_strong"])
        self.assertTrue(criteria["eps_qq_strong"])
        self.assertFalse(criteria["inst_buying"])
        self.assertTrue(criteria["stage2_perfect"])
        self.assertTrue(criteria["ipo_lifecycle"])
        self.assertEqual(sum(criteria.values()), 3)  # 3/6, below threshold

    def test_ttm_distorted_is_not_a_criterion(self):
        """Having distorted TTM (negative) by itself earns no points — contrast with the
        old SNDK scoring which awarded a point for `ttm_distorted`."""
        criteria = _score_hidden_growth(_row(
            **{"EPS Y/Y TTM": -200.0, "EPS Q/Q": 10.0},  # distorted but qq<20
        ))
        # None of the 6 criteria should fire just from distorted TTM
        for k, v in criteria.items():
            self.assertFalse(v, f"{k} should be False but got True")

    def test_clean_ttm_is_a_plus(self):
        """Clean TTM > 50 earns eps_yy_strong — the new positive replaces ttm_distorted."""
        criteria = _score_hidden_growth(_row(**{"EPS Y/Y TTM": 80.0}))
        self.assertTrue(criteria["eps_yy_strong"])

    def test_excluded_sectors_set_defined(self):
        # Smoke test — core exclusions intact
        for s in ["Utilities", "Energy", "Real Estate", "Basic Materials", "Consumer Defensive"]:
            self.assertIn(s, _HIDDEN_GROWTH_EXCLUDED_SECTORS)


# --------------------------------------------------------------------------
# Ready-to-Enter Slack block
# --------------------------------------------------------------------------

class TestReadyToEnterSlackBlock(unittest.TestCase):
    @patch("agents.screener.finviz_agent.requests.post")
    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hook/test")
    def test_ready_to_enter_block_rendered(self, mock_post):
        mock_post.return_value.ok = True
        ready = [{"ticker": "MU", "q": 100, "vcp": 85, "dist": -5, "atr": 5.5, "rvol": 0.66}]

        send_slack_notification(
            summary_df=pd.DataFrame([{"Ticker": "MU"}]),
            filter_df=pd.DataFrame([{"Ticker": "MU", "Quality Score": 100,
                                     "Market Cap": "506B", "ATR%": 5.5, "Screeners": "Growth",
                                     "EPS Y/Y TTM": 410, "EPS Q/Q": 756, "Inst Trans": 0, "Sector": "Technology"}]),
            gallery_html="/tmp/g.html",
            today="2026-04-23",
            ai_summary="",
            ready_to_enter=ready,
        )
        self.assertTrue(mock_post.called)
        payload = mock_post.call_args.kwargs["json"]
        text_blobs = [b.get("text", {}).get("text", "") for b in payload["blocks"]
                      if b.get("type") == "section"]
        joined = "\n".join(text_blobs)
        self.assertIn("Ready to Enter", joined)
        self.assertIn("MU", joined)
        self.assertIn("VCP 85%", joined)
        self.assertIn("/stock-research MU", joined)

    @patch("agents.screener.finviz_agent.requests.post")
    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hook/test")
    def test_no_ready_to_enter_block_when_empty(self, mock_post):
        mock_post.return_value.ok = True
        send_slack_notification(
            summary_df=pd.DataFrame([{"Ticker": "X"}]),
            filter_df=pd.DataFrame([{"Ticker": "X", "Quality Score": 50,
                                     "Market Cap": "1B", "ATR%": 5, "Screeners": "",
                                     "EPS Y/Y TTM": 0, "EPS Q/Q": 0, "Inst Trans": 0, "Sector": "Tech"}]),
            gallery_html="/tmp/g.html",
            today="2026-04-23",
            ai_summary="",
            ready_to_enter=[],
        )
        payload = mock_post.call_args.kwargs["json"]
        joined = "\n".join(
            b.get("text", {}).get("text", "") for b in payload["blocks"]
            if b.get("type") == "section"
        )
        self.assertNotIn("Ready to Enter", joined)

    @patch("agents.screener.finviz_agent.requests.post")
    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hook/test")
    def test_hidden_growth_uncapped_shows_all_tickers(self, mock_post):
        """Hidden Growth block must render all candidates (no top-3 cap).
        Uses 12 candidates — ticker list shows all, research_cmd caps at 10."""
        mock_post.return_value.ok = True
        big_list = [(f"T{i:02d}", 100.0, 200.0) for i in range(12)]
        send_slack_notification(
            summary_df=pd.DataFrame([{"Ticker": "T00"}]),
            filter_df=pd.DataFrame([{"Ticker": "T00", "Quality Score": 90,
                                     "Market Cap": "1B", "ATR%": 5, "Screeners": "Growth",
                                     "EPS Y/Y TTM": 100, "EPS Q/Q": 200, "Inst Trans": 0, "Sector": "Tech"}]),
            gallery_html="/tmp/g.html",
            today="2026-04-23",
            ai_summary="",
            hidden_growth_candidates=big_list,
        )
        payload = mock_post.call_args.kwargs["json"]
        joined = "\n".join(
            b.get("text", {}).get("text", "") for b in payload["blocks"]
            if b.get("type") == "section"
        )
        # All 12 tickers appear
        for i in range(12):
            self.assertIn(f"T{i:02d}", joined)
        # Count is shown
        self.assertIn("12 names", joined)
        # Research commands capped at 10 — "+2 more" note
        self.assertIn("+2 more", joined)

    @patch("agents.screener.finviz_agent.requests.post")
    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hook/test")
    def test_hidden_growth_label_used(self, mock_post):
        mock_post.return_value.ok = True
        send_slack_notification(
            summary_df=pd.DataFrame([{"Ticker": "SNDK"}]),
            filter_df=pd.DataFrame([{"Ticker": "SNDK", "Quality Score": 100,
                                     "Market Cap": "133B", "ATR%": 6, "Screeners": "Growth, IPO",
                                     "EPS Y/Y TTM": -328, "EPS Q/Q": 618, "Inst Trans": 0, "Sector": "Technology"}]),
            gallery_html="/tmp/g.html",
            today="2026-04-23",
            ai_summary="",
            hidden_growth_candidates=[("SNDK", -328.0, 618.0)],
        )
        payload = mock_post.call_args.kwargs["json"]
        joined = "\n".join(
            b.get("text", {}).get("text", "") for b in payload["blocks"]
            if b.get("type") == "section"
        )
        self.assertIn("Hidden Growth", joined)
        self.assertNotIn("SNDK pattern", joined)
        self.assertIn("distorted", joined)  # SNDK has TTM<-50 and Q/Q>0 → distorted flag


if __name__ == "__main__":
    unittest.main()
