"""
Integration tests for the daily → weekly signal merge pipeline.

Run locally: python -m pytest test_integration.py -v

These tests validate scoring logic, stage classification, signal detection,
quality modifiers, and the end-to-end daily-quality → weekly-ranking pipeline.
No HTTP calls, no API keys required.
"""

import datetime
import json
import os
import tempfile
import unittest
import unittest.mock

import pandas as pd

from agents.screener.finviz_agent import (
    compute_stage,
    compute_vcp,
    compute_quality_score,
    _classify_ticker,
)
from agents.screener.finviz_weekly_agent import (
    load_daily_quality,
    _compute_quality_modifier,
    _detect_signals,
    build_persistence_scores,
    compute_revenue_growth,
    is_character_change_deep,
    auto_promote_to_watchlist,
)


# ============================================================
# Helpers
# ============================================================

def _make_row(**overrides) -> pd.Series:
    """Build a ticker row with sensible defaults, overridable per field."""
    base = {
        "Ticker": "TEST",
        "Company": "Test Corp",
        "Sector": "Technology",
        "Industry": "Software",
        "Country": "USA",
        "Market Cap": "10B",
        "Appearances": 2,
        "Screeners": "Growth, 52 Week High",
        "ATR%": 5.0,
        "EPS Y/Y TTM": 50.0,
        "Sales Y/Y TTM": 30.0,
        "Dist From High%": -8.0,
        "Rel Volume": 1.5,
        "Avg Volume": 5_000_000,
        "SMA20%": 4.0,
        "SMA50%": 2.5,
        "SMA200%": 1.0,
        "Stage": {"stage": 2, "badge": "🟢 Stage 2", "perfect": True,
                  "sma20": 4.0, "sma50": 2.5, "sma200": 1.0},
        "VCP": {"vcp_possible": False, "confidence": 0, "reason": "no signals"},
        "Quality Score": 65.0,
    }
    base.update(overrides)
    return pd.Series(base)


def _make_daily_csv_df(tickers_data: list[dict]) -> pd.DataFrame:
    """Build a DataFrame mimicking a daily CSV row for weekly aggregation."""
    rows = []
    for td in tickers_data:
        rows.append({
            "Ticker": td.get("Ticker", "TEST"),
            "Company": td.get("Company", "Test Corp"),
            "Sector": td.get("Sector", "Technology"),
            "Industry": td.get("Industry", "Software"),
            "Country": td.get("Country", "USA"),
            "Market Cap": td.get("Market Cap", "10B"),
            "Appearances": td.get("Appearances", 1),
            "Screeners": td.get("Screeners", "Growth"),
            "ATR%": td.get("ATR%", 5.0),
            "EPS Y/Y TTM": td.get("EPS Y/Y TTM", 50.0),
            "date": td.get("date", "2026-03-20"),
        })
    return pd.DataFrame(rows)


# ============================================================
# Part 1: Daily — Weinstein Stage Classification
# ============================================================

class TestComputeStage(unittest.TestCase):
    """Verify Weinstein stage assignment for various market conditions."""

    def test_stage2_confirmed_uptrend(self):
        """50MA above 200MA (sma200 > sma50), price near/above 50MA → Stage 2."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 10.0, "SMA200%": 25.0,
            "Rel Volume": 1.5, "Dist From High%": -10.0,
        })
        result = compute_stage(row)
        self.assertEqual(result["stage"], 2)
        self.assertIn("Stage 2", result["badge"])

    def test_stage2_perfect_minervini(self):
        """Stage 2 + price above all MAs (sma20 > 0, sma50 > 0) → perfect alignment."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 10.0, "SMA200%": 25.0,
            "Rel Volume": 1.5, "Dist From High%": -5.0,
        })
        result = compute_stage(row)
        self.assertTrue(result["perfect"])

    def test_stage2_not_perfect_on_pullback(self):
        """Stage 2 with price below 20MA (pullback) → perfect=False."""
        row = _make_row(**{
            "SMA20%": -2.0, "SMA50%": 5.0, "SMA200%": 15.0,
            "Rel Volume": 1.2, "Dist From High%": -15.0,
        })
        # MAs stacked (sma200 > sma50) → stage2, but sma20 < 0 → not perfect
        result = compute_stage(row)
        self.assertEqual(result["stage"], 2)
        self.assertFalse(result["perfect"])

    def test_not_stage2_when_no_volume(self):
        """MAs stacked but low volume → NOT Stage 2 (falls to Stage 1 since near 200d)."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 3.0, "SMA200%": 1.0,
            "Rel Volume": 0.5,  # below 1.0 threshold
            "Dist From High%": -8.0,
        })
        result = compute_stage(row)
        self.assertNotEqual(result["stage"], 2, "Low volume should prevent Stage 2")
        # SMA200=1.0 → within 8% of 200-day → Stage 1 (basing)
        self.assertEqual(result["stage"], 1)

    def test_transitional_when_far_from_high(self):
        """MAs stacked but >25% from high → Transitional (Stage 0)."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 3.0, "SMA200%": 1.0,
            "Rel Volume": 1.5,
            "Dist From High%": -30.0,  # beyond -25% threshold
        })
        result = compute_stage(row)
        self.assertNotEqual(result["stage"], 2, "-30% from high should prevent Stage 2")

    def test_stage3_distribution(self):
        """Price meaningfully below 50-day (< -5%) but above 200-day → Stage 3."""
        row = _make_row(**{
            "SMA20%": -2.0, "SMA50%": -6.0, "SMA200%": 3.0,
            "Rel Volume": 1.0, "Dist From High%": -20.0,
        })
        result = compute_stage(row)
        self.assertEqual(result["stage"], 3)

    def test_stage4_downtrend(self):
        """Price below 200-day → Stage 4."""
        row = _make_row(**{
            "SMA20%": -10.0, "SMA50%": -8.0, "SMA200%": -5.0,
            "Rel Volume": 1.0, "Dist From High%": -40.0,
        })
        result = compute_stage(row)
        self.assertEqual(result["stage"], 4)

    def test_stage1_basing(self):
        """Near 200-day, MAs not stacked for Stage 2 → Stage 1."""
        row = _make_row(**{
            "SMA20%": 3.0, "SMA50%": 5.0, "SMA200%": 2.0,
            "Rel Volume": 0.5, "Dist From High%": -35.0,
        })
        result = compute_stage(row)
        # sma200(2) NOT > sma50(5) → not stage2, abs(sma200)=2 < 8 → stage1
        self.assertEqual(result["stage"], 1)


# ============================================================
# Part 2: Daily — VCP Detection
# ============================================================

class TestComputeVCP(unittest.TestCase):
    """Verify Minervini VCP pattern detection."""

    def test_vcp_requires_stage2(self):
        """VCP is only meaningful in Stage 2."""
        row = _make_row(Stage={"stage": 0, "badge": "⚪", "perfect": False,
                               "sma20": 1, "sma50": 0.5, "sma200": 0.2})
        result = compute_vcp(row)
        self.assertFalse(result["vcp_possible"])
        self.assertIn("Not Stage 2", result["reason"])

    def test_vcp_detected_tight_range_low_volume(self):
        """Tight ATR + volume dry-up + tight pullback in Stage 2 → VCP possible."""
        row = _make_row(**{
            "ATR%": 3.5,            # tight
            "Rel Volume": 0.7,      # dry-up
            "Dist From High%": -8.0, # tight pullback
            "SMA20%": 2.0,          # above 20-day
            "Stage": {"stage": 2, "badge": "🟢", "perfect": True,
                      "sma20": 2.0, "sma50": 1.5, "sma200": 0.5},
        })
        result = compute_vcp(row)
        self.assertTrue(result["vcp_possible"])
        self.assertGreaterEqual(result["confidence"], 50)

    def test_vcp_not_detected_high_atr_high_volume(self):
        """High ATR + high volume in Stage 2 → VCP not likely."""
        row = _make_row(**{
            "ATR%": 8.0,
            "Rel Volume": 3.0,
            "Dist From High%": -2.0,  # too close — outside -3 to -20 window
            "SMA20%": 5.0,
            "Stage": {"stage": 2, "badge": "🟢", "perfect": True,
                      "sma20": 5.0, "sma50": 3.0, "sma200": 1.0},
        })
        result = compute_vcp(row)
        self.assertFalse(result["vcp_possible"])


# ============================================================
# Part 3: Daily — Quality Score
# ============================================================

