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
# Part 1: Load & Score
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


def _is_ep(screeners_hit: set, max_appearances: int) -> bool:
    """
    Tight EP criteria — all three required:
    1. Gap/surge screener: '10% Change' OR 'Week 20%+ Gain'
    2. '52 Week High' also fired (real breakout, not a bounce)
    3. max_appearances >= 2 (multi-screener same day = volume conviction)
    """
    has_gap   = "10% Change" in screeners_hit or "Week 20%+ Gain" in screeners_hit
    has_high  = "52 Week High" in screeners_hit
    has_multi = max_appearances >= 2
    return has_gap and has_high and has_multi


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
            "EP":              _is_ep(r["screeners_hit"], r["max_appearances"]),
        })

    return pd.DataFrame(rows).sort_values("Conviction", ascending=False)


def select_focus_names(persistence_df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Top 3 conviction + up to 2 EP candidates not already in top 3."""
    top3   = persistence_df.head(3)
    ep_df  = persistence_df[persistence_df["EP"] == True]
    ep_new = ep_df[~ep_df["Ticker"].isin(top3["Ticker"].tolist())].head(2)
    return pd.concat([top3, ep_new]).drop_duplicates("Ticker").head(n)


def select_leaderboard(persistence_df: pd.DataFrame) -> pd.DataFrame:
    """
    Recurring names worth monitoring — filtered to the upper half by conviction.
    If max score is 150, show everything >= 75.
    Hard cap at 30 names so it stays scannable.
    """
    if persistence_df.empty:
        return persistence_df
    max_score  = persistence_df["Conviction"].max()
    threshold  = max_score / 2
    filtered   = persistence_df[persistence_df["Conviction"] >= threshold]
    return filtered.head(30)


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


def _color(val_str: str) -> str:
    try:
        return "pos" if float(val_str.replace("%", "").strip()) > 0 else "neg"
    except Exception:
        return ""

def _arrow(val_str: str) -> str:
    try:
        return "▲" if float(val_str.replace("%", "").strip()) > 0 else "▼"
    except Exception:
        return ""


# ----------------------------
# Part 2a: Crypto
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
            }
            log.info(f"Crypto: {name} {result[coin_id]['price']} 7d:{result[coin_id]['chg_7d']}")
        return result
    except Exception as e:
        log.error(f"Crypto fetch failed: {e}")
        return {}


# ----------------------------
# Part 2b: Fear & Greed
# ----------------------------

def fetch_fear_and_greed() -> dict:
    try:
        resp = make_session().get(CNN_FNG_URL, timeout=10)
        if not resp.ok:
            return {}
        data       = resp.json()
        fg         = data.get("fear_and_greed", {})
        historical = data.get("fear_and_greed_historical", {}).get("data", [])
        recent_30  = historical[-30:] if len(historical) >= 30 else historical
        scores     = [d["y"] for d in recent_30] if recent_30 else []
        result = {
            "score":          round(fg.get("score", 0), 1),
            "rating":         fg.get("rating", "unknown").title(),
            "prev_close":     round(fg.get("previous_close",   0), 1),
            "prev_1_week":    round(fg.get("previous_1_week",  0), 1),
            "prev_1_month":   round(fg.get("previous_1_month", 0), 1),
            "prev_1_year":    round(fg.get("previous_1_year",  0), 1),
            "trend_30d_low":  round(min(scores), 1) if scores else None,
            "trend_30d_high": round(max(scores), 1) if scores else None,
            "trend_30d_avg":  round(sum(scores)/len(scores), 1) if scores else None,
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
        base = "Extreme Fear"
        ctx  = "historically a better buying zone — confirm with breadth before acting"
    elif score <= 45:
        base = "Fear"
        ctx  = "caution warranted, momentum stocks face headwinds"
    elif score <= 55:
        base = "Neutral"
        ctx  = "no strong directional bias"
    elif score <= 75:
        base = "Greed"
        ctx  = "momentum favourable, watch for overextension"
    else:
        base = "Extreme Greed"
        ctx  = "late-stage momentum — tighten stops"
    return f"{base} ({score}). {magnitude}pt {direction} vs last month. {ctx}."


# ----------------------------
# Part 3: HTML Report
# ----------------------------

def generate_weekly_html(persistence_df: pd.DataFrame, macro_data: dict,
                          dates_found: list, ai_brief: str,
                          fng_data: dict = None, crypto_data: dict = None) -> str:
    today      = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    out_html   = os.path.join(DATA_DIR, f"finviz_weekly_{today}.html")
    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else today

    # --- FOCUS CARDS: top 3 + up to 2 EPs ---
    focus_df    = select_focus_names(persistence_df)
    rank_colors = ["#facc15", "#94a3b8", "#b45309", "#4f6ef7", "#4f6ef7"]
    focus_cards = ""

    for i, (_, row) in enumerate(focus_df.iterrows()):
        rank_color = rank_colors[i] if i < len(rank_colors) else "#334155"
        days       = row["Days Seen"]
        total      = row["Total Days"]
        atr        = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps        = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        rank_label = f"#{i+1}" if i < 3 else "EP"
        ep_badge   = "<span class='ep-badge'>⚡ EP</span>" if row["EP"] else ""
        mb         = f"<span class='multi-badge'>x{row['Max Appearances']} screens</span>" if row["Max Appearances"] >= 2 else ""
        chart_url  = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        fv_url     = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"

        focus_cards += (
            "<div class='focus-card'>"
            f"<div class='focus-rank' style='color:{rank_color}'>{rank_label}</div>"
            "<div class='focus-header'>"
            f"<a href='{fv_url}' target='_blank' class='focus-ticker'>{row['Ticker']}</a>"
            f"<span class='focus-sector'>{row['Sector']}</span>"
            "</div>"
            f"<div class='focus-company'>{row['Company']}</div>"
            f"<div class='focus-badges'>{ep_badge}{mb}</div>"
            f"<div class='focus-persist'>{days}/{total} days · score {row['Conviction']}</div>"
            f"<div class='focus-meta'>ATR {atr} · EPS {eps}</div>"
            f"<div class='focus-screeners'>{row['Screeners Hit']}</div>"
            f"<a href='{chart_url}' target='_blank'>"
            f"<img src='{chart_url}' class='focus-chart' alt='{row['Ticker']}'>"
            "</a>"
            "</div>"
        )

    # --- MACRO TABLE ---
    macro_rows = ""
    for symbol, m in macro_data.items():
        wk_cls = _color(m["perf_week"])
        mo_cls = _color(m["perf_month"])
        dy_cls = _color(m["change"])
        wk_arr = _arrow(m["perf_week"])
        mo_arr = _arrow(m["perf_month"])
        macro_rows += (
            "<tr>"
            f"<td class='bold'>{symbol}</td>"
            f"<td class='mname'>{m['name']}</td>"
            f"<td class='mono'>{m['price']}</td>"
            f"<td class='mono {dy_cls}'>{m['change']}</td>"
            f"<td class='mono {wk_cls}'>{wk_arr} {m['perf_week']}</td>"
            f"<td class='mono {mo_cls}'>{mo_arr} {m['perf_month']}</td>"
            "</tr>"
        )

    # --- LEADERBOARD: upper half by conviction, max 30 ---
    leaderboard_df  = select_leaderboard(persistence_df)
    max_score       = persistence_df["Conviction"].max() if not persistence_df.empty else 100
    threshold       = max_score / 2
    leaderboard_rows = ""

    for idx, (_, row) in enumerate(leaderboard_df.iterrows()):
        days      = row["Days Seen"]
        total     = row["Total Days"]
        pct       = int((days / total) * 100) if total > 0 else 0
        bar_color = "#4f6ef7" if pct >= 80 else "#38bdf8" if pct >= 60 else "#64748b"
        atr       = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps       = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        apps      = f"x{row['Max Appearances']}" if row["Max Appearances"] >= 2 else ""
        ep_flag   = " ⚡" if row["EP"] else ""
        fv_url    = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"
        chart_url = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        row_cls   = "ep-row" if row["EP"] else ""

        leaderboard_rows += (
            f"<tr class='{row_cls}'>"
            f"<td class='dim'>{idx+1}</td>"
            f"<td><a href='{fv_url}' target='_blank' class='tlink'>{row['Ticker']}</a>{ep_flag}</td>"
            f"<td class='company'>{row['Company']}</td>"
            f"<td><span class='sector-pill'>{row['Sector']}</span></td>"
            "<td><div class='bar-wrap'>"
            f"<div class='bar' style='width:{pct}%;background:{bar_color}'></div>"
            f"<span>{days}/{total}d</span></div></td>"
            f"<td class='center bold'>{row['Conviction']}</td>"
            f"<td class='center'>{atr}</td>"
            f"<td class='center'>{eps}</td>"
            f"<td class='center'>{apps}</td>"
            f"<td class='screeners'>{row['Screeners Hit']}</td>"
            f"<td><a href='{chart_url}' target='_blank' class='chart-link'>chart</a></td>"
            "</tr>"
        )

    # --- AI BRIEF ---
    ai_html = ""
    if ai_brief:
        paras  = [p.strip() for p in ai_brief.split("\n") if p.strip()]
        inner  = "".join(f"<p>{p}</p>" for p in paras)
        ai_html = "<h2>Weekly Intelligence Brief</h2><div class='ai-brief'>" + inner + "</div>"

    # --- CRYPTO ---
    crypto_html = ""
    if crypto_data:
        cards = ""
        for d in crypto_data.values():
            c24 = "pos" if d["raw_chg_24h"] >= 0 else "neg"
            c7d = "pos" if d["raw_chg_7d"]  >= 0 else "neg"
            cards += (
                "<div class='crypto-card'>"
                f"<div class='cname'>{d['name']}</div>"
                f"<div class='cprice'>{d['price']}</div>"
                "<div class='cchanges'>"
                f"<span class='{c24}'>24h {d['chg_24h']}</span>"
                f"<span class='{c7d}'>7d {d['chg_7d']}</span>"
                "</div>"
                f"<div class='cmcap'>MCap {d['mcap']} · Vol {d['vol_24h']}</div>"
                "</div>"
            )
        crypto_html = "<h2>Crypto Snapshot</h2><div class='crypto-bar'>" + cards + "</div>"

    # --- FEAR & GREED ---
    fng_html = ""
    if fng_data:
        emoji = _fng_emoji(fng_data["score"])
        ctx   = _fng_context(fng_data["score"], fng_data["prev_1_month"])
        fng_html = (
            "<div class='fng-bar'>"
            "<div>"
            "<div class='fng-label'>Fear &amp; Greed</div>"
            f"<div class='fng-score'>{emoji} {fng_data['score']}</div>"
            f"<div class='fng-rating'>{fng_data['rating']}</div>"
            "</div>"
            f"<div class='fng-ctx'>{ctx}</div>"
            "<div class='fng-hist'>"
            f"<span>Prev close <b>{fng_data['prev_close']}</b></span>"
            f"<span>1wk ago <b>{fng_data['prev_1_week']}</b></span>"
            f"<span>1mo ago <b>{fng_data['prev_1_month']}</b></span>"
            f"<span>1yr ago <b>{fng_data['prev_1_year']}</b></span>"
            "</div>"
            "</div>"
        )

    macro_html = (
        "<h2>Macro Snapshot</h2>"
        "<table class='macro-table'><thead><tr>"
        "<th>Symbol</th><th>Name</th><th>Price</th><th>Day</th><th>Week</th><th>Month</th>"
        "</tr></thead><tbody>" + macro_rows + "</tbody></table>"
    ) if macro_rows else ""

    leaderboard_count = len(leaderboard_df)
    leaderboard_html  = (
        f"<h2>Recurring Names — conviction score &gt; {threshold:.0f} ({leaderboard_count} names)</h2>"
        "<p class='lb-note'>Tickers that kept appearing across multiple days this week. "
        "⚡ = Episodic Pivot candidate (gap/surge + new high + multi-screen).</p>"
        "<table class='lb-table'><thead><tr>"
        "<th>#</th><th>Ticker</th><th>Company</th><th>Sector</th>"
        "<th>Persistence</th><th>Score</th><th>ATR%</th><th>EPS%</th>"
        "<th>Multi</th><th>Screeners</th><th>Chart</th>"
        "</tr></thead><tbody>" + leaderboard_rows + "</tbody></table>"
    ) if leaderboard_rows else ""

    css = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0f1117; color: #e2e8f0; padding: 32px; max-width: 1400px; }
h1    { font-size: 1.4rem; font-weight: 700; margin-bottom: 4px; }
h2    { font-size: .78rem; font-weight: 600; color: #64748b; margin: 28px 0 10px;
        text-transform: uppercase; letter-spacing: .08em;
        border-bottom: 1px solid #1a1f2e; padding-bottom: 6px; }
.subtitle  { color: #64748b; font-size: 0.82rem; margin-bottom: 28px; }
.lb-note   { font-size: 0.74rem; color: #4b5563; margin-bottom: 10px; }
.pos  { color: #4ade80; }
.neg  { color: #f87171; }
.mono { font-variant-numeric: tabular-nums; }
.bold { font-weight: 700; }
.dim  { color: #334155; }
/* Focus cards */
.focus-grid   { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 14px; margin-bottom: 36px; }
.focus-card   { background: #1a1f2e; border-radius: 10px; padding: 14px 16px; border: 1px solid #252d40; }
.focus-rank   { font-size: 1.6rem; font-weight: 800; line-height: 1; margin-bottom: 6px; }
.focus-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; flex-wrap: wrap; }
.focus-ticker { font-size: 1.1rem; font-weight: 700; color: #7aa2f7; text-decoration: none; }
.focus-ticker:hover { color: #a5b4fc; }
.focus-sector  { font-size: 0.63rem; color: #38bdf8; background: #0c2240; padding: 1px 5px; border-radius: 3px; flex-shrink: 0; }
.focus-company { font-size: 0.68rem; color: #4b5563; margin-bottom: 6px; }
.focus-badges  { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 5px; min-height: 18px; }
.ep-badge      { font-size: 0.65rem; background: #451a03; color: #fbbf24; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.multi-badge   { font-size: 0.65rem; background: #1e3a5f; color: #60a5fa; padding: 1px 6px; border-radius: 3px; }
.focus-persist { font-size: 0.72rem; color: #94a3b8; margin-bottom: 2px; }
.focus-meta    { font-size: 0.7rem; color: #475569; margin-bottom: 5px; }
.focus-screeners { font-size: 0.64rem; color: #374151; margin-bottom: 9px; line-height: 1.4; }
.focus-chart   { width: 100%; border-radius: 6px; display: block; }
/* Macro */
.macro-table    { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-bottom: 8px; }
.macro-table th { text-align: left; padding: 7px 10px; color: #475569; font-weight: 500;
                  border-bottom: 1px solid #1e2130; text-transform: uppercase; font-size: 0.67rem; letter-spacing: .05em; }
.macro-table td { padding: 7px 10px; border-bottom: 1px solid #161b27; vertical-align: middle; }
.macro-table tr:hover td { background: #181d2b; }
.mname { color: #64748b; font-size: 0.75rem; }
/* Leaderboard */
.lb-table    { width: 100%; border-collapse: collapse; font-size: 0.79rem; }
.lb-table th { text-align: left; padding: 6px 9px; color: #475569; font-weight: 500;
               border-bottom: 1px solid #1e2130; text-transform: uppercase; font-size: 0.65rem; letter-spacing: .05em; }
.lb-table td { padding: 7px 9px; border-bottom: 1px solid #161b27; vertical-align: middle; }
.lb-table tr:hover td { background: #181d2b; }
.lb-table tr.ep-row td { background: #1c1708; }
.lb-table tr.ep-row:hover td { background: #251e0a; }
.tlink      { color: #7aa2f7; font-weight: 700; text-decoration: none; }
.tlink:hover { color: #a5b4fc; }
.chart-link { color: #38bdf8; font-size: 0.7rem; text-decoration: none; }
.company    { color: #94a3b8; font-size: 0.73rem; }
.sector-pill { background: #0c2240; color: #38bdf8; font-size: 0.63rem; padding: 2px 5px; border-radius: 3px; white-space: nowrap; }
.screeners  { color: #374151; font-size: 0.67rem; }
.bar-wrap   { display: flex; align-items: center; gap: 7px; }
.bar-wrap span { font-size: 0.7rem; color: #94a3b8; white-space: nowrap; }
.bar        { height: 5px; border-radius: 3px; min-width: 2px; }
.center     { text-align: center; }
/* AI brief */
.ai-brief   { background: #151b2e; border-left: 3px solid #3b55c9; border-radius: 0 8px 8px 0;
              padding: 16px 20px; margin-bottom: 8px; }
.ai-brief p { line-height: 1.75; color: #c4cfe8; font-size: 0.87rem; margin-bottom: 10px; }
.ai-brief p:last-child { margin-bottom: 0; }
/* Crypto */
.crypto-bar  { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.crypto-card { background: #1a1f2e; border-radius: 8px; padding: 13px 16px; min-width: 170px; }
.cname  { font-size: 0.67rem; color: #64748b; margin-bottom: 3px; }
.cprice { font-size: 1.05rem; font-weight: 700; margin-bottom: 5px; }
.cchanges { display: flex; gap: 10px; font-size: 0.75rem; margin-bottom: 4px; }
.cmcap  { font-size: 0.66rem; color: #374151; }
/* F&G */
.fng-bar    { background: #1a1f2e; border-radius: 8px; padding: 14px 18px; margin-bottom: 8px;
              display: flex; flex-direction: column; gap: 7px; }
.fng-label  { font-size: 0.64rem; color: #64748b; text-transform: uppercase; letter-spacing: .06em; }
.fng-score  { font-size: 1.35rem; font-weight: 700; }
.fng-rating { font-size: 0.74rem; color: #64748b; }
.fng-ctx    { font-size: 0.78rem; color: #94a3b8; line-height: 1.5; }
.fng-hist   { display: flex; gap: 16px; font-size: 0.71rem; color: #4b5563; flex-wrap: wrap; }
"""

    html = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        f"<title>Finviz Weekly — {today}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        "<h1>Finviz Weekly Review</h1>"
        f"<p class='subtitle'>{week_range} · {len(persistence_df)} tickers scanned · {len(dates_found)} trading days</p>"
        + crypto_html
        + fng_html
        + ai_html
        + "<h2>Focus Names This Week</h2>"
        + "<div class='focus-grid'>" + focus_cards + "</div>"
        + macro_html
        + leaderboard_html
        + "</body></html>"
    )

    with open(out_html, "w") as f:
        f.write(html)
    return out_html


