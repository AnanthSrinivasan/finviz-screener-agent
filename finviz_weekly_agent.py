# ----------------------------
# Finviz Weekly Review Agent
# ----------------------------
import os
import logging
import random
import datetime
import requests
import pandas as pd
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ATR_THRESHOLD     = float(os.environ.get("ATR_THRESHOLD", "3.0"))
DATA_DIR          = os.environ.get("DATA_DIR", "data")

CNN_FNG_URL   = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
COINGECKO_URL = "https://api.coingecko.com/api/v3"
FINVIZ_BASE   = "https://finviz.com"

MACRO_WATCHLIST = {
    "SPY":  "S&P 500",
    "QQQ":  "Nasdaq 100",
    "IWM":  "Russell 2000",
    "DIA":  "Dow Jones",
    "SLV":  "Silver",
    "GLD":  "Gold",
    "GDX":  "Gold Miners",
    "USO":  "Oil",
    "XLK":  "Technology",
    "SMH":  "Semiconductors",
    "XLE":  "Energy",
    "XLF":  "Financials",
    "XBI":  "Biotech",
    "TLT":  "20yr Treasuries",
    "UUP":  "US Dollar",
    "IBIT": "iShares Bitcoin ETF",
    "MSTR": "MicroStrategy",
    "COIN": "Coinbase",
}

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
# Part 1: Load & Score Weekly Data
# ----------------------------

