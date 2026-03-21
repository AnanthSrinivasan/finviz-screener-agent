"""
Unit tests for finviz_agent.py and finviz_earnings_alert.py

Run locally: python -m pytest test_finviz_agent.py -v

These tests use mocks — no real HTTP calls are made.
"""

import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from finviz_agent import (
    aggregate_and_save,
    get_snapshot_metrics,
    generate_finviz_gallery,
    generate_ai_summary,
    send_slack_notification,
)
from finviz_earnings_alert import find_upcoming_earnings

# ----------------------------
# Helpers
# ----------------------------

def make_mock_screener_html(tickers: list) -> str:
    """Build a minimal Finviz screener HTML page with the given tickers."""
    rows = ""
    for i, t in enumerate(tickers):
        rows += f"""
        <tr valign="top">
            <td>{i+1}</td>
            <td>{t}</td>
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


def make_mock_snapshot_html(price="50.00", atr="2.50", eps="25.0", sales="15.0") -> bytes:
    """Build a minimal Finviz quote page snapshot table."""
    html = f"""
    <html><body>
    <table class="snapshot-table2">
        <tr><td>Price</td><td>{price}</td><td>ATR (14)</td><td>{atr}</td></tr>
        <tr><td>EPS Y/Y TTM</td><td>{eps}%</td><td>Sales Y/Y TTM</td><td>{sales}%</td></tr>
        <tr><td>52W High</td><td>55.00</td><td>Rel Volume</td><td>1.2</td></tr>
        <tr><td>Avg Volume</td><td>500K</td><td>SMA20</td><td>3.5%</td></tr>
        <tr><td>SMA50</td><td>2.1%</td><td>SMA200</td><td>1.0%</td></tr>
    </table>
    </body></html>"""
    return html.encode()


# ----------------------------
# Tests: fetch_all_tickers / aggregate_and_save
# ----------------------------

class TestAggregateAndSave(unittest.TestCase):

    @patch("finviz_agent.session")
    def test_basic_fetch_returns_dataframe(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = make_mock_screener_html(["AAPL", "MSFT", "NVDA"])
        mock_session.get.return_value = mock_resp

        with patch("finviz_agent.fetch_all_tickers") as mock_fetch:
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
            with patch("finviz_agent.pd.DataFrame.to_csv"), \
                 patch("finviz_agent.pd.DataFrame.to_html"), \
                 patch("finviz_agent.os.makedirs"):
                summary_df, _, _ = aggregate_and_save({"Growth": "http://fake-url"})

        self.assertFalse(summary_df.empty)
        self.assertIn("Ticker", summary_df.columns)
        self.assertIn("Sector", summary_df.columns)

    @patch("finviz_agent.fetch_all_tickers")
    def test_empty_screener_returns_empty_df(self, mock_fetch):
        mock_fetch.return_value = (
            pd.DataFrame(columns=["No.", "Ticker", "Company", "Sector", "Industry",
                                   "Country", "Market Cap", "P/E", "Volume", "Price", "Change"]),
            {}
        )
        with patch("finviz_agent.os.makedirs"):
            summary_df, csv, html = aggregate_and_save({"Growth": "http://fake-url"})
        self.assertTrue(summary_df.empty)

    @patch("finviz_agent.fetch_all_tickers")
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
        with patch("finviz_agent.pd.DataFrame.to_csv"), \
             patch("finviz_agent.pd.DataFrame.to_html"), \
             patch("finviz_agent.os.makedirs"):
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

    @patch("finviz_agent.make_session")
    def test_parses_metrics_correctly(self, mock_make_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = make_mock_snapshot_html(price="50.00", atr="2.50", eps="25.0", sales="15.0")
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        result = get_snapshot_metrics("AAPL")
        atr_pct, eps, sales, dist_high, rel_vol, avg_vol, sma20, sma50, sma200 = result

        self.assertAlmostEqual(atr_pct, 5.0, places=1)   # 2.50 / 50.00 * 100
        self.assertAlmostEqual(eps, 25.0, places=1)
        self.assertAlmostEqual(sales, 15.0, places=1)

    @patch("finviz_agent.make_session")
    def test_returns_none_on_missing_table(self, mock_make_session):
        mock_resp = MagicMock()
        mock_resp.content = b"<html><body>no table here</body></html>"
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        result = get_snapshot_metrics("FAKE")
        self.assertEqual(result, (None,) * 9)

    @patch("finviz_agent.make_session")
    def test_retries_on_429(self, mock_make_session):
        """Should retry on rate limit and eventually return None after exhausting retries."""
        import requests as req

        mock_resp = MagicMock()
        http_err = req.HTTPError(response=MagicMock(status_code=429))
        mock_resp.raise_for_status.side_effect = http_err

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_make_session.return_value = mock_session

        with patch("finviz_agent.time.sleep"):  # don't actually sleep in tests
            result = get_snapshot_metrics("AAPL", max_retries=2)

        self.assertEqual(result, (None,) * 9)


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
            "SMA200%": [1.0, 2.5],
            "Stage": [
                {"stage": 2, "badge": "🟢 Stage 2", "perfect": True, "sma20": 3.5, "sma50": 2.1, "sma200": 1.0},
                {"stage": 2, "badge": "🟢 Stage 2", "perfect": False, "sma20": 4.0, "sma50": 3.0, "sma200": 2.5},
            ],
            "VCP": [
                {"vcp_possible": False, "confidence": 0, "reason": "no signals"},
                {"vcp_possible": True, "confidence": 75, "reason": "tight range · volume dry-up"},
            ],
            "Quality Score": [55.0, 72.0],
        })

    def test_creates_html_file(self):
        filter_df = self._make_filter_df()
        with patch("finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            path = generate_finviz_gallery(["AAPL", "NVDA"], filter_df)
        self.assertIn("finviz_chart_grid_", path)
        self.assertTrue(path.endswith(".html"))

    def test_html_contains_sector_tag(self):
        filter_df = self._make_filter_df()
        written = []
        with patch("finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["AAPL"], filter_df)
        html_content = "".join(written)
        self.assertIn("sector-tag", html_content)
        self.assertIn("Technology", html_content)

    def test_html_contains_company_name(self):
        filter_df = self._make_filter_df()
        written = []
        with patch("finviz_agent.os.makedirs"), \
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
        with patch("finviz_agent.os.makedirs"), \
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
        with patch("finviz_agent.os.makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_file.return_value.__enter__.return_value.write.side_effect = written.append
            generate_finviz_gallery(["ENGY"], filter_df)
        html_content = "".join(written)
        self.assertNotIn("Lead Sector", html_content)


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
        with patch("finviz_agent.ANTHROPIC_API_KEY", ""):
            result = generate_ai_summary(self._make_filter_df(), "2026-01-01")
        self.assertEqual(result, "")

    @patch("finviz_agent.ANTHROPIC_API_KEY", "sk-ant-fake")
    @patch("finviz_agent.requests.post")
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

    @patch("finviz_agent.ANTHROPIC_API_KEY", "sk-ant-fake")
    @patch("finviz_agent.requests.post")
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
        with patch("finviz_agent.SLACK_WEBHOOK_URL", ""):
            with patch("finviz_agent.requests.post") as mock_post:
                summary_df, filter_df = self._make_dfs()
                send_slack_notification(summary_df, filter_df, "data/gallery.html", "2026-01-01", "")
                mock_post.assert_not_called()

    @patch("finviz_agent.SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    @patch("finviz_agent.requests.post")
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

    @patch("finviz_agent.SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    @patch("finviz_agent.GITHUB_PAGES_BASE", "https://user.github.io/repo")
    @patch("finviz_agent.requests.post")
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

    @patch("finviz_earnings_alert.fetch_earnings_date")
    def test_skips_low_quality_score(self, mock_fetch):
        """Tickers with Quality Score <= 50 should be skipped without calling Finviz."""
        tickers = self._make_tickers(quality=40.0, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("finviz_earnings_alert.fetch_earnings_date")
    def test_skips_missing_sector(self, mock_fetch):
        """Tickers with no sector should be skipped without calling Finviz."""
        tickers = self._make_tickers(quality=70.0, sector="")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("finviz_earnings_alert.fetch_earnings_date")
    def test_skips_none_quality(self, mock_fetch):
        """Tickers with None quality should be skipped."""
        tickers = self._make_tickers(quality=None, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])

    @patch("finviz_earnings_alert.fetch_earnings_date")
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

    @patch("finviz_earnings_alert.fetch_earnings_date")
    def test_boundary_quality_score_excluded(self, mock_fetch):
        """Quality Score exactly 50 should be excluded (strictly greater than required)."""
        tickers = self._make_tickers(quality=50.0, sector="Technology")
        result = find_upcoming_earnings(tickers)
        mock_fetch.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
