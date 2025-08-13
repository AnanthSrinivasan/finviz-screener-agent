# ----------------------------
# Imports & Setup
# ----------------------------
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
import pandas as pd
import datetime
import time
import random

FINVIZ_BASE = "https://finviz.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

# ----------------------------
# Part 1: Screener Fetch & Save
# ----------------------------
screener_urls = {
    "10% Change": (
        f"{FINVIZ_BASE}/screener.ashx?v=151"
        f"&f=ind_stocksonly,sh_avgvol_o500,sh_price_o5,ta_changeopen_u10,"
        f"ta_sma20_sa50,ta_sma50_pa&ft=4&o=-relativevolume&"
        f"c=0,1,2,3,4,5,6,64,67,65,66"
    ),
    "Growth": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=an_recom_buybetter,fa_epsqoq_o20,fa_salesqoq_o20,"
        f"ind_stocksonly,sh_avgvol_o1000,sh_price_o10,ta_perf_4wup,"
        f"ta_perf2_13wup,ta_sma20_pa,ta_sma200_pa,ta_sma50_pa&ft=4"
    ),
    "IPO": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=cap_midover,ind_stocksonly,ipodate_prev3yrs,sh_avgvol_o1000,"
        f"sh_price_o10,ta_beta_o0.5,ta_sma20_pa&ft=4"
    ),
    "52 Week High": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=ind_stocksonly,sh_avgvol_o1000,sh_price_o10,ta_beta_o1,"
        f"ta_highlow52w_nh&ft=4"
    ),
    "Week 20%+ Gain": (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f=cap_smallover,ind_stocksonly,sh_avgvol_o1000,ta_perf_1w30o,"
        f"ta_sma20_pa,ta_volatility_wo4&ft=4&o=-marketcap"
    )
}

def fetch_all_tickers(screener_url: str, max_pages: int = 10) -> pd.DataFrame:
    combined = []
    seen = set()
    page = 1

    while page <= max_pages:
        resp = session.get(f"{screener_url}&r={1+(page-1)*20}", timeout=10)
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select('tr[valign="top"]')
        if not rows:
            break

        new_data = False
        for row in rows:
            cols = row.find_all('td')
            if len(cols) == 11:
                ticker = cols[1].text.strip()
                if ticker and ticker not in seen:
                    combined.append([c.text.strip() for c in cols])
                    seen.add(ticker)
                    new_data = True
        if not new_data:
            break
        page += 1
        time.sleep(1)

    columns = ['No.', 'Ticker', 'Company', 'Sector', 'Industry',
               'Country', 'Market Cap', 'P/E', 'Volume', 'Price', 'Change']
    return pd.DataFrame(combined, columns=columns) if combined else pd.DataFrame(columns=columns)

def aggregate_and_save(screener_map: dict) -> (pd.DataFrame, str, str):
    mapping = defaultdict(list)
    today = datetime.date.today().strftime("%Y-%m-%d")

    for name, url in screener_map.items():
        df = fetch_all_tickers(url)
        print(f"{name}: {len(df)} tickers found")
        for t in df['Ticker'].unique():
            mapping[t].append(name)

    if not mapping:
        mapping['TSLA'].append('Default Screener')

    data = []
    for t, screens in mapping.items():
        data.append({
            'Ticker': t,
            'Appearances': len(screens),
            'Screeners': ", ".join(screens)
        })
    summary_df = pd.DataFrame(data).sort_values(['Appearances','Ticker'], ascending=[False, True])

    csv_file = f"finviz_screeners_{today}.csv"
    html_file = f"finviz_screeners_{today}.html"
    summary_df.to_csv(csv_file, index=False)
    summary_df.to_html(html_file, index=False)

    return summary_df, csv_file, html_file

# ----------------------------
# Part 2: Snapshot Fetch with Retries
# ----------------------------
def get_snapshot_metrics(ticker: str, max_retries: int = 5):
    for attempt in range(max_retries):
        try:
            resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": ticker})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                raise ValueError("Snapshot table not found")

            data = {}
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                    key = key_cell.get_text(strip=True).rstrip('.')
                    data[key] = val_cell.get_text(strip=True)

            atr_pct = float(data.get("ATR (14)", 0)) / float(data.get("Price", "1").replace(',', '')) * 100
            eps_str = data.get("EPS Y/Y TTM", '0').replace('%','').strip()
            eps = float(eps_str) if eps_str != '-' else 0.0

            sales_str = data.get("Sales Y/Y TTM", '0').replace('%','').strip()
            sales = float(sales_str) if sales_str != '-' else 0.0
            return atr_pct, eps, sales
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = (2 ** attempt) + random.random()
                print(f"Rate limited fetching snapshot for {ticker}; retrying in {wait:.1f}s…")
                time.sleep(wait)
                continue
            else:
                print(f"HTTP error for {ticker}: {e}")
                break
        except Exception as e:
            print(f"Error fetching snapshot for {ticker}: {e}")
            break

    return None, None, None

# ----------------------------
# Part 3: Chart Gallery
# ----------------------------
def generate_finviz_gallery(tickers: list) -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    out_html = f"finviz_chart_grid_{today}.html"
    html = [
        '<html><head><title>Finviz Chart Gallery</title>',
        '<style>',
        'body { font-family: Arial; background: #f5f5f5; padding: 20px }',
        '.chart-grid { display: flex; flex-wrap: wrap; gap: 20px }',
        '.chart-item { width: 23%; background: white; border:1px solid #ccc;',
        'padding:10px; box-shadow:2px 2px 5px rgba(0,0,0,0.1); text-align:center }',
        '.chart-item img { max-width:100%; height:auto }',
        'h2 { text-align:center }',
        '</style></head><body><h2>Finviz Chart Gallery</h2>',
        '<div class="chart-grid">'
    ]
    for t in tickers:
        url = f"{FINVIZ_BASE}/chart.ashx"
        params = {"t": t, "ty": "c", "ta": 1, "p": "d", "s": "m"}
        req = requests.Request('GET', url, params=params).prepare()
        html.append(f'<div class="chart-item"><h4>{t}</h4>'
                    f'<img src="{req.url}" alt="{t}"></div>')
    html.append('</div></body></html>')

    with open(out_html, 'w') as f:
        f.write("\n".join(html))
    return out_html

# ----------------------------
# Part 4: Main Execution with Logging
# ----------------------------
if __name__ == "__main__":
    summary_df, csv_path, html_summary = aggregate_and_save(screener_urls)
    print(f"Summary CSV: {csv_path}\nSummary HTML: {html_summary}")

    print(f"Initial tickers count: {len(summary_df)}")

    # Fetch snapshot metrics
    summary_df[['ATR%', 'EPS Y/Y TTM', 'Sales Y/Y TTM']] = summary_df['Ticker'].apply(
        lambda t: pd.Series(get_snapshot_metrics(t))
    )

    filter_a = summary_df[summary_df['ATR%'] > 3.0]
    print(f"Tickers with ATR% > 3.0: {len(filter_a)}")

    gallery_path = generate_finviz_gallery(filter_a['Ticker'].tolist())
    print(f"Filtered chart gallery HTML: {gallery_path}")
