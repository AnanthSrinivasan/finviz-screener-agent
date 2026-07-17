"""
Unit tests for finviz_agent.py and finviz_earnings_alert.py

Run locally: python -m pytest test_finviz_agent.py -v

These tests use mocks — no real HTTP calls are made.
"""

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from agents.screener.finviz_agent import (
    aggregate_and_save,
    get_snapshot_metrics,
    generate_finviz_gallery,
    generate_ai_summary,
    send_slack_notification,
    _build_card,
    _classify_ticker,
    _score_hidden_growth,
    _is_base_building,
    _is_stage_transition,
    assert_scrape_healthy,
    scrape_canary,
    ScrapeHealthError,
)
from agents.alerts.earnings_alert import find_upcoming_earnings

# ----------------------------
# Helpers
# ----------------------------

def make_mock_screener_html(tickers: list) -> str:
    """Build a minimal Finviz screener HTML page with the given tickers.

    Mirrors the real post-2026-07-17 ticker cell: a company-logo link whose
    fallback ``<span>`` holds the ticker's FIRST LETTER, then the real
    ``a.tab-link``. A naive ``td.text`` read returns "AAAPL" for AAPL — the
    doubled-first-letter regression this fixture exists to catch."""
    rows = ""
    for i, t in enumerate(tickers):
        rows += f"""
        <tr valign="top">
            <td>{i+1}</td>
            <td align="left" data-boxover-ticker="{t}"><span class="flex items-center gap-1 pl-0.5"><a class="company-ticker" href="stock?t={t}&amp;ty=c"><img alt="{t} logo" src="https://logo.finviz.com/{t}.svg"><span>{t[0]}</span></img></a><a class="tab-link" href="stock?t={t}&amp;ty=c">{t}</a></span></td>
            <td>{t} Inc</td>
            <td>Technology</td>
            <td>Semiconductors</td>
            <td>USA</td>
            <td>1.5B</td>
            <td>25.0</td>
            <td>500K</td>
            <td>50.00</td>
            <td>+5.0%</td>
        </tr>"""
    return f"<html><body><table>{rows}</table></body></html>"


def make_mock_snapshot_html(price="50.00", atr="2.50", eps="25.0", sales="15.0",
                             eps_qq="30.0", inst_own="45.0", inst_trans="5.0") -> bytes:
    """Build a minimal Finviz quote page snapshot.

    Mirrors the real Finviz layout (post-2026-07 redesign): individual data
    cells carry class ``snapshot-td2`` and are emitted as flat key/value pairs
    in source order — NOT wrapped in a ``snapshot-table2`` table with 2-pair
    rows. Parsers pair ``snapshot-td2`` cells 0::2 (key) / 1::2 (value).
    """
    def kv(key, val):
        return (f'<td class="snapshot-td2">{key}</td>'
                f'<td class="snapshot-td2">{val}</td>')

    pairs = "".join([
        kv("Price", price),
        kv("ATR (14)", atr),
        kv("EPS Y/Y TTM", f"{eps}%"),
        kv("Sales Y/Y TTM", f"{sales}%"),
        kv("52W High", "55.00"),
        kv("Rel Volume", "1.2"),
        kv("Avg Volume", "500K"),
        kv("SMA20", "3.5%"),
        kv("SMA50", "2.1%"),
        kv("SMA200", "1.0%"),
        kv("EPS Q/Q", f"{eps_qq}%"),
        kv("Inst Own", f"{inst_own}%"),
        kv("Inst Trans", f"{inst_trans}%"),
    ])
    html = f"""
    <html><body>
    <table><tr>{pairs}</tr></table>
    </body></html>"""
    return html.encode()


# ----------------------------
# Tests: fetch_all_tickers / aggregate_and_save
# ----------------------------

