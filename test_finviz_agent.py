
import unittest
from finviz_agent import fetch_all_tickers_from_screener

class TestFinvizAgent(unittest.TestCase):
    def test_fetch_tickers(self):
        # Lightweight test URL
        test_url = "https://finviz.com/screener.ashx?v=111&f=ind_stocksonly,sh_price_o5&ft=4"
        df = fetch_all_tickers_from_screener(test_url, max_pages=1)
        self.assertTrue(len(df) >= 1, "No tickers fetched from Finviz!")

if __name__ == "__main__":
    unittest.main()
