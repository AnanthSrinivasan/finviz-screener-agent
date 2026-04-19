# ----------------------------
# Finviz Weekly Review Agent
# ----------------------------
import os
import json
import time
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
# Market Monitor Integration
# ----------------------------

MARKET_HISTORY_FILE = os.path.join(DATA_DIR, "market_monitor_history.json")


def load_market_state() -> dict | None:
    """
    Load the most recent market monitor state for Agent 3 context.
    Returns the latest day's record, or None if unavailable.
    """
    if not os.path.exists(MARKET_HISTORY_FILE):
        return None
    try:
        with open(MARKET_HISTORY_FILE) as f:
            history = json.load(f)
        if not history:
            return None
        return history[-1]
    except Exception as e:
        log.warning(f"Could not load market monitor state: {e}")
        return None


def any_thrust_in_history() -> bool:
    """Check if any thrust was detected in the last 30 trading days."""
    if not os.path.exists(MARKET_HISTORY_FILE):
        return False
    try:
        with open(MARKET_HISTORY_FILE) as f:
            history = json.load(f)
        return any(d.get("thrust_detected") for d in history)
    except Exception:
        return False


# ----------------------------
# Part 1a: Character Change Deep Check (yfinance)
# ----------------------------

def fetch_earnings_history(ticker: str) -> dict | None:
    """
    Fetch quarterly earnings + revenue history from yfinance.
    Returns dict with eps_history, sales_history, or None on failure.
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)

        # Quarterly income statement — columns are dates (newest first), rows are line items
        stmt = stock.quarterly_income_stmt
        if stmt is None or stmt.empty:
            log.debug(f"{ticker}: no quarterly income statement")
            return None

        # Revenue — sort_index() gives chronological (oldest first)
        revenue_history = []
        if "Total Revenue" in stmt.index:
            revenue_history = stmt.loc["Total Revenue"].dropna().sort_index().tolist()

        # EPS — Basic EPS preferred, fall back to Diluted EPS
        eps_values = []
        for eps_key in ("Basic EPS", "Diluted EPS"):
            if eps_key in stmt.index:
                eps_values = stmt.loc[eps_key].dropna().sort_index().tolist()
                break

        if len(eps_values) < 4:
            log.debug(f"{ticker}: only {len(eps_values)} quarters of EPS data")
            return None

        return {
            "eps_history": eps_values,
            "revenue_history": revenue_history,
        }
    except Exception as e:
        log.warning(f"{ticker}: yfinance earnings fetch failed — {e}")
        return None


def compute_revenue_growth(revenue_history: list) -> list:
    """Compute quarter-over-quarter revenue growth percentages."""
    if len(revenue_history) < 2:
        return []
    growth = []
    for i in range(1, len(revenue_history)):
        prev = revenue_history[i - 1]
        curr = revenue_history[i]
        if prev and prev != 0:
            growth.append(round((curr / prev - 1) * 100, 1))
        else:
            growth.append(0.0)
    return growth


def is_character_change_deep(ticker: str, sma200_pct: float | None,
                              rvol: float | None) -> dict:
    """
    Full 4-condition character change check using yfinance quarterly data.

    Conditions:
    1. 3+ consecutive quarters of improving EPS
    2. Sales growth accelerating last 2 quarters (both positive)
    3. Price cleared 200-day MA (SMA200% between 0 and ~60%)
    4. Volume confirming (RVol >= 2.0)

    Returns dict with 'is_cc', 'is_cc_watch', 'details'.
    """
    result = {
        "is_cc": False,
        "is_cc_watch": False,
        "eps_trend": [],
        "sales_trend": [],
        "conditions_met": [],
        "conditions_failed": [],
        "note": "",
    }

    # Condition 3: 200-day MA cleared (SMA200% > 0 means above it)
    if sma200_pct is None:
        result["conditions_failed"].append("200d MA data unavailable")
        return result
    ma_cleared = 0 < sma200_pct <= 60
    if ma_cleared:
        result["conditions_met"].append(f"200d MA cleared (SMA200: +{sma200_pct:.1f}%)")
    else:
        result["conditions_failed"].append(f"200d MA not cleared (SMA200: {sma200_pct:+.1f}%)")

    # Condition 4: Volume confirming
    rvol_val = float(rvol) if rvol is not None and pd.notna(rvol) else 0
    if rvol_val >= 2.0:
        result["conditions_met"].append(f"RVol confirming ({rvol_val:.1f}x)")
    else:
        result["conditions_failed"].append(f"RVol low ({rvol_val:.1f}x, need 2.0x)")

    # Fetch quarterly earnings
    earnings = fetch_earnings_history(ticker)
    if not earnings:
        result["conditions_failed"].append("Earnings data unavailable")
        return result

    eps_values = earnings["eps_history"]
    result["eps_trend"] = [round(e, 2) for e in eps_values[-6:]]

    # Condition 1: 3+ consecutive quarters of improving EPS
    recent_eps = eps_values[-4:]  # last 4 quarters
    eps_improving = all(
        recent_eps[i] > recent_eps[i - 1]
        for i in range(1, len(recent_eps))
    )
    if eps_improving:
        result["conditions_met"].append(
            f"EPS improving 3+ qtrs ({' → '.join(f'{e:.2f}' for e in recent_eps)})"
        )
    else:
        result["conditions_failed"].append(
            f"EPS not consistently improving ({' → '.join(f'{e:.2f}' for e in recent_eps)})"
        )

    # Condition 2: Sales growth accelerating last 2 quarters
    rev_growth = compute_revenue_growth(earnings["revenue_history"])
    result["sales_trend"] = [round(g, 1) for g in rev_growth[-4:]]
    if len(rev_growth) >= 2:
        recent_sales = rev_growth[-2:]
        sales_accelerating = recent_sales[-1] > recent_sales[-2] and recent_sales[-2] > 0
        if sales_accelerating:
            result["conditions_met"].append(
                f"Sales accelerating ({recent_sales[-2]:+.1f}% → {recent_sales[-1]:+.1f}%)"
            )
        else:
            result["conditions_failed"].append(
                f"Sales not accelerating ({recent_sales[-2]:+.1f}% → {recent_sales[-1]:+.1f}%)"
            )
    else:
        result["conditions_failed"].append("Insufficient revenue history")

    # Evaluate: all 4 = CC, 3/4 with sales still growing = CC_WATCH
    all_passed = len(result["conditions_failed"]) == 0
    if all_passed:
        result["is_cc"] = True
        return result

    # CC_WATCH: EPS improving + MA cleared + volume — but sales dip
    if (eps_improving and ma_cleared and rvol_val >= 2.0
            and len(rev_growth) >= 2 and rev_growth[-1] > 0):
        result["is_cc_watch"] = True
        result["note"] = "Sales growth positive but not accelerating — watch for confirmation"

    return result


def run_character_change_checks(persistence_df: pd.DataFrame,
                                 combined_df: pd.DataFrame,
                                 max_candidates: int = 25) -> dict:
    """
    Run deep character change checks on top candidates.
    Returns {ticker: cc_result_dict}.
    """
    import time as _time
    candidates = persistence_df.head(max_candidates)
    cc_results = {}

    for _, row in candidates.iterrows():
        ticker = row["Ticker"]

        # Get SMA200% and RVol from the most recent daily data
        ticker_rows = combined_df[combined_df["Ticker"] == ticker]
        if ticker_rows.empty:
            continue
        latest = ticker_rows.iloc[-1]
        sma200_pct = latest.get("SMA200%")
        rvol = latest.get("Rel Volume")

        if pd.notna(sma200_pct):
            sma200_pct = float(sma200_pct)
        else:
            sma200_pct = None

        # Quick pre-filter: skip if SMA200% clearly wrong direction
        if sma200_pct is not None and sma200_pct < -10:
            continue

        log.info(f"CC check: {ticker} (SMA200: {sma200_pct}, RVol: {rvol})")
        result = is_character_change_deep(ticker, sma200_pct, rvol)
        if result["is_cc"] or result["is_cc_watch"]:
            cc_results[ticker] = result
            status = "CC" if result["is_cc"] else "CC_WATCH"
            log.info(f"  → {status}: {', '.join(result['conditions_met'])}")

        _time.sleep(0.5)  # Rate limit yfinance

    return cc_results


# ----------------------------
# Part 1b: Score & Rank
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


def load_daily_quality(data_dir: str, lookback_days: int = 7) -> dict:
    """
    Load daily quality JSON files and merge into a single dict.
    For tickers appearing on multiple days, keep the most recent data.
    Returns {ticker: {q_rank, stage, stage_label, section}}.
    """
    today = datetime.date.today()
    quality = {}

    for i in range(lookback_days):
        date = today - datetime.timedelta(days=i)
        path = os.path.join(data_dir, f"daily_quality_{date.strftime('%Y-%m-%d')}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    day_data = json.load(f)
                # Older days don't overwrite newer data
                for ticker, meta in day_data.items():
                    if ticker not in quality:
                        quality[ticker] = meta
                log.info(f"Loaded daily quality: {path} ({len(day_data)} tickers)")
            except Exception as e:
                log.warning(f"Could not load {path}: {e}")

    log.info(f"Daily quality data: {len(quality)} unique tickers from up to {lookback_days} days")
    return quality


def _compute_quality_modifier(q_rank: int, stage: int) -> int:
    """
    Compute signal score modifier based on daily chart grid quality data.
    Stage 2 + high Q-rank = strong bonus. Stage 4 = heavy penalty.
    """
    if stage == 2:
        if q_rank >= 60:
            return 30
        elif q_rank >= 40:
            return 15
        return 0
    elif stage == 0:  # Transitional
        if q_rank >= 60:
            return 10
        elif q_rank >= 40:
            return 0
        return -20
    elif stage == 1:
        return -10
    elif stage == 4:
        return -40
    elif stage == 3:
        return -20
    return 0


def _detect_signals(screeners_hit: set, max_appearances: int) -> dict:
    """
    Detect setup signals for a ticker based on which screeners fired.
    Returns a dict of active signals and their bonus scores.

    Each signal explains *why* the ticker ranks where it does.
    All signals compete in the same unified score — no separate buckets.

    EP (Episodic Pivot) +30
      Gap/surge screener + 52 Week High + multi-screen same day.
      Stockbee/Qullamaggie: catalyst move, new high confirmation, volume implied.
      Low persistence names like PL can rank #1-3 if EP criteria are strong.

    IPO Lifecycle +15
      From IPO screener — recently public or re-listed.
      Different evaluation rules: no long base expected, evaluate on narrative.
      VG, KRMN type names.

    Multi-screen same day +20
      3+ screeners fired on a single day — institutional conviction signal.
      Separate from EP because a stock can be multi-screen without being an EP.

    52 Week High +10
      Making new highs = price leadership. Simple but powerful.

    Character Change (CHAR) is detected separately in build_persistence_scores (+25).
      200d gain > 50% + RVol > 2.5x + Week 20%+ Gain screener on the same day.
      Stock that was dead/ignored is now surging with institutional volume.
    """
    has_gap   = "10% Change" in screeners_hit or "Week 20%+ Gain" in screeners_hit
    has_high  = "52 Week High" in screeners_hit
    has_ipo   = "IPO" in screeners_hit
    has_multi = max_appearances >= 3
    is_ep     = has_gap and has_high and max_appearances >= 2

    signals = {}
    bonuses = 0

    if is_ep:
        signals["EP"]    = True
        bonuses         += 30
    if has_ipo:
        signals["IPO"]   = True
        bonuses         += 15
    if has_multi:
        signals["MULTI"] = True
        bonuses         += 20
    if has_high and not is_ep:
        signals["HIGH"]  = True
        bonuses         += 10

    signals["bonuses"] = bonuses
    return signals


def build_persistence_scores(combined_df: pd.DataFrame, dates_found: list,
                             daily_quality: dict = None,
                             cc_results: dict = None) -> pd.DataFrame:
    if combined_df.empty:
        return pd.DataFrame()

    if daily_quality is None:
        daily_quality = {}
    if cc_results is None:
        cc_results = {}

    records = defaultdict(lambda: {
        "days_seen": 0, "dates": [], "max_atr": None, "max_eps": None,
        "max_appearances": 0, "screeners_hit": set(),
        "sector": "", "industry": "", "company": "", "market_cap": "",
        "has_char_change": False,
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

        # Character change detection: 200d gain > 50%, RVol > 2.5x, Week 20%+ Gain
        sma200 = row.get("SMA200%")
        rvol = row.get("Rel Volume")
        screeners_str = str(screeners)
        if (pd.notna(sma200) and float(sma200) > 50 and
                pd.notna(rvol) and float(rvol) > 2.5 and
                "Week 20%+ Gain" in screeners_str):
            r["has_char_change"] = True

        for field in ("sector", "industry", "company", "market_cap"):
            col = {"sector": "Sector", "industry": "Industry",
                   "company": "Company", "market_cap": "Market Cap"}[field]
            if not r[field] and pd.notna(row.get(col, "")):
                r[field] = row.get(col, "")

    rows = []
    total_days = len(dates_found)
    for ticker, r in records.items():
        # Base conviction: persistence + screener diversity + multi-screen bonus
        screener_diversity = len(r["screeners_hit"])
        base_score = (r["days_seen"] / max(total_days, 1)) * 100
        base_score += screener_diversity * 10
        if r["max_appearances"] >= 2:
            base_score += 20

        # Signal detection — adds bonuses that can push EP/IPO names into top 5
        signals = _detect_signals(r["screeners_hit"], r["max_appearances"])
        signal_score = round(base_score + signals["bonuses"], 1)

        # Character change: deep check (+35) takes priority over simple heuristic (+25)
        cc = cc_results.get(ticker)
        if cc and cc.get("is_cc"):
            signals["CHAR"] = True
            signals["CC_DEEP"] = True
            signal_score += 35
        elif cc and cc.get("is_cc_watch"):
            signals["CHAR"] = True
            signals["CC_WATCH"] = True
            signal_score += 25
        elif r["has_char_change"]:
            # Fallback: simple heuristic (200d gain >50%, RVol >2.5x, Week 20%+ Gain)
            signals["CHAR"] = True
            signal_score += 25

        # Quality modifier from daily chart grid data
        dq = daily_quality.get(ticker, {})
        q_rank = dq.get("q_rank", 0)
        stage = dq.get("stage", 0)
        stage_label = dq.get("stage_label", "—")
        section = dq.get("section", "")
        quality_mod = _compute_quality_modifier(q_rank, stage) if dq else 0
        signal_score = round(signal_score + quality_mod, 1)
        is_watch = section in ("watch", "excluded")

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
            "Base Score":      round(base_score, 1),
            "Signal Score":    signal_score,
            "Q Rank":          q_rank if dq else None,
            "Stage":           stage_label,
            "Quality Mod":     quality_mod,
            "Watch":           is_watch,
            # Individual signal flags for badges
            "EP":              signals.get("EP",    False),
            "IPO":             signals.get("IPO",   False),
            "MULTI":           signals.get("MULTI", False),
            "HIGH":            signals.get("HIGH",  False),
            "CHAR":            signals.get("CHAR",  False),
            "CC_DEEP":         signals.get("CC_DEEP", False),
            "CC_WATCH":        signals.get("CC_WATCH", False),
        })

    # Sort by unified signal score — EP/IPO names compete fairly for top 5
    return pd.DataFrame(rows).sort_values("Signal Score", ascending=False)


def select_leaderboard(persistence_df: pd.DataFrame) -> pd.DataFrame:
    """
    Recurring names worth monitoring.
    Show top half by signal score, hard cap at 30.
    """
    if persistence_df.empty:
        return persistence_df
    max_score = persistence_df["Signal Score"].max()
    threshold = max_score / 2
    return persistence_df[persistence_df["Signal Score"] >= threshold].head(30)


def _build_badges(row) -> str:
    """Build badge HTML explaining why a ticker ranks where it does."""
    badges = ""
    if row.get("EP"):
        badges += "<span class='badge-ep'>⚡ EP</span>"
    if row.get("IPO"):
        badges += "<span class='badge-ipo'>🚀 IPO</span>"
    if row.get("MULTI"):
        badges += "<span class='badge-multi'>x3 screens</span>"
    if row.get("HIGH") and not row.get("EP"):
        badges += "<span class='badge-high'>52w High</span>"
    if row.get("CC_DEEP"):
        badges += "<span class='badge-cc'>⚡ CC</span>"
    elif row.get("CC_WATCH"):
        badges += "<span class='badge-cc-watch'>⚡ CC Watch</span>"
    elif row.get("CHAR"):
        badges += "<span class='badge-char'>🔄 Char Change</span>"
    return badges


# ----------------------------
# Part 2: Macro
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
                "perf_prev_month": None,  # populated by fetch_macro_prev_month via yfinance
            }
        except Exception as e:
            log.warning(f"Macro fetch failed for {symbol}: {e}")

    # Enrich with prior-30-day return via yfinance so the Month cell can show
    # current-month perf alongside the prior-month perf in brackets.
    fetch_macro_prev_month(macro_data)

    return macro_data


def fetch_macro_prev_month(macro_data: dict) -> None:
    """
    For each macro symbol, compute the prior-30-day return (the ~30 days before
    the current Perf Month window) using yfinance daily closes. Mutates
    macro_data in place — sets perf_prev_month as a formatted percent string
    (e.g. "-5.2%") or leaves it None on failure.
    """
    if not macro_data:
        return
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        log.warning("Prev-month enrichment skipped — yfinance/pandas unavailable: %s", e)
        return

    symbols = list(macro_data.keys())
    try:
        # period="4mo" gives enough calendar days on either side of the 30/60d marks.
        hist = yf.download(symbols, period="4mo", interval="1d",
                           auto_adjust=True, progress=False, group_by="ticker",
                           threads=True)
    except Exception as e:
        log.warning("Prev-month yfinance batch download failed: %s", e)
        return

    for symbol in symbols:
        try:
            if len(symbols) == 1:
                closes = hist["Close"] if "Close" in hist else None
            else:
                # group_by="ticker" returns multi-level columns keyed by ticker
                if symbol not in hist.columns.get_level_values(0):
                    continue
                closes = hist[symbol]["Close"]
            if closes is None or closes.empty:
                continue
            closes = closes.dropna()
            # Use trading-day offsets (~21 per month) rather than calendar days
            if len(closes) < 45:
                continue
            close_now  = float(closes.iloc[-21]) if len(closes) > 21 else float(closes.iloc[0])
            close_prev = float(closes.iloc[-42]) if len(closes) > 42 else float(closes.iloc[0])
            if close_prev <= 0:
                continue
            prev_ret = (close_now / close_prev - 1.0) * 100
            macro_data[symbol]["perf_prev_month"] = f"{prev_ret:+.1f}%"
        except Exception as e:
            log.debug("Prev-month calc failed for %s: %s", symbol, e)


def _color(val_str: str) -> str:
    try:
        return "pos" if float(val_str.replace("%", "").strip()) > 0 else "neg"
    except Exception:
        return ""


def _heat(val_str: str) -> str:
    """Intensity-binned heat-map class for macro cells. Strong >=2%, mild <2%."""
    try:
        v = float(val_str.replace("%", "").strip())
    except Exception:
        return "heat-zero"
    if v >= 2:   return "heat-pos-strong"
    if v > 0:    return "heat-pos"
    if v == 0:   return "heat-zero"
    if v > -2:   return "heat-neg"
    return "heat-neg-strong"

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
        base = "Extreme Fear"; ctx = "historically a better buying zone — confirm with breadth first"
    elif score <= 45:
        base = "Fear"; ctx = "caution warranted, momentum stocks face headwinds"
    elif score <= 55:
        base = "Neutral"; ctx = "no strong directional bias"
    elif score <= 75:
        base = "Greed"; ctx = "momentum favourable, watch for overextension"
    else:
        base = "Extreme Greed"; ctx = "late-stage momentum — tighten stops"
    return f"{base} ({score}). {magnitude}pt {direction} vs last month. {ctx}."


# ----------------------------
# Part 3: HTML Report
# ----------------------------

def generate_weekly_html(persistence_df: pd.DataFrame, macro_data: dict,
                          dates_found: list, ai_brief: str,
                          fng_data: dict = None, crypto_data: dict = None,
                          cc_results: dict = None) -> str:
    today      = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    out_html   = os.path.join(DATA_DIR, f"finviz_weekly_{today}.html")
    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else today

    # --- TOP 5: unified signal score ranking (Watch List excluded) ---
    actionable  = persistence_df[~persistence_df["Watch"]].copy()
    top5         = actionable.head(5)
    rank_colors  = ["#facc15", "#94a3b8", "#b45309", "#4f6ef7", "#4f6ef7"]
    focus_cards  = ""

    for i, (_, row) in enumerate(top5.iterrows()):
        rank_color = rank_colors[i] if i < len(rank_colors) else "#334155"
        days       = row["Days Seen"]
        total      = row["Total Days"]
        atr        = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps        = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        q_rank     = f"Q{int(row['Q Rank'])}" if pd.notna(row.get("Q Rank")) else "Q?"
        stage      = row.get("Stage", "—")
        badges     = _build_badges(row)
        chart_url  = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        fv_url     = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"
        base_sc    = row["Base Score"]
        sig_sc     = row["Signal Score"]
        bonus      = int(sig_sc - base_sc)
        score_note = f"score {sig_sc:.0f}" + (f" (+{bonus} signal)" if bonus > 0 else "")

        focus_cards += (
            "<div class='focus-card'>"
            f"<div class='focus-rank' style='color:{rank_color}'>#{i+1}</div>"
            "<div class='focus-header'>"
            f"<a href='{fv_url}' target='_blank' class='focus-ticker'>{row['Ticker']}</a>"
            f"<span class='focus-sector'>{row['Sector']}</span>"
            "</div>"
            f"<div class='focus-company'>{row['Company']}</div>"
            f"<div class='focus-badges'>{badges}</div>"
            f"<div class='focus-persist'>{days}/{total} days · {score_note}</div>"
            f"<div class='focus-quality'>{q_rank} · {stage}</div>"
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
        dy_cls = _heat(m["change"])
        wk_cls = _heat(m["perf_week"])
        mo_cls = _heat(m["perf_month"])
        wk_arr = _arrow(m["perf_week"])
        mo_arr = _arrow(m["perf_month"])
        prev_month = m.get("perf_prev_month")
        prev_span  = f" <span class='prev-delta'>(prev {prev_month})</span>" if prev_month else ""
        macro_rows += (
            "<tr>"
            f"<td class='bold'>{symbol}</td>"
            f"<td class='mname'>{m['name']}</td>"
            f"<td class='mono'>{m['price']}</td>"
            f"<td class='mono heat {dy_cls}'>{m['change']}</td>"
            f"<td class='mono heat {wk_cls}'>{wk_arr} {m['perf_week']}</td>"
            f"<td class='mono heat {mo_cls}'>{mo_arr} {m['perf_month']}{prev_span}</td>"
            "</tr>"
        )

    # --- LEADERBOARD: top half by signal score, max 30 ---
    leaderboard_df   = select_leaderboard(persistence_df)
    max_score        = persistence_df["Signal Score"].max() if not persistence_df.empty else 100
    threshold        = max_score / 2
    leaderboard_rows = ""

    for idx, (_, row) in enumerate(leaderboard_df.iterrows()):
        days      = row["Days Seen"]
        total     = row["Total Days"]
        pct       = int((days / total) * 100) if total > 0 else 0
        bar_color = "#4f6ef7" if pct >= 80 else "#38bdf8" if pct >= 60 else "#64748b"
        atr       = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps       = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "—"
        q_rank    = f"Q{int(row['Q Rank'])}" if pd.notna(row.get("Q Rank")) else "—"
        stage     = row.get("Stage", "—")
        is_watch  = row.get("Watch", False)
        badge_str = ""
        if is_watch: badge_str += "<span class='watch-tag'>[Watch]</span> "
        if row.get("EP"):    badge_str += "⚡"
        if row.get("IPO"):   badge_str += "🚀"
        if row.get("MULTI"): badge_str += "x3"
        if row.get("HIGH") and not row.get("EP"): badge_str += "↑hi"
        if row.get("CC_DEEP"):    badge_str += "⚡CC"
        elif row.get("CC_WATCH"): badge_str += "⚡W"
        elif row.get("CHAR"):     badge_str += "🔄"
        fv_url    = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"
        chart_url = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        row_cls   = "watch-row" if is_watch else ("ep-row" if (row.get("EP") or row.get("IPO")) else "")

        leaderboard_rows += (
            f"<tr class='{row_cls}'>"
            f"<td class='dim'>{idx+1}</td>"
            f"<td><a href='{fv_url}' target='_blank' class='tlink'>{row['Ticker']}</a>"
            f" <span class='lb-signals'>{badge_str}</span></td>"
            f"<td class='company'>{row['Company']}</td>"
            f"<td><span class='sector-pill'>{row['Sector']}</span></td>"
            "<td><div class='bar-wrap'>"
            f"<div class='bar' style='width:{pct}%;background:{bar_color}'></div>"
            f"<span>{days}/{total}d</span></div></td>"
            f"<td class='center bold'>{row['Signal Score']:.0f}</td>"
            f"<td class='center dim'>{row['Base Score']:.0f}</td>"
            f"<td class='center'>{q_rank}</td>"
            f"<td class='center'>{stage}</td>"
            f"<td class='center'>{atr}</td>"
            f"<td class='center'>{eps}</td>"
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

    # --- CHARACTER CHANGE ALERTS ---
    cc_html = ""
    if cc_results is None:
        cc_results = {}
    if cc_results:
        cc_cards = ""
        for ticker, cc in cc_results.items():
            status = "⚡ CONFIRMED" if cc.get("is_cc") else "⚡ WATCH"
            status_cls = "cc-confirmed" if cc.get("is_cc") else "cc-watch"
            # Find ticker's row in persistence_df for context
            ticker_row = persistence_df[persistence_df["Ticker"] == ticker]
            q_rank = "Q?"
            stage = "—"
            if not ticker_row.empty:
                r = ticker_row.iloc[0]
                q_rank = f"Q{int(r['Q Rank'])}" if pd.notna(r.get("Q Rank")) else "Q?"
                stage = r.get("Stage", "—")
            eps_trend = " → ".join(str(e) for e in cc.get("eps_trend", []))
            sales_trend = " → ".join(f"{g:+.1f}%" for g in cc.get("sales_trend", []))
            conditions = "<br>".join(cc.get("conditions_met", []))
            note = f"<div class='cc-note'>{cc['note']}</div>" if cc.get("note") else ""
            fv_url = f"{FINVIZ_BASE}/quote.ashx?t={ticker}"
            cc_cards += (
                f"<div class='cc-card {status_cls}'>"
                f"<div class='cc-header'>"
                f"<a href='{fv_url}' target='_blank' class='cc-ticker'>{ticker}</a>"
                f"<span class='cc-status'>{status}</span>"
                f"<span class='cc-quality'>{q_rank} · {stage}</span>"
                f"</div>"
                f"<div class='cc-trend'><b>EPS:</b> {eps_trend}</div>"
                f"<div class='cc-trend'><b>Sales growth:</b> {sales_trend}</div>"
                f"<div class='cc-conditions'>{conditions}</div>"
                f"{note}"
                f"</div>"
            )
        cc_html = (
            "<h2>⚡ Character Change Alerts</h2>"
            "<p class='lb-note'>Names showing fundamental reversal pattern — "
            "3+ quarters improving EPS + accelerating sales + 200MA cleared + volume confirmation</p>"
            "<div class='cc-grid'>" + cc_cards + "</div>"
        )

    leaderboard_count = len(leaderboard_df)
    leaderboard_html = (
        f"<h2>Recurring Names — signal score &gt; {threshold:.0f} ({leaderboard_count} names)</h2>"
        "<p class='lb-note'>"
        "Ranked by signal score (persistence + bonuses). "
        "⚡ EP = episodic pivot · 🚀 IPO = lifecycle play · x3 = 3+ screeners same day · ↑hi = 52w high · ⚡CC = character change (confirmed) · ⚡W = CC watch · 🔄 = char change (heuristic). "
        "Signal score = base + bonuses. Base score shown in grey."
        "</p>"
        "<div class='lb-actions'>"
        "<button type='button' class='lb-dl' onclick='downloadLeaderboard(\"csv\")'>⬇ CSV</button>"
        "<button type='button' class='lb-dl' onclick='downloadLeaderboard(\"tv\")'>⬇ TradingView list</button>"
        "</div>"
        "<table class='lb-table' id='lb-table'><thead><tr>"
        "<th data-col='0'>#</th>"
        "<th>Ticker</th><th>Company</th><th>Sector</th>"
        "<th data-col='4'>Persistence</th>"
        "<th data-col='5'>Signal</th>"
        "<th data-col='6'>Base</th>"
        "<th data-col='7'>Q</th>"
        "<th>Stage</th>"
        "<th data-col='9'>ATR%</th>"
        "<th data-col='10'>EPS%</th>"
        "<th>Screeners</th><th>Chart</th>"
        "</tr></thead><tbody>" + leaderboard_rows + "</tbody></table>"
    ) if leaderboard_rows else ""

    css = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #f9fafb; color: #111827; padding: 32px; max-width: 1400px; }
h1    { font-size: 1.4rem; font-weight: 700; margin-bottom: 4px; color: #111827; }
h2    { font-size: .78rem; font-weight: 600; color: #6b7280; margin: 28px 0 10px;
        text-transform: uppercase; letter-spacing: .08em;
        border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }
.subtitle { color: #6b7280; font-size: 0.82rem; margin-bottom: 28px; }
.lb-note  { font-size: 0.73rem; color: #6b7280; margin-bottom: 10px; line-height: 1.5; }
.lb-actions { display: flex; gap: 8px; margin-bottom: 12px; }
.lb-dl { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; border-radius: 6px;
         padding: 6px 12px; font-size: 0.75rem; font-weight: 600; cursor: pointer;
         font-family: inherit; transition: background .15s; }
.lb-dl:hover { background: #dbeafe; }
.pos  { color: #16a34a; }
.neg  { color: #dc2626; }
.mono { font-variant-numeric: tabular-nums; }
.bold { font-weight: 700; }
.dim  { color: #9ca3af; }
/* PDF export */
.pdf-btn { position: fixed; bottom: 24px; right: 24px; background: #2563eb; color: #fff;
           border: none; border-radius: 50%; width: 44px; height: 44px; font-size: 1.1rem;
           cursor: pointer; z-index: 999; box-shadow: 0 2px 8px rgba(0,0,0,.2);
           display: flex; align-items: center; justify-content: center; }
.pdf-btn:hover { background: #1d4ed8; }
/* Focus cards */
.focus-grid    { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 14px; margin-bottom: 36px; }
.focus-card    { background: #ffffff; border-radius: 10px; padding: 14px 16px; border: 1px solid #e5e7eb; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
.focus-rank    { font-size: 1.6rem; font-weight: 800; line-height: 1; margin-bottom: 6px; color: #111827; }
.focus-header  { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; flex-wrap: wrap; }
.focus-ticker  { font-size: 1.1rem; font-weight: 700; color: #2563eb; text-decoration: none; }
.focus-ticker:hover { color: #1d4ed8; }
.focus-sector  { font-size: 0.62rem; color: #2563eb; background: #eff6ff; padding: 1px 5px; border-radius: 3px; flex-shrink: 0; }
.focus-company { font-size: 0.67rem; color: #6b7280; margin-bottom: 6px; }
.focus-badges  { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 5px; min-height: 18px; }
.badge-ep      { font-size: 0.64rem; background: #fef3c7; color: #92400e; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.badge-ipo     { font-size: 0.64rem; background: #dcfce7; color: #166534; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.badge-multi   { font-size: 0.64rem; background: #eff6ff; color: #2563eb; padding: 1px 6px; border-radius: 3px; }
.badge-high    { font-size: 0.64rem; background: #e0f2fe; color: #0369a1; padding: 1px 6px; border-radius: 3px; }
.badge-char    { font-size: 0.64rem; background: #faf5ff; color: #7c3aed; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.badge-cc      { font-size: 0.64rem; background: #fef3c7; color: #92400e; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.badge-cc-watch { font-size: 0.64rem; background: #fff7ed; color: #c2410c; padding: 1px 6px; border-radius: 3px; font-weight: 700; }
.focus-persist { font-size: 0.71rem; color: #6b7280; margin-bottom: 2px; }
.focus-quality { font-size: 0.72rem; color: #2563eb; font-weight: 600; margin-bottom: 2px; }
.focus-meta    { font-size: 0.69rem; color: #6b7280; margin-bottom: 5px; }
.focus-screeners { font-size: 0.63rem; color: #9ca3af; margin-bottom: 9px; line-height: 1.4; }
.focus-chart   { width: 100%; border-radius: 6px; display: block; }
/* Macro */
.macro-table    { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-bottom: 8px; }
.macro-table th { text-align: left; padding: 7px 10px; color: #6b7280; font-weight: 500;
                  border-bottom: 1px solid #e5e7eb; text-transform: uppercase; font-size: 0.66rem; letter-spacing: .05em; }
.macro-table td { padding: 7px 10px; border-bottom: 1px solid #f3f4f6; color: #111827; }
.macro-table tr:hover td { background: #f9fafb; }
.mname { color: #6b7280; font-size: 0.75rem; }
.prev-delta { font-size: 0.7rem; color: #6b7280; font-weight: 500; margin-left: 4px; }
/* Macro heat-map cells */
.macro-table td.heat { border-radius: 4px; font-weight: 600; }
.heat-pos-strong { background: #bbf7d0; color: #166534; }
.heat-pos        { background: #dcfce7; color: #15803d; }
.heat-zero       { color: #6b7280; }
.heat-neg        { background: #fee2e2; color: #b91c1c; }
.heat-neg-strong { background: #fecaca; color: #991b1b; }
/* Character Change Alerts */
.cc-grid       { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px,1fr)); gap: 12px; margin-bottom: 24px; }
.cc-card       { background: #ffffff; border-radius: 10px; padding: 14px 16px; border: 1px solid #e5e7eb; }
.cc-card.cc-confirmed { border-left: 3px solid #d97706; }
.cc-card.cc-watch     { border-left: 3px solid #c2410c; }
.cc-header     { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.cc-ticker     { font-size: 1.05rem; font-weight: 700; color: #2563eb; text-decoration: none; }
.cc-status     { font-size: 0.7rem; font-weight: 700; color: #d97706; }
.cc-quality    { font-size: 0.68rem; color: #6b7280; }
.cc-trend      { font-size: 0.75rem; color: #374151; margin-bottom: 4px; line-height: 1.5; }
.cc-conditions { font-size: 0.7rem; color: #16a34a; margin-top: 6px; line-height: 1.6; }
.cc-note       { font-size: 0.7rem; color: #c2410c; margin-top: 4px; font-style: italic; }
/* Leaderboard */
.lb-table    { width: 100%; border-collapse: collapse; font-size: 0.79rem; }
.lb-table th { text-align: left; padding: 6px 9px; color: #6b7280; font-weight: 500;
               border-bottom: 1px solid #e5e7eb; text-transform: uppercase; font-size: 0.64rem; letter-spacing: .05em; }
.lb-table th[data-col] { cursor: pointer; user-select: none; }
.lb-table th[data-col]:hover { color: #111827; }
.lb-table th[data-col].sort-asc::after  { content: ' ▲'; font-size: 0.55rem; }
.lb-table th[data-col].sort-desc::after { content: ' ▼'; font-size: 0.55rem; }
.lb-table td { padding: 7px 9px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; color: #111827; }
.lb-table tr:hover td { background: #f9fafb; }
.lb-table tr.ep-row td { background: #fffbeb; }
.lb-table tr.ep-row:hover td { background: #fef3c7; }
.lb-table tr.watch-row td { background: #fafafa; opacity: 0.7; }
.lb-table tr.watch-row:hover td { background: #f3f4f6; opacity: 1; }
.watch-tag { font-size: 0.6rem; background: #fee2e2; color: #dc2626; padding: 1px 5px; border-radius: 3px; font-weight: 600; }
.tlink       { color: #2563eb; font-weight: 700; text-decoration: none; }
.tlink:hover { color: #1d4ed8; }
.chart-link  { color: #2563eb; font-size: 0.69rem; text-decoration: none; }
.company     { color: #6b7280; font-size: 0.72rem; }
.sector-pill { background: #eff6ff; color: #2563eb; font-size: 0.62rem; padding: 2px 5px; border-radius: 3px; white-space: nowrap; }
.screeners   { color: #9ca3af; font-size: 0.66rem; }
.lb-signals  { font-size: 0.65rem; color: #c2410c; }
.bar-wrap    { display: flex; align-items: center; gap: 7px; }
.bar-wrap span { font-size: 0.69rem; color: #6b7280; white-space: nowrap; }
.bar         { height: 5px; border-radius: 3px; min-width: 2px; }
.center      { text-align: center; }
/* AI brief */
.ai-brief   { background: #eff6ff; border-left: 3px solid #2563eb; border-radius: 0 8px 8px 0;
              padding: 16px 20px; margin-bottom: 8px; }
.ai-brief p { line-height: 1.75; color: #1e3a5f; font-size: 0.87rem; margin-bottom: 10px; }
.ai-brief p:last-child { margin-bottom: 0; }
/* Crypto */
.crypto-bar  { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.crypto-card { background: #ffffff; border-radius: 8px; padding: 13px 16px; min-width: 170px; border: 1px solid #e5e7eb; }
.cname  { font-size: 0.67rem; color: #6b7280; margin-bottom: 3px; }
.cprice { font-size: 1.05rem; font-weight: 700; margin-bottom: 5px; color: #111827; }
.cchanges { display: flex; gap: 10px; font-size: 0.75rem; margin-bottom: 4px; }
.cmcap  { font-size: 0.65rem; color: #9ca3af; }
/* F&G */
.fng-bar    { background: #ffffff; border-radius: 8px; padding: 14px 18px; margin-bottom: 8px;
              display: flex; flex-direction: column; gap: 7px; border: 1px solid #e5e7eb; }
.fng-label  { font-size: 0.63rem; color: #6b7280; text-transform: uppercase; letter-spacing: .06em; }
.fng-score  { font-size: 1.35rem; font-weight: 700; color: #111827; }
.fng-rating { font-size: 0.73rem; color: #6b7280; }
.fng-ctx    { font-size: 0.78rem; color: #374151; line-height: 1.5; }
.fng-hist   { display: flex; gap: 16px; font-size: 0.71rem; color: #6b7280; flex-wrap: wrap; }
@media print {
  .pdf-btn, .lb-actions { display: none; }
  body { padding: 0; background: #fff; }
  .focus-card, .cc-card { break-inside: avoid; }
  .lb-table tr { break-inside: avoid; }
  /* Preserve heat-map cell backgrounds in printed PDF */
  .heat, .heat-pos, .heat-pos-strong, .heat-neg, .heat-neg-strong {
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }
}
/* Print-color-adjust also needs to be declared outside @media print
   so Chrome/Safari respect it on the screen-origin style rules. */
.heat, .lb-table tr.ep-row td, .lb-table tr.watch-row td {
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
"""

    html = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        f"<title>Finviz Weekly — {today}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        "<button class='pdf-btn' onclick='window.print()' title='Export PDF'>⬇</button>"
        "<h1>Finviz Weekly Review</h1>"
        f"<p class='subtitle'>{week_range} · {len(persistence_df)} tickers scanned · {len(dates_found)} trading days</p>"
        + crypto_html
        + fng_html
        + ai_html
        + "<h2>Top 5 This Week</h2>"
        + "<div class='focus-grid'>" + focus_cards + "</div>"
        + macro_html
        + cc_html
        + leaderboard_html
        + """<script>
(function(){
  var tbl = document.getElementById('lb-table');
  if (!tbl) return;
  var asc = {};
  tbl.querySelectorAll('th[data-col]').forEach(function(th){
    th.addEventListener('click', function(){
      var col = parseInt(th.getAttribute('data-col'));
      var dir = asc[col] = !asc[col];
      tbl.querySelectorAll('th[data-col]').forEach(function(h){ h.classList.remove('sort-asc','sort-desc'); });
      th.classList.add(dir ? 'sort-asc' : 'sort-desc');
      var tbody = tbl.querySelector('tbody');
      var rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort(function(a, b){
        var av = a.cells[col] ? a.cells[col].textContent.trim() : '';
        var bv = b.cells[col] ? b.cells[col].textContent.trim() : '';
        var an = parseFloat(av.split('/')[0].replace(/[^0-9.\-]/g,'')), bn = parseFloat(bv.split('/')[0].replace(/[^0-9.\-]/g,''));
        if (!isNaN(an) && !isNaN(bn)) return dir ? an - bn : bn - an;
        return dir ? av.localeCompare(bv) : bv.localeCompare(av);
      });
      rows.forEach(function(r){ tbody.appendChild(r); });
    });
  });
})();

function downloadLeaderboard(kind){
  var tbl = document.getElementById('lb-table');
  if (!tbl) return;
  var today = new Date().toISOString().slice(0,10);
  var rows = Array.from(tbl.querySelectorAll('tbody tr'));
  var tickers = rows.map(function(r){
    // Ticker anchor has class .tlink — take only that, never the badge span
    var a = r.querySelector('td:nth-child(2) a.tlink');
    return a ? a.textContent.trim() : '';
  }).filter(Boolean);
  var blob, filename;
  if (kind === 'tv') {
    // TradingView import format: one ticker per line
    blob = new Blob([tickers.join('\\n')], {type: 'text/plain'});
    filename = 'tv_recurring_' + today + '.txt';
  } else {
    // CSV with core columns
    var headers = ['Ticker','Company','Sector','Persistence','Signal','Base','Q','Stage','ATR%','EPS%','Screeners'];
    var csvRows = [headers.join(',')];
    rows.forEach(function(r){
      var cells = Array.from(r.querySelectorAll('td'));
      if (cells.length < 11) return;
      var tickerA = cells[1].querySelector('a.tlink');
      var tickerText = tickerA ? tickerA.textContent.trim() : cells[1].textContent.trim();
      var vals = [tickerText].concat([2,3,4,5,6,7,8,9,10,11].map(function(i){
        return cells[i] ? cells[i].textContent.trim().replace(/\\s+/g,' ') : '';
      }));
      csvRows.push(vals.map(function(v){ return '"' + v.replace(/"/g,'""') + '"'; }).join(','));
    });
    blob = new Blob([csvRows.join('\\n')], {type: 'text/csv'});
    filename = 'recurring_names_' + today + '.csv';
  }
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(url); a.remove(); }, 100);
}
</script>"""
        + "</body></html>"
    )

    with open(out_html, "w") as f:
        f.write(html)
    return out_html


