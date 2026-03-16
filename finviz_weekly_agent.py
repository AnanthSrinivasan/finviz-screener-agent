# ----------------------------
# Finviz Weekly Review Agent
# ----------------------------
# Reads the last 5 daily CSV files produced by finviz_agent.py,
# scores tickers by persistence and conviction, calls Claude for
# a deep weekly brief, and pushes it to Slack.
# ----------------------------

import os
import glob
import logging
import random
import datetime
import requests
import pandas as pd
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_URL  = "https://api.anthropic.com/v1/messages"
SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_PAGES_BASE  = os.environ.get("GITHUB_PAGES_BASE", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ATR_THRESHOLD      = float(os.environ.get("ATR_THRESHOLD", "3.0"))
DATA_DIR           = os.environ.get("DATA_DIR", "data")

CNN_FNG_URL      = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
COINGECKO_URL    = "https://api.coingecko.com/api/v3"

# Crypto assets: CoinGecko ID -> display name
CRYPTO_WATCHLIST = {
    "bitcoin":  "Bitcoin (BTC)",
    "ethereum": "Ethereum (ETH)",
    "coinbase-exchange": "Coinbase (COIN)",  # COIN stock via CoinGecko
}

# ETF proxies for crypto via Finviz (treated as normal macro tickers)
CRYPTO_ETFS = ["IBIT", "FBTC", "COIN", "MSTR"]

# Key indices and sector ETFs for macro context
MACRO_WATCHLIST = {
    # Indices
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000",
    "DIA": "Dow Jones",
    # Commodities
    "SLV": "Silver",
    "GLD": "Gold",
    "GDX": "Gold Miners",
    "USO": "Oil",
    # Sectors
    "XLK": "Technology",
    "SMH": "Semiconductors",
    "XLE": "Energy",
    "XLF": "Financials",
    "XBI": "Biotech",
    # Macro
    "TLT": "20yr Treasuries",
    "UUP": "US Dollar",
    "VIX": "Volatility Index",
    # Crypto proxies (equity/ETF)
    "IBIT": "iShares Bitcoin ETF",
    "MSTR": "MicroStrategy",
    "COIN": "Coinbase",
}

FINVIZ_BASE = "https://finviz.com"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return s


# ----------------------------
# Part 1: Load & Aggregate Weekly CSV Data
# ----------------------------
def load_weekly_data(data_dir: str, lookback_days: int = 7) -> tuple:
    """
    Load all daily CSVs from the last `lookback_days` days.
    Returns (combined_df, daily_dfs dict, dates_found list).
    """
    today = datetime.date.today()
    dates_found = []
    daily_dfs = {}

    for i in range(lookback_days):
        date = today - datetime.timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        path = os.path.join(data_dir, f"finviz_screeners_{date_str}.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                df['date'] = date_str
                daily_dfs[date_str] = df
                dates_found.append(date_str)
                log.info(f"Loaded {path} — {len(df)} tickers")
            except Exception as e:
                log.warning(f"Could not load {path}: {e}")

    if not daily_dfs:
        log.error("No daily CSV files found for the past week.")
        return pd.DataFrame(), {}, []

    combined_df = pd.concat(daily_dfs.values(), ignore_index=True)
    return combined_df, daily_dfs, sorted(dates_found)


def build_persistence_scores(combined_df: pd.DataFrame, dates_found: list) -> pd.DataFrame:
    """
    Score each ticker by how many days it appeared this week.
    Also track best ATR%, best EPS, which screeners it hit, and sectors.
    """
    if combined_df.empty:
        return pd.DataFrame()

    records = defaultdict(lambda: {
        'days_seen': 0,
        'dates': [],
        'max_atr': None,
        'max_eps': None,
        'max_appearances': 0,
        'screeners_hit': set(),
        'sector': '',
        'industry': '',
        'company': '',
        'market_cap': '',
    })

    for _, row in combined_df.iterrows():
        t = row['Ticker']
        r = records[t]
        r['days_seen'] += 1
        r['dates'].append(row.get('date', ''))
        r['max_appearances'] = max(r['max_appearances'], row.get('Appearances', 1))

        atr = row.get('ATR%')
        if pd.notna(atr):
            r['max_atr'] = max(r['max_atr'] or 0, float(atr))

        eps = row.get('EPS Y/Y TTM')
        if pd.notna(eps):
            r['max_eps'] = max(r['max_eps'] or -9999, float(eps))

        screeners = row.get('Screeners', '')
        if screeners:
            for s in str(screeners).split(','):
                r['screeners_hit'].add(s.strip())

        if not r['sector'] and pd.notna(row.get('Sector', '')):
            r['sector'] = row.get('Sector', '')
        if not r['industry'] and pd.notna(row.get('Industry', '')):
            r['industry'] = row.get('Industry', '')
        if not r['company'] and pd.notna(row.get('Company', '')):
            r['company'] = row.get('Company', '')
        if not r['market_cap'] and pd.notna(row.get('Market Cap', '')):
            r['market_cap'] = row.get('Market Cap', '')

    rows = []
    total_days = len(dates_found)
    for ticker, r in records.items():
        # Conviction score: days_seen * screener diversity * multi-screener bonus
        screener_diversity = len(r['screeners_hit'])
        conviction = (r['days_seen'] / max(total_days, 1)) * 100
        conviction += screener_diversity * 10
        if r['max_appearances'] >= 2:
            conviction += 20   # bonus for appearing in 2+ screeners same day

        rows.append({
            'Ticker':        ticker,
            'Company':       r['company'],
            'Sector':        r['sector'],
            'Industry':      r['industry'],
            'Market Cap':    r['market_cap'],
            'Days Seen':     r['days_seen'],
            'Total Days':    total_days,
            'Dates':         ', '.join(sorted(set(r['dates']))),
            'Max ATR%':      round(r['max_atr'], 1) if r['max_atr'] is not None else None,
            'Max EPS%':      round(r['max_eps'], 1) if r['max_eps'] is not None else None,
            'Max Appearances': r['max_appearances'],
            'Screeners Hit': ', '.join(sorted(r['screeners_hit'])),
            'Conviction':    round(conviction, 1),
        })

    df = pd.DataFrame(rows).sort_values('Conviction', ascending=False)
    return df


# ----------------------------
# Part 2: Macro Snapshot
# ----------------------------
def fetch_macro_snapshot() -> dict:
    """Fetch price/change for key macro ETFs from Finviz."""
    session = make_session()
    macro_data = {}

    for symbol, name in MACRO_WATCHLIST.items():
        try:
            resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": symbol}, timeout=10)
            if not resp.ok:
                continue
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                continue
            data = {}
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                    data[key_cell.get_text(strip=True).rstrip('.')] = val_cell.get_text(strip=True)

            macro_data[symbol] = {
                'name':   name,
                'price':  data.get('Price', 'n/a'),
                'change': data.get('Change', 'n/a'),
                'perf_week': data.get('Perf Week', 'n/a'),
                'perf_month': data.get('Perf Month', 'n/a'),
            }
            log.info(f"Macro: {symbol} {data.get('Price')} {data.get('Change')}")
        except Exception as e:
            log.warning(f"Macro fetch failed for {symbol}: {e}")

    return macro_data


# ----------------------------
# Part 2a: Crypto Prices via CoinGecko
# ----------------------------
def fetch_crypto_data() -> dict:
    """
    Fetch BTC and ETH price, 24h change, 7d change, market cap from CoinGecko.
    No API key needed for public endpoints.
    """
    session = make_session()
    try:
        ids = ",".join(CRYPTO_WATCHLIST.keys() - {"coinbase-exchange"})  # BTC + ETH
        resp = session.get(
            f"{COINGECKO_URL}/simple/price",
            params={
                "ids": "bitcoin,ethereum",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_7d_change": "true",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"CoinGecko fetch failed: {resp.status_code}")
            return {}

        raw = resp.json()
        result = {}
        name_map = {"bitcoin": "Bitcoin (BTC)", "ethereum": "Ethereum (ETH)"}

        for coin_id, name in name_map.items():
            d = raw.get(coin_id, {})
            price = d.get("usd", 0)
            chg_24h = d.get("usd_24h_change", 0) or 0
            chg_7d  = d.get("usd_7d_change", 0) or 0
            mcap    = d.get("usd_market_cap", 0) or 0
            vol     = d.get("usd_24h_vol", 0) or 0

            result[coin_id] = {
                "name":    name,
                "price":   f"${price:,.0f}" if price > 100 else f"${price:,.2f}",
                "chg_24h": f"{chg_24h:+.1f}%",
                "chg_7d":  f"{chg_7d:+.1f}%",
                "mcap":    f"${mcap/1e9:.0f}B" if mcap > 1e9 else f"${mcap/1e6:.0f}M",
                "vol_24h": f"${vol/1e9:.1f}B" if vol > 1e9 else f"${vol/1e6:.0f}M",
                "raw_chg_24h": chg_24h,
                "raw_chg_7d":  chg_7d,
                "raw_price":   price,
            }
            log.info(f"Crypto: {name} {result[coin_id]['price']} 7d:{result[coin_id]['chg_7d']}")

        return result
    except Exception as e:
        log.error(f"Crypto data fetch failed: {e}")
        return {}


# ----------------------------
# Part 2b: CNN Fear & Greed
# ----------------------------
def fetch_fear_and_greed() -> dict:
    """Fetch CNN Fear & Greed index — current value + historical context."""
    try:
        session = make_session()
        resp = session.get(CNN_FNG_URL, timeout=10)
        if not resp.ok:
            log.warning(f"Fear & Greed fetch failed: {resp.status_code}")
            return {}
        data = resp.json()
        fg = data.get("fear_and_greed", {})

        # Extract last 30 days trend from historical data
        historical = data.get("fear_and_greed_historical", {}).get("data", [])
        recent_30 = historical[-30:] if len(historical) >= 30 else historical
        if recent_30:
            scores = [d["y"] for d in recent_30]
            trend_low  = round(min(scores), 1)
            trend_high = round(max(scores), 1)
            trend_avg  = round(sum(scores) / len(scores), 1)
        else:
            trend_low = trend_high = trend_avg = None

        result = {
            "score":          round(fg.get("score", 0), 1),
            "rating":         fg.get("rating", "unknown").title(),
            "prev_close":     round(fg.get("previous_close", 0), 1),
            "prev_1_week":    round(fg.get("previous_1_week", 0), 1),
            "prev_1_month":   round(fg.get("previous_1_month", 0), 1),
            "prev_1_year":    round(fg.get("previous_1_year", 0), 1),
            "trend_30d_low":  trend_low,
            "trend_30d_high": trend_high,
            "trend_30d_avg":  trend_avg,
        }
        log.info(f"Fear & Greed: {result['score']} ({result['rating']})")
        return result
    except Exception as e:
        log.error(f"Fear & Greed fetch failed: {e}")
        return {}


def _fng_emoji(score: float) -> str:
    if score <= 25:   return "🔴"
    elif score <= 45: return "🟠"
    elif score <= 55: return "🟡"
    elif score <= 75: return "🟢"
    else:             return "💚"


def _fng_context(score: float, prev_month: float) -> str:
    """Plain English context — no hype, no fear mongering."""
    change = score - prev_month
    direction = "up" if change > 0 else "down"
    magnitude = abs(round(change, 1))

    if score <= 25:
        base = "Market is in Extreme Fear"
        context = "historically a better buying zone — but confirm with breadth before acting"
    elif score <= 45:
        base = "Market is in Fear"
        context = "caution warranted, momentum stocks face headwinds"
    elif score <= 55:
        base = "Market is Neutral"
        context = "no strong directional bias, stock-picking matters more than macro"
    elif score <= 75:
        base = "Market is in Greed"
        context = "momentum favourable, but watch for overextension"
    else:
        base = "Market is in Extreme Greed"
        context = "late-stage momentum — tighten stops, don't chase extended moves"

    return f"{base} ({score}). {magnitude}pt {direction} vs last month. {context}."


# ----------------------------
# Part 3: Weekly HTML Report
# ----------------------------
def generate_weekly_html(persistence_df: pd.DataFrame, macro_data: dict,
                          dates_found: list, ai_brief: str,
                          fng_data: dict = None, crypto_data: dict = None) -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    out_html = os.path.join(DATA_DIR, f"finviz_weekly_{today}.html")

    week_range = f"{dates_found[0]} → {dates_found[-1]}" if dates_found else today

    # Top persistent tickers table
    top = persistence_df.head(30)
    ticker_rows = ""
    for _, row in top.iterrows():
        days = row['Days Seen']
        total = row['Total Days']
        pct = int((days / total) * 100) if total > 0 else 0
        bar_color = "#4f6ef7" if pct >= 80 else "#38bdf8" if pct >= 60 else "#64748b"
        atr = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get('Max ATR%')) else "—"
        eps = f"{row['Max EPS%']:.1f}%" if pd.notna(row.get('Max EPS%')) else "—"
        apps = f"×{row['Max Appearances']}" if row['Max Appearances'] >= 2 else ""
        chart_url = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"  # weekly chart

        ticker_rows += f"""
        <tr>
          <td><a href="{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}" target="_blank" class="ticker-link">{row['Ticker']}</a></td>
          <td class="company">{row['Company']}</td>
          <td><span class="sector-pill">{row['Sector']}</span></td>
          <td>
            <div class="bar-wrap">
              <div class="bar" style="width:{pct}%;background:{bar_color}"></div>
              <span>{days}/{total}d</span>
            </div>
          </td>
          <td class="center">{row['Conviction']}</td>
          <td class="center">{atr}</td>
          <td class="center">{eps}</td>
          <td class="center bold">{apps}</td>
          <td class="screeners">{row['Screeners Hit']}</td>
          <td><a href="{chart_url}" target="_blank" class="chart-link">chart →</a></td>
        </tr>"""

    # Macro table
    macro_rows = ""
    for symbol, m in macro_data.items():
        change = m['change']
        change_class = "pos" if change.startswith('+') else "neg" if change.startswith('-') else ""
        macro_rows += f"""
        <tr>
          <td class="bold">{symbol}</td>
          <td>{m['name']}</td>
          <td>{m['price']}</td>
          <td class="{change_class}">{change}</td>
          <td class="{change_class}">{m['perf_week']}</td>
          <td class="{change_class}">{m['perf_month']}</td>
        </tr>"""

    # AI brief section
    ai_section = ""
    if ai_brief:
        # Split into paragraphs for readability
        paragraphs = [p.strip() for p in ai_brief.split('\n') if p.strip()]
        ai_section = "".join(f"<p>{p}</p>" for p in paragraphs)

    # Build crypto cards
    crypto_section = ""
    if crypto_data:
        cards = ""
        for coin_id, d in crypto_data.items():
            c24 = "pos" if d["raw_chg_24h"] >= 0 else "neg"
            c7d = "pos" if d["raw_chg_7d"]  >= 0 else "neg"
            cards += f"""
            <div class="crypto-card">
              <div class="crypto-name">{d['name']}</div>
              <div class="crypto-price">{d['price']}</div>
              <div class="crypto-changes">
                <span class="{c24}">24h {d['chg_24h']}</span>
                <span class="{c7d}">7d {d['chg_7d']}</span>
              </div>
              <div class="crypto-mcap">MCap {d['mcap']} · Vol {d['vol_24h']}</div>
            </div>"""
        crypto_section = cards

    # Build Fear & Greed bar
    fng_bar = ""
    if fng_data:
        emoji = _fng_emoji(fng_data["score"])
        ctx   = _fng_context(fng_data["score"], fng_data["prev_1_month"])
        fng_bar = (
            f'<div><div class="fng-label">Fear &amp; Greed</div>' 
            f'<div class="fng-score">{emoji} {fng_data["score"]}</div>' 
            f'<div style="font-size:.8rem;color:#64748b">{fng_data["rating"]}</div></div>' 
            f'<div class="fng-context">{ctx}</div>' 
            f'<div class="fng-hist">' 
            f'<span>Prev close <strong>{fng_data["prev_close"]}</strong></span>' 
            f'<span>1 week ago <strong>{fng_data["prev_1_week"]}</strong></span>' 
            f'<span>1 month ago <strong>{fng_data["prev_1_month"]}</strong></span>' 
            f'<span>1 year ago <strong>{fng_data["prev_1_year"]}</strong></span>' 
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Finviz Weekly Review — {today}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f1117; color: #e2e8f0; padding: 32px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1rem; font-weight: 600; color: #94a3b8; margin: 28px 0 12px; text-transform: uppercase; letter-spacing: .05em; }}
  .subtitle {{ color: #64748b; font-size: 0.85rem; margin-bottom: 32px; }}
  .ai-brief {{ background: #1a1f35; border-left: 3px solid #4f6ef7; border-radius: 0 8px 8px 0;
               padding: 16px 20px; margin-bottom: 32px; }}
  .ai-brief p {{ line-height: 1.7; color: #cbd5e1; font-size: 0.92rem; margin-bottom: 10px; }}
  .ai-brief p:last-child {{ margin-bottom: 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ text-align: left; padding: 8px 12px; color: #64748b; font-weight: 500;
        border-bottom: 1px solid #1e2130; text-transform: uppercase; font-size: 0.72rem; letter-spacing: .05em; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #1a1f2e; vertical-align: middle; }}
  tr:hover td {{ background: #1a1f2e; }}
  .ticker-link {{ color: #7aa2f7; font-weight: 700; text-decoration: none; }}
  .ticker-link:hover {{ color: #a5b4fc; }}
  .chart-link {{ color: #38bdf8; font-size: 0.75rem; text-decoration: none; }}
  .company {{ color: #94a3b8; font-size: 0.78rem; }}
  .sector-pill {{ background: #0c2240; color: #38bdf8; font-size: 0.68rem;
                  padding: 2px 6px; border-radius: 4px; white-space: nowrap; }}
  .screeners {{ color: #64748b; font-size: 0.72rem; }}
  .bar-wrap {{ display: flex; align-items: center; gap: 8px; }}
  .bar-wrap span {{ font-size: 0.75rem; color: #94a3b8; white-space: nowrap; }}
  .bar {{ height: 6px; border-radius: 3px; min-width: 2px; }}
  .center {{ text-align: center; }}
  .bold {{ font-weight: 700; }}
  .pos {{ color: #4ade80; }}
  .neg {{ color: #f87171; }}
  .macro-table td {{ color: #cbd5e1; }}
</style>
</head>
<body>
  <h1>Finviz Weekly Review</h1>
  <p class="subtitle">{week_range} · {len(persistence_df)} unique tickers · {len(dates_found)} trading days scanned</p>

  {"<h2>Crypto Snapshot</h2><div class=\'crypto-bar\'>" + crypto_section + "</div>" if crypto_section else ""}
  {"<div class=\'fng-bar\'>" + fng_bar + "</div>" if fng_bar else ""}
  {"<h2>Weekly Intelligence Brief</h2><div class=\'ai-brief\'>" + ai_section + "</div>" if ai_brief else ""}

  <h2>Persistence Leaderboard — highest conviction setups</h2>
  <table>
    <thead>
      <tr>
        <th>Ticker</th><th>Company</th><th>Sector</th><th>Persistence</th>
        <th>Score</th><th>Max ATR%</th><th>Max EPS%</th><th>Multi</th>
        <th>Screeners</th><th>Chart</th>
      </tr>
    </thead>
    <tbody>{ticker_rows}</tbody>
  </table>

  {"<h2>Macro Snapshot</h2><table class='macro-table'><thead><tr><th>Symbol</th><th>Name</th><th>Price</th><th>Change</th><th>Week</th><th>Month</th></tr></thead><tbody>" + macro_rows + "</tbody></table>" if macro_rows else ""}

</body>
</html>"""

    with open(out_html, 'w') as f:
        f.write(html)
    return out_html


# ----------------------------
# Part 4: AI Weekly Brief
# ----------------------------
def generate_weekly_ai_brief(persistence_df: pd.DataFrame, macro_data: dict,
                              dates_found: list, fng_data: dict = None,
                              crypto_data: dict = None) -> str:
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping AI brief.")
        return ""

    # Build persistence summary for prompt
    top = persistence_df.head(20)
    ticker_lines = []
    for _, row in top.iterrows():
        atr  = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get('Max ATR%')) else "n/a"
        eps  = f"{row['Max EPS%']:.1f}%" if pd.notna(row.get('Max EPS%')) else "n/a"
        ticker_lines.append(
            f"{row['Ticker']} ({row['Sector']} / {row['Industry']}) "
            f"| seen {row['Days Seen']}/{row['Total Days']} days "
            f"| conviction {row['Conviction']} "
            f"| max ATR {atr} | max EPS {eps} "
            f"| screeners: {row['Screeners Hit']}"
            + (f" | MULTI-SCREENER x{row['Max Appearances']}" if row['Max Appearances'] >= 2 else "")
        )

    # Build macro summary for prompt
    macro_lines = []
    for symbol, m in macro_data.items():
        macro_lines.append(
            f"{symbol} ({m['name']}): price {m['price']} | "
            f"week {m['perf_week']} | month {m['perf_month']}"
        )

    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"

    # Build F&G context for prompt
    fng_context = ""
    if fng_data:
        fng_context = (
            f"\n## CNN FEAR & GREED INDEX:\n"
            f"Current: {fng_data['score']} ({fng_data['rating']}) | "
            f"1 week ago: {fng_data['prev_1_week']} | "
            f"1 month ago: {fng_data['prev_1_month']} | "
            f"30d range: {fng_data['trend_30d_low']}–{fng_data['trend_30d_high']} | "
            f"30d avg: {fng_data['trend_30d_avg']}\n"
            f"Interpret this as market context for your brief — extreme fear zones have historically "
            f"been better entry points for quality momentum setups. If the score is rising from "
            f"extreme fear toward neutral, that's a green light. If falling into extreme fear, flag caution."
        )

    crypto_context = ""
    if crypto_data:
        lines = []
        for coin_id, d in crypto_data.items():
            lines.append(
                f"{d['name']}: {d['price']} | 24h {d['chg_24h']} | 7d {d['chg_7d']} | "
                f"MCap {d['mcap']} | Vol {d['vol_24h']}"
            )
        crypto_context = (
            "\n## CRYPTO MARKET:\n" + "\n".join(lines) +
            "\nNote: BTC/ETH risk-on/risk-off behaviour often leads or confirms equity "
            "momentum. If BTC is down 10%+ on the week while momentum stocks are holding "
            "up, that divergence is meaningful — flag it. If crypto and equities are both "
            "selling off, that is a risk-off environment — be selective. IBIT/MSTR/COIN "
            "performance in the macro data above confirms crypto equity correlation."
        )

    prompt = f"""You are an experienced momentum trader and portfolio analyst doing a weekly review ({week_range}).

You have access to two data sets:

## WEEKLY PERSISTENCE LEADERBOARD (tickers that kept appearing in daily momentum screeners this week, ranked by conviction score):
{chr(10).join(ticker_lines)}

## MACRO ENVIRONMENT:
{chr(10).join(macro_lines) if macro_lines else "No macro data available."}{fng_context}{crypto_context}

Write a thorough weekly intelligence brief covering:

1. TOP CONVICTION SETUPS: Which 3-5 tickers have the strongest case for follow-through next week and why. Be specific — reference their persistence score, which screeners they hit, sector tailwinds, ATR suggesting room to move.

2. SECTOR THEMES: What sectors are dominating this week's scans? Is there a macro tailwind (from the ETF data) supporting these sectors?

3. WATCH FOR DOUBLES: Based on the data, which tickers have the profile of a significant multi-week mover — high ATR, strong EPS growth, appearing in multiple screeners, backed by a macro theme? Flag these explicitly.

4. RISK FLAGS: Which tickers look extended or too volatile to chase? What's the macro environment saying — is it risk-on or risk-off?

5. WHAT TO DO MONDAY: Specific, actionable — which tickers to have on your platform ready to trade, what price levels or signals to watch for entries.

Be direct and specific. Use ticker names throughout. Think like a partner who owns the research. No generic disclaimers. Plain paragraphs, no bullet points, no markdown headers."""

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if not resp.ok:
            log.error(f"AI weekly brief HTTP {resp.status_code}: {resp.text}")
            return ""
        brief = resp.json()["content"][0]["text"].strip()
        log.info("Weekly AI brief generated.")
        return brief
    except Exception as e:
        log.error(f"Weekly AI brief failed: {e}")
        return ""


# ----------------------------
# Part 5: Slack Weekly Push
# ----------------------------
def send_weekly_slack(persistence_df: pd.DataFrame, macro_data: dict,
                       ai_brief: str, weekly_html: str,
                       dates_found: list, fng_data: dict = None,
                       crypto_data: dict = None):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return

    week_range = f"{dates_found[0]} → {dates_found[-1]}" if dates_found else "this week"
    top5 = persistence_df.head(5)

    # Top 5 conviction tickers
    ticker_lines = []
    for _, row in top5.iterrows():
        atr = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get('Max ATR%')) else "—"
        multi = f" · ×{row['Max Appearances']} screeners" if row['Max Appearances'] >= 2 else ""
        ticker_lines.append(
            f"*{row['Ticker']}* · {row['Sector']} · "
            f"{row['Days Seen']}/{row['Total Days']}d · ATR {atr}{multi}\n"
            f"  _{row['Screeners Hit']}_"
        )

    # Macro highlights — just the movers
    macro_highlights = []
    for symbol, m in macro_data.items():
        wk = m['perf_week'].replace('%', '')
        try:
            if abs(float(wk)) >= 2.0:
                direction = "↑" if float(wk) > 0 else "↓"
                macro_highlights.append(f"{symbol} {direction} {m['perf_week']} wk")
        except:
            pass

    fng_line = ""
    if fng_data:
        emoji = _fng_emoji(fng_data["score"])
        fng_line = f"\n*Fear & Greed:* {emoji} {fng_data['score']} ({fng_data['rating']}) · 1wk ago {fng_data['prev_1_week']} · 1mo ago {fng_data['prev_1_month']}"

    crypto_line = ""
    if crypto_data:
        parts = []
        for d in crypto_data.values():
            c = "↑" if d["raw_chg_7d"] >= 0 else "↓"
            parts.append(f"{d['name'].split(' ')[0]} {d['price']} {c}{abs(d['raw_chg_7d']):.1f}% wk")
        crypto_line = "\n*Crypto:* " + " · ".join(parts)

    gallery_link = ""
    if GITHUB_PAGES_BASE:
        fname = os.path.basename(weekly_html)
        gallery_link = f"\n\n:page_facing_up: <{GITHUB_PAGES_BASE}/data/{fname}|Open full weekly report>"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Weekly Review — {week_range}"}
        },
    ]

    if ai_brief:
        # Send first 2 paragraphs in Slack, full report in the HTML link
        short_brief = " ".join(ai_brief.split('\n\n')[:2])
        if len(short_brief) > 2900:
            short_brief = short_brief[:2900] + "…"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":brain: *Weekly take:*\n{short_brief}"}
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*Top 5 conviction setups:*\n" +
                "\n".join(ticker_lines) +
                (f"\n\n*Macro movers:* {' · '.join(macro_highlights)}" if macro_highlights else "") +
                fng_line +
                crypto_line +
                gallery_link
            )
        }
    })
    blocks.append({"type": "divider"})

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Weekly Slack notification sent.")
    except Exception as e:
        log.error(f"Weekly Slack failed: {e}")


# ----------------------------
# Part 6: Main
# ----------------------------
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    log.info(f"=== Finviz weekly agent starting — {today} ===")

    # Step 1: Load this week's daily CSVs
    combined_df, daily_dfs, dates_found = load_weekly_data(DATA_DIR, lookback_days=7)

    if combined_df.empty:
        log.error("No data found for the past week — aborting.")
        exit(1)

    log.info(f"Loaded {len(dates_found)} trading days: {dates_found}")

    # Step 2: Score tickers by persistence and conviction
    persistence_df = build_persistence_scores(combined_df, dates_found)
    log.info(f"Persistence scores built — {len(persistence_df)} unique tickers")

    # Save persistence scores
    os.makedirs(DATA_DIR, exist_ok=True)
    persistence_df.to_csv(
        os.path.join(DATA_DIR, f"finviz_weekly_persistence_{today}.csv"), index=False
    )

    # Step 3: Macro snapshot + Fear & Greed
    log.info("Fetching macro snapshot...")
    macro_data = fetch_macro_snapshot()
    log.info(f"Macro data fetched for {len(macro_data)} symbols")

    log.info("Fetching CNN Fear & Greed index...")
    fng_data = fetch_fear_and_greed()

    log.info("Fetching crypto prices from CoinGecko...")
    crypto_data = fetch_crypto_data()

    # Step 4: AI weekly brief
    ai_brief = generate_weekly_ai_brief(persistence_df, macro_data, dates_found, fng_data, crypto_data)

    # Step 5: Weekly HTML report
    weekly_html = generate_weekly_html(persistence_df, macro_data, dates_found, ai_brief, fng_data, crypto_data)
    log.info(f"Weekly report: {weekly_html}")

    # Step 6: Slack push
    send_weekly_slack(persistence_df, macro_data, ai_brief, weekly_html, dates_found, fng_data, crypto_data)

    log.info("=== Weekly agent done ===")