class TestAggregateAndSave(unittest.TestCase):

    @patch("agents.screener.finviz_agent.session")
    def test_basic_fetch_returns_dataframe(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = make_mock_screener_html(["AAPL", "MSFT", "NVDA"])
        mock_session.get.return_value = mock_resp

        with patch("agents.screener.finviz_agent.fetch_all_tickers") as mock_fetch:
            mock_fetch.return_value = (
                pd.DataFrame({
                    "No.": [1, 2, 3],
                    "Ticker": ["AAPL", "MSFT", "NVDA"],
                    "Company": ["Apple", "Microsoft", "Nvidia"],
                    "Sector": ["Technology"] * 3,
                    "Industry": ["Consumer Electronics", "Software", "Semiconductors"],
                    "Country": ["USA"] * 3,
                    "Market Cap": ["3T", "3T", "2T"],
                    "P/E": ["30", "35", "40"],
                    "Volume": ["50M", "20M", "40M"],
                    "Price": ["170", "380", "850"],
                    "Change": ["+1%", "+2%", "+3%"],
                }),
                {
                    "AAPL": {"Company": "Apple", "Sector": "Technology", "Industry": "Consumer Electronics", "Country": "USA", "Market Cap": "3T"},
                    "MSFT": {"Company": "Microsoft", "Sector": "Technology", "Industry": "Software", "Country": "USA", "Market Cap": "3T"},
                    "NVDA": {"Company": "Nvidia", "Sector": "Technology", "Industry": "Semiconductors", "Country": "USA", "Market Cap": "2T"},
                }
            )
            with patch("agents.screener.finviz_agent.pd.DataFrame.to_csv"), \
                 patch("agents.screener.finviz_agent.pd.DataFrame.to_html"), \
                 patch("agents.screener.finviz_agent.os.makedirs"):
                summary_df, _, _ = aggregate_and_save({"Growth": "http://fake-url"})

        self.assertFalse(summary_df.empty)
        self.assertIn("Ticker", summary_df.columns)
        self.assertIn("Sector", summary_df.columns)

    @patch("agents.screener.finviz_agent.fetch_all_tickers")
    def test_empty_screener_returns_empty_df(self, mock_fetch):
        mock_fetch.return_value = (
            pd.DataFrame(columns=["No.", "Ticker", "Company", "Sector", "Industry",
                                   "Country", "Market Cap", "P/E", "Volume", "Price", "Change"]),
            {}
        )
        with patch("agents.screener.finviz_agent.os.makedirs"):
            summary_df, csv, html = aggregate_and_save({"Growth": "http://fake-url"})
        self.assertTrue(summary_df.empty)

    @patch("agents.screener.finviz_agent.fetch_all_tickers")
    def test_deduplication_across_screeners(self, mock_fetch):
        """Same ticker appearing in two screeners should have Appearances=2."""
        shared_df = pd.DataFrame({
            "No.": [1], "Ticker": ["NVDA"], "Company": ["Nvidia"],
            "Sector": ["Technology"], "Industry": ["Semiconductors"],
            "Country": ["USA"], "Market Cap": ["2T"],
            "P/E": ["40"], "Volume": ["40M"], "Price": ["850"], "Change": ["+3%"],
        })
        mock_fetch.return_value = (
            shared_df,
            {"NVDA": {"Company": "Nvidia", "Sector": "Technology", "Industry": "Semiconductors", "Country": "USA", "Market Cap": "2T"}}
        )
        with patch("agents.screener.finviz_agent.pd.DataFrame.to_csv"), \
             patch("agents.screener.finviz_agent.pd.DataFrame.to_html"), \
             patch("agents.screener.finviz_agent.os.makedirs"):
            summary_df, _, _ = aggregate_and_save({
                "Growth": "http://fake1",
                "IPO": "http://fake2",
            })

        nvda_row = summary_df[summary_df["Ticker"] == "NVDA"]
        self.assertEqual(nvda_row.iloc[0]["Appearances"], 2)


# ----------------------------
# Tests: get_snapshot_metrics
# ----------------------------

class TestGetSnapshotMetrics(unittest.TestCase):

    @patch("agents.screener.finviz_agent.make_session")
    def test_parses_metrics_correctly(self, mock_make_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = make_mock_snapshot_html(price="50.00", atr="2.50", eps="25.0", sales="15.0")
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        result = get_snapshot_metrics("AAPL")
        (atr_pct, eps, sales, dist_high, rel_vol, avg_vol,
         sma20, sma50, sma200, eps_qq, inst_own, inst_trans,
         perf_month, perf_quarter, perf_half_y, perf_year) = result

        self.assertAlmostEqual(atr_pct, 5.0, places=1)   # 2.50 / 50.00 * 100
        self.assertAlmostEqual(eps, 25.0, places=1)
        self.assertAlmostEqual(sales, 15.0, places=1)
        self.assertAlmostEqual(eps_qq, 30.0, places=1)
        self.assertAlmostEqual(inst_own, 45.0, places=1)
        self.assertAlmostEqual(inst_trans, 5.0, places=1)

    @patch("agents.screener.finviz_agent.make_session")
    def test_returns_none_on_missing_table(self, mock_make_session):
        mock_resp = MagicMock()
        mock_resp.content = b"<html><body>no table here</body></html>"
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        result = get_snapshot_metrics("FAKE")
        self.assertEqual(result, (None,) * 16)

    @patch("agents.screener.finviz_agent.make_session")
    def test_retries_on_429(self, mock_make_session):
        """Should retry on rate limit and eventually return None after exhausting retries."""
        import requests as req

        mock_resp = MagicMock()
        http_err = req.HTTPError(response=MagicMock(status_code=429))
        mock_resp.raise_for_status.side_effect = http_err

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        with patch("agents.screener.finviz_agent.time.sleep"):  # don't actually sleep in tests
            result = get_snapshot_metrics("AAPL", max_retries=2)

        self.assertEqual(result, (None,) * 16)


# ----------------------------
# Tests: scrape-health guard (2026-07-03 Finviz layout regression)
# ----------------------------

class TestScrapeHealthGuard(unittest.TestCase):
    """The June 29–July 2 2026 silent break: Finviz layout change zeroed every
    ATR%/SMA, screener posted '0 passed' and exited green for 3 days. These
    guards must make the next such break fail LOUDLY on day 1."""

    def test_assert_healthy_passes_on_real_data(self):
        df = pd.DataFrame({"ATR%": [2.5, 0.0, 4.1], "SMA50%": [1.0, 0.0, -3.0]})
        # Should not raise — some tickers have real ATR/SMA.
        assert_scrape_healthy(df)

    def test_assert_healthy_raises_on_all_zero(self):
        # The exact 2026-07-02 signature: every ticker ATR%=0 AND SMA50%=0.
        df = pd.DataFrame({"ATR%": [0.0, 0.0, 0.0], "SMA50%": [0.0, 0.0, 0.0]})
        with self.assertRaises(ScrapeHealthError):
            assert_scrape_healthy(df)

    def test_assert_healthy_raises_on_all_nan(self):
        df = pd.DataFrame({"ATR%": [None, None], "SMA50%": [None, None]})
        with self.assertRaises(ScrapeHealthError):
            assert_scrape_healthy(df)

    def test_assert_healthy_empty_df_is_noop(self):
        # 0 tickers is a legitimate quiet-market no-op, NOT a scrape break.
        assert_scrape_healthy(pd.DataFrame(columns=["ATR%", "SMA50%"]))

    def test_assert_healthy_passes_when_atr_zero_but_sma_present(self):
        # Only both-zero trips the guard; a real name can have ATR%=0 rounding
        # while SMA is populated, so require BOTH columns dead.
        df = pd.DataFrame({"ATR%": [0.0, 0.0], "SMA50%": [1.2, -3.4]})
        assert_scrape_healthy(df)

    @patch("agents.screener.finviz_agent.get_snapshot_metrics")
    def test_canary_passes_on_good_metrics(self, mock_metrics):
        mock_metrics.return_value = (2.83, 28.9, 12.7, -2.7, 1.4, 5e7,
                                     4.6, 5.1, 14.0, 22.0, 67.2, 0.0,
                                     -2.0, 20.7, 13.0, 45.2)
        scrape_canary("AAPL")  # should not raise

    @patch("agents.screener.finviz_agent.get_snapshot_metrics")
    def test_canary_raises_on_none(self, mock_metrics):
        mock_metrics.return_value = (None,) * 16
        with self.assertRaises(ScrapeHealthError):
            scrape_canary("AAPL")

    @patch("agents.screener.finviz_agent.get_snapshot_metrics")
    def test_canary_raises_on_zero_atr(self, mock_metrics):
        # ATR%=0 for AAPL is impossible — the broken-scrape signature.
        vals = [0.0] + [0.0] * 15
        mock_metrics.return_value = tuple(vals)
        with self.assertRaises(ScrapeHealthError):
            scrape_canary("AAPL")


# ----------------------------
# Tests: generate_finviz_gallery
# ----------------------------

class TestGenerateGallery(unittest.TestCase):

    def _make_filter_df(self):
        return pd.DataFrame({
            "Ticker": ["AAPL", "NVDA"],
            "Appearances": [2, 1],
            "Screeners": ["Growth, IPO", "Growth"],
            "Company": ["Apple Inc", "Nvidia Corp"],
            "Sector": ["Technology", "Technology"],
            "Industry": ["Consumer Electronics", "Semiconductors"],
            "Country": ["USA", "USA"],
            "Market Cap": ["3T", "2T"],
            "ATR%": [4.5, 6.2],
            "EPS Y/Y TTM": [15.0, 80.0],
            "Sales Y/Y TTM": [10.0, 120.0],
            "Dist From High%": [-10.0, -5.0],
            "Rel Volume": [1.5, 2.0],
            "Avg Volume": [50_000_000, 40_000_000],
            "SMA20%": [3.5, 4.0],
            "SMA50%": [2.1, 3.0],
            "SMA200%": [15.0, 10.0],
            "Stage": [
                {"stage": 2, "badge": "🟢 Stage 2", "perfect": True, "sma20": 3.5, "sma50": 2.1, "sma200": 15.0},
                {"stage": 2, "badge": "🟢 Stage 2", "perfect": True, "sma20": 4.0, "sma50": 3.0, "sma200": 10.0},
            ],
            "VCP": [
                {"vcp_possible": False, "confidence": 0, "reason": "no signals"},
                {"vcp_possible": True, "confidence": 75, "reason": "tight range · volume dry-up"},
            ],
            "Quality Score": [55.0, 72.0],
        })

    def test_creates_html_file(self):
        filter_df = self._make_filter_df()
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            path = generate_finviz_gallery(["AAPL", "NVDA"], filter_df)
        self.assertIn("finviz_chart_grid_", path)
        self.assertTrue(path.endswith(".html"))

    def test_html_contains_sector_tag(self):
        filter_df = self._make_filter_df()
        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["AAPL"], filter_df)
        html_content = "".join(written)
        self.assertIn("sector-tag", html_content)
        self.assertIn("Technology", html_content)

    def test_html_contains_company_name(self):
        filter_df = self._make_filter_df()
        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["AAPL"], filter_df)
        html_content = "".join(written)
        self.assertIn("Apple Inc", html_content)

    def test_sector_lead_badge_shown_for_dominant_sector(self):
        """Tickers in the top 2 sectors by count should get the Lead Sector badge."""
        # Both AAPL and NVDA are Technology — it will be the top sector
        filter_df = self._make_filter_df()
        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["AAPL", "NVDA"], filter_df)
        html_content = "".join(written)
        self.assertIn("tag-sector-lead", html_content)
        self.assertIn("Lead Sector", html_content)

    def test_sector_lead_badge_absent_for_minority_sector(self):
        """A ticker in a 3rd-place sector should not get the Lead Sector badge."""
        # Build a df with 3 sectors: Technology(2), Healthcare(1), Energy(1-rendered)
        # top_sectors = {Technology, Healthcare} — Energy ticker should get no badge
        base = self._make_filter_df()
        extra_row = base.iloc[0:1].copy()
        extra_row["Ticker"] = "EXTRA"
        extra_row["Sector"] = "Healthcare"
        energy_row = base.iloc[0:1].copy()
        energy_row["Ticker"] = "ENGY"
        energy_row["Sector"] = "Energy"
        filter_df = pd.concat([base, extra_row, energy_row], ignore_index=True)
        # Sector counts: Technology=2, Healthcare=1, Energy=1  → top2 = Technology + one of the tied pair
        # Force unambiguous minority: add 2nd Healthcare to push it clearly to top2
        extra_row2 = extra_row.copy()
        extra_row2["Ticker"] = "HLTH2"
        filter_df = pd.concat([filter_df, extra_row2], ignore_index=True)
        # Now: Technology=2, Healthcare=2, Energy=1 → top2 = Technology & Healthcare; Energy is minority

        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["ENGY"], filter_df)
        html_content = "".join(written)
        self.assertNotIn("Lead Sector", html_content)