# ----------------------------
# Part 4: AI Brief
# ----------------------------

# ----------------------------
# Part 4a: Agent 2 — Catalyst Research
# ----------------------------

def research_catalysts(persistence_df: pd.DataFrame) -> dict:
    """
    Agent 2: For each of the top 3 tickers by signal score, call Claude API
    with web_search tool to find real catalysts that explain the screener activity.
    Returns {ticker: research_summary_string}.
    """
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping catalyst research.")
        return {}

    top3 = persistence_df.head(3)
    research = {}

    for _, row in top3.iterrows():
        ticker = row["Ticker"]
        sector = row.get("Sector", "")
        industry = row.get("Industry", "")
        sig_tags = []
        if row.get("EP"):    sig_tags.append("episodic pivot (gap + new high)")
        if row.get("IPO"):   sig_tags.append("IPO lifecycle")
        if row.get("MULTI"): sig_tags.append("3+ screeners same day")
        if row.get("HIGH") and not row.get("EP"): sig_tags.append("52w high")
        signal_ctx = (" Signals: " + ", ".join(sig_tags) + ".") if sig_tags else ""

        # Quality context from daily chart grid
        q_rank = row.get("Q Rank")
        stage_label = row.get("Stage", "—")
        days_seen = row.get("Days Seen", "?")
        total_days = row.get("Total Days", "?")
        is_watch = row.get("Watch", False)
        is_char = row.get("CHAR", False)
        quality_ctx = ""
        if pd.notna(q_rank) and q_rank:
            category = "WATCH LIST — not actionable" if is_watch else "ACTIONABLE"
            quality_ctx = (
                f"\nQ-RANK: {int(q_rank)} · STAGE: {stage_label} · CATEGORY: {category}"
                f" · PERSISTENCE: {days_seen}/{total_days} days"
            )
            if row.get("CC_DEEP"):
                quality_ctx += " · CHARACTER CHANGE CONFIRMED (fundamental reversal: 3+ qtrs improving EPS + sales accelerating + 200MA cleared + volume)"
            elif row.get("CC_WATCH"):
                quality_ctx += " · CHARACTER CHANGE WATCH (EPS improving + volume confirming — sales need confirmation)"
            elif is_char:
                quality_ctx += " · CHARACTER CHANGE (200d gain >50%, RVol >2.5x)"

        prompt = (
            f"Research {ticker} ({sector} / {industry}) for a momentum trader weekly review.{signal_ctx}{quality_ctx}\n"
            f"Find: recent earnings beats or misses, analyst upgrades/downgrades, "
            f"sector tailwinds, any catalyst in the past 2 weeks that explains "
            f"why this stock appeared in momentum screeners all week.\n"
            f"Be specific. 3-4 sentences max. No fluff."
        )

        for attempt in range(3):
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
                        "max_tokens": 600,
                        "tools": [
                            {
                                "type": "web_search_20250305",
                                "name": "web_search",
                                "max_uses": 3,
                            }
                        ],
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=90,
                )
                if resp.status_code in (429, 529):
                    wait = 30 * (attempt + 1)
                    reason = "rate limited" if resp.status_code == 429 else "overloaded"
                    log.warning(f"Catalyst research {reason} for {ticker}, retrying in {wait}s ({attempt + 1}/3)...")
                    time.sleep(wait)
                    continue
                if not resp.ok:
                    log.error(f"Catalyst research HTTP {resp.status_code} for {ticker}: {resp.text}")
                    research[ticker] = ""
                    break

                # Extract text blocks from the response (skip tool_use / search_result blocks)
                content_blocks = resp.json().get("content", [])
                text_parts = [b["text"] for b in content_blocks if b.get("type") == "text" and b.get("text")]
                summary = " ".join(text_parts).strip()

                if summary:
                    research[ticker] = summary
                    log.info(f"Catalyst research for {ticker}: {summary[:80]}...")
                else:
                    research[ticker] = ""
                    log.warning(f"No catalyst text returned for {ticker}")
                break

            except Exception as e:
                log.error(f"Catalyst research failed for {ticker}: {e}")
                research[ticker] = ""
                break
        else:
            log.error(f"Catalyst research failed for {ticker} after 3 rate-limit retries.")
            research[ticker] = ""

        # Delay between tickers to stay within rate limits
        time.sleep(15)

    found = sum(1 for v in research.values() if v)
    log.info(f"Catalyst research complete: {found}/{len(research)} tickers with results.")
    return research


