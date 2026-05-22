"""Unit tests for ⚡ Episodic Pivot helpers (agents/utils/episodic_pivot.py).

Coverage:
- passes_pre_filter (positive + negative paths)
- passes_bar_shape (Pattern B reversal gates)
- compute_bar_metrics shape + RVol/range_contract/prior_3d_cum arithmetic
- compute_context_tags (SECTOR / PEERS / STANDALONE / LEADER)
- EPFire.emoji + tier sort order
- is_in_cooldown
- update_ep_history / save / load roundtrip
- format_daily_teaser
- format_momentum_slack_blocks structure
"""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

import pandas as pd

from agents.utils import episodic_pivot as ep


def _row(**over):
    """Default row passes pre-filter — QBTS-like clean state on the SB day.

    All fields named to match the Finviz screener row schema."""
    base = {
        "Ticker":         "QBTS",
        "SMA50%":          30.0,
        "Perf Quarter":    32.0,
        "ATR%":            8.0,
        "Avg Volume":      26_000_000,
        "Market Cap":      8_800_000_000,
        "Price":           19.30,
        "Sector":          "Technology",
        "Industry":        "Computer Hardware",
    }
    base.update(over)
    return pd.Series(base)


def _bars(n=40, base_close=20.0, today_chg=0.06, today_vol_mult=0.85,
          pullback_pct=-0.13, today_range_pct=0.05):
    """Generate a synthetic 40-bar series ending with a Pattern B SB day.

    - First (n-5) bars: drift-up base with WIDE ranges (~8% — semis-like)
      so prior 10d range_avg is large, letting today's tighter range contract
    - Bars -5..-3: 3-day pullback (cum return ≈ pullback_pct), wide ranges too
    - Bar -1 (today): green reversal — chg = today_chg, range = today_range_pct,
      vol = today_vol_mult × avg_vol
    """
    rows = []
    avg_vol = 30_000_000
    price = base_close * 0.9
    # Drift-up base with ~8% ranges (semis-class volatility)
    for i in range(n - 5):
        open_p = price
        high_p = price * 1.04
        low_p = price * 0.96
        close_p = price * 1.005
        rows.append({
            "t": f"2026-04-{i+1:02d}", "o": open_p, "h": high_p, "l": low_p,
            "c": close_p, "v": avg_vol,
        })
        price = close_p

    # 3-day pullback with wide ranges
    pre_pull_close = price
    target_close = pre_pull_close * (1.0 + pullback_pct) if pullback_pct < 0 else pre_pull_close * 0.995
    step = (target_close / pre_pull_close) ** (1 / 3) if pre_pull_close > 0 else 1.0
    for j in range(3):
        new_close = price * step
        rows.append({
            "t": f"2026-05-1{j+2}", "o": price, "h": price * 1.03,
            "l": new_close * 0.96, "c": new_close, "v": avg_vol,
        })
        price = new_close

    # Padding day at low (so today's "yesterday" is the pullback bottom)
    rows.append({
        "t": "2026-05-19", "o": price, "h": price * 1.01, "l": price * 0.98,
        "c": price * 0.995, "v": avg_vol,
    })

    # Today's SB — tight green reversal on drying volume
    yest_close = rows[-1]["c"]
    today_close = yest_close * (1.0 + today_chg)
    rng = today_close * today_range_pct
    today_high = today_close + rng * 0.7
    today_low = today_close - rng * 0.3
    rows.append({
        "t": "2026-05-20", "o": yest_close, "h": today_high, "l": today_low,
        "c": today_close, "v": avg_vol * today_vol_mult,
    })
    return rows