class TestComputeQualityScore(unittest.TestCase):
    """Verify quality score calculation components."""

    def test_mega_cap_stage2_perfect_high_eps(self):
        """$100B+ market cap, Stage 2 perfect, high EPS → high Q score."""
        row = _make_row(**{
            "Market Cap": "150B",
            "Rel Volume": 2.5,
            "EPS Y/Y TTM": 120.0,
            "Appearances": 3,
            "Stage": {"stage": 2, "badge": "🟢", "perfect": True,
                      "sma20": 5, "sma50": 3, "sma200": 1},
            "VCP": {"vcp_possible": False, "confidence": 0, "reason": "no signals"},
            "Dist From High%": -8.0,
        })
        score = compute_quality_score(row)
        # 30 (mcap) + 15 (rvol 2.5) + 16 (eps 120) + 15 (3 apps) + 25+10 (stage2 perfect) + 2 (dist -8)
        self.assertGreaterEqual(score, 100)

    def test_micro_cap_stage4_low_score(self):
        """Micro cap, Stage 4 → low / negative Q score."""
        row = _make_row(**{
            "Market Cap": "200M",
            "Rel Volume": 0.5,
            "EPS Y/Y TTM": -10.0,
            "Appearances": 1,
            "Stage": {"stage": 4, "badge": "⚫", "perfect": False,
                      "sma20": -5, "sma50": -3, "sma200": -1},
            "VCP": {"vcp_possible": False, "confidence": 0, "reason": "no signals"},
            "Dist From High%": -60.0,
        })
        score = compute_quality_score(row)
        self.assertLess(score, 20)

    def test_vcp_bonus_adds_15(self):
        """VCP possible should add 15 points."""
        base_row = _make_row(VCP={"vcp_possible": False, "confidence": 0, "reason": ""})
        vcp_row = _make_row(VCP={"vcp_possible": True, "confidence": 60, "reason": "tight"})
        base_score = compute_quality_score(base_row)
        vcp_score = compute_quality_score(vcp_row)
        self.assertEqual(vcp_score - base_score, 15)

    def test_score_ranges_are_bounded(self):
        """Quality score should be in a reasonable range (roughly -35 to 150)."""
        # Best case
        best = _make_row(**{
            "Market Cap": "200B", "Rel Volume": 6.0, "EPS Y/Y TTM": 300.0,
            "Appearances": 4,
            "Stage": {"stage": 2, "badge": "🟢", "perfect": True,
                      "sma20": 10, "sma50": 5, "sma200": 2},
            "VCP": {"vcp_possible": True, "confidence": 80, "reason": "tight"},
            "Dist From High%": -55.0,
        })
        self.assertLessEqual(compute_quality_score(best), 160)

        # Worst case
        worst = _make_row(**{
            "Market Cap": "50M", "Rel Volume": 0.3, "EPS Y/Y TTM": -50.0,
            "Appearances": 1,
            "Stage": {"stage": 4, "badge": "⚫", "perfect": False,
                      "sma20": -10, "sma50": -5, "sma200": -3},
            "VCP": {"vcp_possible": False, "confidence": 0, "reason": ""},
            "Dist From High%": -2.0,
        })
        self.assertGreaterEqual(compute_quality_score(worst), -50)


# ============================================================
# Part 4: Daily — Ticker Classification (chart grid sections)
# ============================================================

class TestClassifyTicker(unittest.TestCase):

    def test_ipo_screener_classifies_as_ipo(self):
        row = _make_row(Screeners="IPO, Growth", Stage={"stage": 2})
        self.assertEqual(_classify_ticker(row), "ipo")

    def test_stage2_classifies_as_stage2(self):
        row = _make_row(Screeners="Growth", Stage={"stage": 2})
        self.assertEqual(_classify_ticker(row), "stage2")

    def test_high_rvol_high_atr_classifies_as_momentum(self):
        row = _make_row(Screeners="Growth", Stage={"stage": 0},
                        **{"Rel Volume": 3.0, "ATR%": 5.0})
        self.assertEqual(_classify_ticker(row), "momentum")

    def test_low_conviction_classifies_as_watch(self):
        row = _make_row(Screeners="Growth", Stage={"stage": 0},
                        **{"Rel Volume": 0.8, "ATR%": 3.0})
        self.assertEqual(_classify_ticker(row), "watch")

    def test_ipo_takes_priority_over_stage2(self):
        """IPO classification should win even if Stage 2."""
        row = _make_row(Screeners="IPO, Growth", Stage={"stage": 2},
                        **{"Rel Volume": 5.0, "ATR%": 8.0})
        self.assertEqual(_classify_ticker(row), "ipo")


# ============================================================
# Part 5: Weekly — Quality Modifier
# ============================================================

class TestQualityModifier(unittest.TestCase):
    """Verify stage + Q-rank → signal score modifier."""

    def test_stage2_high_q(self):
        self.assertEqual(_compute_quality_modifier(75, 2), 30)

    def test_stage2_mid_q(self):
        self.assertEqual(_compute_quality_modifier(45, 2), 15)

    def test_stage2_low_q(self):
        self.assertEqual(_compute_quality_modifier(30, 2), 0)

    def test_transitional_high_q(self):
        self.assertEqual(_compute_quality_modifier(65, 0), 10)

    def test_transitional_mid_q(self):
        self.assertEqual(_compute_quality_modifier(45, 0), 0)

    def test_transitional_low_q(self):
        self.assertEqual(_compute_quality_modifier(20, 0), -20)

    def test_stage1_penalty(self):
        self.assertEqual(_compute_quality_modifier(80, 1), -10)

    def test_stage3_penalty(self):
        self.assertEqual(_compute_quality_modifier(60, 3), -20)

    def test_stage4_heavy_penalty(self):
        self.assertEqual(_compute_quality_modifier(90, 4), -40)


# ============================================================
# Part 6: Weekly — Signal Detection
# ============================================================

class TestDetectSignals(unittest.TestCase):
    """Verify EP, IPO, MULTI, HIGH signal detection and bonus scoring."""

    def test_episodic_pivot(self):
        """Gap screener + 52W High + multi-screen → EP (+30)."""
        signals = _detect_signals({"10% Change", "52 Week High", "Growth"}, max_appearances=2)
        self.assertTrue(signals.get("EP"))
        self.assertGreaterEqual(signals["bonuses"], 30)

    def test_ep_with_week_20pct_gain(self):
        """Week 20%+ Gain also counts as gap screener for EP."""
        signals = _detect_signals({"Week 20%+ Gain", "52 Week High"}, max_appearances=2)
        self.assertTrue(signals.get("EP"))

    def test_ep_requires_multi_screen(self):
        """EP needs max_appearances ≥ 2."""
        signals = _detect_signals({"10% Change", "52 Week High"}, max_appearances=1)
        self.assertFalse(signals.get("EP", False))

    def test_ipo_signal(self):
        """IPO screener → IPO signal (+15)."""
        signals = _detect_signals({"IPO", "Growth"}, max_appearances=1)
        self.assertTrue(signals.get("IPO"))
        self.assertIn(15, [signals["bonuses"]])  # at least 15 from IPO

    def test_multi_screen_signal(self):
        """3+ appearances → MULTI (+20)."""
        signals = _detect_signals({"Growth"}, max_appearances=3)
        self.assertTrue(signals.get("MULTI"))

    def test_high_signal_without_ep(self):
        """52W High alone (no gap, so no EP) → HIGH (+10)."""
        signals = _detect_signals({"52 Week High", "Growth"}, max_appearances=1)
        self.assertTrue(signals.get("HIGH"))
        self.assertFalse(signals.get("EP", False))
        self.assertEqual(signals["bonuses"], 10)

    def test_high_suppressed_when_ep(self):
        """When EP fires, HIGH should NOT also fire (avoids double-counting)."""
        signals = _detect_signals({"10% Change", "52 Week High", "Growth"}, max_appearances=2)
        self.assertTrue(signals.get("EP"))
        self.assertFalse(signals.get("HIGH", False))

    def test_no_signals(self):
        """Single growth screener, 1 appearance → no signals."""
        signals = _detect_signals({"Growth"}, max_appearances=1)
        self.assertFalse(signals.get("EP", False))
        self.assertFalse(signals.get("IPO", False))
        self.assertFalse(signals.get("MULTI", False))
        self.assertFalse(signals.get("HIGH", False))
        self.assertEqual(signals["bonuses"], 0)

    def test_all_signals_stack(self):
        """EP + IPO + MULTI should all fire and bonuses stack."""
        signals = _detect_signals(
            {"10% Change", "52 Week High", "IPO", "Growth"},
            max_appearances=3,
        )
        self.assertTrue(signals.get("EP"))
        self.assertTrue(signals.get("IPO"))
        self.assertTrue(signals.get("MULTI"))
        # EP(30) + IPO(15) + MULTI(20) = 65
        self.assertEqual(signals["bonuses"], 65)


# ============================================================
# Part 6b: Weekly — Character Change Signal
# ============================================================