# ----------------------------
# Part 4b: Agent 3 — Synthesiser (enhanced AI brief)
# ----------------------------

def generate_weekly_ai_brief(persistence_df: pd.DataFrame, macro_data: dict,
                              dates_found: list, fng_data: dict = None,
                              crypto_data: dict = None, research: dict = None,
                              market_state: dict = None) -> str:
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping AI brief.")
        return ""

    top5         = persistence_df.head(5)
    newline      = "\n"
    ticker_lines = []

    for _, row in top5.iterrows():
        atr      = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "n/a"
        eps      = f"{row['Max EPS%']:.1f}%"  if pd.notna(row.get("Max EPS%"))  else "n/a"
        q_rank   = f"Q{int(row['Q Rank'])}" if pd.notna(row.get("Q Rank")) else "Q?"
        stage    = row.get("Stage", "—")
        sig_tags = []
        if row.get("EP"):    sig_tags.append("EPISODIC PIVOT (gap+high+multi-screen)")
        if row.get("IPO"):   sig_tags.append("IPO LIFECYCLE")
        if row.get("MULTI"): sig_tags.append("3+ SCREENERS SAME DAY")
        if row.get("HIGH") and not row.get("EP"): sig_tags.append("52W HIGH")
        if row.get("CC_DEEP"):
            sig_tags.append("CHARACTER CHANGE CONFIRMED (fundamental reversal: improving EPS + accelerating sales + 200MA cleared)")
        elif row.get("CC_WATCH"):
            sig_tags.append("CHARACTER CHANGE WATCH (EPS improving, sales need confirmation)")
        elif row.get("CHAR"):
            sig_tags.append("CHARACTER CHANGE (200d gain >50%, RVol >2.5x)")
        sig_str = " | " + " + ".join(sig_tags) if sig_tags else ""
        bonus   = int(row["Signal Score"] - row["Base Score"])
        ticker_lines.append(
            f"{row['Ticker']} ({row['Sector']} / {row['Industry']}) "
            f"| {q_rank} · {stage} "
            f"| {row['Days Seen']}/{row['Total Days']} days "
            f"| signal score {row['Signal Score']:.0f} (base {row['Base Score']:.0f}, +{bonus} bonus) "
            f"| ATR {atr} | EPS {eps} "
            f"| screeners: {row['Screeners Hit']}{sig_str}"
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

    # Agent 3 — build catalyst context from Agent 2 research
    research_ctx = ""
    if research:
        catalyst_lines = []
        for ticker, summary in research.items():
            if summary:
                catalyst_lines.append(f"{ticker}: {summary}")
        if catalyst_lines:
            research_ctx = (
                "\n\nCatalyst research (web-sourced for each top name):\n"
                + "\n".join(catalyst_lines) + "\n"
            )

    # Market monitor context
    market_ctx = ""
    if market_state:
        thrust_recent = any_thrust_in_history()
        market_ctx = (
            f"\n\nCURRENT MARKET STATE: {market_state.get('market_state', 'UNKNOWN')}\n"
            f"5-DAY RATIO: {market_state.get('ratio_5day', 'n/a')}\n"
            f"10-DAY RATIO: {market_state.get('ratio_10day', 'n/a')}\n"
            f"T2108: {market_state.get('t2108_equiv', 'n/a')}%\n"
            f"SPY ABOVE 200D MA: {market_state.get('spy_above_200d', 'n/a')}\n"
            f"THRUST LAST 30 DAYS: {thrust_recent}\n"
        )

    market_rules = ""
    if market_state:
        state = market_state.get("market_state", "UNKNOWN")
        if state in ("RED", "BLACKOUT"):
            market_rules = (
                "\n\nMARKET STATE RULES (MUST FOLLOW):\n"
                "- Market state is RED/BLACKOUT — do NOT recommend actionable Monday entries.\n"
                "- Frame the top 5 as 'names to watch when conditions improve'.\n"
                "- Note what breadth signal would trigger re-entry (e.g. 5-day ratio > 1.5, thrust).\n"
            )
        elif state in ("CAUTION",):
            market_rules = (
                "\n\nMARKET STATE RULES (MUST FOLLOW):\n"
                "- Market state is CAUTION — recommend entries but at HALF SIZE only.\n"
                "- Be more selective — only highest conviction setups.\n"
                "- Note that full sizing requires GREEN confirmation.\n"
            )
        elif state in ("GREEN", "THRUST"):
            market_rules = (
                "\n\nMARKET STATE RULES (MUST FOLLOW):\n"
                "- Market state is GREEN/THRUST — full size entries are appropriate.\n"
                "- Recommend entries with specific price levels.\n"
                "- Size guidance: 10-15% for high conviction names.\n"
            )

    prompt = (
        f"You are an experienced momentum trader doing a weekly review ({week_range}).\n\n"
        "Top 5 names this week, ranked by unified signal score "
        "(persistence + EP bonus + IPO bonus + multi-screen bonus + 52w high bonus):\n"
        f"{newline.join(ticker_lines)}\n\n"
        f"Macro: {newline.join(macro_lines) if macro_lines else 'unavailable'}"
        f"{fng_ctx}{crypto_ctx}{research_ctx}{market_ctx}{market_rules}\n\n"
        "Write a sharp weekly brief (4-6 paragraphs):\n"
        "1. For each of the top 5: why it ranks here, and critically — use the catalyst research "
        "to explain the *real-world reason* behind the screener activity (earnings beat, analyst upgrade, "
        "sector rotation, product launch, etc). Don't just say 'appeared in screeners' — say *why*.\n"
        "2. Sector themes and macro backdrop — what's supporting or fighting these names?\n"
        "3. Monday plan — specific names, specific entry triggers to watch.\n\n"
        "QUALITY RULES — follow strictly:\n"
        "- Only recommend names as Monday actionable if they are Stage 2 (Uptrend) or "
        "high-quality Transitional (Q > 60). These are the only names that belong in the Monday plan.\n"
        "- Watch List names (Transitional with low Q, Stage 3/4, or section='watch') are NOT actionable. "
        "If any appear in the data, explicitly flag them as 'watch only — not actionable' and explain why.\n"
        "- Character Change CONFIRMED (⚡ CC) names are the highest-conviction reversal plays — "
        "they have 3+ quarters of improving EPS, accelerating sales, cleared 200MA, and volume confirmation. "
        "These are Stage 1→2 character change trades, not sector trades. Highlight the fundamental turnaround.\n"
        "- Character Change WATCH (⚡ CC Watch) names meet most criteria but sales need confirmation. "
        "Worth mentioning but flag the caveat.\n"
        "- Simple CHAR (🔄) names show price/volume character change but haven't been verified fundamentally.\n"
        "- Flag extended names explicitly.\n"
        "- The Monday plan should only include names where both screener persistence and chart quality agree.\n\n"
        "Be direct. Name names. No disclaimers. Plain paragraphs."
    )

    for attempt in range(3):
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
                    "max_tokens": 2000,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            if resp.status_code in (429, 529):
                wait = 30 * (attempt + 1)
                reason = "rate limited" if resp.status_code == 429 else "overloaded"
                log.warning(f"AI brief {reason} ({resp.status_code}), retrying in {wait}s (attempt {attempt + 1}/3)...")
                time.sleep(wait)
                continue
            if not resp.ok:
                log.error(f"AI brief HTTP {resp.status_code}: {resp.text}")
                return ""
            brief = resp.json()["content"][0]["text"].strip()
            log.info("Weekly AI brief generated.")
            return brief
        except Exception as e:
            log.error(f"Weekly AI brief failed: {e}")
            return ""
    log.warning("AI brief unavailable this week — exhausted 3 retries (429/529).")
    return "AI synthesis unavailable due to API load — see individual catalyst notes below."


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

    week_range   = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"
    actionable   = persistence_df[~persistence_df["Watch"]].copy() if "Watch" in persistence_df.columns else persistence_df
    top5         = actionable.head(5)
    ticker_lines = []

    for _, row in top5.iterrows():
        atr      = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        q_rank   = f"Q{int(row['Q Rank'])}" if pd.notna(row.get("Q Rank")) else "Q?"
        stage    = row.get("Stage", "—")
        tags     = []
        if row.get("EP"):       tags.append("⚡EP")
        if row.get("IPO"):      tags.append("🚀IPO")
        if row.get("MULTI"):    tags.append("x3")
        if row.get("CC_DEEP"):  tags.append("⚡CC")
        elif row.get("CC_WATCH"): tags.append("⚡CCw")
        elif row.get("CHAR"):   tags.append("🔄")
        tag_str  = " " + " ".join(tags) if tags else ""
        ticker_lines.append(
            f"*{row['Ticker']}*{tag_str} · {q_rank} · {stage} · {row['Sector']} · "
            f"{row['Days Seen']}/{row['Total Days']}d · score {row['Signal Score']:.0f}\n"
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
        "*Top 5 this week:*\n"
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
# Part 5b: Auto-Promote to Watchlist
# ----------------------------

def auto_promote_to_watchlist(
    persistence_df: pd.DataFrame,
    watchlist_path: str = "data/watchlist.json",
    min_days: int = 3,
    min_screens: int = 3,
    slack_webhook: str = "",
) -> list:
    """
    Auto-adds tickers to watchlist.json when they meet persistence thresholds
    (appeared min_days+ days AND hit min_screens+ screeners this week).
    Sends a Slack alert listing promoted tickers. Never re-adds existing entries.
    Returns list of promoted tickers.
    """
    try:
        with open(watchlist_path) as f:
            watchlist = json.load(f)
    except FileNotFoundError:
        watchlist = {}

    existing = set(watchlist.get("tickers", []))
    promoted = []
    today_str = datetime.date.today().isoformat()

    for _, row in persistence_df.iterrows():
        ticker   = str(row.get("Ticker", "")).strip()
        days     = int(row.get("Days Seen", row.get("days_seen", 0)) or 0)
        screens_raw = row.get("Screeners Hit", row.get("screens", 0))
        if isinstance(screens_raw, str) and not screens_raw.strip().lstrip("-").isdigit():
            screens = len([s for s in screens_raw.split(",") if s.strip()])
        else:
            screens = int(screens_raw or 0)

        if not ticker or ticker in existing:
            continue
        if days < min_days or screens < min_screens:
            continue

        watchlist.setdefault("tickers", []).append(ticker)
        watchlist.setdefault("auto_promoted", []).append({
            "ticker":    ticker,
            "added":     today_str,
            "days_seen": days,
            "screens":   screens,
        })
        promoted.append((ticker, days, screens))
        existing.add(ticker)

    if promoted:
        os.makedirs(os.path.dirname(watchlist_path) if os.path.dirname(watchlist_path) else ".", exist_ok=True)
        with open(watchlist_path, "w") as f:
            json.dump(watchlist, f, indent=2)
        log.info("Auto-promoted %d tickers to watchlist: %s", len(promoted), [t for t, _, _ in promoted])

        if slack_webhook:
            lines = [f":bookmark: *AUTO-PROMOTED TO WATCHLIST — {today_str}*"]
            for t, d, s in promoted:
                lines.append(f"• *{t}* — {s} screener{'s' if s != 1 else ''} × {d} days")
            lines.append("_Review before trading. System added automatically._")
            try:
                resp = requests.post(slack_webhook, json={
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
                }, timeout=10)
                resp.raise_for_status()
            except Exception as e:
                log.error("Auto-promote Slack alert failed: %s", e)

    return [t for t, _, _ in promoted]


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

    log.info("Loading daily quality data...")
    daily_quality = load_daily_quality(DATA_DIR, lookback_days=7)

    # First pass: score without CC deep check to identify candidates
    persistence_df_initial = build_persistence_scores(combined_df, dates_found, daily_quality)
    log.info(f"{len(persistence_df_initial)} unique tickers scored (initial pass)")

    # Run deep character change checks on top 25 candidates
    log.info("Running character change deep checks (yfinance)...")
    cc_results = run_character_change_checks(persistence_df_initial, combined_df, max_candidates=25)
    if cc_results:
        log.info(f"Character change signals: {list(cc_results.keys())}")
    else:
        log.info("No character change signals detected")

    # Second pass: re-score with CC results applied
    persistence_df = build_persistence_scores(combined_df, dates_found, daily_quality, cc_results)
    log.info(f"{len(persistence_df)} unique tickers scored (with CC bonuses)")

    # Filter Watch List tickers from top 5 — they appear in full leaderboard with [Watch] tag
    actionable_df = persistence_df[~persistence_df["Watch"]].copy()
    watch_count = persistence_df["Watch"].sum()
    if watch_count:
        log.info(f"Filtered {int(watch_count)} Watch List tickers from top 5 selection")

    top5 = actionable_df.head(5)
    log.info("Top 5 by signal score (Watch List excluded):")
    for _, row in top5.iterrows():
        tags = []
        if row["EP"]:    tags.append("EP")
        if row["IPO"]:   tags.append("IPO")
        if row["MULTI"]: tags.append("MULTI")
        if row.get("CC_DEEP"): tags.append("CC")
        elif row.get("CC_WATCH"): tags.append("CC_WATCH")
        elif row.get("CHAR"): tags.append("CHAR")
        q_str = f" Q{row['Q Rank']}" if pd.notna(row.get("Q Rank")) else ""
        stage_str = f" {row['Stage']}" if row.get("Stage") and row["Stage"] != "—" else ""
        tag_str = " [" + ",".join(tags) + "]" if tags else ""
        log.info(f"  {row['Ticker']}{tag_str}{q_str} · {stage_str} · signal={row['Signal Score']} base={row['Base Score']} mod={row['Quality Mod']}")

    os.makedirs(DATA_DIR, exist_ok=True)
    persistence_df.to_csv(
        os.path.join(DATA_DIR, f"finviz_weekly_persistence_{today}.csv"), index=False
    )

    # Auto-promote persistent tickers to watchlist (3+ days × 3+ screeners)
    log.info("Checking for watchlist auto-promotions...")
    promoted = auto_promote_to_watchlist(
        persistence_df,
        watchlist_path=os.path.join(DATA_DIR, "watchlist.json"),
        min_days=3,
        min_screens=3,
        slack_webhook=SLACK_WEBHOOK_URL,
    )
    if promoted:
        log.info("Auto-promoted to watchlist: %s", promoted)
    else:
        log.info("No tickers met auto-promotion threshold this week.")

    log.info("Fetching macro...")
    macro_data  = fetch_macro_snapshot()
    log.info("Fetching Fear & Greed...")
    fng_data    = fetch_fear_and_greed()
    log.info("Fetching crypto...")
    crypto_data = fetch_crypto_data()

    log.info("Running Agent 2 — catalyst research for top 3 (actionable only)...")
    research    = research_catalysts(actionable_df)

    # Load market monitor state for Agent 3
    market_state = load_market_state()
    if market_state:
        log.info(f"Market state: {market_state['market_state']} | 5d ratio: {market_state.get('ratio_5day')}")
    else:
        log.info("Market monitor data not available — Agent 3 will run without market context")

    # Cooldown — Agent 2 exhausts token bucket; give it time to refill before Agent 3
    log.info("Cooldown 45s before Agent 3...")
    time.sleep(45)

    log.info("Running Agent 3 — synthesised AI brief...")
    ai_brief    = generate_weekly_ai_brief(actionable_df, macro_data, dates_found, fng_data, crypto_data, research, market_state)
    weekly_html = generate_weekly_html(persistence_df, macro_data, dates_found, ai_brief, fng_data, crypto_data, cc_results)
    log.info(f"Report: {weekly_html}")

    send_weekly_slack(persistence_df, macro_data, ai_brief, weekly_html, dates_found, fng_data, crypto_data)
    log.info("=== Done ===")