class TestPreFilter(unittest.TestCase):
    def test_passes_with_clean_qbts_row(self):
        ok, reason = ep.passes_pre_filter(_row(), {"QBTS", "OTHER"}, set(), set())
        self.assertTrue(ok, reason)

    def test_blocks_held(self):
        ok, _ = ep.passes_pre_filter(_row(), {"QBTS"}, {"QBTS"}, set())
        self.assertFalse(ok)

    def test_blocks_already_surfaced(self):
        ok, _ = ep.passes_pre_filter(_row(), {"QBTS"}, set(), {"QBTS"})
        self.assertFalse(ok)

    def test_blocks_low_sma50(self):
        ok, _ = ep.passes_pre_filter(_row(**{"SMA50%": 5.0}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_low_perf_q(self):
        ok, _ = ep.passes_pre_filter(_row(**{"Perf Quarter": 8.0}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_high_atr(self):
        ok, _ = ep.passes_pre_filter(_row(**{"ATR%": 20.0}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_small_mcap(self):
        ok, _ = ep.passes_pre_filter(_row(**{"Market Cap": 100_000_000}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_low_price(self):
        ok, _ = ep.passes_pre_filter(_row(**{"Price": 2.0}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_excluded_sector(self):
        ok, _ = ep.passes_pre_filter(_row(**{"Sector": "Utilities"}), {"QBTS"}, set(), set())
        self.assertFalse(ok)

    def test_blocks_biotech_industry(self):
        ok, _ = ep.passes_pre_filter(
            _row(**{"Sector": "Healthcare", "Industry": "Biotechnology"}),
            {"QBTS"}, set(), set(),
        )
        self.assertFalse(ok)

    def test_blocks_not_in_universe(self):
        ok, _ = ep.passes_pre_filter(_row(), {"OTHER"}, set(), set())
        self.assertFalse(ok)

    def test_empty_universe_does_not_block(self):
        ok, _ = ep.passes_pre_filter(_row(), set(), set(), set())
        self.assertTrue(ok)


class TestBarMetrics(unittest.TestCase):
    def test_clean_sb_metrics(self):
        m = ep.compute_bar_metrics(_bars())
        self.assertIsNotNone(m)
        self.assertGreater(m["chg_pct"], 3.0)
        self.assertLess(m["rvol"], 1.0)
        self.assertLess(m["range_contract"], 1.0)
        self.assertLess(m["prior_3d_cum"], -3.0)
        self.assertFalse(m["has_expansion_recent"])

    def test_returns_none_when_insufficient_bars(self):
        self.assertIsNone(ep.compute_bar_metrics(_bars(n=10)))
        self.assertIsNone(ep.compute_bar_metrics([]))


class TestBarShape(unittest.TestCase):
    def test_clean_sb_passes(self):
        m = ep.compute_bar_metrics(_bars(today_chg=0.06, today_vol_mult=0.85))
        ok, reason = ep.passes_bar_shape(m)
        self.assertTrue(ok, reason)

    def test_blocks_when_volume_too_high(self):
        m = ep.compute_bar_metrics(_bars(today_vol_mult=1.5))
        m["rvol"] = 1.5
        ok, _ = ep.passes_bar_shape(m)
        self.assertFalse(ok)

    def test_blocks_when_no_pullback(self):
        m = ep.compute_bar_metrics(_bars(pullback_pct=0.0))
        m["prior_3d_cum"] = -2.0
        ok, _ = ep.passes_bar_shape(m)
        self.assertFalse(ok)

    def test_blocks_when_chg_too_small(self):
        m = ep.compute_bar_metrics(_bars(today_chg=0.01))
        m["chg_pct"] = 1.0
        ok, _ = ep.passes_bar_shape(m)
        self.assertFalse(ok)

    def test_blocks_when_recent_expansion(self):
        m = ep.compute_bar_metrics(_bars())
        m["has_expansion_recent"] = True
        ok, _ = ep.passes_bar_shape(m)
        self.assertFalse(ok)


class TestContextTags(unittest.TestCase):
    def test_standalone_when_no_sector_no_peers(self):
        etf_data = {"etfs_by_symbol": {"SMH": {"bucket": "EXTENDED"}}}
        sector_snap = {"SMH": {"rank_delta_5d": 0}}
        tags, peers = ep.compute_context_tags(
            "AMKR", "SMH", etf_data, sector_snap, "Semiconductors", {}, "2026-05-20",
        )
        self.assertEqual(tags, [])
        self.assertEqual(peers, [])

    def test_sector_when_etf_rotating_in_by_rank(self):
        etf_data = {"etfs_by_symbol": {"SMH": {"bucket": "EXTENDED"}}}
        sector_snap = {"SMH": {"rank_delta_5d": -8}}
        tags, _ = ep.compute_context_tags(
            "AMKR", "SMH", etf_data, sector_snap, "Semiconductors", {}, "2026-05-20",
        )
        self.assertIn("SECTOR", tags)

    def test_sector_when_etf_in_base_bucket(self):
        etf_data = {"etfs_by_symbol": {"SMH": {"bucket": "BASE"}}}
        sector_snap = {}
        tags, _ = ep.compute_context_tags(
            "AMKR", "SMH", etf_data, sector_snap, "Semiconductors", {}, "2026-05-20",
        )
        self.assertIn("SECTOR", tags)

    def test_peers_when_same_industry_recent_fire(self):
        history = {
            "AXTI": {"last_fire_date": "2026-05-19", "industry": "Semiconductors"},
            "OTHER": {"last_fire_date": "2026-05-19", "industry": "Software"},
        }
        tags, peers = ep.compute_context_tags(
            "AMKR", None, {"etfs_by_symbol": {}}, {}, "Semiconductors", history, "2026-05-20",
        )
        self.assertIn("PEERS", tags)
        self.assertIn("AXTI", peers)
        self.assertNotIn("OTHER", peers)

    def test_peers_ignores_old_fires(self):
        history = {
            "AXTI": {"last_fire_date": "2026-04-01", "industry": "Semiconductors"},
        }
        tags, peers = ep.compute_context_tags(
            "AMKR", None, {"etfs_by_symbol": {}}, {}, "Semiconductors", history, "2026-05-20",
        )
        self.assertNotIn("PEERS", tags)


class TestEmojiTier(unittest.TestCase):
    def _f(self, tags=None, peers=None):
        return ep.EPFire(
            ticker="X", date="2026-05-20", close=10, chg_pct=5, rvol=0.5, atr_pct=5,
            range_contract=0.6, prior_3d_cum=-10, dist_52w_hi=-5, sector="Tech",
            industry="Semis", etf="SMH", tags=tags or [], peers=peers or [],
        )

    def test_emoji_for_each_tier(self):
        self.assertEqual(self._f(["SECTOR", "PEERS"], ["Y"]).emoji, "🔥")
        self.assertEqual(self._f(["PEERS"], ["Y"]).emoji, "🌊")
        self.assertEqual(self._f(["SECTOR"]).emoji, "📈")
        self.assertEqual(self._f().emoji, "⚡")

    def test_tier_sort_order(self):
        fires = [self._f(), self._f(["SECTOR"]), self._f(["SECTOR", "PEERS"], ["Y"]), self._f(["PEERS"], ["Y"])]
        ranks = sorted(fires, key=lambda x: x.tier)
        self.assertEqual([r.emoji for r in ranks], ["🔥", "🌊", "📈", "⚡"])


class TestCooldown(unittest.TestCase):
    def test_no_history_no_cooldown(self):
        self.assertFalse(ep.is_in_cooldown("X", "2026-05-20", {}))

    def test_recent_fire_blocks(self):
        hist = {"X": {"last_fire_date": "2026-05-10"}}
        self.assertTrue(ep.is_in_cooldown("X", "2026-05-20", hist))

    def test_old_fire_does_not_block(self):
        hist = {"X": {"last_fire_date": "2026-03-01"}}
        self.assertFalse(ep.is_in_cooldown("X", "2026-05-20", hist))


class TestHistoryRoundtrip(unittest.TestCase):
    def test_save_load_roundtrip(self):
        fires = [
            ep.EPFire(ticker="AMKR", date="2026-05-20", close=68.49, chg_pct=4.5,
                      rvol=0.58, atr_pct=5, range_contract=0.71, prior_3d_cum=-9.1,
                      dist_52w_hi=-10.5, sector="Tech", industry="Semiconductors",
                      etf="SMH", tags=["SECTOR", "PEERS"], peers=["AXTI", "COHU"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ep.json")
            history = ep.update_ep_history(fires, "2026-05-20", {})
            ep.save_ep_history(history, path)
            loaded = ep.load_ep_history(path)
            self.assertIn("AMKR", loaded)
            self.assertEqual(loaded["AMKR"]["industry"], "Semiconductors")
            self.assertEqual(loaded["AMKR"]["fire_count"], 1)
            self.assertEqual(loaded["AMKR"]["last_fire_tags"], ["SECTOR", "PEERS"])


class TestSlackFormatting(unittest.TestCase):
    def test_daily_teaser_none_when_empty(self):
        self.assertIsNone(ep.format_daily_teaser([]))

    def test_daily_teaser_singular(self):
        fires = [ep.EPFire(ticker="X", date="d", close=1, chg_pct=4, rvol=0.5,
                           atr_pct=3, range_contract=0.5, prior_3d_cum=-9,
                           dist_52w_hi=-5, sector="", industry="", etf=None,
                           tags=[], peers=[])]
        self.assertIn("1 EP setup today", ep.format_daily_teaser(fires))

    def test_daily_teaser_with_hot(self):
        hot = ep.EPFire(ticker="X", date="d", close=1, chg_pct=4, rvol=0.5,
                       atr_pct=3, range_contract=0.5, prior_3d_cum=-9,
                       dist_52w_hi=-5, sector="", industry="", etf="SMH",
                       tags=["SECTOR", "PEERS"], peers=["Y"])
        cold = ep.EPFire(ticker="Z", date="d", close=1, chg_pct=4, rvol=0.5,
                       atr_pct=3, range_contract=0.5, prior_3d_cum=-9,
                       dist_52w_hi=-5, sector="", industry="", etf=None,
                       tags=[], peers=[])
        msg = ep.format_daily_teaser([hot, cold])
        self.assertIn("2 EP setups", msg)
        self.assertIn("1 🔥", msg)

    def test_momentum_blocks_structure(self):
        fires = [ep.EPFire(ticker="AMKR", date="2026-05-20", close=68.49, chg_pct=4.5,
                           rvol=0.58, atr_pct=5, range_contract=0.71, prior_3d_cum=-9.1,
                           dist_52w_hi=-10.5, sector="Tech", industry="Semis",
                           etf="SMH", tags=["SECTOR", "PEERS"], peers=["AXTI"])]
        blocks = ep.format_momentum_slack_blocks(fires, "2026-05-20")
        kinds = [b.get("type") for b in blocks]
        self.assertEqual(kinds[0], "header")
        self.assertIn("section", kinds)


class TestHTMLSection(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        self.assertEqual(ep.format_html_section([]), "")

    def test_renders_fire_card(self):
        fires = [ep.EPFire(ticker="AMKR", date="2026-05-20", close=68.49, chg_pct=4.5,
                          rvol=0.58, atr_pct=5, range_contract=0.71, prior_3d_cum=-9.1,
                          dist_52w_hi=-10.5, sector="Tech", industry="Semis",
                          etf="SMH", tags=["SECTOR"], peers=[])]
        html = ep.format_html_section(fires)
        self.assertIn("AMKR", html)
        self.assertIn("SECTOR ↑ SMH", html)
        self.assertIn("📈", html)  # leader emoji


if __name__ == "__main__":
    unittest.main()