def load_weekly_data(data_dir: str, lookback_days: int = 7) -> tuple:
    today       = datetime.date.today()
    dates_found = []
    daily_dfs   = {}

    for i in range(lookback_days):
        date     = today - datetime.timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        path     = os.path.join(data_dir, f"finviz_screeners_{date_str}.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                df["date"] = date_str
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


def _is_ep_candidate(screeners_hit: set, max_appearances: int) -> bool:
    """
    Episodic Pivot detection — Stockbee / Qullamaggie criteria:

    An EP is a catalyst-driven gap or surge of 10%+ with volume confirmation
    that fundamentally changes the story of the stock. We detect this via screener
    combinations — the screeners themselves are the volume/price evidence:

    - '10% Change' or 'Week 20%+ Gain' = the gap/surge event happened
    - '52 Week High' = price broke out to new highs = confirms the move is real
    - OR 3+ screeners on same day = institutional volume implied by multi-screen hit

    PL (Planet Labs) this week: 10% Change + 52 Week High + Week 20%+ Gain
    = textbook EP. Low persistence (3/7 days) but high explosive potential.
    These surface regardless of persistence rank.
    """
    has_gap    = "10% Change" in screeners_hit or "Week 20%+ Gain" in screeners_hit
    has_high   = "52 Week High" in screeners_hit
    return (has_gap and has_high) or (has_gap and max_appearances >= 3)


def build_persistence_scores(combined_df: pd.DataFrame, dates_found: list) -> pd.DataFrame:
    if combined_df.empty:
        return pd.DataFrame()

    records = defaultdict(lambda: {
        "days_seen": 0, "dates": [], "max_atr": None, "max_eps": None,
        "max_appearances": 0, "screeners_hit": set(),
        "sector": "", "industry": "", "company": "", "market_cap": "",
    })

    for _, row in combined_df.iterrows():
        t = row["Ticker"]
        r = records[t]
        r["days_seen"] += 1
        r["dates"].append(row.get("date", ""))
        r["max_appearances"] = max(r["max_appearances"], row.get("Appearances", 1))

        atr = row.get("ATR%")
        if pd.notna(atr):
            r["max_atr"] = max(r["max_atr"] or 0, float(atr))

        eps = row.get("EPS Y/Y TTM")
        if pd.notna(eps):
            r["max_eps"] = max(r["max_eps"] or -9999, float(eps))

        screeners = row.get("Screeners", "")
        if screeners:
            for s in str(screeners).split(","):
                r["screeners_hit"].add(s.strip())

        for field in ("sector", "industry", "company", "market_cap"):
            col = {"sector": "Sector", "industry": "Industry",
                   "company": "Company", "market_cap": "Market Cap"}[field]
            if not r[field] and pd.notna(row.get(col, "")):
                r[field] = row.get(col, "")

    rows = []
    total_days = len(dates_found)
    for ticker, r in records.items():
        screener_diversity = len(r["screeners_hit"])
        conviction = (r["days_seen"] / max(total_days, 1)) * 100
        conviction += screener_diversity * 10
        if r["max_appearances"] >= 2:
            conviction += 20

        is_ep = _is_ep_candidate(r["screeners_hit"], r["max_appearances"])

        rows.append({
            "Ticker":          ticker,
            "Company":         r["company"],
            "Sector":          r["sector"],
            "Industry":        r["industry"],
            "Market Cap":      r["market_cap"],
            "Days Seen":       r["days_seen"],
            "Total Days":      total_days,
            "Dates":           ", ".join(sorted(set(r["dates"]))),
            "Max ATR%":        round(r["max_atr"], 1) if r["max_atr"] is not None else None,
            "Max EPS%":        round(r["max_eps"], 1) if r["max_eps"] is not None else None,
            "Max Appearances": r["max_appearances"],
            "Screeners Hit":   ", ".join(sorted(r["screeners_hit"])),
            "Conviction":      round(conviction, 1),
            "EP":              is_ep,
        })

    return pd.DataFrame(rows).sort_values("Conviction", ascending=False)


# ----------------------------
# Part 2: Macro Snapshot
# ----------------------------

def fetch_macro_snapshot() -> dict:
    from bs4 import BeautifulSoup
    session    = make_session()
    macro_data = {}

    for symbol, name in MACRO_WATCHLIST.items():
        try:
            resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": symbol}, timeout=10)
            if not resp.ok:
                continue
            soup  = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", class_="snapshot-table2")
            if not table:
                continue
            data = {}
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                for kc, vc in zip(cells[0::2], cells[1::2]):
                    data[kc.get_text(strip=True).rstrip(".")] = vc.get_text(strip=True)
            macro_data[symbol] = {
                "name":       name,
                "price":      data.get("Price",      "n/a"),
                "change":     data.get("Change",     "n/a"),
                "perf_week":  data.get("Perf Week",  "n/a"),
                "perf_month": data.get("Perf Month", "n/a"),
            }
            log.info(f"Macro: {symbol} {data.get('Price')} {data.get('Change')}")
        except Exception as e:
            log.warning(f"Macro fetch failed for {symbol}: {e}")

    return macro_data


def _pct_color(val_str: str) -> str:
    v = val_str.replace("%", "").strip()
    try:
        f = float(v)
        if f > 0:  return "pos"
        if f < 0:  return "neg"
    except Exception:
        pass
    return ""


def _pct_arrow(val_str: str) -> str:
    v = val_str.replace("%", "").strip()
    try:
        f = float(v)
        if f > 0:  return "▲"
        if f < 0:  return "▼"
    except Exception:
        pass
    return ""


# ----------------------------
# Part 2a: Crypto via CoinGecko
# ----------------------------

def fetch_crypto_data() -> dict:
    session = make_session()
    try:
        resp = session.get(
            f"{COINGECKO_URL}/simple/price",
            params={
                "ids": "bitcoin,ethereum",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_7d_change":  "true",
                "include_market_cap": "true",
                "include_24hr_vol":   "true",
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"CoinGecko fetch failed: {resp.status_code}")
            return {}

        raw      = resp.json()
        name_map = {"bitcoin": "Bitcoin (BTC)", "ethereum": "Ethereum (ETH)"}
        result   = {}

        for coin_id, name in name_map.items():
            d       = raw.get(coin_id, {})
            price   = d.get("usd", 0)
            chg_24h = d.get("usd_24h_change", 0) or 0
            chg_7d  = d.get("usd_7d_change",  0) or 0
            mcap    = d.get("usd_market_cap", 0) or 0
            vol     = d.get("usd_24h_vol",    0) or 0

            result[coin_id] = {
                "name":        name,
                "price":       f"${price:,.0f}" if price > 100 else f"${price:,.2f}",
                "chg_24h":     f"{chg_24h:+.1f}%",
                "chg_7d":      f"{chg_7d:+.1f}%",
                "mcap":        f"${mcap/1e9:.0f}B" if mcap > 1e9 else f"${mcap/1e6:.0f}M",
                "vol_24h":     f"${vol/1e9:.1f}B"  if vol  > 1e9 else f"${vol/1e6:.0f}M",
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
    try:
        session = make_session()
        resp    = session.get(CNN_FNG_URL, timeout=10)
        if not resp.ok:
            log.warning(f"Fear & Greed fetch failed: {resp.status_code}")
            return {}

        data       = resp.json()
        fg         = data.get("fear_and_greed", {})
        historical = data.get("fear_and_greed_historical", {}).get("data", [])
        recent_30  = historical[-30:] if len(historical) >= 30 else historical

        if recent_30:
            scores     = [d["y"] for d in recent_30]
            trend_low  = round(min(scores), 1)
            trend_high = round(max(scores), 1)
            trend_avg  = round(sum(scores) / len(scores), 1)
        else:
            trend_low = trend_high = trend_avg = None

        result = {
            "score":          round(fg.get("score", 0), 1),
            "rating":         fg.get("rating", "unknown").title(),
            "prev_close":     round(fg.get("previous_close",   0), 1),
            "prev_1_week":    round(fg.get("previous_1_week",  0), 1),
            "prev_1_month":   round(fg.get("previous_1_month", 0), 1),
            "prev_1_year":    round(fg.get("previous_1_year",  0), 1),
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
    change    = score - prev_month
    direction = "up" if change > 0 else "down"
    magnitude = abs(round(change, 1))
    if score <= 25:
        base    = "Market is in Extreme Fear"
        context = "historically a better buying zone — but confirm with breadth before acting"
    elif score <= 45:
        base    = "Market is in Fear"
        context = "caution warranted, momentum stocks face headwinds"
    elif score <= 55:
        base    = "Market is Neutral"
        context = "no strong directional bias, stock-picking matters more than macro"
    elif score <= 75:
        base    = "Market is in Greed"
        context = "momentum favourable, but watch for overextension"
    else:
        base    = "Market is in Extreme Greed"
        context = "late-stage momentum — tighten stops, don't chase extended moves"
    return f"{base} ({score}). {magnitude}pt {direction} vs last month. {context}."


# ----------------------------
# Part 3: Weekly HTML Report
# ----------------------------

def generate_weekly_html(persistence_df: pd.DataFrame, macro_data: dict,
                          dates_found: list, ai_brief: str,
                          fng_data: dict = None, crypto_data: dict = None) -> str:
    today      = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    out_html   = os.path.join(DATA_DIR, f"finviz_weekly_{today}.html")
    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else today

    # --- TOP 5 PODIUM + EP candidates ---
    ep_df    = persistence_df[persistence_df["EP"] == True]
    top5_df  = persistence_df.head(5)
    ep_extra = ep_df[~ep_df["Ticker"].isin(top5_df["Ticker"].tolist())]
    featured = pd.concat([top5_df, ep_extra]).drop_duplicates("Ticker")

    rank_colors  = ["#facc15", "#94a3b8", "#b45309", "#4f6ef7", "#4f6ef7"]
    podium_cards = ""

    for i, (_, row) in enumerate(featured.iterrows()):
        rank_color  = rank_colors[i] if i < len(rank_colors) else "#334155"
        days        = row["Days Seen"]
        total       = row["Total Days"]
        atr         = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps         = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        multi_badge = f"<span class='multi-badge'>x{row['Max Appearances']} screens</span>" if row["Max Appearances"] >= 2 else ""
        ep_badge    = "<span class='ep-badge'>⚡ EP</span>" if row["EP"] else ""
        rank_label  = f"#{i+1}" if i < 5 else "EP"
        chart_url   = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        fv_url      = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"

        podium_cards += (
            "<div class='podium-card'>"
            f"<div class='podium-rank' style='color:{rank_color}'>{rank_label}</div>"
            "<div class='podium-header'>"
            f"<a href='{fv_url}' target='_blank' class='podium-ticker'>{row['Ticker']}</a>"
            f"<span class='podium-sector'>{row['Sector']}</span>"
            "</div>"
            f"<div class='podium-company'>{row['Company']}</div>"
            f"<div class='podium-badges'>{ep_badge}{multi_badge}</div>"
            f"<div class='podium-persist'>{days}/{total} days · score {row['Conviction']}</div>"
            f"<div class='podium-meta'>ATR {atr} · EPS {eps}</div>"
            f"<div class='podium-screeners'>{row['Screeners Hit']}</div>"
            f"<a href='{chart_url}' target='_blank'>"
            f"<img src='{chart_url}' class='podium-chart' alt='{row['Ticker']}'>"
            "</a>"
            "</div>"
        )

    # --- FULL TABLE ---
    ticker_rows = ""
    for idx, (_, row) in enumerate(persistence_df.iterrows()):
        days      = row["Days Seen"]
        total     = row["Total Days"]
        pct       = int((days / total) * 100) if total > 0 else 0
        bar_color = "#4f6ef7" if pct >= 80 else "#38bdf8" if pct >= 60 else "#64748b"
        atr       = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps       = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        apps      = f"x{row['Max Appearances']}" if row["Max Appearances"] >= 2 else ""
        ep_flag   = " ⚡" if row["EP"] else ""
        chart_url = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        fv_url    = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"
        row_cls   = "ep-row" if row["EP"] else ""

        ticker_rows += (
            f"<tr class='{row_cls}'>"
            f"<td class='dim'>{idx+1}</td>"
            f"<td><a href='{fv_url}' target='_blank' class='ticker-link'>{row['Ticker']}</a>{ep_flag}</td>"
            f"<td class='company'>{row['Company']}</td>"
            f"<td><span class='sector-pill'>{row['Sector']}</span></td>"
            "<td><div class='bar-wrap'>"
            f"<div class='bar' style='width:{pct}%;background:{bar_color}'></div>"
            f"<span>{days}/{total}d</span></div></td>"
            f"<td class='center'>{row['Conviction']}</td>"
            f"<td class='center'>{atr}</td>"
            f"<td class='center'>{eps}</td>"
            f"<td class='center bold'>{apps}</td>"
            f"<td class='screeners'>{row['Screeners Hit']}</td>"
            f"<td><a href='{chart_url}' target='_blank' class='chart-link'>chart</a></td>"
            "</tr>"
        )

    # --- MACRO TABLE with colour + arrows ---
    macro_rows = ""
    for symbol, m in macro_data.items():
        chg_cls = _pct_color(m["change"])
        wk_cls  = _pct_color(m["perf_week"])
        mo_cls  = _pct_color(m["perf_month"])
        wk_arr  = _pct_arrow(m["perf_week"])
        mo_arr  = _pct_arrow(m["perf_month"])

        macro_rows += (
            "<tr>"
            f"<td class='bold'>{symbol}</td>"
            f"<td class='macro-name'>{m['name']}</td>"
            f"<td class='mono'>{m['price']}</td>"
            f"<td class='mono {chg_cls}'>{m['change']}</td>"
            f"<td class='mono {wk_cls}'>{wk_arr} {m['perf_week']}</td>"
            f"<td class='mono {mo_cls}'>{mo_arr} {m['perf_month']}</td>"
            "</tr>"
        )

    # --- AI brief ---
    ai_html = ""
    if ai_brief:
        paras  = [p.strip() for p in ai_brief.split("\n") if p.strip()]
        inner  = "".join(f"<p>{p}</p>" for p in paras)
        ai_html = "<h2>Weekly Intelligence Brief</h2><div class='ai-brief'>" + inner + "</div>"

    # --- Crypto ---
    crypto_html = ""
    if crypto_data:
        cards = ""
        for d in crypto_data.values():
            c24 = "pos" if d["raw_chg_24h"] >= 0 else "neg"
            c7d = "pos" if d["raw_chg_7d"]  >= 0 else "neg"
            cards += (
                "<div class='crypto-card'>"
                f"<div class='crypto-name'>{d['name']}</div>"
                f"<div class='crypto-price'>{d['price']}</div>"
                "<div class='crypto-changes'>"
                f"<span class='{c24}'>24h {d['chg_24h']}</span>"
                f"<span class='{c7d}'>7d {d['chg_7d']}</span>"
                "</div>"
                f"<div class='crypto-mcap'>MCap {d['mcap']} · Vol {d['vol_24h']}</div>"
                "</div>"
            )
        crypto_html = "<h2>Crypto Snapshot</h2><div class='crypto-bar'>" + cards + "</div>"

    # --- Fear & Greed ---
    fng_html = ""
    if fng_data:
        emoji   = _fng_emoji(fng_data["score"])
        ctx     = _fng_context(fng_data["score"], fng_data["prev_1_month"])
        fng_html = (
            "<div class='fng-bar'>"
            "<div>"
            "<div class='fng-label'>Fear &amp; Greed</div>"
            f"<div class='fng-score'>{emoji} {fng_data['score']}</div>"
            f"<div style='font-size:.8rem;color:#64748b'>{fng_data['rating']}</div>"
            "</div>"
            f"<div class='fng-context'>{ctx}</div>"
            "<div class='fng-hist'>"
            f"<span>Prev close <strong>{fng_data['prev_close']}</strong></span>"
            f"<span>1 week ago <strong>{fng_data['prev_1_week']}</strong></span>"
            f"<span>1 month ago <strong>{fng_data['prev_1_month']}</strong></span>"
            f"<span>1 year ago <strong>{fng_data['prev_1_year']}</strong></span>"
            "</div>"
            "</div>"
        )

    macro_html = (
        "<h2>Macro Snapshot</h2>"
        "<table class='macro-table'><thead><tr>"
        "<th>Symbol</th><th>Name</th><th>Price</th><th>Day</th><th>Week</th><th>Month</th>"
        "</tr></thead><tbody>" + macro_rows + "</tbody></table>"
    ) if macro_rows else ""

    css = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0f1117; color: #e2e8f0; padding: 32px; }
h1    { font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }
h2    { font-size: .82rem; font-weight: 600; color: #64748b; margin: 32px 0 14px;
        text-transform: uppercase; letter-spacing: .08em; }
.subtitle { color: #64748b; font-size: 0.85rem; margin-bottom: 32px; }
.pos  { color: #4ade80; }
.neg  { color: #f87171; }
.mono { font-variant-numeric: tabular-nums; }
.dim  { color: #334155; }
/* Podium */
.podium-grid    { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px,1fr)); gap: 14px; margin-bottom: 40px; }
.podium-card    { background: #1e2130; border-radius: 10px; padding: 14px 16px; border: 1px solid #2d3148; }
.podium-rank    { font-size: 1.7rem; font-weight: 800; line-height: 1; margin-bottom: 6px; }
.podium-header  { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
.podium-ticker  { font-size: 1.1rem; font-weight: 700; color: #7aa2f7; text-decoration: none; }
.podium-ticker:hover { color: #a5b4fc; }
.podium-sector  { font-size: 0.65rem; color: #38bdf8; background: #0c2240; padding: 1px 5px; border-radius: 3px; }
.podium-company { font-size: 0.7rem; color: #64748b; margin-bottom: 6px; }
.podium-badges  { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 6px; }
.ep-badge       { font-size: 0.68rem; background: #451a03; color: #fbbf24; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.multi-badge    { font-size: 0.68rem; background: #1e3a5f; color: #60a5fa; padding: 1px 6px; border-radius: 3px; }
.podium-persist { font-size: 0.75rem; color: #94a3b8; margin-bottom: 3px; }
.podium-meta    { font-size: 0.72rem; color: #475569; margin-bottom: 4px; }
.podium-screeners { font-size: 0.67rem; color: #4b5563; margin-bottom: 8px; line-height: 1.4; }
.podium-chart   { width: 100%; border-radius: 6px; display: block; opacity: .9; }
.podium-chart:hover { opacity: 1; }
/* EP legend */
.ep-legend { font-size: 0.75rem; color: #92400e; background: #1c1708; border: 1px solid #451a03;
             border-radius: 6px; padding: 8px 14px; margin-bottom: 20px; line-height: 1.6; }
/* Full table */
table  { width: 100%; border-collapse: collapse; font-size: 0.81rem; }
th     { text-align: left; padding: 7px 10px; color: #64748b; font-weight: 500;
         border-bottom: 1px solid #1e2130; text-transform: uppercase; font-size: 0.68rem; letter-spacing: .05em; }
td     { padding: 8px 10px; border-bottom: 1px solid #1a1f2e; vertical-align: middle; }
tr:hover td { background: #1a1f2e; }
tr.ep-row td { background: #1c1708; }
tr.ep-row:hover td { background: #251e0a; }
.ticker-link { color: #7aa2f7; font-weight: 700; text-decoration: none; }
.ticker-link:hover { color: #a5b4fc; }
.chart-link  { color: #38bdf8; font-size: 0.7rem; text-decoration: none; }
.company     { color: #94a3b8; font-size: 0.74rem; }
.sector-pill { background: #0c2240; color: #38bdf8; font-size: 0.64rem; padding: 2px 5px; border-radius: 3px; white-space: nowrap; }
.screeners   { color: #4b5563; font-size: 0.68rem; }
.bar-wrap    { display: flex; align-items: center; gap: 7px; }
.bar-wrap span { font-size: 0.7rem; color: #94a3b8; white-space: nowrap; }
.bar         { height: 5px; border-radius: 3px; min-width: 2px; }
.center      { text-align: center; }
.bold        { font-weight: 700; }
/* Macro */
.macro-table td { color: #cbd5e1; }
.macro-name     { color: #94a3b8; font-size: 0.76rem; }
/* AI brief */
.ai-brief   { background: #1a1f35; border-left: 3px solid #4f6ef7; border-radius: 0 8px 8px 0;
              padding: 16px 20px; margin-bottom: 32px; }
.ai-brief p { line-height: 1.7; color: #cbd5e1; font-size: 0.88rem; margin-bottom: 10px; }
.ai-brief p:last-child { margin-bottom: 0; }
/* Crypto */
.crypto-bar  { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 24px; }
.crypto-card { background: #1e2130; border-radius: 8px; padding: 14px 18px; min-width: 175px; }
.crypto-name  { font-size: 0.7rem; color: #64748b; margin-bottom: 4px; }
.crypto-price { font-size: 1.1rem; font-weight: 700; }
.crypto-changes { display: flex; gap: 10px; margin-top: 5px; font-size: 0.78rem; }
.crypto-mcap  { font-size: 0.68rem; color: #475569; margin-top: 4px; }
/* F&G */
.fng-bar     { background: #1e2130; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;
               display: flex; flex-direction: column; gap: 8px; }
.fng-label   { font-size: 0.68rem; color: #64748b; text-transform: uppercase; letter-spacing: .05em; }
.fng-score   { font-size: 1.4rem; font-weight: 700; }
.fng-context { font-size: 0.8rem; color: #94a3b8; line-height: 1.5; }
.fng-hist    { display: flex; gap: 18px; font-size: 0.74rem; color: #64748b; flex-wrap: wrap; }
"""

    ep_legend = (
        "<p class='ep-legend'>"
        "⚡ <strong>EP = Episodic Pivot candidate</strong> — Stockbee/Qullamaggie criteria: "
        "gap or surge screener (10% Change / Week 20%+ Gain) fired AND the stock hit a 52-week high the same week, "
        "OR 3+ screeners fired on the same day. "
        "This is the catalyst + confirmation + volume signature of an EP. "
        "These are surfaced regardless of persistence rank — a 3/7 day EP beats a passive 7/7 drifter for explosive potential."
        "</p>"
    )

    html = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        f"<title>Finviz Weekly Review — {today}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        "<h1>Finviz Weekly Review</h1>"
        f"<p class='subtitle'>{week_range} · {len(persistence_df)} unique tickers · {len(dates_found)} trading days scanned</p>"
        + crypto_html
        + fng_html
        + ai_html
        + "<h2>Best of Week — Top Conviction + Episodic Pivots</h2>"
        + ep_legend
        + "<div class='podium-grid'>" + podium_cards + "</div>"
        + "<h2>Full Persistence Leaderboard</h2>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Ticker</th><th>Company</th><th>Sector</th><th>Persistence</th>"
        + "<th>Score</th><th>ATR%</th><th>EPS%</th><th>Multi</th><th>Screeners</th><th>Chart</th>"
        + "</tr></thead><tbody>" + ticker_rows + "</tbody></table>"
        + macro_html
        + "</body></html>"
    )

    with open(out_html, "w") as f:
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

    top          = persistence_df.head(20)
    newline      = "\n"
    ticker_lines = []

    for _, row in top.iterrows():
        atr   = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "n/a"
        eps   = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "n/a"
        multi = f" | MULTI-SCREENER x{row['Max Appearances']}" if row["Max Appearances"] >= 2 else ""
        ep    = " | EPISODIC PIVOT CANDIDATE" if row["EP"] else ""
        ticker_lines.append(
            f"{row['Ticker']} ({row['Sector']} / {row['Industry']}) "
            f"| seen {row['Days Seen']}/{row['Total Days']} days "
            f"| conviction {row['Conviction']} "
            f"| max ATR {atr} | max EPS {eps} "
            f"| screeners: {row['Screeners Hit']}{multi}{ep}"
        )

    ep_extra = persistence_df[
        (persistence_df["EP"] == True) &
        (~persistence_df["Ticker"].isin(top["Ticker"].tolist()))
    ]
    for _, row in ep_extra.iterrows():
        atr = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "n/a"
        ticker_lines.append(
            f"{row['Ticker']} ({row['Sector']}) "
            f"| seen {row['Days Seen']}/{row['Total Days']} days "
            f"| screeners: {row['Screeners Hit']} | EPISODIC PIVOT CANDIDATE"
        )

    macro_lines = [
        f"{sym} ({m['name']}): price {m['price']} | week {m['perf_week']} | month {m['perf_month']}"
        for sym, m in macro_data.items()
    ]

    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"

    fng_context = ""
    if fng_data:
        fng_context = (
            "\n## CNN FEAR & GREED INDEX:\n"
            f"Current: {fng_data['score']} ({fng_data['rating']}) | "
            f"1 week ago: {fng_data['prev_1_week']} | "
            f"1 month ago: {fng_data['prev_1_month']} | "
            f"30d range: {fng_data['trend_30d_low']}-{fng_data['trend_30d_high']}\n"
        )

    crypto_context = ""
    if crypto_data:
        lines = [
            f"{d['name']}: {d['price']} | 24h {d['chg_24h']} | 7d {d['chg_7d']} | MCap {d['mcap']}"
            for d in crypto_data.values()
        ]
        crypto_context = "\n## CRYPTO MARKET:\n" + "\n".join(lines) + "\n"

    prompt = (
        f"You are an experienced momentum trader doing a weekly review ({week_range}).\n\n"
        "## WEEKLY PERSISTENCE LEADERBOARD:\n"
        "(Tickers marked EPISODIC PIVOT CANDIDATE fired gap/surge + 52wk high screeners — Stockbee EP criteria.)\n"
        f"{newline.join(ticker_lines)}\n\n"
        f"## MACRO ENVIRONMENT:\n{newline.join(macro_lines) if macro_lines else 'No macro data.'}"
        f"{fng_context}{crypto_context}\n\n"
        "Write a weekly intelligence brief covering:\n"
        "1. TOP CONVICTION SETUPS: 3-5 tickers with strongest follow-through case. Reference persistence, screeners, sector tailwind, ATR.\n"
        "2. EPISODIC PIVOT SETUPS: For EP candidates, explain the catalyst signature — what screeners fired, what that implies. EP = lower persistence but higher explosive potential.\n"
        "3. SECTOR THEMES: Which sectors dominate and is there macro support?\n"
        "4. RISK FLAGS: What looks extended? What is macro environment saying?\n"
        "5. WHAT TO DO MONDAY: Specific — which tickers, what signals to watch for entries.\n\n"
        "Be direct. Use ticker names. No disclaimers. Plain paragraphs only."
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1400,
                "messages":   [{"role": "user", "content": prompt}],
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

    week_range   = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"
    top5         = persistence_df.head(5)
    ticker_lines = []

    for _, row in top5.iterrows():
        atr   = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        multi = f" · x{row['Max Appearances']} screeners" if row["Max Appearances"] >= 2 else ""
        ep    = " · ⚡ EP" if row["EP"] else ""
        ticker_lines.append(
            f"*{row['Ticker']}* · {row['Sector']} · {row['Days Seen']}/{row['Total Days']}d · ATR {atr}{multi}{ep}\n"
            f" _{row['Screeners Hit']}_"
        )

    ep_extra = persistence_df[
        (persistence_df["EP"] == True) &
        (~persistence_df["Ticker"].isin(top5["Ticker"].tolist()))
    ]
    if not ep_extra.empty:
        ep_lines = [f"*{row['Ticker']}* ⚡ EP · {row['Sector']} · {row['Screeners Hit']}" for _, row in ep_extra.iterrows()]
        ticker_lines.append("\n*EP candidates outside top 5:*\n" + "\n".join(ep_lines))

    macro_highlights = []
    for symbol, m in macro_data.items():
        wk = m["perf_week"].replace("%", "")
        try:
            if abs(float(wk)) >= 2.0:
                arrow = "↑" if float(wk) > 0 else "↓"
                macro_highlights.append(f"{symbol} {arrow} {m['perf_week']} wk")
        except Exception:
            pass

    fng_line = ""
    if fng_data:
        emoji    = _fng_emoji(fng_data["score"])
        fng_line = (
            f"\n*Fear & Greed:* {emoji} {fng_data['score']} ({fng_data['rating']}) "
            f"· 1wk ago {fng_data['prev_1_week']} · 1mo ago {fng_data['prev_1_month']}"
        )

    crypto_line = ""
    if crypto_data:
        parts = []
        for d in crypto_data.values():
            c = "↑" if d["raw_chg_7d"] >= 0 else "↓"
            parts.append(f"{d['name'].split(' ')[0]} {d['price']} {c}{abs(d['raw_chg_7d']):.1f}% wk")
        crypto_line = "\n*Crypto:* " + " · ".join(parts)

    gallery_link = ""
    if GITHUB_PAGES_BASE:
        fname        = os.path.basename(weekly_html)
        gallery_link = f"\n\n:page_facing_up: <{GITHUB_PAGES_BASE}/data/{fname}|Open full weekly report>"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Weekly Review — {week_range}"}},
    ]

    if ai_brief:
        short_brief = " ".join(ai_brief.split("\n\n")[:2])
        if len(short_brief) > 2900:
            short_brief = short_brief[:2900] + "…"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":brain: *Weekly take:*\n{short_brief}"}})
        blocks.append({"type": "divider"})

    macro_str = " · ".join(macro_highlights) if macro_highlights else ""
    body = (
        "*Top conviction + EP setups:*\n"
        + "\n".join(ticker_lines)
        + (f"\n\n*Macro movers:* {macro_str}" if macro_str else "")
        + fng_line + crypto_line + gallery_link
    )
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
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

    combined_df, daily_dfs, dates_found = load_weekly_data(DATA_DIR, lookback_days=7)
    if combined_df.empty:
        log.error("No data found for the past week — aborting.")
        exit(1)
    log.info(f"Loaded {len(dates_found)} trading days: {dates_found}")

    persistence_df = build_persistence_scores(combined_df, dates_found)
    log.info(f"Persistence scores built — {len(persistence_df)} unique tickers")

    ep_count = int(persistence_df["EP"].sum())
    log.info(f"Episodic Pivot candidates: {ep_count}")
    if ep_count > 0:
        log.info(f"EP tickers: {persistence_df[persistence_df['EP']]['Ticker'].tolist()}")

    os.makedirs(DATA_DIR, exist_ok=True)
    persistence_df.to_csv(
        os.path.join(DATA_DIR, f"finviz_weekly_persistence_{today}.csv"), index=False
    )

    log.info("Fetching macro snapshot...")
    macro_data  = fetch_macro_snapshot()

    log.info("Fetching CNN Fear & Greed index...")
    fng_data    = fetch_fear_and_greed()

    log.info("Fetching crypto prices from CoinGecko...")
    crypto_data = fetch_crypto_data()

    ai_brief    = generate_weekly_ai_brief(persistence_df, macro_data, dates_found, fng_data, crypto_data)
    weekly_html = generate_weekly_html(persistence_df, macro_data, dates_found, ai_brief, fng_data, crypto_data)
    log.info(f"Weekly report: {weekly_html}")

    send_weekly_slack(persistence_df, macro_data, ai_brief, weekly_html, dates_found, fng_data, crypto_data)
    log.info("=== Weekly agent done ===")