# ----------------------------
# Part 4: AI Brief
# ----------------------------

def generate_weekly_ai_brief(persistence_df: pd.DataFrame, macro_data: dict,
                              dates_found: list, fng_data: dict = None,
                              crypto_data: dict = None) -> str:
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping AI brief.")
        return ""

    focus_df     = select_focus_names(persistence_df)
    newline      = "\n"
    ticker_lines = []

    for _, row in focus_df.iterrows():
        atr   = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "n/a"
        eps   = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "n/a"
        multi = f" | x{row['Max Appearances']} screeners same day" if row["Max Appearances"] >= 2 else ""
        ep    = " | EPISODIC PIVOT — gap/surge + new high + multi-screen" if row["EP"] else ""
        ticker_lines.append(
            f"{row['Ticker']} ({row['Sector']} / {row['Industry']}) "
            f"| {row['Days Seen']}/{row['Total Days']} days "
            f"| conviction {row['Conviction']} "
            f"| ATR {atr} | EPS {eps} "
            f"| screeners: {row['Screeners Hit']}{multi}{ep}"
        )

    macro_lines = [
        f"{sym} ({m['name']}): {m['price']} | week {m['perf_week']} | month {m['perf_month']}"
        for sym, m in macro_data.items()
    ]

    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"

    fng_ctx = ""
    if fng_data:
        fng_ctx = (
            f"\nFear & Greed: {fng_data['score']} ({fng_data['rating']}) | "
            f"1wk ago: {fng_data['prev_1_week']} | 1mo ago: {fng_data['prev_1_month']}\n"
        )

    crypto_ctx = ""
    if crypto_data:
        lines = [
            f"{d['name']}: {d['price']} | 24h {d['chg_24h']} | 7d {d['chg_7d']} | MCap {d['mcap']}"
            for d in crypto_data.values()
        ]
        crypto_ctx = "\nCrypto: " + " | ".join(lines) + "\n"

    prompt = (
        f"You are an experienced momentum trader doing a weekly review ({week_range}).\n\n"
        "These are the focus names for the week — top conviction leaders plus any episodic pivot setups:\n"
        f"{newline.join(ticker_lines)}\n\n"
        f"Macro: {newline.join(macro_lines) if macro_lines else 'unavailable'}"
        f"{fng_ctx}{crypto_ctx}\n\n"
        "Write a sharp weekly brief (4-6 paragraphs):\n"
        "1. For each focus name: why it stands out, what the screener combination tells you, "
        "what to watch for next week. For EP candidates — explain the catalyst signature.\n"
        "2. Sector themes and macro backdrop.\n"
        "3. What to do Monday — specific names, specific triggers.\n\n"
        "Be direct. Name names. No disclaimers. Plain paragraphs."
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if not resp.ok:
            log.error(f"AI brief HTTP {resp.status_code}: {resp.text}")
            return ""
        brief = resp.json()["content"][0]["text"].strip()
        log.info("Weekly AI brief generated.")
        return brief
    except Exception as e:
        log.error(f"Weekly AI brief failed: {e}")
        return ""


# ----------------------------
# Part 5: Slack
# ----------------------------

def send_weekly_slack(persistence_df: pd.DataFrame, macro_data: dict,
                       ai_brief: str, weekly_html: str,
                       dates_found: list, fng_data: dict = None,
                       crypto_data: dict = None):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return

    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"
    focus_df   = select_focus_names(persistence_df)

    ticker_lines = []
    for _, row in focus_df.iterrows():
        atr   = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        multi = f" · x{row['Max Appearances']}" if row["Max Appearances"] >= 2 else ""
        ep    = " · ⚡ EP" if row["EP"] else ""
        ticker_lines.append(
            f"*{row['Ticker']}* · {row['Sector']} · "
            f"{row['Days Seen']}/{row['Total Days']}d{multi}{ep}\n"
            f" _{row['Screeners Hit']}_"
        )

    macro_movers = []
    for sym, m in macro_data.items():
        wk = m["perf_week"].replace("%", "")
        try:
            if abs(float(wk)) >= 2.0:
                arrow = "↑" if float(wk) > 0 else "↓"
                macro_movers.append(f"{sym} {arrow} {m['perf_week']} wk")
        except Exception:
            pass

    fng_line = ""
    if fng_data:
        emoji    = _fng_emoji(fng_data["score"])
        fng_line = (
            f"\n*F&G:* {emoji} {fng_data['score']} ({fng_data['rating']}) "
            f"· 1wk {fng_data['prev_1_week']} · 1mo {fng_data['prev_1_month']}"
        )

    crypto_line = ""
    if crypto_data:
        parts = []
        for d in crypto_data.values():
            c = "↑" if d["raw_chg_7d"] >= 0 else "↓"
            parts.append(f"{d['name'].split(' ')[0]} {d['price']} {c}{abs(d['raw_chg_7d']):.1f}%wk")
        crypto_line = "\n*Crypto:* " + " · ".join(parts)

    gallery_link = ""
    if GITHUB_PAGES_BASE:
        fname        = os.path.basename(weekly_html)
        gallery_link = f"\n\n:page_facing_up: <{GITHUB_PAGES_BASE}/data/{fname}|Full weekly report>"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Weekly Review — {week_range}"}},
    ]

    if ai_brief:
        short = " ".join(ai_brief.split("\n\n")[:2])
        if len(short) > 2900:
            short = short[:2900] + "…"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":brain: *Weekly take:*\n{short}"}})
        blocks.append({"type": "divider"})

    macro_str = " · ".join(macro_movers) if macro_movers else ""
    body = (
        "*Focus names this week:*\n"
        + "\n".join(ticker_lines)
        + (f"\n\n*Macro movers:* {macro_str}" if macro_str else "")
        + fng_line + crypto_line + gallery_link
    )
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    blocks.append({"type": "divider"})

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Weekly Slack sent.")
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
        log.error("No data found — aborting.")
        exit(1)
    log.info(f"Loaded {len(dates_found)} trading days: {dates_found}")

    persistence_df = build_persistence_scores(combined_df, dates_found)
    log.info(f"{len(persistence_df)} unique tickers scored")

    ep_tickers = persistence_df[persistence_df["EP"]]["Ticker"].tolist()
    log.info(f"EP candidates ({len(ep_tickers)}): {ep_tickers}")

    focus_df = select_focus_names(persistence_df)
    log.info(f"Focus names: {focus_df['Ticker'].tolist()}")

    lb_df = select_leaderboard(persistence_df)
    log.info(f"Leaderboard: {len(lb_df)} names above threshold")

    os.makedirs(DATA_DIR, exist_ok=True)
    persistence_df.to_csv(
        os.path.join(DATA_DIR, f"finviz_weekly_persistence_{today}.csv"), index=False
    )

    log.info("Fetching macro...")
    macro_data  = fetch_macro_snapshot()
    log.info("Fetching Fear & Greed...")
    fng_data    = fetch_fear_and_greed()
    log.info("Fetching crypto...")
    crypto_data = fetch_crypto_data()

    ai_brief    = generate_weekly_ai_brief(persistence_df, macro_data, dates_found, fng_data, crypto_data)
    weekly_html = generate_weekly_html(persistence_df, macro_data, dates_found, ai_brief, fng_data, crypto_data)
    log.info(f"Report: {weekly_html}")

    send_weekly_slack(persistence_df, macro_data, ai_brief, weekly_html, dates_found, fng_data, crypto_data)
    log.info("=== Done ===")