# ----------------------------
# Tests: _build_card & gallery with CC-hint trigger data
# ----------------------------

def _make_realistic_df():
    """
    Realistic multi-ticker DataFrame that exercises every branch in _build_card
    and _classify_ticker:
      - GEV:  Stage 2, high RVol (3.1), high ATR (5.8%), positive EPS → CC hint ON
      - PLTR: Stage 2, moderate RVol (2.0), positive EPS → CC hint ON (stage2 path)
      - HIMS: Not Stage 2 (stage 1), RVol 2.8, ATR 4.5%, positive EPS → CC hint ON (momentum path)
      - SMCI: Not Stage 2 (stage 4), low RVol, low EPS → CC hint OFF, stage4 border
      - CXW:  Stage 2, VCP confirmed, negative EPS → CC hint OFF (EPS fails)
      - RKLB: IPO screener, stage 0 → classified as 'ipo'
    """
    return pd.DataFrame({
        "Ticker":          ["GEV",     "PLTR",    "HIMS",    "SMCI",    "CXW",     "RKLB"],
        "Appearances":     [3,          2,         2,         1,         1,          1],
        "Screeners":       ["Growth, 52 Week High", "Growth", "10% Change, Growth", "Growth", "Growth", "IPO"],
        "Company":         ["GE Vernova", "Palantir Technologies", "Hims & Hers Health",
                            "Super Micro Computer", "CoreCivic", "Rocket Lab USA"],
        "Sector":          ["Industrials", "Technology", "Healthcare",
                            "Technology", "Industrials", "Industrials"],
        "Industry":        ["Electrical Equipment", "Software - Infrastructure", "Health Information Services",
                            "Computer Hardware", "Security & Protection Services", "Aerospace & Defense"],
        "Country":         ["USA"] * 6,
        "Market Cap":      ["93.5B",  "280.1B",  "8.2B",    "22.6B",   "1.98B",   "12.4B"],
        "ATR%":            [5.8,       3.9,       4.5,       7.2,       3.1,        6.0],
        "EPS Y/Y TTM":     [42.0,      35.0,      120.0,     -15.0,     -8.0,       None],
        "Sales Y/Y TTM":   [28.0,      20.0,      70.0,      10.0,      5.0,        55.0],
        "Dist From High%": [-8.0,      -12.0,     -25.0,     -45.0,     -18.0,      -30.0],
        "Rel Volume":      [3.1,       2.0,       2.8,       0.9,       1.1,        1.8],
        "Avg Volume":      [5_200_000, 80_000_000, 15_000_000, 30_000_000, 1_200_000, 8_000_000],
        "SMA20%":          [4.2,       2.8,       6.1,       -3.5,      1.0,        5.5],
        "SMA50%":          [8.1,       5.0,       12.0,      -8.0,      3.0,        10.0],
        "SMA200%":         [15.0,      10.0,      10.0,      -20.0,     8.0,        None],
        "Stage": [
            {"stage": 2, "badge": "🟢 Stage 2", "perfect": True,  "sma20": 4.2, "sma50": 8.1, "sma200": 15.0},
            {"stage": 2, "badge": "🟢 Stage 2", "perfect": True,  "sma20": 2.8, "sma50": 5.0, "sma200": 10.0},
            {"stage": 0, "badge": "⚪ Transitional", "perfect": False, "sma20": 6.1, "sma50": 12.0, "sma200": 10.0},
            {"stage": 4, "badge": "⚫ Stage 4", "perfect": False, "sma20": -3.5, "sma50": -8.0, "sma200": -20.0},
            {"stage": 2, "badge": "🟢 Stage 2", "perfect": True,  "sma20": 1.0, "sma50": 3.0, "sma200": 8.0},
            {"stage": 0, "badge": "",            "perfect": False, "sma20": 5.5, "sma50": 10.0, "sma200": 0.0},
        ],
        "VCP": [
            {"vcp_possible": False, "confidence": 0,  "reason": "no signals"},
            {"vcp_possible": False, "confidence": 20, "reason": "weak"},
            {"vcp_possible": True,  "confidence": 70, "reason": "tight range · volume dry-up"},
            {"vcp_possible": False, "confidence": 0,  "reason": "downtrend"},
            {"vcp_possible": True,  "confidence": 65, "reason": "narrowing range"},
            {"vcp_possible": False, "confidence": 0,  "reason": "too volatile"},
        ],
        "Quality Score":   [74.0, 68.0, 62.0, 35.0, 40.0, 55.0],
    })


