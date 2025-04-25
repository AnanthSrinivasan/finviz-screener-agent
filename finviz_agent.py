
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
import pandas as pd
import datetime
import time

# Screener URLs
screener_urls = {
    "10% Change": "https://finviz.com/screener.ashx?v=151&f=ind_stocksonly,sh_avgvol_o500,sh_price_o5,ta_changeopen_u10,ta_sma20_sa50,ta_sma50_pa&ft=4&o=-relativevolume&c=0,1,2,3,4,5,6,64,67,65,66",
    "Growth": "https://finviz.com/screener.ashx?v=111&f=an_recom_buybetter,fa_epsqoq_o20,fa_salesqoq_o20,ind_stocksonly,sh_avgvol_o1000,sh_price_o10,ta_perf_4wup,ta_perf2_13wup,ta_sma20_pa,ta_sma200_pa,ta_sma50_pa&ft=4",
    "IPO": "https://finviz.com/screener.ashx?v=211&f=ind_stocksonly,ipodate_prev2yrs,sh_avgvol_o1000,sh_price_o10,ta_sma20_pa&ft=4",
    "52 Week High": "https://finviz.com/screener.ashx?v=211&f=sh_avgvol_o1000,sh_price_o10,ta_beta_o1,ta_highlow52w_nh&ft=4",
    "Week 20%+ Gain": "https://finviz.com/screener.ashx?v=211&f=cap_smallover,sh_avgvol_o1000,sh_price_o3,ta_perf_1w20o,ta_volatility_wo4&ft=4&o=-marketcap&r=25"
}

def fetch_all_tickers_from_screener(screener_url, max_pages=10):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/91.0.4472.124 Safari/537.36"
    }
    session = requests.Session()
    session.headers.update(headers)

    combined_data = []
    seen_tickers = set()
    page = 1

    while page <= max_pages:
        paged_url = f"{screener_url}&r={1 + (page - 1) * 20}"
        try:
            response = session.get(paged_url, timeout=10)
        except requests.exceptions.Timeout:
            print(f"Timeout fetching page {page}. Skipping...")
            break
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break

        if response.status_code != 200:
            print(f"Failed to fetch page {page}. Status code: {response.status_code}")
            break

        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('tr[valign="top"]')

        if not rows:
            print(f"No rows found on page {page}. Ending pagination.")
            break

        page_new_data = False
        for row in rows:
            cells = row.find_all('td')
            if len(cells) == 11:
                ticker = cells[1].text.strip()
                if ticker and ticker not in seen_tickers:
                    combined_data.append([cell.text.strip() for cell in cells])
                    seen_tickers.add(ticker)
                    page_new_data = True

        if not page_new_data:
            print(f"No new tickers found on page {page}. Ending pagination.")
            break

        page += 1
        time.sleep(1)

    columns = [
        'No.', 'Ticker', 'Company', 'Sector', 'Industry',
        'Country', 'Market Cap', 'P/E', 'Volume', 'Price', 'Change'
    ]

    if combined_data:
        df = pd.DataFrame(combined_data, columns=columns)
    else:
        df = pd.DataFrame(columns=columns)

    return df

def aggregate_tickers(screener_urls):
    ticker_to_screeners = defaultdict(list)

    for name, url in screener_urls.items():
        df = fetch_all_tickers_from_screener(url)
        tickers = df['Ticker'].tolist()

        print(f"{name} fetched {len(tickers)} tickers.")

        for ticker in set(tickers):
            ticker_to_screeners[ticker].append(name)

    if not ticker_to_screeners:
        print("All screeners returned no results. Adding default ticker TSLA.")
        ticker_to_screeners['TSLA'].append('Default Screener')

    return ticker_to_screeners

def save_results(ticker_to_screeners):
    today = datetime.date.today().strftime("%Y-%m-%d")
    all_data = []

    for ticker, screeners in ticker_to_screeners.items():
        all_data.append({
            "Ticker": ticker,
            "Appearances": len(screeners),
            "Screeners": ", ".join(screeners)
        })

    df = pd.DataFrame(all_data)
    df.sort_values(by=["Appearances", "Ticker"], ascending=[False, True], inplace=True)

    filename = f"finviz_screeners_{today}.csv"
    df.to_csv(filename, index=False)
    print(f"
Results saved to {filename}")

def display_results(ticker_to_screeners):
    print("
=== All Tickers Grouped by Screener ===
")
    for ticker, screeners in ticker_to_screeners.items():
        print(f"{ticker}: from {', '.join(screeners)}")

    print("
=== Strong Overlap Candidates (Appearing in Multiple Screeners) ===
")
    for ticker, screeners in ticker_to_screeners.items():
        if len(screeners) > 1:
            print(f"{ticker}: Appears in {len(screeners)} screeners -> {', '.join(screeners)}")

if __name__ == "__main__":
    ticker_to_screeners = aggregate_tickers(screener_urls)
    display_results(ticker_to_screeners)
    save_results(ticker_to_screeners)