class TestCharacterChangeSignal(unittest.TestCase):
    """Verify character change detection: 200d gain > 50%, RVol > 2.5x, Week 20%+ Gain."""

    def test_char_change_detected(self):
        """Ticker meeting all 3 criteria gets CHAR bonus (+25)."""
        combined = _make_daily_csv_df([{
            "Ticker": "SEDG", "Screeners": "Week 20%+ Gain, Growth",
            "Appearances": 1, "date": "2026-03-20",
        }])
        # Add the metrics columns that build_persistence_scores reads
        combined["SMA200%"] = [55.0]   # > 50
        combined["Rel Volume"] = [3.0]  # > 2.5
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        sedg = result[result["Ticker"] == "SEDG"].iloc[0]

        self.assertTrue(sedg["CHAR"])
        # Signal score should include +25 CHAR bonus
        base = sedg["Base Score"]
        expected_bonus = 25  # CHAR only (no EP/IPO/MULTI/HIGH)
        self.assertGreaterEqual(sedg["Signal Score"], base + expected_bonus)

    def test_char_change_not_detected_low_sma200(self):
        """SMA200% ≤ 50 → no character change."""
        combined = _make_daily_csv_df([{
            "Ticker": "TEST", "Screeners": "Week 20%+ Gain",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [40.0]   # ≤ 50
        combined["Rel Volume"] = [3.0]
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        self.assertFalse(result.iloc[0]["CHAR"])

    def test_char_change_not_detected_low_rvol(self):
        """Rel Volume ≤ 2.5 → no character change."""
        combined = _make_daily_csv_df([{
            "Ticker": "TEST", "Screeners": "Week 20%+ Gain",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [55.0]
        combined["Rel Volume"] = [2.0]  # ≤ 2.5
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        self.assertFalse(result.iloc[0]["CHAR"])

    def test_char_change_not_detected_wrong_screener(self):
        """Missing 'Week 20%+ Gain' screener → no character change."""
        combined = _make_daily_csv_df([{
            "Ticker": "TEST", "Screeners": "Growth, 52 Week High",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [55.0]
        combined["Rel Volume"] = [3.0]
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        self.assertFalse(result.iloc[0]["CHAR"])

    def test_char_change_stacks_with_other_signals(self):
        """CHAR should stack with EP and other signals."""
        combined = _make_daily_csv_df([{
            "Ticker": "BOOM", "Screeners": "Week 20%+ Gain, 10% Change, 52 Week High",
            "Appearances": 2, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [60.0]
        combined["Rel Volume"] = [4.0]
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        boom = result[result["Ticker"] == "BOOM"].iloc[0]

        self.assertTrue(boom["CHAR"])
        self.assertTrue(boom["EP"])  # gap + high + multi
        # EP(30) + CHAR(25) should both be in signal score
        self.assertGreater(boom["Signal Score"], boom["Base Score"] + 50)

    def test_char_change_boosts_ranking(self):
        """CHAR ticker should rank above equivalent ticker without CHAR."""
        combined = pd.concat([
            _make_daily_csv_df([
                {"Ticker": "CHAR_YES", "Screeners": "Week 20%+ Gain, Growth",
                 "Appearances": 1, "date": "2026-03-20"},
                {"Ticker": "CHAR_NO", "Screeners": "Growth",
                 "Appearances": 1, "date": "2026-03-20"},
            ]),
        ], ignore_index=True)
        # Only CHAR_YES has the character change metrics
        combined["SMA200%"] = [55.0, 10.0]
        combined["Rel Volume"] = [3.0, 1.0]
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        char_yes = result[result["Ticker"] == "CHAR_YES"].iloc[0]
        char_no = result[result["Ticker"] == "CHAR_NO"].iloc[0]

        self.assertGreater(char_yes["Signal Score"], char_no["Signal Score"])


# ============================================================
# Part 6b: Character Change Deep Check (yfinance-backed)
# ============================================================

class TestRevenueGrowthComputation(unittest.TestCase):
    """Tests for compute_revenue_growth helper."""

    def test_basic_growth(self):
        """Revenue growing 10% each quarter."""
        rev = [100, 110, 121, 133.1]
        growth = compute_revenue_growth(rev)
        self.assertEqual(len(growth), 3)
        self.assertAlmostEqual(growth[0], 10.0, places=0)

    def test_declining_revenue(self):
        """Revenue declining should show negative growth."""
        rev = [200, 180, 160]
        growth = compute_revenue_growth(rev)
        self.assertLess(growth[0], 0)
        self.assertLess(growth[1], 0)

    def test_single_quarter(self):
        """Insufficient data returns empty list."""
        growth = compute_revenue_growth([100])
        self.assertEqual(growth, [])

    def test_empty_input(self):
        growth = compute_revenue_growth([])
        self.assertEqual(growth, [])

    def test_zero_previous_revenue(self):
        """Zero in prior quarter should produce 0 growth, not crash."""
        rev = [0, 100, 200]
        growth = compute_revenue_growth(rev)
        self.assertEqual(growth[0], 0.0)
        self.assertGreater(growth[1], 0)

    def test_accelerating_growth(self):
        """Each quarter growing faster than the last."""
        rev = [100, 105, 115, 135]  # +5%, +9.5%, +17.4%
        growth = compute_revenue_growth(rev)
        self.assertLess(growth[0], growth[1])
        self.assertLess(growth[1], growth[2])


class TestIsCharacterChangeDeep(unittest.TestCase):
    """Tests for the 4-condition character change deep check.

    These tests mock yfinance by patching fetch_earnings_history.
    """

    def _mock_earnings(self, eps_values, revenue_values):
        """Create a mock earnings result."""
        return {
            "eps_history": eps_values,
            "revenue_history": revenue_values,
        }

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_all_conditions_met_is_cc(self, mock_fetch):
        """All 4 conditions met → is_cc = True."""
        # EPS: clearly improving across 4 quarters
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        result = is_character_change_deep("SEDG", sma200_pct=15.0, rvol=3.0)
        self.assertTrue(result["is_cc"])
        self.assertFalse(result["is_cc_watch"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_eps_not_improving_no_cc(self, mock_fetch):
        """EPS flat/declining → no CC."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[2.0, 2.0, 1.5, 1.0],
            revenue_values=[100, 110, 120, 135],
        )
        result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=3.0)
        self.assertFalse(result["is_cc"])
        self.assertFalse(result["is_cc_watch"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_sma200_negative_no_cc(self, mock_fetch):
        """Below 200-day MA → no CC (MA not cleared)."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        result = is_character_change_deep("TEST", sma200_pct=-5.0, rvol=3.0)
        self.assertFalse(result["is_cc"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_sma200_too_high_no_cc(self, mock_fetch):
        """SMA200% > 60 means stock ran too far — not a fresh clearing."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        result = is_character_change_deep("TEST", sma200_pct=65.0, rvol=3.0)
        self.assertFalse(result["is_cc"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_low_rvol_no_cc(self, mock_fetch):
        """RVol < 2.0 → no volume confirmation."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=1.5)
        self.assertFalse(result["is_cc"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_cc_watch_when_sales_positive_but_not_accelerating(self, mock_fetch):
        """EPS improving + volume + MA cleared, but sales flat → CC_WATCH."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 120, 125, 130, 133],  # growing but decelerating
        )
        result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=2.5)
        self.assertFalse(result["is_cc"])
        self.assertTrue(result["is_cc_watch"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_sma200_none_no_cc(self, mock_fetch):
        """None SMA200 → immediate return, no CC."""
        result = is_character_change_deep("TEST", sma200_pct=None, rvol=3.0)
        self.assertFalse(result["is_cc"])
        mock_fetch.assert_not_called()

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_no_earnings_data_no_cc(self, mock_fetch):
        """yfinance returns None → no CC."""
        mock_fetch.return_value = None
        result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=3.0)
        self.assertFalse(result["is_cc"])

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_cc_deep_gets_35_bonus_in_scoring(self, mock_fetch):
        """Deep CC confirmed should add +35 to signal score."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        cc_result = is_character_change_deep("SEDG", sma200_pct=15.0, rvol=3.0)
        self.assertTrue(cc_result["is_cc"])

        combined = _make_daily_csv_df([{
            "Ticker": "SEDG", "Screeners": "Growth",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [15.0]
        combined["Rel Volume"] = [3.0]
        dates = ["2026-03-20"]

        # With cc_results
        result = build_persistence_scores(combined, dates, cc_results={"SEDG": cc_result})
        sedg = result[result["Ticker"] == "SEDG"].iloc[0]
        self.assertTrue(sedg["CC_DEEP"])
        self.assertGreaterEqual(sedg["Signal Score"], sedg["Base Score"] + 35)

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_cc_watch_gets_25_bonus_in_scoring(self, mock_fetch):
        """CC Watch should add +25 to signal score (same as old CHAR)."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 120, 125, 130, 133],
        )
        cc_result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=2.5)
        self.assertTrue(cc_result["is_cc_watch"])

        combined = _make_daily_csv_df([{
            "Ticker": "TEST", "Screeners": "Growth",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [15.0]
        combined["Rel Volume"] = [2.5]
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates, cc_results={"TEST": cc_result})
        test = result[result["Ticker"] == "TEST"].iloc[0]
        self.assertTrue(test["CC_WATCH"])
        self.assertGreaterEqual(test["Signal Score"], test["Base Score"] + 25)

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_cc_deep_overrides_simple_heuristic(self, mock_fetch):
        """When deep CC fires, the simple CHAR heuristic bonus (+25) should not stack.
        Deep CC gives +35, not +35 + 25."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        cc_result = is_character_change_deep("SEDG", sma200_pct=15.0, rvol=3.0)

        # This ticker also meets simple CHAR heuristic (SMA200 > 50, RVol > 2.5, Week 20%+)
        combined = _make_daily_csv_df([{
            "Ticker": "SEDG", "Screeners": "Week 20%+ Gain, Growth",
            "Appearances": 1, "date": "2026-03-20",
        }])
        combined["SMA200%"] = [55.0]  # meets simple heuristic too
        combined["Rel Volume"] = [3.0]
        dates = ["2026-03-20"]

        # Without cc_results → simple heuristic (+25)
        result_simple = build_persistence_scores(combined, dates)
        sedg_simple = result_simple[result_simple["Ticker"] == "SEDG"].iloc[0]

        # With cc_results → deep CC (+35)
        result_deep = build_persistence_scores(combined, dates, cc_results={"SEDG": cc_result})
        sedg_deep = result_deep[result_deep["Ticker"] == "SEDG"].iloc[0]

        # Deep should be exactly 10 more than simple (35 vs 25)
        self.assertAlmostEqual(
            sedg_deep["Signal Score"] - sedg_simple["Signal Score"], 10.0,
            msg="Deep CC (+35) should be exactly 10 more than simple CHAR (+25)"
        )

    @unittest.mock.patch("agents.screener.finviz_weekly_agent.fetch_earnings_history")
    def test_conditions_tracking(self, mock_fetch):
        """Result should track which conditions passed and failed."""
        mock_fetch.return_value = self._mock_earnings(
            eps_values=[-2.0, -1.0, 0.5, 1.0, 2.0, 3.0],
            revenue_values=[100, 110, 125, 145, 170, 205],
        )
        result = is_character_change_deep("TEST", sma200_pct=15.0, rvol=3.0)
        self.assertGreater(len(result["conditions_met"]), 0)
        self.assertIsInstance(result["eps_trend"], list)
        self.assertIsInstance(result["sales_trend"], list)


# ============================================================
# Part 7: Weekly — Load Daily Quality JSONs
# ============================================================

class TestLoadDailyQuality(unittest.TestCase):
    """Verify daily quality JSON loading and merge logic."""

    def test_loads_single_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            today = datetime.date.today().strftime("%Y-%m-%d")
            data = {"NVDA": {"q_rank": 80, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}}
            with open(os.path.join(tmpdir, f"daily_quality_{today}.json"), "w") as f:
                json.dump(data, f)

            result = load_daily_quality(tmpdir, lookback_days=7)
            self.assertIn("NVDA", result)
            self.assertEqual(result["NVDA"]["q_rank"], 80)

    def test_most_recent_day_wins(self):
        """For tickers on multiple days, most recent data takes precedence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            today = datetime.date.today()

            # Yesterday — old data
            yesterday = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            old_data = {"NVDA": {"q_rank": 50, "stage": 0, "stage_label": "Transitional", "section": "watch"}}
            with open(os.path.join(tmpdir, f"daily_quality_{yesterday}.json"), "w") as f:
                json.dump(old_data, f)

            # Today — newer data
            today_str = today.strftime("%Y-%m-%d")
            new_data = {"NVDA": {"q_rank": 85, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}}
            with open(os.path.join(tmpdir, f"daily_quality_{today_str}.json"), "w") as f:
                json.dump(new_data, f)

            result = load_daily_quality(tmpdir, lookback_days=7)
            # load_daily_quality iterates i=0 (today) first, then i=1 (yesterday)
            # Today's data loads first; yesterday doesn't overwrite
            self.assertEqual(result["NVDA"]["q_rank"], 85)
            self.assertEqual(result["NVDA"]["stage"], 2)

    def test_merges_different_tickers_across_days(self):
        """Tickers from different days should all appear."""
        with tempfile.TemporaryDirectory() as tmpdir:
            today = datetime.date.today()

            day1 = today.strftime("%Y-%m-%d")
            day2 = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

            with open(os.path.join(tmpdir, f"daily_quality_{day1}.json"), "w") as f:
                json.dump({"NVDA": {"q_rank": 80, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}}, f)
            with open(os.path.join(tmpdir, f"daily_quality_{day2}.json"), "w") as f:
                json.dump({"COIN": {"q_rank": 60, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}}, f)

            result = load_daily_quality(tmpdir, lookback_days=7)
            self.assertIn("NVDA", result)
            self.assertIn("COIN", result)

    def test_empty_directory_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_daily_quality(tmpdir, lookback_days=7)
            self.assertEqual(result, {})

    def test_corrupted_json_skipped(self):
        """Malformed JSON files should be skipped gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            today = datetime.date.today().strftime("%Y-%m-%d")
            with open(os.path.join(tmpdir, f"daily_quality_{today}.json"), "w") as f:
                f.write("{bad json!!!")

            result = load_daily_quality(tmpdir, lookback_days=7)
            self.assertEqual(result, {})


# ============================================================
# Part 8: Weekly — build_persistence_scores (end-to-end)
# ============================================================

class TestBuildPersistenceScores(unittest.TestCase):
    """Verify the full weekly scoring pipeline."""

    def test_basic_persistence_score(self):
        """Single ticker, 3 days out of 5 → base score ~60 + screener diversity."""
        combined = pd.concat([
            _make_daily_csv_df([{"Ticker": "NVDA", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-18"}]),
            _make_daily_csv_df([{"Ticker": "NVDA", "Screeners": "Growth, 52 Week High", "Appearances": 2, "date": "2026-03-19"}]),
            _make_daily_csv_df([{"Ticker": "NVDA", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"}]),
        ], ignore_index=True)
        dates = ["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20"]

        result = build_persistence_scores(combined, dates)
        nvda = result[result["Ticker"] == "NVDA"].iloc[0]

        self.assertEqual(nvda["Days Seen"], 3)
        self.assertEqual(nvda["Total Days"], 5)
        # Base: (3/5)*100=60 + screener diversity(2)*10=20 + multi-screen(2)=20 = 100
        self.assertGreaterEqual(nvda["Base Score"], 80)

    def test_quality_modifier_applied(self):
        """Daily quality data should modify signal score."""
        combined = _make_daily_csv_df([
            {"Ticker": "NVDA", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
        ])
        dates = ["2026-03-20"]
        quality = {"NVDA": {"q_rank": 80, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}}

        result = build_persistence_scores(combined, dates, daily_quality=quality)
        nvda = result[result["Ticker"] == "NVDA"].iloc[0]

        # Stage 2 + Q≥60 → +30 modifier
        self.assertEqual(nvda["Quality Mod"], 30)
        self.assertEqual(nvda["Stage"], "Uptrend")
        self.assertEqual(nvda["Q Rank"], 80)

    def test_stage4_penalty_lowers_score(self):
        """Stage 4 tickers should get -40 quality modifier."""
        combined = _make_daily_csv_df([
            {"Ticker": "BAD", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
        ])
        dates = ["2026-03-20"]
        quality = {"BAD": {"q_rank": 30, "stage": 4, "stage_label": "Downtrend", "section": "watch"}}

        result = build_persistence_scores(combined, dates, daily_quality=quality)
        bad = result[result["Ticker"] == "BAD"].iloc[0]

        self.assertEqual(bad["Quality Mod"], -40)
        self.assertTrue(bad["Watch"])

    def test_watch_flag_set_for_watch_section(self):
        """Tickers with section='watch' should have Watch=True."""
        combined = _make_daily_csv_df([
            {"Ticker": "MEH", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
        ])
        dates = ["2026-03-20"]
        quality = {"MEH": {"q_rank": 20, "stage": 0, "stage_label": "Transitional", "section": "watch"}}

        result = build_persistence_scores(combined, dates, daily_quality=quality)
        meh = result[result["Ticker"] == "MEH"].iloc[0]
        self.assertTrue(meh["Watch"])

    def test_ep_signal_boosts_score(self):
        """Ticker with EP-qualifying screeners should get EP bonus."""
        combined = _make_daily_csv_df([
            {"Ticker": "PL", "Screeners": "10% Change, 52 Week High", "Appearances": 2, "date": "2026-03-20"},
        ])
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates)
        pl = result[result["Ticker"] == "PL"].iloc[0]

        self.assertTrue(pl["EP"])
        # Signal score should include EP bonus (+30)
        self.assertGreater(pl["Signal Score"], pl["Base Score"])

    def test_no_quality_data_means_zero_modifier(self):
        """Missing quality data → no modifier applied."""
        combined = _make_daily_csv_df([
            {"Ticker": "NEW", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
        ])
        dates = ["2026-03-20"]

        result = build_persistence_scores(combined, dates, daily_quality={})
        new = result[result["Ticker"] == "NEW"].iloc[0]
        self.assertEqual(new["Quality Mod"], 0)
        self.assertIsNone(new["Q Rank"])

    def test_empty_dataframe_returns_empty(self):
        result = build_persistence_scores(pd.DataFrame(), [])
        self.assertTrue(result.empty)

    def test_ranking_order_with_mixed_quality(self):
        """Stage 2 high-Q ticker should rank above Stage 4 ticker despite same persistence."""
        combined = pd.concat([
            _make_daily_csv_df([
                {"Ticker": "GOOD", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
                {"Ticker": "BAD", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
            ]),
        ], ignore_index=True)
        dates = ["2026-03-20"]
        quality = {
            "GOOD": {"q_rank": 80, "stage": 2, "stage_label": "Uptrend", "section": "stage2"},
            "BAD": {"q_rank": 30, "stage": 4, "stage_label": "Downtrend", "section": "watch"},
        }

        result = build_persistence_scores(combined, dates, daily_quality=quality)
        good = result[result["Ticker"] == "GOOD"].iloc[0]
        bad = result[result["Ticker"] == "BAD"].iloc[0]

        # Stage 2 Q≥60: +30 vs Stage 4: -40 → 70 point swing
        self.assertGreater(good["Signal Score"], bad["Signal Score"])


# ============================================================
# Part 9: End-to-End Pipeline — Daily Quality JSON → Weekly Ranking
# ============================================================

class TestEndToEndPipeline(unittest.TestCase):
    """
    Simulate the full daily → weekly pipeline:
    1. Create daily quality JSONs (as daily agent would)
    2. Load them (as weekly agent does)
    3. Build persistence scores with quality merge
    4. Verify final rankings reflect quality data
    """

    def test_full_pipeline_5_day_week(self):
        """Simulate a 5-day trading week with quality data driving rankings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            today = datetime.date.today()
            dates = []
            daily_dfs = []

            # Simulate 5 days of daily data
            for i in range(5):
                date = today - datetime.timedelta(days=4 - i)
                date_str = date.strftime("%Y-%m-%d")
                dates.append(date_str)

                # NVDA appears every day (persistent), COIN on 3 days, JUNK on 2 days
                tickers = [
                    {"Ticker": "NVDA", "Screeners": "Growth, 52 Week High",
                     "Appearances": 2, "ATR%": 6.0, "EPS Y/Y TTM": 80.0, "date": date_str},
                ]
                if i >= 2:  # COIN appears days 3-5
                    tickers.append(
                        {"Ticker": "COIN", "Screeners": "10% Change, 52 Week High",
                         "Appearances": 2, "ATR%": 5.0, "EPS Y/Y TTM": 30.0, "date": date_str}
                    )
                if i < 2:  # JUNK appears days 1-2 only
                    tickers.append(
                        {"Ticker": "JUNK", "Screeners": "Growth",
                         "Appearances": 1, "ATR%": 3.5, "EPS Y/Y TTM": -5.0, "date": date_str}
                    )

                daily_dfs.append(_make_daily_csv_df(tickers))

                # Write daily quality JSON (most recent day has latest quality data)
                quality_data = {
                    "NVDA": {"q_rank": 85, "stage": 2, "stage_label": "Uptrend", "section": "stage2"},
                }
                if i >= 2:
                    quality_data["COIN"] = {"q_rank": 65, "stage": 2, "stage_label": "Uptrend", "section": "stage2"}
                if i < 2:
                    quality_data["JUNK"] = {"q_rank": 15, "stage": 4, "stage_label": "Downtrend", "section": "watch"}

                with open(os.path.join(tmpdir, f"daily_quality_{date_str}.json"), "w") as f:
                    json.dump(quality_data, f)

            # Load quality data (as weekly agent does)
            daily_quality = load_daily_quality(tmpdir, lookback_days=7)

            # Verify quality data loaded correctly
            self.assertIn("NVDA", daily_quality)
            self.assertIn("COIN", daily_quality)
            self.assertIn("JUNK", daily_quality)
            self.assertEqual(daily_quality["NVDA"]["q_rank"], 85)

            # Build persistence scores
            combined = pd.concat(daily_dfs, ignore_index=True)
            result = build_persistence_scores(combined, dates, daily_quality=daily_quality)

            # Verify rankings
            result_sorted = result.sort_values("Signal Score", ascending=False)
            tickers_ranked = result_sorted["Ticker"].tolist()

            # NVDA should rank #1: 5 days persistence + Stage 2 Q=85 (+30)
            self.assertEqual(tickers_ranked[0], "NVDA")

            # JUNK should be last: 2 days + Stage 4 (-40) + Watch
            junk = result[result["Ticker"] == "JUNK"].iloc[0]
            self.assertTrue(junk["Watch"])
            self.assertEqual(junk["Quality Mod"], -40)

            # COIN should rank above JUNK: Stage 2 Q=65 (+30) + EP signal
            coin = result[result["Ticker"] == "COIN"].iloc[0]
            self.assertGreater(coin["Signal Score"], junk["Signal Score"])

    def test_pipeline_no_quality_data_still_works(self):
        """Weekly ranking should work even if no daily quality JSONs exist."""
        combined = pd.concat([
            _make_daily_csv_df([
                {"Ticker": "AAPL", "Screeners": "Growth", "Appearances": 1, "date": "2026-03-20"},
                {"Ticker": "MSFT", "Screeners": "Growth, IPO", "Appearances": 2, "date": "2026-03-20"},
            ]),
        ], ignore_index=True)
        dates = ["2026-03-20"]

        # No quality data
        result = build_persistence_scores(combined, dates, daily_quality={})
        self.assertEqual(len(result), 2)

        # Both should have zero quality modifier
        for _, row in result.iterrows():
            self.assertEqual(row["Quality Mod"], 0)


# ============================================================
# Part 10: Regression guards
# ============================================================

class TestRegressionGuards(unittest.TestCase):
    """Catch specific regressions that could silently break the pipeline."""

    def test_quality_json_schema(self):
        """Daily quality JSON must have required keys."""
        required_keys = {"q_rank", "stage", "stage_label", "section"}
        # Simulate what finviz_agent writes
        row = _make_row()
        stage_data = row.get("Stage", {})
        stage_num = stage_data.get("stage", 0) if isinstance(stage_data, dict) else 0
        stage_labels = {1: "Basing", 2: "Uptrend", 3: "Distribution", 4: "Downtrend", 0: "Transitional"}
        section = _classify_ticker(row)

        quality_entry = {
            "q_rank": round(float(row.get("Quality Score", 0) or 0)),
            "stage": stage_num,
            "stage_label": stage_labels.get(stage_num, "Transitional"),
            "section": section,
        }

        self.assertEqual(set(quality_entry.keys()), required_keys)
        self.assertIsInstance(quality_entry["q_rank"], int)
        self.assertIsInstance(quality_entry["stage"], int)
        self.assertIsInstance(quality_entry["stage_label"], str)
        self.assertIsInstance(quality_entry["section"], str)
        self.assertIn(quality_entry["section"], {"stage2", "ipo", "momentum", "watch"})

    def test_weekly_reads_what_daily_writes(self):
        """Weekly agent's quality modifier must handle all sections daily writes."""
        valid_sections = {"stage2", "ipo", "momentum", "watch"}
        valid_stages = {0, 1, 2, 3, 4}

        for section in valid_sections:
            for stage in valid_stages:
                # Should not raise
                mod = _compute_quality_modifier(50, stage)
                self.assertIsInstance(mod, int)

    def test_build_persistence_handles_missing_columns_gracefully(self):
        """If a daily CSV is missing optional columns, pipeline shouldn't crash."""
        minimal_df = pd.DataFrame({
            "Ticker": ["TEST"],
            "Screeners": ["Growth"],
            "date": ["2026-03-20"],
        })
        # Should not raise
        result = build_persistence_scores(minimal_df, ["2026-03-20"])
        self.assertEqual(len(result), 1)


# ============================================================
# Part 11: Weekly — auto_promote_to_watchlist
# ============================================================

class TestAutoPromoteToWatchlist(unittest.TestCase):
    """Regression tests for auto_promote_to_watchlist.

    Covers the bug where 'Screeners Hit' contained screener names
    (e.g. '10% Change, 52 Week High') instead of an integer count,
    causing a ValueError on int() conversion.
    """

    def _make_persistence_df(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_screener_names_string_counted_correctly(self):
        """'Screeners Hit' as comma-separated names → count by splitting."""
        df = self._make_persistence_df([{
            "Ticker": "FLY",
            "Days Seen": 4,
            "Screeners Hit": "10% Change, 52 Week High, Growth",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0][0], "FLY")
        self.assertEqual(promoted[0][2], 3)  # 3 screeners counted

    def test_screener_names_below_min_screens_not_promoted(self):
        """2 screener names → count=2 → below min_screens=3 → not promoted."""
        df = self._make_persistence_df([{
            "Ticker": "MEH",
            "Days Seen": 5,
            "Screeners Hit": "Growth, 52 Week High",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 0)

    def test_integer_screens_value_still_works(self):
        """'Screeners Hit' as an integer (old format) → still works."""
        df = self._make_persistence_df([{
            "Ticker": "OLD",
            "Days Seen": 4,
            "Screeners Hit": 4,
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 1)

    def test_below_min_days_not_promoted(self):
        """Days Seen < min_days → not promoted."""
        df = self._make_persistence_df([{
            "Ticker": "SHY",
            "Days Seen": 2,
            "Screeners Hit": "Growth, 52 Week High, IPO, Momentum",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 0)

    def test_existing_watchlist_ticker_not_re_added(self):
        """Ticker already in watchlist.json → skipped."""
        df = self._make_persistence_df([{
            "Ticker": "NVDA",
            "Days Seen": 5,
            "Screeners Hit": "Growth, 52 Week High, IPO",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"tickers": ["NVDA"], "auto_promoted": []}, f)
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 0)

    def test_new_watchlist_file_created(self):
        """watchlist.json created from scratch when it doesn't exist."""
        df = self._make_persistence_df([{
            "Ticker": "NEW",
            "Days Seen": 4,
            "Screeners Hit": "Growth, 52 Week High, IPO",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
            self.assertEqual(len(promoted), 1)
            with open(wl_path) as f:
                saved = json.load(f)
            self.assertIn("NEW", saved["tickers"])

    def test_screens_fallback_to_screens_column(self):
        """Falls back to 'screens' column when 'Screeners Hit' is absent."""
        df = self._make_persistence_df([{
            "Ticker": "ALT",
            "Days Seen": 4,
            "screens": 3,
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            wl_path = os.path.join(tmpdir, "watchlist.json")
            promoted = auto_promote_to_watchlist(
                df, watchlist_path=wl_path, min_days=3, min_screens=3
            )
        self.assertEqual(len(promoted), 1)


# ============================================================
# Market Monitor Tests
# ============================================================

from agents.market.finviz_market_monitor import (
    calculate_metrics,
    classify_market_state,
    is_blackout,
    build_daily_record,
    THRUST_THRESHOLD,
    DANGER_DOWN_THRESHOLD,
)
from agents.screener.finviz_weekly_agent import load_market_state, any_thrust_in_history


def _make_monitor_day(**overrides) -> dict:
    """Build a market monitor daily record with sensible defaults."""
    base = {
        "date": "2026-03-20",
        "up_4_today": 50,
        "down_4_today": 100,
        "breadth_source": "alpaca_4pct",
        "universe_size": 2500,
        "adv_total": 1800,
        "dec_total": 900,
        "up_25_quarter": 200,
        "down_25_quarter": 400,
        "thrust_detected": False,
        "fg": 45.0,
        "spy_price": 550.0,
        "spy_sma200_pct": 2.0,
        "spy_above_200d": True,
        "market_state": "RED",
        "state_message": "No new trades",
        "ratio_today": 0.5,
        "ratio_5day": 0.6,
        "ratio_10day": 0.7,
        "blackout": False,
    }
    base.update(overrides)
    return base


class TestCalculateMetrics(unittest.TestCase):
    """Tests for breadth ratio calculations and thrust detection."""

    def test_daily_ratio_basic(self):
        """Daily ratio = up_4 / down_4."""
        today = {"up_4_today": 100, "down_4_today": 50,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics([], today)
        self.assertAlmostEqual(metrics["ratio_today"], 2.0)

    def test_daily_ratio_zero_down(self):
        """Zero down stocks should not divide by zero."""
        today = {"up_4_today": 100, "down_4_today": 0,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics([], today)
        self.assertEqual(metrics["ratio_today"], 100.0)

    def test_5day_ratio_with_history(self):
        """5-day ratio uses last 4 history days + today."""
        history = [
            {"up_4_today": 80, "down_4_today": 40},
            {"up_4_today": 60, "down_4_today": 30},
            {"up_4_today": 100, "down_4_today": 50},
            {"up_4_today": 70, "down_4_today": 35},
        ]
        today = {"up_4_today": 90, "down_4_today": 45,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics(history, today)
        # (80+60+100+70+90) / (40+30+50+35+45) = 400/200 = 2.0
        self.assertAlmostEqual(metrics["ratio_5day"], 2.0)

    def test_5day_ratio_short_history(self):
        """With less than 4 history days, uses what's available + today."""
        history = [{"up_4_today": 100, "down_4_today": 50}]
        today = {"up_4_today": 100, "down_4_today": 50,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics(history, today)
        self.assertAlmostEqual(metrics["ratio_5day"], 2.0)

    def test_10day_ratio_with_full_history(self):
        """10-day ratio uses last 9 history days + today."""
        history = [{"up_4_today": 50, "down_4_today": 100}] * 9
        today = {"up_4_today": 50, "down_4_today": 100,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics(history, today)
        self.assertAlmostEqual(metrics["ratio_10day"], 0.5)

    def test_thrust_detected_at_threshold(self):
        """Thrust fires when up_4 >= THRUST_THRESHOLD."""
        today = {"up_4_today": THRUST_THRESHOLD, "down_4_today": 10,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics([], today)
        self.assertTrue(metrics["thrust"])

    def test_thrust_not_detected_below_threshold(self):
        """Thrust does not fire below threshold."""
        today = {"up_4_today": THRUST_THRESHOLD - 1, "down_4_today": 10,

                 "spy_sma200_pct": 1.0}
        metrics = calculate_metrics([], today)
        self.assertFalse(metrics["thrust"])

    def test_spy_above_200d_positive(self):
        """SPY is above 200d MA when sma200_pct > 0."""
        today = {"up_4_today": 50, "down_4_today": 50,

                 "spy_sma200_pct": 3.5}
        metrics = calculate_metrics([], today)
        self.assertTrue(metrics["spy_above_200d"])

    def test_spy_below_200d_negative(self):
        """SPY is below 200d MA when sma200_pct < 0."""
        today = {"up_4_today": 50, "down_4_today": 50,

                 "spy_sma200_pct": -2.0}
        metrics = calculate_metrics([], today)
        self.assertFalse(metrics["spy_above_200d"])

    def test_spy_200d_none(self):
        """SPY above 200d should be False when data unavailable."""
        today = {"up_4_today": 50, "down_4_today": 50,

                 "spy_sma200_pct": None}
        metrics = calculate_metrics([], today)
        self.assertFalse(metrics["spy_above_200d"])


class TestBlackoutPeriods(unittest.TestCase):
    """Tests for seasonal blackout detection."""

    def test_september_is_blackout(self):
        self.assertTrue(is_blackout(datetime.date(2026, 9, 1)))
        self.assertTrue(is_blackout(datetime.date(2026, 9, 15)))
        self.assertTrue(is_blackout(datetime.date(2026, 9, 30)))

    def test_october_before_16_is_blackout(self):
        self.assertTrue(is_blackout(datetime.date(2026, 10, 1)))
        self.assertTrue(is_blackout(datetime.date(2026, 10, 15)))

    def test_october_after_15_is_not_blackout(self):
        self.assertFalse(is_blackout(datetime.date(2026, 10, 16)))
        self.assertFalse(is_blackout(datetime.date(2026, 10, 31)))

    def test_february_is_blackout(self):
        self.assertTrue(is_blackout(datetime.date(2026, 2, 1)))
        self.assertTrue(is_blackout(datetime.date(2026, 2, 28)))

    def test_march_before_16_is_blackout(self):
        self.assertTrue(is_blackout(datetime.date(2026, 3, 1)))
        self.assertTrue(is_blackout(datetime.date(2026, 3, 15)))

    def test_march_after_15_is_not_blackout(self):
        self.assertFalse(is_blackout(datetime.date(2026, 3, 16)))
        self.assertFalse(is_blackout(datetime.date(2026, 3, 31)))

    def test_normal_months_not_blackout(self):
        self.assertFalse(is_blackout(datetime.date(2026, 4, 15)))
        self.assertFalse(is_blackout(datetime.date(2026, 6, 1)))
        self.assertFalse(is_blackout(datetime.date(2026, 7, 4)))
        self.assertFalse(is_blackout(datetime.date(2026, 11, 15)))
        self.assertFalse(is_blackout(datetime.date(2026, 12, 25)))
        self.assertFalse(is_blackout(datetime.date(2026, 1, 15)))


class TestClassifyMarketState(unittest.TestCase):
    """Tests for market state classification logic."""

    def _make_metrics(self, **overrides):
        base = {
            "ratio_today": 1.0,
            "ratio_5day": 1.0,
            "ratio_10day": 1.0,
            "thrust": False,
            "spy_above_200d": True,
        }
        base.update(overrides)
        return base

    def test_blackout_overrides_everything(self):
        """Blackout should override even thrust signals."""
        metrics = self._make_metrics(thrust=True)
        today = {"up_4_today": 600, "down_4_today": 10}
        state, _ = classify_market_state(
            metrics, fg=80.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 9, 15)
        )
        self.assertEqual(state, "BLACKOUT")

    def test_thrust_is_highest_priority_outside_blackout(self):
        """THRUST should take priority over GREEN when not in blackout."""
        metrics = self._make_metrics(
            thrust=True, ratio_5day=3.0, ratio_10day=2.0
        )
        today = {"up_4_today": 600, "down_4_today": 10}
        state, msg = classify_market_state(
            metrics, fg=50.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertEqual(state, "THRUST")
        self.assertIn("600", msg)

    def test_green_all_conditions_met(self):
        """GREEN when all conditions satisfied."""
        metrics = self._make_metrics(
            ratio_5day=2.5, ratio_10day=2.0
        )
        today = {"up_4_today": 100, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=45.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertEqual(state, "GREEN")

    def test_green_fails_without_spy_above_200d(self):
        """GREEN requires SPY above 200d MA."""
        metrics = self._make_metrics(
            ratio_5day=2.5, ratio_10day=2.0, spy_above_200d=False
        )
        today = {"up_4_today": 100, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=45.0, spy_price=500.0, spy_above_200d=False,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertNotEqual(state, "GREEN")

    def test_green_fails_low_fg(self):
        """GREEN requires F&G >= 35."""
        metrics = self._make_metrics(
            ratio_5day=2.5, ratio_10day=2.0
        )
        today = {"up_4_today": 100, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=20.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertNotEqual(state, "GREEN")

    def test_caution_half_conditions(self):
        """CAUTION when partial conditions met."""
        metrics = self._make_metrics(
            ratio_5day=1.6, ratio_10day=1.0
        )
        today = {"up_4_today": 60, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertEqual(state, "CAUTION")

    def test_caution_fails_without_spy_above_200d(self):
        """CAUTION also requires SPY above 200d MA."""
        metrics = self._make_metrics(
            ratio_5day=1.6, spy_above_200d=False
        )
        today = {"up_4_today": 60, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=500.0, spy_above_200d=False,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertNotEqual(state, "CAUTION")

    def test_danger_state(self):
        """DANGER when down stocks high (A/D scale) and 5-day ratio very low."""
        metrics = self._make_metrics(
            ratio_5day=0.3, spy_above_200d=False
        )
        today = {"up_4_today": 500, "down_4_today": DANGER_DOWN_THRESHOLD}
        state, _ = classify_market_state(
            metrics, fg=10.0, spy_price=450.0, spy_above_200d=False,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertEqual(state, "DANGER")

    def test_danger_requires_high_down_count(self):
        """DANGER needs down_4 >= DANGER_DOWN_THRESHOLD."""
        metrics = self._make_metrics(
            ratio_5day=0.3, spy_above_200d=False
        )
        today = {"up_4_today": 10, "down_4_today": DANGER_DOWN_THRESHOLD - 1}
        state, _ = classify_market_state(
            metrics, fg=10.0, spy_price=450.0, spy_above_200d=False,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertNotEqual(state, "DANGER")

    def test_red_explicit_bearish(self):
        """RED when SPY below 200d MA and 5d ratio < 1.0."""
        metrics = self._make_metrics(
            ratio_5day=0.8, ratio_10day=0.6, spy_above_200d=False
        )
        today = {"up_4_today": 30, "down_4_today": 80}
        state, _ = classify_market_state(
            metrics, fg=20.0, spy_price=500.0, spy_above_200d=False,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        self.assertEqual(state, "RED")

    def test_cooling_fires_when_prev_green_and_conditions_fall(self):
        """COOLING fires when previous state was GREEN and conditions no longer met."""
        metrics = self._make_metrics(ratio_5day=1.6, ratio_10day=1.2)
        today = {"up_4_today": 80, "down_4_today": 50}
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=580.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15),
            prev_state="GREEN"
        )
        self.assertEqual(state, "COOLING")

    def test_cooling_does_not_fire_without_prev_green(self):
        """COOLING only fires when coming DOWN from GREEN, not from other states."""
        metrics = self._make_metrics(ratio_5day=1.6, ratio_10day=1.2)
        today = {"up_4_today": 80, "down_4_today": 50}
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=580.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15),
            prev_state="CAUTION"
        )
        self.assertEqual(state, "CAUTION")

    def test_cooling_does_not_fire_if_green_conditions_still_met(self):
        """COOLING does not fire if GREEN conditions are still satisfied."""
        metrics = self._make_metrics(ratio_5day=2.5, ratio_10day=1.8)
        today = {"up_4_today": 150, "down_4_today": 50}
        state, _ = classify_market_state(
            metrics, fg=40.0, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15),
            prev_state="GREEN"
        )
        self.assertEqual(state, "GREEN")

    def test_none_fg_handled(self):
        """None F&G should not crash — treated as 0."""
        metrics = self._make_metrics(ratio_5day=2.5, ratio_10day=2.0)
        today = {"up_4_today": 100, "down_4_today": 40}
        state, _ = classify_market_state(
            metrics, fg=None, spy_price=600.0, spy_above_200d=True,
            today_data=today, date=datetime.date(2026, 4, 15)
        )
        # F&G is None → treated as 0, so GREEN won't trigger (needs >= 35)
        self.assertNotEqual(state, "GREEN")


class TestBuildDailyRecord(unittest.TestCase):
    """Tests for daily record construction."""

    def test_record_has_all_fields(self):
        """Daily record should contain all expected fields."""
        today_data = {
            "up_4_today": 43, "down_4_today": 198,
            "up_25_quarter": 312, "down_25_quarter": 847,
            "fg": 14.6, "spy_price": 548.23, "spy_sma200_pct": -4.0,
        }
        metrics = {
            "ratio_today": 0.22, "ratio_5day": 0.38, "ratio_10day": 0.51,
            "thrust": False, "spy_above_200d": False,
        }
        record = build_daily_record(
            datetime.date(2026, 3, 22), today_data, metrics, "RED", "No new trades"
        )

        expected_fields = [
            "date", "up_4_today", "down_4_today", "breadth_source",
            "universe_size", "adv_total", "dec_total",
            "ratio_today", "ratio_5day", "ratio_10day",
            "up_25_quarter", "down_25_quarter",
            "thrust_detected", "fg", "spy_price", "spy_sma200_pct",
            "spy_above_200d", "market_state", "state_message", "blackout",
        ]
        for field in expected_fields:
            self.assertIn(field, record, f"Missing field: {field}")

    def test_record_values_match_inputs(self):
        """Record values should reflect input data."""
        today_data = {
            "up_4_today": 43, "down_4_today": 198,
            "up_25_quarter": 312, "down_25_quarter": 847,
            "fg": 14.6, "spy_price": 548.23, "spy_sma200_pct": -4.0,
        }
        metrics = {
            "ratio_today": 0.22, "ratio_5day": 0.38, "ratio_10day": 0.51,
            "thrust": False, "spy_above_200d": False,
        }
        record = build_daily_record(
            datetime.date(2026, 3, 22), today_data, metrics, "RED", "No new trades"
        )

        self.assertEqual(record["date"], "2026-03-22")
        self.assertEqual(record["up_4_today"], 43)
        self.assertEqual(record["market_state"], "RED")
        self.assertFalse(record["thrust_detected"])
        self.assertEqual(record["fg"], 14.6)

    def test_blackout_flag_matches_date(self):
        """Blackout flag should reflect the date."""
        today_data = {"up_4_today": 0, "down_4_today": 0}
        metrics = {"ratio_today": 0, "ratio_5day": 0, "ratio_10day": 0,
                   "thrust": False, "spy_above_200d": False}

        # March 10 is blackout
        record = build_daily_record(
            datetime.date(2026, 3, 10), today_data, metrics, "BLACKOUT", "blackout"
        )
        self.assertTrue(record["blackout"])

        # April 15 is not blackout
        record = build_daily_record(
            datetime.date(2026, 4, 15), today_data, metrics, "RED", "red"
        )
        self.assertFalse(record["blackout"])


class TestMarketStateIntegration(unittest.TestCase):
    """Tests for market state integration with weekly agent."""

    def test_load_market_state_missing_file(self):
        """load_market_state returns None when file doesn't exist."""
        import agents.screener.finviz_weekly_agent as wa
        original = wa.MARKET_HISTORY_FILE
        wa.MARKET_HISTORY_FILE = "/tmp/nonexistent_market_monitor_history.json"
        result = load_market_state()
        self.assertIsNone(result)
        wa.MARKET_HISTORY_FILE = original

    def test_load_market_state_empty_history(self):
        """load_market_state returns None for empty history."""
        import agents.screener.finviz_weekly_agent as wa
        original = wa.MARKET_HISTORY_FILE
        tmp = os.path.join(tempfile.mkdtemp(), "empty_history.json")
        with open(tmp, "w") as f:
            json.dump([], f)
        wa.MARKET_HISTORY_FILE = tmp
        result = load_market_state()
        self.assertIsNone(result)
        wa.MARKET_HISTORY_FILE = original

    def test_load_market_state_returns_latest(self):
        """load_market_state returns the most recent record."""
        import agents.screener.finviz_weekly_agent as wa
        original = wa.MARKET_HISTORY_FILE
        tmp = os.path.join(tempfile.mkdtemp(), "test_history.json")
        history = [
            _make_monitor_day(date="2026-03-19", market_state="RED"),
            _make_monitor_day(date="2026-03-20", market_state="CAUTION"),
        ]
        with open(tmp, "w") as f:
            json.dump(history, f)
        wa.MARKET_HISTORY_FILE = tmp
        result = load_market_state()
        self.assertEqual(result["market_state"], "CAUTION")
        self.assertEqual(result["date"], "2026-03-20")
        wa.MARKET_HISTORY_FILE = original

    def test_any_thrust_in_history_true(self):
        """any_thrust_in_history returns True when thrust detected."""
        import agents.screener.finviz_weekly_agent as wa
        original = wa.MARKET_HISTORY_FILE
        tmp = os.path.join(tempfile.mkdtemp(), "thrust_history.json")
        history = [
            _make_monitor_day(date="2026-03-18", thrust_detected=False),
            _make_monitor_day(date="2026-03-19", thrust_detected=True),
            _make_monitor_day(date="2026-03-20", thrust_detected=False),
        ]
        with open(tmp, "w") as f:
            json.dump(history, f)
        wa.MARKET_HISTORY_FILE = tmp
        self.assertTrue(any_thrust_in_history())
        wa.MARKET_HISTORY_FILE = original

    def test_any_thrust_in_history_false(self):
        """any_thrust_in_history returns False when no thrust."""
        import agents.screener.finviz_weekly_agent as wa
        original = wa.MARKET_HISTORY_FILE
        tmp = os.path.join(tempfile.mkdtemp(), "no_thrust_history.json")
        history = [
            _make_monitor_day(date="2026-03-19", thrust_detected=False),
            _make_monitor_day(date="2026-03-20", thrust_detected=False),
        ]
        with open(tmp, "w") as f:
            json.dump(history, f)
        wa.MARKET_HISTORY_FILE = tmp
        self.assertFalse(any_thrust_in_history())
        wa.MARKET_HISTORY_FILE = original


class TestMarketStateTransitions(unittest.TestCase):
    """Tests for realistic state transition scenarios."""

    def _make_metrics(self, **overrides):
        base = {
            "ratio_today": 1.0, "ratio_5day": 1.0, "ratio_10day": 1.0,
            "thrust": False, "spy_above_200d": True,
        }
        base.update(overrides)
        return base

    def test_red_to_thrust_transition(self):
        """Market should go from RED to THRUST on massive up day."""
        # Day 1: RED
        m1 = self._make_metrics(ratio_5day=0.4, spy_above_200d=False)
        s1, _ = classify_market_state(
            m1, fg=15.0, spy_price=450.0, spy_above_200d=False,
            today_data={"up_4_today": 20, "down_4_today": 150},
            date=datetime.date(2026, 4, 8)
        )
        self.assertEqual(s1, "RED")

        # Day 2: THRUST (500+ stocks up 4%)
        m2 = self._make_metrics(thrust=True, ratio_5day=0.6)
        s2, _ = classify_market_state(
            m2, fg=12.0, spy_price=455.0, spy_above_200d=False,
            today_data={"up_4_today": 520, "down_4_today": 30},
            date=datetime.date(2026, 4, 9)
        )
        self.assertEqual(s2, "THRUST")

    def test_thrust_to_green_confirmation(self):
        """After thrust, market should confirm to GREEN when all conditions met."""
        metrics = self._make_metrics(
            ratio_5day=2.5, ratio_10day=1.8
        )
        state, _ = classify_market_state(
            metrics, fg=35.0, spy_price=580.0, spy_above_200d=True,
            today_data={"up_4_today": 120, "down_4_today": 50},
            date=datetime.date(2026, 4, 24)
        )
        self.assertEqual(state, "GREEN")

    def test_green_to_cooling_when_ratios_fade(self):
        """GREEN transitions to COOLING when conditions weaken."""
        metrics = self._make_metrics(ratio_5day=1.6, ratio_10day=1.2)
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=580.0, spy_above_200d=True,
            today_data={"up_4_today": 80, "down_4_today": 50},
            date=datetime.date(2026, 5, 10),
            prev_state="GREEN"
        )
        self.assertEqual(state, "COOLING")

    def test_cooling_to_caution_when_prev_not_green(self):
        """After COOLING, further weakening goes to CAUTION (not COOLING again)."""
        metrics = self._make_metrics(ratio_5day=1.6, ratio_10day=1.2)
        state, _ = classify_market_state(
            metrics, fg=30.0, spy_price=570.0, spy_above_200d=True,
            today_data={"up_4_today": 70, "down_4_today": 45},
            date=datetime.date(2026, 5, 11),
            prev_state="COOLING"
        )
        self.assertEqual(state, "CAUTION")

    def test_green_to_danger_fast_deterioration(self):
        """DANGER fires even from GREEN if breadth collapses hard enough (overrides COOLING)."""
        metrics = self._make_metrics(
            ratio_5day=0.3, spy_above_200d=False
        )
        state, _ = classify_market_state(
            metrics, fg=18.0, spy_price=480.0, spy_above_200d=False,
            today_data={"up_4_today": 500, "down_4_today": DANGER_DOWN_THRESHOLD},
            date=datetime.date(2026, 5, 10),
            prev_state="GREEN"
        )
        self.assertEqual(state, "DANGER")

    def test_caution_to_green_when_ratios_improve(self):
        """CAUTION upgrades to GREEN when all conditions are met."""
        # CAUTION state
        m1 = self._make_metrics(ratio_5day=1.6, ratio_10day=1.2)
        s1, _ = classify_market_state(
            m1, fg=30.0, spy_price=560.0, spy_above_200d=True,
            today_data={"up_4_today": 70, "down_4_today": 45},
            date=datetime.date(2026, 4, 20)
        )
        self.assertEqual(s1, "CAUTION")

        # GREEN state — ratios improved
        m2 = self._make_metrics(ratio_5day=2.2, ratio_10day=1.6)
        s2, _ = classify_market_state(
            m2, fg=40.0, spy_price=575.0, spy_above_200d=True,
            today_data={"up_4_today": 90, "down_4_today": 40},
            date=datetime.date(2026, 4, 24)
        )
        self.assertEqual(s2, "GREEN")

    def test_entering_blackout_period(self):
        """State should switch to BLACKOUT regardless of breadth."""
        metrics = self._make_metrics(ratio_5day=3.0, ratio_10day=2.5)
        state, _ = classify_market_state(
            metrics, fg=55.0, spy_price=600.0, spy_above_200d=True,
            today_data={"up_4_today": 150, "down_4_today": 30},
            date=datetime.date(2026, 9, 1)
        )
        self.assertEqual(state, "BLACKOUT")

    def test_exiting_blackout_period(self):
        """After blackout ends, normal classification resumes."""
        metrics = self._make_metrics(ratio_5day=0.7, spy_above_200d=False)
        state, _ = classify_market_state(
            metrics, fg=25.0, spy_price=530.0, spy_above_200d=False,
            today_data={"up_4_today": 40, "down_4_today": 90},
            date=datetime.date(2026, 10, 16)
        )
        self.assertNotEqual(state, "BLACKOUT")


class TestHistoryRolling(unittest.TestCase):
    """Tests for rolling history management."""

    def test_history_stays_at_30_days(self):
        """History should keep at most 30 days."""
        import agents.market.finviz_market_monitor as mm
        tmp_dir = tempfile.mkdtemp()
        original_dir = mm.DATA_DIR
        original_file = mm.HISTORY_FILE
        mm.DATA_DIR = tmp_dir
        mm.HISTORY_FILE = os.path.join(tmp_dir, "market_monitor_history.json")

        # Create 35-day history
        history = [_make_monitor_day(date=f"2026-02-{i:02d}") for i in range(1, 29)]
        history += [_make_monitor_day(date=f"2026-03-{i:02d}") for i in range(1, 8)]
        self.assertEqual(len(history), 35)

        # Simulate appending one more and trimming
        history.append(_make_monitor_day(date="2026-03-08"))
        history = history[-30:]
        mm.save_history(history)

        loaded = mm.load_history()
        self.assertEqual(len(loaded), 30)

        mm.DATA_DIR = original_dir
        mm.HISTORY_FILE = original_file

    def test_save_and_load_roundtrip(self):
        """History should survive save/load cycle."""
        import agents.market.finviz_market_monitor as mm
        tmp_dir = tempfile.mkdtemp()
        original_dir = mm.DATA_DIR
        original_file = mm.HISTORY_FILE
        mm.DATA_DIR = tmp_dir
        mm.HISTORY_FILE = os.path.join(tmp_dir, "market_monitor_history.json")

        history = [
            _make_monitor_day(date="2026-03-19", market_state="RED"),
            _make_monitor_day(date="2026-03-20", market_state="THRUST"),
        ]
        mm.save_history(history)
        loaded = mm.load_history()

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["market_state"], "RED")
        self.assertEqual(loaded[1]["market_state"], "THRUST")

        mm.DATA_DIR = original_dir
        mm.HISTORY_FILE = original_file


# ============================================================
# Paper Trading Layer Tests
# ============================================================

from agents.trading.alpaca_executor import compute_allocation
from agents.screener.finviz_agent import _update_watchlist


class TestPaperPositionSizing(unittest.TestCase):
    """compute_allocation() — Q score tiers, VCP flag, equity scaling."""

    def _vcp(self, confirmed: bool) -> dict:
        return {"vcp_possible": confirmed, "confidence": 80, "reason": "test"}

    def test_below_threshold_skipped(self):
        # Q=59 — below the 60 floor, should return 0 regardless of VCP
        self.assertEqual(compute_allocation(59, self._vcp(True), 100_000), 0.0)

    def test_exactly_at_threshold(self):
        # Q=60 — first passing tier, 15%
        self.assertAlmostEqual(compute_allocation(60, self._vcp(False), 100_000), 15_000)

    def test_standard_tier(self):
        # Q=75 — 15% tier
        self.assertAlmostEqual(compute_allocation(75, self._vcp(False), 200_000), 30_000)

    def test_strong_tier(self):
        # Q=85 — 20% tier
        self.assertAlmostEqual(compute_allocation(85, self._vcp(False), 100_000), 20_000)

    def test_high_conviction_requires_vcp(self):
        # Q=92 but no VCP — should be 20% (strong tier), NOT 25%
        self.assertAlmostEqual(compute_allocation(92, self._vcp(False), 100_000), 20_000)

    def test_high_conviction_with_vcp(self):
        # Q=92 + VCP — 25% tier
        self.assertAlmostEqual(compute_allocation(92, self._vcp(True), 100_000), 25_000)

    def test_vcp_dict_missing_key(self):
        # Malformed VCP dict — should not crash, treat as no VCP
        result = compute_allocation(90, {}, 100_000)
        self.assertAlmostEqual(result, 20_000)

    def test_non_dict_vcp(self):
        # Non-dict VCP value — should not crash
        result = compute_allocation(90, None, 100_000)
        self.assertAlmostEqual(result, 20_000)

    def test_scales_with_equity(self):
        # Allocation is a fixed % of equity — verify proportionality
        small = compute_allocation(65, self._vcp(False), 50_000)
        large = compute_allocation(65, self._vcp(False), 200_000)
        self.assertAlmostEqual(large / small, 4.0)


class TestPaperStopCalculation(unittest.TestCase):
    """Stop = entry - 2 * ATR_dollar. ATR_dollar = (ATR% / 100) * price."""

    def _stop(self, price: float, atr_pct: float) -> float:
        atr_dollar = (atr_pct / 100) * price
        return round(price - (2 * atr_dollar), 2)

    def test_standard_stop(self):
        # Price $100, ATR 5% → ATR_dollar=$5, stop=$90
        self.assertAlmostEqual(self._stop(100, 5.0), 90.0)

    def test_low_atr(self):
        # Price $50, ATR 2% → ATR_dollar=$1, stop=$48
        self.assertAlmostEqual(self._stop(50, 2.0), 48.0)

    def test_high_atr(self):
        # Price $200, ATR 10% → ATR_dollar=$20, stop=$160
        self.assertAlmostEqual(self._stop(200, 10.0), 160.0)

    def test_stop_is_below_entry(self):
        # Stop must always be below entry for a long
        for price in [20, 50, 100, 500]:
            for atr in [1, 3, 5, 8, 12]:
                self.assertLess(self._stop(price, atr), price)


class TestCSVStageVCPParsing(unittest.TestCase):
    """Stage and VCP columns in the CSV are stored as string repr of dicts.
    ast.literal_eval must recover them correctly."""

    def _parse(self, raw: str) -> dict:
        import ast
        if raw and raw not in ("", "nan"):
            try:
                return ast.literal_eval(raw)
            except Exception:
                return {}
        return {}

    def test_stage2_round_trip(self):
        stage = {"stage": 2, "badge": "🟢 Stage 2", "perfect": True}
        recovered = self._parse(str(stage))
        self.assertEqual(recovered["stage"], 2)
        self.assertTrue(recovered["perfect"])

    def test_stage4_round_trip(self):
        stage = {"stage": 4, "badge": "⚫ Stage 4", "perfect": False}
        recovered = self._parse(str(stage))
        self.assertEqual(recovered["stage"], 4)

    def test_vcp_confirmed_round_trip(self):
        vcp = {"vcp_possible": True, "confidence": 75, "reason": "ATR contraction"}
        recovered = self._parse(str(vcp))
        self.assertTrue(recovered["vcp_possible"])
        self.assertEqual(recovered["confidence"], 75)

    def test_vcp_not_confirmed(self):
        vcp = {"vcp_possible": False, "confidence": 0, "reason": "Not Stage 2"}
        recovered = self._parse(str(vcp))
        self.assertFalse(recovered["vcp_possible"])

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(self._parse(""), {})

    def test_nan_string_returns_empty_dict(self):
        self.assertEqual(self._parse("nan"), {})

    def test_malformed_returns_empty_dict(self):
        self.assertEqual(self._parse("{bad json}"), {})


class TestWatchlistAutoPopulation(unittest.TestCase):
    """_update_watchlist() — only Stage 2 + Q>=60, no duplicates, max 5."""

    def _make_filter_df(self, rows: list) -> pd.DataFrame:
        """Build a minimal filter_df from list of (ticker, q_score, stage_num, vcp_ok, sector)."""
        data = []
        for ticker, qs, stage_num, vcp_ok, sector in rows:
            data.append({
                "Ticker":        ticker,
                "Quality Score": qs,
                "Stage":         {"stage": stage_num, "badge": "🟢 Stage 2", "perfect": stage_num == 2},
                "VCP":           {"vcp_possible": vcp_ok, "confidence": 70, "reason": "test"},
                "ATR%":          5.0,
                "Sector":        sector,
            })
        return pd.DataFrame(data)

    def test_adds_stage2_above_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("AAPL", 75, 2, False, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            tickers = [e["ticker"] for e in result["watchlist"]]
            self.assertIn("AAPL", tickers)

    def test_skips_below_q60(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("WEAK", 55, 2, False, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            self.assertEqual(result["watchlist"], [])

    def test_skips_non_stage2(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("S3TICK", 80, 3, False, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            self.assertEqual(result["watchlist"], [])

    def test_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            existing = {"watchlist": [{"ticker": "AAPL", "status": "watching"}]}
            with open(wl_path, "w") as f:
                json.dump(existing, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("AAPL", 85, 2, True, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            # Still only one AAPL entry
            aapl_entries = [e for e in result["watchlist"] if e["ticker"] == "AAPL"]
            self.assertEqual(len(aapl_entries), 1)

    def test_max_5_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            tickers = [("T" + str(i), 60 + i, 2, False, "Tech") for i in range(8)]
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df(tickers)
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            self.assertLessEqual(len(result["watchlist"]), 5)

    def test_vcp_entry_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("VCPTICK", 90, 2, True, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            entry = result["watchlist"][0]
            self.assertIn("VCP", entry["entry_note"])

    def test_source_field_is_screener_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl_path = os.path.join(tmp, "watchlist.json")
            with open(wl_path, "w") as f:
                json.dump({"watchlist": []}, f)
            import agents.screener.finviz_agent as finviz_agent
            orig = finviz_agent.os.path.join
            with unittest.mock.patch("agents.screener.finviz_agent.os.path.join", side_effect=lambda *a: wl_path if a[-1] == "watchlist.json" else orig(*a)):
                df = self._make_filter_df([("AUTO", 70, 2, False, "Technology")])
                _update_watchlist(df, "2026-03-31")
            with open(wl_path) as f:
                result = json.load(f)
            self.assertEqual(result["watchlist"][0]["source"], "screener_auto")


if __name__ == "__main__":
    unittest.main()