class TestBuildCardAllBranches(unittest.TestCase):
    """Direct _build_card tests that exercise every conditional branch."""

    def _row(self, df, ticker):
        return df[df['Ticker'] == ticker].iloc[0]

    def test_cc_hint_stage2_high_rvol(self):
        """GEV: Stage 2 + EPS > 0 + RVol >= 2.0 → CC hint badge should appear."""
        df = _make_realistic_df()
        html = _build_card("GEV", self._row(df, "GEV"), "https://finviz.com")
        self.assertIn("tag-cc-hint", html)
        self.assertIn("CC?", html)

    def test_cc_hint_stage2_moderate_rvol(self):
        """PLTR: Stage 2 + EPS > 0 + RVol == 2.0 → CC hint via stage2 path."""
        df = _make_realistic_df()
        html = _build_card("PLTR", self._row(df, "PLTR"), "https://finviz.com")
        self.assertIn("tag-cc-hint", html)

    def test_cc_hint_momentum_path(self):
        """HIMS: Not Stage 2, but RVol >= 2.5 + ATR >= 4.0 + EPS > 0 → CC hint."""
        df = _make_realistic_df()
        html = _build_card("HIMS", self._row(df, "HIMS"), "https://finviz.com")
        self.assertIn("tag-cc-hint", html)

    def test_no_cc_hint_negative_eps(self):
        """CXW: Stage 2 but EPS < 0 → no CC hint."""
        df = _make_realistic_df()
        html = _build_card("CXW", self._row(df, "CXW"), "https://finviz.com")
        self.assertNotIn("tag-cc-hint", html)

    def test_no_cc_hint_low_rvol(self):
        """SMCI: Stage 4, low RVol, negative EPS → no CC hint."""
        df = _make_realistic_df()
        html = _build_card("SMCI", self._row(df, "SMCI"), "https://finviz.com")
        self.assertNotIn("tag-cc-hint", html)

    def test_stage4_border_color(self):
        """SMCI is stage 4 → gray border."""
        df = _make_realistic_df()
        html = _build_card("SMCI", self._row(df, "SMCI"), "https://finviz.com")
        self.assertIn("#6b7280", html)

    def test_stage2_vcp_border_color(self):
        """CXW is stage 2 + VCP → yellow border."""
        df = _make_realistic_df()
        html = _build_card("CXW", self._row(df, "CXW"), "https://finviz.com")
        self.assertIn("#facc15", html)
        self.assertIn("tag-vcp", html)

    def test_perfect_alignment_badge(self):
        """GEV has stage.perfect=True → aligned badge."""
        df = _make_realistic_df()
        html = _build_card("GEV", self._row(df, "GEV"), "https://finviz.com")
        self.assertIn("tag-perf", html)
        self.assertIn("aligned", html)

    def test_sector_lead_badge_with_top_sectors(self):
        """Industrials has 3 tickers (GEV, CXW, RKLB) → should be top sector."""
        df = _make_realistic_df()
        top_sectors = {"Industrials", "Technology"}
        html = _build_card("GEV", self._row(df, "GEV"), "https://finviz.com", top_sectors)
        self.assertIn("tag-sector-lead", html)

    def test_no_sector_lead_badge_minority(self):
        """Healthcare has only 1 ticker (HIMS) → not in top_sectors."""
        df = _make_realistic_df()
        top_sectors = {"Industrials", "Technology"}
        html = _build_card("HIMS", self._row(df, "HIMS"), "https://finviz.com", top_sectors)
        self.assertNotIn("tag-sector-lead", html)

    def test_none_sma200_renders_dash(self):
        """RKLB has SMA200% = None → should render as '—'."""
        df = _make_realistic_df()
        html = _build_card("RKLB", self._row(df, "RKLB"), "https://finviz.com")
        # SMA200 should be the em-dash fallback
        self.assertIn("200d —", html)

    def test_none_eps_renders_dash(self):
        """RKLB has EPS Y/Y TTM = None → should render as '—'."""
        df = _make_realistic_df()
        html = _build_card("RKLB", self._row(df, "RKLB"), "https://finviz.com")
        self.assertIn("EPS —", html)

    def test_quality_score_color_high(self):
        """GEV has QS 74 → green color."""
        df = _make_realistic_df()
        html = _build_card("GEV", self._row(df, "GEV"), "https://finviz.com")
        self.assertIn("#4ade80", html)  # green for QS >= 60

    def test_quality_score_color_mid(self):
        """CXW has QS 40 → yellow color."""
        df = _make_realistic_df()
        html = _build_card("CXW", self._row(df, "CXW"), "https://finviz.com")
        self.assertIn("#facc15", html)  # yellow for QS 35-59

    def test_quality_score_color_low(self):
        """SMCI has QS 35 → yellow boundary (>= 35)."""
        df = _make_realistic_df()
        html = _build_card("SMCI", self._row(df, "SMCI"), "https://finviz.com")
        # QS 35 is exactly the boundary → yellow
        self.assertIn("Q 35", html)


