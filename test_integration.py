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

import pandas as pd

from finviz_agent import (
    compute_stage,
    compute_vcp,
    compute_quality_score,
    _classify_ticker,
)
from finviz_weekly_agent import (
    load_daily_quality,
    _compute_quality_modifier,
    _detect_signals,
    build_persistence_scores,
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
        """All MAs stacked, volume ≥ 1.0, within 25% of high → Stage 2."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 3.0, "SMA200%": 1.0,
            "Rel Volume": 1.5, "Dist From High%": -10.0,
        })
        result = compute_stage(row)
        self.assertEqual(result["stage"], 2)
        self.assertIn("Stage 2", result["badge"])

    def test_stage2_perfect_minervini(self):
        """Stage 2 with SMA50 ≥ SMA200 → perfect alignment."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 3.0, "SMA200%": 1.0,
            "Rel Volume": 1.5, "Dist From High%": -5.0,
        })
        result = compute_stage(row)
        self.assertTrue(result["perfect"])

    def test_stage2_not_perfect_when_sma50_below_sma200(self):
        """Stage 2 can fire but perfect=False if SMA50 < SMA200."""
        row = _make_row(**{
            "SMA20%": 5.0, "SMA50%": 0.5, "SMA200%": 1.0,
            "Rel Volume": 1.2, "Dist From High%": -15.0,
        })
        # SMA20 > SMA50 required for stage2: 5.0 >= 0.5 ✓
        # But SMA50 < SMA200: not perfect
        result = compute_stage(row)
        if result["stage"] == 2:
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
        """Price below 50-day but above 200-day → Stage 3."""
        row = _make_row(**{
            "SMA20%": -2.0, "SMA50%": -5.0, "SMA200%": 3.0,
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
        """Near 200-day, above 50-day but no volume/proximity → Stage 1."""
        row = _make_row(**{
            "SMA20%": 3.0, "SMA50%": 1.0, "SMA200%": 2.0,
            "Rel Volume": 0.5,  # below 1.0 → not Stage 2
            "Dist From High%": -35.0,  # beyond -25% → not Stage 2
        })
        result = compute_stage(row)
        # MAs stacked above zero, SMA200=2% (within 8%) → Stage 1
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


if __name__ == "__main__":
    unittest.main()
