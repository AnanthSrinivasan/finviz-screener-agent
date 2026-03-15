# ----------------------------
# Imports & Setup
# ----------------------------
import requests
from bs4 import BeautifulSoup
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import datetime
import time
import random
import os
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

FINVIZ_BASE = "https://finviz.com"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")   # e.g. https://youruser.github.io/your-repo
ATR_THRESHOLD = float(os.environ.get("ATR_THRESHOLD", "3.0"))
SNAPSHOT_WORKERS = int(os.environ.get("SNAPSHOT_WORKERS", "6"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s

session = make_session()

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
            log.warning(f"HTTP {resp.status_code} on page {page}, stopping.")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select('tr[valign="top"]')
        if not rows:
            break

        new_data = False
        for row in rows:
            cols = row.find_all('td')
            if len(cols) != 11:
                if cols:
                    log.debug(f"Skipping row with unexpected col count: {len(cols)}")
                continue
            ticker = cols[1].text.strip()
            if ticker and ticker not in seen:
                combined.append([c.text.strip() for c in cols])
                seen.add(ticker)
                new_data = True
        if not new_data:
            break
        page += 1
        time.sleep(1 + random.uniform(0, 0.5))

    columns = ['No.', 'Ticker', 'Company', 'Sector', 'Industry',
               'Country', 'Market Cap', 'P/E', 'Volume', 'Price', 'Change']
    return pd.DataFrame(combined, columns=columns) if combined else pd.DataFrame(columns=columns)


def aggregate_and_save(screener_map: dict) -> tuple:
    mapping = defaultdict(list)
    today = datetime.date.today().strftime("%Y-%m-%d")

    for name, url in screener_map.items():
        df = fetch_all_tickers(url)
        log.info(f"{name}: {len(df)} tickers found")
        for t in df['Ticker'].unique():
            mapping[t].append(name)

    if not mapping:
        log.warning("No tickers found across all screeners — check Finviz connectivity.")
        return pd.DataFrame(columns=['Ticker', 'Appearances', 'Screeners']), "", ""

    data = [
        {'Ticker': t, 'Appearances': len(screens), 'Screeners': ", ".join(screens)}
        for t, screens in mapping.items()
    ]
    summary_df = pd.DataFrame(data).sort_values(['Appearances', 'Ticker'], ascending=[False, True])

    os.makedirs("data", exist_ok=True)
    csv_file = f"data/finviz_screeners_{today}.csv"
    html_file = f"data/finviz_screeners_{today}.html"
    summary_df.to_csv(csv_file, index=False)
    summary_df.to_html(html_file, index=False)

    return summary_df, csv_file, html_file


# ----------------------------
# Part 2: Concurrent Snapshot Fetch
# ----------------------------
def get_snapshot_metrics(ticker: str, max_retries: int = 5):
    # Each call gets its own session — safe to call from multiple threads
    thread_session = make_session()
    for attempt in range(max_retries):
        try:
            resp = thread_session.get(
                f"{FINVIZ_BASE}/quote.ashx",
                params={"t": ticker},
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                log.warning(f"{ticker}: snapshot table not found (layout may have changed)")
                return None, None, None

            data = {}
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                    key = key_cell.get_text(strip=True).rstrip('.')
                    data[key] = val_cell.get_text(strip=True)

            price_raw = data.get("Price", "1").replace(',', '')
            price = float(price_raw) if price_raw else 1.0
            atr_pct = float(data.get("ATR (14)", 0)) / price * 100

            eps_str = data.get("EPS Y/Y TTM", '0').replace('%', '').strip()
            eps = float(eps_str) if eps_str not in ('-', '') else 0.0

            sales_str = data.get("Sales Y/Y TTM", '0').replace('%', '').strip()
            sales = float(sales_str) if sales_str not in ('-', '') else 0.0

            return atr_pct, eps, sales

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = (2 ** attempt) + random.random()
                log.warning(f"{ticker}: rate limited, retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                log.error(f"{ticker}: HTTP error {e.response.status_code}")
                break
        except Exception as e:
            log.error(f"{ticker}: unexpected error — {e}")
            break

    return None, None, None


def fetch_snapshots_concurrent(tickers: list, workers: int = SNAPSHOT_WORKERS) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(get_snapshot_metrics, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            results[ticker] = future.result()
    return results


# ----------------------------
# Part 3: Chart Gallery (self-contained HTML)
# ----------------------------
def generate_finviz_gallery(tickers: list, filter_df: pd.DataFrame) -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs("data", exist_ok=True)
    out_html = f"data/finviz_chart_grid_{today}.html"

    chart_items = []
    for t in tickers:
        row = filter_df[filter_df['Ticker'] == t].iloc[0] if t in filter_df['Ticker'].values else None
        chart_url = f"{FINVIZ_BASE}/chart.ashx?t={t}&ty=c&ta=1&p=d&s=m"
        atr = f"{row['ATR%']:.1f}%" if row is not None and pd.notna(row.get('ATR%')) else "—"
        eps = f"{row['EPS Y/Y TTM']:.1f}%" if row is not None and pd.notna(row.get('EPS Y/Y TTM')) else "—"
        apps = row['Appearances'] if row is not None else "—"
        screeners = row['Screeners'] if row is not None else "—"
        chart_items.append(f"""
        <div class="chart-item">
          <div class="chart-header">
            <span class="ticker">{t}</span>
            <span class="badge">{apps} screen{'s' if apps != 1 else ''}</span>
          </div>
          <img src="{chart_url}" alt="{t}" loading="lazy">
          <div class="meta">
            <span title="ATR%">ATR {atr}</span>
            <span title="EPS Y/Y TTM">EPS {eps}</span>
          </div>
          <div class="screeners">{screeners}</div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Finviz Chart Gallery — {today}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f1117; color: #e2e8f0; padding: 24px; }}
  h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 6px; }}
  .subtitle {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 24px; }}
  .chart-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
  .chart-item {{ background: #1e2130; border: 1px solid #2d3148; border-radius: 10px;
                padding: 12px; transition: border-color .2s; }}
  .chart-item:hover {{ border-color: #4f6ef7; }}
  .chart-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
  .ticker {{ font-size: 1rem; font-weight: 700; color: #e2e8f0; }}
  .badge {{ background: #2d3f6e; color: #7aa2f7; font-size: 0.72rem; font-weight: 600;
            padding: 2px 8px; border-radius: 20px; }}
  .chart-item img {{ width: 100%; border-radius: 6px; display: block; }}
  .meta {{ display: flex; gap: 12px; margin-top: 10px; font-size: 0.78rem; color: #94a3b8; }}
  .meta span {{ background: #161b27; padding: 3px 8px; border-radius: 4px; }}
  .screeners {{ margin-top: 6px; font-size: 0.72rem; color: #64748b; line-height: 1.4; }}
</style>
</head>
<body>
  <h1>Finviz Chart Gallery</h1>
  <p class="subtitle">{today} · {len(tickers)} tickers · ATR% &gt; {ATR_THRESHOLD}</p>
  <div class="chart-grid">{''.join(chart_items)}</div>
</body>
</html>"""

    with open(out_html, 'w') as f:
        f.write(html)
    return out_html


# ----------------------------
# Part 4: Slack Notification
# ----------------------------
def send_slack_notification(summary_df: pd.DataFrame, filter_df: pd.DataFrame,
                             gallery_html: str, today: str):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack notification.")
        return

    top = filter_df.head(10)
    ticker_lines = []
    for _, row in top.iterrows():
        atr = f"{row['ATR%']:.1f}%" if pd.notna(row.get('ATR%')) else "—"
        eps = f"{row['EPS Y/Y TTM']:.1f}%" if pd.notna(row.get('EPS Y/Y TTM')) else "—"
        ticker_lines.append(
            f"*{row['Ticker']}* · {row['Appearances']} screens · ATR {atr} · EPS {eps}\n"
            f"  _{row['Screeners']}_"
        )

    gallery_link = ""
    if GITHUB_PAGES_BASE:
        fname = os.path.basename(gallery_html)
        gallery_link = f"\n\n:bar_chart: <{GITHUB_PAGES_BASE}/data/{fname}|Open chart gallery>"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📈 Finviz Daily Screener — {today}"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{len(summary_df)}* total tickers · "
                        f"*{len(filter_df)}* passed ATR% > {ATR_THRESHOLD}\n\n"
                        f"*Top picks:*\n" + "\n".join(ticker_lines) +
                        gallery_link
                    )
                }
            },
            {"type": "divider"}
        ]
    }

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent.")
    except Exception as e:
        log.error(f"Failed to send Slack notification: {e}")


# ----------------------------
# Part 5: Main Execution
# ----------------------------
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    log.info(f"=== Finviz agent starting — {today} ===")

    # Step 1: Screener fetch & aggregate
    summary_df, csv_path, html_summary = aggregate_and_save(screener_urls)
    log.info(f"Total unique tickers: {len(summary_df)}")

    if summary_df.empty:
        log.error("No tickers — aborting.")
        exit(1)

    # Step 2: Concurrent snapshot metrics
    log.info(f"Fetching snapshots with {SNAPSHOT_WORKERS} workers...")
    snapshot_results = fetch_snapshots_concurrent(summary_df['Ticker'].tolist())

    summary_df['ATR%']         = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*(3))[0])
    summary_df['EPS Y/Y TTM']  = summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*(3))[1])
    summary_df['Sales Y/Y TTM']= summary_df['Ticker'].map(lambda t: snapshot_results.get(t, (None,)*(3))[2])

    # Step 3: Filter
    filter_df = summary_df[summary_df['ATR%'] > ATR_THRESHOLD].copy()
    log.info(f"Tickers with ATR% > {ATR_THRESHOLD}: {len(filter_df)}")

    # Step 4: Chart gallery
    gallery_path = generate_finviz_gallery(filter_df['Ticker'].tolist(), filter_df)
    log.info(f"Chart gallery: {gallery_path}")

    # Step 5: Slack push
    send_slack_notification(summary_df, filter_df, gallery_path, today)

    log.info("=== Done ===")