class TestClassifyTicker(unittest.TestCase):
    """Ensure _classify_ticker routes tickers to the correct gallery section."""

    def _row(self, df, ticker):
        return df[df['Ticker'] == ticker].iloc[0]

    def test_ipo_classification(self):
        df = _make_realistic_df()
        self.assertEqual(_classify_ticker(self._row(df, "RKLB")), "ipo")

    def test_stage2_classification(self):
        df = _make_realistic_df()
        self.assertEqual(_classify_ticker(self._row(df, "GEV")), "stage2")

    def test_momentum_classification(self):
        """HIMS: not stage 2, RVol 2.8 >= 2.0, ATR 4.5 >= 4.0 → momentum."""
        df = _make_realistic_df()
        self.assertEqual(_classify_ticker(self._row(df, "HIMS")), "momentum")

    def test_watch_classification(self):
        """SMCI: stage 4, low RVol → watch."""
        df = _make_realistic_df()
        self.assertEqual(_classify_ticker(self._row(df, "SMCI")), "watch")


class TestGalleryEndToEndRealistic(unittest.TestCase):
    """
    End-to-end generate_finviz_gallery with realistic data that triggers
    every _build_card branch — the exact scenario that caused today's crash.
    """

    def test_gallery_with_cc_hint_triggering_data(self):
        """
        Runs generate_finviz_gallery with 6 tickers including Stage 2 stocks
        with RVol > 2.5 and ATR > 4%. This is the exact condition that was
        untested and caused the atr_pct NameError crash in production.
        """
        df = _make_realistic_df()
        tickers = df['Ticker'].tolist()
        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            path = generate_finviz_gallery(tickers, df)

        html = "".join(written)

        # Gallery was created
        self.assertIn("finviz_chart_grid_", path)
        self.assertTrue(path.endswith(".html"))

        # All 6 tickers rendered
        for t in tickers:
            self.assertIn(t, html)

        # Section headers present (at least stage2 and ipo sections have cards)
        self.assertIn("Stage 2 Leaders", html)
        self.assertIn("IPO Lifecycle", html)

        # CC hint badges rendered for the 3 qualifying tickers (+ 1 CSS rule = 4 total)
        self.assertEqual(html.count("tag-cc-hint"), 4)  # GEV, PLTR, HIMS + CSS def

        # VCP badge for CXW and HIMS (+ 1 CSS rule = 3 total)
        self.assertEqual(html.count("tag-vcp"), 3)

        # Stage 4 gray border for SMCI
        self.assertIn("#6b7280", html)

        # Perfect alignment for GEV
        self.assertIn("tag-perf", html)

    def test_gallery_handles_missing_optional_fields(self):
        """Gallery shouldn't crash when optional fields are None/NaN."""
        df = _make_realistic_df()
        # Null out several optional fields on one ticker
        idx = df[df['Ticker'] == 'RKLB'].index[0]
        df.at[idx, 'EPS Y/Y TTM'] = None
        df.at[idx, 'Sales Y/Y TTM'] = None
        df.at[idx, 'SMA200%'] = None
        df.at[idx, 'Avg Volume'] = None
        df.at[idx, 'Quality Score'] = None
        df.at[idx, 'Dist From High%'] = None

        written = []
        with patch("agents.screener.finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            path = generate_finviz_gallery(["RKLB"], df)

        html = "".join(written)
        self.assertIn("RKLB", html)
        # Null fields should render as em-dash, not crash
        self.assertIn("EPS —", html)
        self.assertIn("200d —", html)


# ----------------------------
# Tests: generate_ai_summary
# ----------------------------

class TestAiSummary(unittest.TestCase):

    def _make_filter_df(self):
        return pd.DataFrame({
            "Ticker": ["NVDA", "AAPL"],
            "Appearances": [2, 1],
            "Screeners": ["Growth, IPO", "Growth"],
            "Sector": ["Technology", "Technology"],
            "Industry": ["Semiconductors", "Consumer Electronics"],
            "Market Cap": ["2T", "3T"],
            "ATR%": [6.2, 4.5],
            "EPS Y/Y TTM": [80.0, 15.0],
            "Sales Y/Y TTM": [120.0, 10.0],
            "Dist From High%": [-5.0, -10.0],
            "Rel Volume": [2.0, 1.5],
            "Quality Score": [72.0, 55.0],
        })

    def test_returns_empty_string_without_api_key(self):
        with patch("agents.screener.finviz_agent.ANTHROPIC_API_KEY", ""):
            result = generate_ai_summary(self._make_filter_df(), "2026-01-01")
        self.assertEqual(result, "")

    @patch("agents.screener.finviz_agent.ANTHROPIC_API_KEY", "sk-ant-fake")
    @patch("agents.screener.finviz_agent.requests.post")
    def test_returns_summary_on_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": "NVDA and AAPL are top picks today."}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_resp.ok = True
        mock_post.return_value = mock_resp

        result = generate_ai_summary(self._make_filter_df(), "2026-01-01")
        self.assertEqual(result, "NVDA and AAPL are top picks today.")

    @patch("agents.screener.finviz_agent.ANTHROPIC_API_KEY", "sk-ant-fake")
    @patch("agents.screener.finviz_agent.requests.post")
    def test_returns_empty_string_on_api_error(self, mock_post):
        mock_post.side_effect = Exception("connection error")
        result = generate_ai_summary(self._make_filter_df(), "2026-01-01")
        self.assertEqual(result, "")


# ----------------------------
# Tests: send_slack_notification
# ----------------------------

class TestSlackNotification(unittest.TestCase):

    def _make_dfs(self):
        summary_df = pd.DataFrame({
            "Ticker": ["NVDA", "AAPL", "MSFT"],
            "Appearances": [2, 1, 1],
            "Screeners": ["Growth, IPO", "Growth", "IPO"],
            "Sector": ["Technology", "Technology", "Technology"],
            "ATR%": [6.2, 4.5, 3.8],
            "EPS Y/Y TTM": [80.0, 15.0, 20.0],
            "Market Cap": ["2T", "3T", "3T"],
            "Quality Score": [72.0, 55.0, 50.0],
        })
        filter_df = summary_df[summary_df["ATR%"] > 3.0].copy()
        return summary_df, filter_df

    def test_skips_when_no_webhook(self):
        with patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", ""):
            with patch("agents.screener.finviz_agent.requests.post") as mock_post:
                summary_df, filter_df = self._make_dfs()
                send_slack_notification(summary_df, filter_df, "data/gallery.html", "2026-01-01", "")
                mock_post.assert_not_called()

    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    @patch("agents.screener.finviz_agent.requests.post")
    def test_sends_message_with_webhook(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        summary_df, filter_df = self._make_dfs()
        send_slack_notification(summary_df, filter_df, "data/gallery.html", "2026-01-01", "NVDA is top pick.")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"]
        self.assertIn("blocks", payload)

    @patch("agents.screener.finviz_agent.SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    @patch("agents.screener.finviz_agent.GITHUB_PAGES_BASE", "https://user.github.io/repo")
    @patch("agents.screener.finviz_agent.requests.post")
    def test_includes_gallery_link_when_pages_set(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        summary_df, filter_df = self._make_dfs()
        send_slack_notification(summary_df, filter_df, "data/finviz_chart_grid_2026-01-01.html", "2026-01-01", "")

        payload = mock_post.call_args[1]["json"]
        blocks_text = str(payload)
        self.assertIn("github.io", blocks_text)


# ----------------------------
# Tests: finviz_earnings_alert — quality + sector filter
# ----------------------------

class TestEarningsQualityFilter(unittest.TestCase):

    def _make_tickers(self, quality, sector):
        return {"FAKE": {"appearances": 3, "atr": 4.5, "quality": quality, "sector": sector, "screeners": "Growth", "market_cap": "1B"}}

    @patch("agents.alerts.earnings_alert.fetch_earnings_date")
    def test_skips_low_quality_score(self, mock_fetch):
        """Tickers with Quality Score <= 50 should be skipped without calling Finviz."""
        tickers = self._make_tickers(quality=40.0, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("agents.alerts.earnings_alert.fetch_earnings_date")
    def test_skips_missing_sector(self, mock_fetch):
        """Tickers with no sector should be skipped without calling Finviz."""
        tickers = self._make_tickers(quality=70.0, sector="")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("agents.alerts.earnings_alert.fetch_earnings_date")
    def test_skips_none_quality(self, mock_fetch):
        """Tickers with None quality should be skipped."""
        tickers = self._make_tickers(quality=None, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("agents.alerts.earnings_alert.fetch_earnings_date")
    def test_passes_high_quality_with_sector(self, mock_fetch):
        """Tickers with Quality Score > 50 and a sector should proceed to Finviz lookup."""
        import datetime
        today = datetime.date.today()
        mock_fetch.return_value = today + datetime.timedelta(days=3)  # earnings in 3 days

        tickers = self._make_tickers(quality=65.0, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_called_once()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "FAKE")

    @patch("agents.alerts.earnings_alert.fetch_earnings_date")
    def test_boundary_quality_score_excluded(self, mock_fetch):
        """Quality Score exactly 50 should be excluded (strictly greater than required)."""
        tickers = self._make_tickers(quality=50.0, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])




class TestScoreHiddenGrowthDistortedTTM(unittest.TestCase):
    """Tests for the distorted-TTM 3/6 path in _score_hidden_growth and its threshold logic."""

    def _row(self, eps_yy=0, eps_qq=0, inst_trans=0, appearances=0,
             stage_num=2, stage_perfect=True, screeners=""):
        return {
            "EPS Y/Y TTM": eps_yy,
            "EPS Q/Q":     eps_qq,
            "Inst Trans":  inst_trans,
            "Appearances": appearances,
            "Screeners":   screeners,
            "Stage":       {"stage": stage_num, "perfect": stage_perfect},
        }

    # ── _score_hidden_growth criteria ──────────────────────────────────────────

    def test_eps_qq_strong_fires_when_qq_above_50(self):
        c = _score_hidden_growth(self._row(eps_yy=10, eps_qq=60))
        self.assertTrue(c["eps_qq_strong"])

    def test_eps_qq_strong_fires_when_ttm_negative_and_qq_above_20(self):
        """FSLY-like: TTM -80%, Q/Q +55% → eps_qq_strong = True, eps_yy_strong = False."""
        c = _score_hidden_growth(self._row(eps_yy=-80, eps_qq=55))
        self.assertTrue(c["eps_qq_strong"])
        self.assertFalse(c["eps_yy_strong"])

    def test_eps_qq_strong_false_when_qq_below_20_and_ttm_negative(self):
        c = _score_hidden_growth(self._row(eps_yy=-50, eps_qq=10))
        self.assertFalse(c["eps_qq_strong"])

    def test_stage2_perfect_criterion(self):
        c = _score_hidden_growth(self._row(stage_num=2, stage_perfect=True))
        self.assertTrue(c["stage2_perfect"])

    def test_inst_buying_criterion(self):
        c = _score_hidden_growth(self._row(inst_trans=5))
        self.assertTrue(c["inst_buying"])

    # ── Distorted-TTM 3/6 threshold: the FSLY case ────────────────────────────

    def test_fsly_profile_qualifies_at_3_of_6(self):
        """
        FSLY: eps_qq=+55 (strong), eps_yy=-50 (negative, distorted), inst_trans=+7.91,
        stage2_perfect=True, appearances=1 → eps_qq_strong + inst_buying + stage2_perfect = 3
        criteria, all with confirmed TTM distortion → should flag as HG.
        """
        criteria = _score_hidden_growth(self._row(
            eps_yy=-50, eps_qq=55.77,
            inst_trans=7.91,
            appearances=1,
            stage_num=2, stage_perfect=True,
        ))
        score = sum(criteria.values())
        ttm_distorted = criteria["eps_qq_strong"] and not criteria["eps_yy_strong"]
        threshold = 3 if ttm_distorted else 4
        self.assertTrue(ttm_distorted, "TTM should be flagged as distorted")
        self.assertGreaterEqual(score, threshold, f"Score {score} should meet threshold {threshold}")

    def test_normal_path_requires_4_of_6(self):
        """Without TTM distortion (eps_yy > 0), threshold stays 4."""
        criteria = _score_hidden_growth(self._row(
            eps_yy=60, eps_qq=70,
            inst_trans=5,
            appearances=1,
            stage_num=2, stage_perfect=True,
        ))
        score = sum(criteria.values())
        ttm_distorted = criteria["eps_qq_strong"] and not criteria["eps_yy_strong"]
        threshold = 3 if ttm_distorted else 4
        self.assertFalse(ttm_distorted)
        self.assertEqual(threshold, 4)
        # score here is eps_qq_strong + eps_yy_strong + inst_buying + stage2_perfect = 4
        self.assertGreaterEqual(score, threshold)

    def test_distorted_ttm_with_only_2_criteria_does_not_qualify(self):
        """TTM distorted but only 2 criteria met (no inst_buying, no stage2_perfect)."""
        criteria = _score_hidden_growth(self._row(
            eps_yy=-50, eps_qq=55,
            inst_trans=1,    # below inst_buying threshold (3)
            appearances=0,
            stage_num=1, stage_perfect=False,
        ))
        score = sum(criteria.values())
        ttm_distorted = criteria["eps_qq_strong"] and not criteria["eps_yy_strong"]
        threshold = 3 if ttm_distorted else 4
        self.assertTrue(ttm_distorted)
        self.assertLess(score, threshold, f"Score {score} should be below threshold {threshold}")

    def test_high_vol_badge_on_build_card(self):
        """ATR > 7 + Q >= 80 → badge-warn should appear in card HTML."""
        row = {
            "Ticker": "TSLA", "ATR%": 8.5, "Quality Score": 85,
            "EPS Y/Y TTM": 20.0, "Appearances": 3, "Screeners": "Stage 2",
            "Dist From High%": -5.0, "Rel Volume": 1.5,
            "SMA20%": 3.0, "SMA50%": 5.0, "SMA200%": 12.0,
            "Market Cap": "Large", "Company": "Tesla", "Sector": "Technology",
            "Industry": "Auto", "Stage": {"stage": 2, "perfect": True, "badge": "S2"},
            "VCP": {"vcp_possible": False, "confidence": 0},
        }
        html = _build_card("TSLA", row, "https://finviz.com")
        self.assertIn("badge-warn", html)
        self.assertIn("High-vol", html)

    def test_no_high_vol_badge_when_atr_below_threshold(self):
        """ATR <= 7 → no badge-warn even with high Q."""
        row = {
            "Ticker": "AAPL", "ATR%": 3.0, "Quality Score": 90,
            "EPS Y/Y TTM": 30.0, "Appearances": 5, "Screeners": "Stage 2",
            "Dist From High%": -3.0, "Rel Volume": 1.0,
            "SMA20%": 2.0, "SMA50%": 4.0, "SMA200%": 10.0,
            "Market Cap": "Large", "Company": "Apple", "Sector": "Technology",
            "Industry": "Consumer Electronics",
            "Stage": {"stage": 2, "perfect": True, "badge": "S2"},
            "VCP": {"vcp_possible": False, "confidence": 0},
        }
        html = _build_card("AAPL", row, "https://finviz.com")
        self.assertNotIn("badge-warn", html)

    def test_no_high_vol_badge_when_quality_below_80(self):
        """ATR > 7 but Q < 80 → no badge-warn."""
        row = {
            "Ticker": "XYZ", "ATR%": 9.0, "Quality Score": 75,
            "EPS Y/Y TTM": 10.0, "Appearances": 2, "Screeners": "Momentum",
            "Dist From High%": -8.0, "Rel Volume": 1.2,
            "SMA20%": 1.0, "SMA50%": 3.0, "SMA200%": 8.0,
            "Market Cap": "Small", "Company": "XYZ Corp", "Sector": "Industrials",
            "Industry": "Manufacturing",
            "Stage": {"stage": 2, "perfect": False, "badge": "S2"},
            "VCP": {"vcp_possible": False, "confidence": 0},
        }
        html = _build_card("XYZ", row, "https://finviz.com")
        self.assertNotIn("badge-warn", html)


class TestIsBaseBuilding(unittest.TestCase):
    """Tests for the _is_base_building predicate."""

    def _row(self, ticker="TEST", stage_num=2, stage_perfect=True, qs=80,
             dist=-18.0, atr=4.0):
        return {
            "Ticker": ticker,
            "Stage": {"stage": stage_num, "perfect": stage_perfect},
            "Quality Score": qs,
            "Dist From High%": dist,
            "ATR%": atr,
        }

    def test_qualifies_with_all_criteria_met(self):
        self.assertTrue(_is_base_building(self._row(), set(), set()))

    def test_fails_when_held(self):
        self.assertFalse(_is_base_building(self._row(), {"TEST"}, set()))

    def test_fails_when_in_exclude_tickers(self):
        self.assertFalse(_is_base_building(self._row(), set(), {"TEST"}))

    def test_fails_when_stage_not_2(self):
        self.assertFalse(_is_base_building(self._row(stage_num=1), set(), set()))
        self.assertFalse(_is_base_building(self._row(stage_num=3), set(), set()))

    def test_fails_when_quality_score_below_75(self):
        self.assertFalse(_is_base_building(self._row(qs=74), set(), set()))

    def test_passes_at_exactly_qs_75(self):
        self.assertTrue(_is_base_building(self._row(qs=75), set(), set()))

    def test_fails_when_dist_too_shallow(self):
        """dist > -12 → too close to high (Ready-to-Enter/Breakout territory)."""
        self.assertFalse(_is_base_building(self._row(dist=-11.0), set(), set()))

    def test_fails_when_dist_too_deep(self):
        """dist < -25 → too deep, likely broken base."""
        self.assertFalse(_is_base_building(self._row(dist=-26.0), set(), set()))

    def test_passes_at_dist_boundary_minus_12(self):
        self.assertTrue(_is_base_building(self._row(dist=-12.0), set(), set()))

    def test_passes_at_dist_boundary_minus_25(self):
        self.assertTrue(_is_base_building(self._row(dist=-25.0), set(), set()))

    def test_fails_when_atr_above_7(self):
        self.assertFalse(_is_base_building(self._row(atr=7.1), set(), set()))

    def test_passes_at_exactly_atr_7(self):
        self.assertTrue(_is_base_building(self._row(atr=7.0), set(), set()))

    def test_fails_when_dist_missing(self):
        row = self._row()
        del row["Dist From High%"]
        self.assertFalse(_is_base_building(row, set(), set()))


class TestIsStageTransition(unittest.TestCase):
    """Tests for the _is_stage_transition predicate (🌱 early Stage 2 reclaim)."""

    def _row(self, ticker="SNOW", sma20=3.0, sma50=2.0, sma200=-3.0,
             atr=4.0, qs=82, rvol=1.4, industry="Software - Application",
             sector="Technology"):
        return pd.Series({
            "Ticker": ticker,
            "SMA20%": sma20,
            "SMA50%": sma50,
            "SMA200%": sma200,
            "ATR%": atr,
            "Quality Score": qs,
            "Rel Volume": rvol,
            "Sector": sector,
            "Industry": industry,
        })

    def _snapshot(self, etf="IGV", rank_delta_5d=-8, rs_score=72):
        return {etf: {"rank_delta_5d": rank_delta_5d, "rs_score": rs_score, "rank": 5, "name": etf}}

    def test_qualifies_with_all_criteria(self):
        self.assertTrue(_is_stage_transition(self._row(), set(), set(), self._snapshot()))

    def test_fails_when_held(self):
        self.assertFalse(_is_stage_transition(self._row(), {"SNOW"}, set(), self._snapshot()))

    def test_fails_when_in_exclude(self):
        self.assertFalse(_is_stage_transition(self._row(), set(), {"SNOW"}, self._snapshot()))

    def test_fails_when_sma20_below_sma50(self):
        self.assertFalse(_is_stage_transition(
            self._row(sma20=1.0, sma50=2.0), set(), set(), self._snapshot()))

    def test_fails_when_price_below_50ma(self):
        self.assertFalse(_is_stage_transition(
            self._row(sma50=-1.0), set(), set(), self._snapshot()))

    def test_fails_when_200ma_far_overhead(self):
        # sma200 ≤ -15 = 200 SMA more than 15% overhead → too early
        self.assertFalse(_is_stage_transition(
            self._row(sma200=-16.0), set(), set(), self._snapshot()))

    def test_passes_when_200ma_within_reach(self):
        # sma200 = -10% (overhead, but reachable) — classic early Stage 2A reclaim
        self.assertTrue(_is_stage_transition(
            self._row(sma200=-10.0), set(), set(), self._snapshot()))

    def test_passes_at_200ma_boundary_minus_15(self):
        # Boundary: -14.9% (just inside) passes
        self.assertTrue(_is_stage_transition(
            self._row(sma200=-14.9), set(), set(), self._snapshot()))

    def test_passes_when_above_200ma(self):
        self.assertTrue(_is_stage_transition(
            self._row(sma200=2.0), set(), set(), self._snapshot()))

    def test_fails_when_atr_too_high(self):
        self.assertFalse(_is_stage_transition(
            self._row(atr=7.5), set(), set(), self._snapshot()))

    def test_fails_when_quality_below_70(self):
        self.assertFalse(_is_stage_transition(
            self._row(qs=69), set(), set(), self._snapshot()))

    def test_passes_at_quality_70_boundary(self):
        self.assertTrue(_is_stage_transition(
            self._row(qs=70), set(), set(), self._snapshot()))

    def test_fails_when_rvol_below_1(self):
        self.assertFalse(_is_stage_transition(
            self._row(rvol=0.9), set(), set(), self._snapshot()))

    def test_fails_when_no_etf_resolved(self):
        # No sector and no industry → no ETF → skip
        row = self._row(industry="", sector="")
        self.assertFalse(_is_stage_transition(row, set(), set(), self._snapshot()))

    def test_fails_when_sector_not_rotating_in(self):
        # rank_delta_5d > -5 → sector not actively rotating in
        snap = self._snapshot(rank_delta_5d=-3)
        self.assertFalse(_is_stage_transition(self._row(), set(), set(), snap))

    def test_passes_at_rank_delta_boundary(self):
        # rank_delta_5d == -5 should pass (≤ -5)
        snap = self._snapshot(rank_delta_5d=-5)
        self.assertTrue(_is_stage_transition(self._row(), set(), set(), snap))

    def test_fails_when_etf_missing_from_snapshot(self):
        # Industry maps to IGV but snapshot only has SMH → skip
        snap = {"SMH": {"rank_delta_5d": -10, "rs_score": 90, "rank": 1, "name": "SMH"}}
        self.assertFalse(_is_stage_transition(self._row(), set(), set(), snap))

    def test_semis_route_to_smh(self):
        # AMAT → semis industry → SMH; snapshot has SMH rotating in
        row = self._row(ticker="AMAT", industry="Semiconductor Equipment & Materials")
        snap = self._snapshot(etf="SMH", rank_delta_5d=-7)
        self.assertTrue(_is_stage_transition(row, set(), set(), snap))


# ----------------------------
# Tests: screener-table ticker extraction (2026-07-17 logo-cell regression)
# ----------------------------

class TestExtractTicker(unittest.TestCase):
    """Finviz added a logo element to the v=111 ticker cell whose fallback
    <span> holds the ticker's first letter — bare td.text doubles it
    ("AAPL" → "AAAPL") and every snapshot fetch 404s."""

    def _td(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").find("td")

    def test_logo_cell_text_is_doubled_but_extract_is_clean(self):
        td = self._td(
            '<td data-boxover-ticker="AAPL"><span>'
            '<a class="company-ticker" href="stock?t=AAPL">'
            '<img src="x.svg"><span>A</span></img></a>'
            '<a class="tab-link" href="stock?t=AAPL">AAPL</a></span></td>')
        from agents.utils.finviz_table import extract_ticker
        self.assertEqual(td.text.strip(), "AAAPL")  # the bug shape
        self.assertEqual(extract_ticker(td), "AAPL")

    def test_tab_link_fallback_when_attr_missing(self):
        td = self._td(
            '<td><span><a class="company-ticker"><span>N</span></a>'
            '<a class="tab-link">NVDA</a></span></td>')
        from agents.utils.finviz_table import extract_ticker
        self.assertEqual(extract_ticker(td), "NVDA")

    def test_legacy_plain_cell(self):
        from agents.utils.finviz_table import extract_ticker
        self.assertEqual(extract_ticker(self._td("<td>MSFT</td>")), "MSFT")

    @patch("agents.screener.finviz_agent.session")
    def test_fetch_all_tickers_clean_on_logo_cells(self, mock_session):
        from agents.screener.finviz_agent import fetch_all_tickers
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = make_mock_screener_html(["AAPL", "MSFT", "NVDA"])
        mock_session.get.return_value = mock_resp
        df, meta = fetch_all_tickers("http://fake-url", max_pages=1)
        self.assertEqual(sorted(df["Ticker"].tolist()), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(sorted(meta.keys()), ["AAPL", "MSFT", "NVDA"])


if __name__ == "__main__":
    unittest.main()
