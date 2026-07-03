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

from agents.utils.etf_rotation_summary import (
    load_etf_rotation,
    summarize_etf_rotation,
    render_sector_setup_html,
    render_sector_setup_slack,
    SECTOR_SETUP_CSS,
)
from agents.utils.weekly_positioning import (
    build_positioning_summary,
    render_positioning_html,
    render_positioning_slack,
    POSITIONING_CSS,
)
from agents.utils.week_ahead_shortlist import (
    select_shortlist_candidates,
    build_shortlist_cards,
    enrich_shortlist_notes_ai,
    render_shortlist_html,
    render_shortlist_slack,
    SHORTLIST_CSS,
)
from agents.utils.book_weekend_review import (
    build_book_review_rows,
    render_book_review_html,
    render_book_review_slack,
    BOOK_REVIEW_CSS,
)

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
        "last_sma50_pct": None,
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

        sma50 = row.get("SMA50%")
        if pd.notna(sma50):
            r["last_sma50_pct"] = float(sma50)

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
            "Last SMA50%":     round(r["last_sma50_pct"], 1) if r["last_sma50_pct"] is not None else None,
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


def select_emerging_candidates(persistence_df: pd.DataFrame, top_n: int = 5,
                               excluded_tickers: set | None = None) -> pd.DataFrame:
    """Names that are *setting up* to become next week's Top 5 — not yet on the
    leaderboard but carrying the characteristics of past winners.

    Filter (predicate, not score-based):
      - NOT in current Top 5 (already broken out)
      - Stage 2 (technical foundation)
      - Q Rank ≥ 70 (passes quality gate)
      - Watch=False (not in watchlist tag)
      - At least one *catalyst* signal: EP, IPO, MULTI, CC_WATCH, or HIGH

    Then rank by an emergence score that rewards setup readiness over history:
      - Q Rank weight (heaviest)        : Q rank
      - CC_WATCH bonus                  : +20 (early character change)
      - EP / IPO bonus                  : +15
      - HIGH (52w high proximity) bonus : +10
      - MULTI screeners bonus           : +8
      - Days seen (lower is fresher)    : -3 per extra day above 1

    The point is: surface names with strong quality + a fresh catalyst, before
    persistence builds them into Top 5.
    """
    if persistence_df.empty:
        return persistence_df

    actionable = persistence_df[~persistence_df.get("Watch", False)].copy()
    top5_tickers = set(actionable.head(5)["Ticker"].tolist()) if not actionable.empty else set()
    excluded = set(excluded_tickers or set()) | top5_tickers

    def _is_stage2(stage_label):
        # Persistence CSV uses Weinstein word labels (Uptrend/Downtrend/Basing/Transitional);
        # Stage 2 = Uptrend. Some other surfaces use "Stage 2 perfect" string.
        if not isinstance(stage_label, str):
            return False
        s = stage_label.lower()
        return ("uptrend" in s
                or "stage 2" in s
                or "stage2" in s
                or s.startswith("2"))

    def _has_catalyst(row):
        # HIGH (52w-high screener) means the stock has already broken out — not a
        # qualifying catalyst for "next on radar". Require a fundamental/structural
        # signal instead: EP, IPO, MULTI, or CC_WATCH.
        return bool(row.get("EP") or row.get("IPO") or row.get("MULTI")
                    or row.get("CC_WATCH"))

    def _not_extended(row):
        sma50 = row.get("Last SMA50%")
        if sma50 is None:
            return True  # no data — let it through
        return float(sma50) <= 20.0

    candidates = actionable[
        (~actionable["Ticker"].isin(excluded))
        & (actionable["Q Rank"].fillna(0) >= 70)
        & actionable["Stage"].apply(_is_stage2)
        & actionable.apply(_has_catalyst, axis=1)
        & actionable.apply(_not_extended, axis=1)
    ].copy()

    if candidates.empty:
        return candidates

    def _emergence_score(row):
        s = float(row.get("Q Rank") or 0)
        if row.get("CC_WATCH"):    s += 20
        if row.get("EP"):          s += 15
        if row.get("IPO"):         s += 15
        # HIGH removed from score: at-52w-high names have already shown the move.
        # Pre-breakout bonus: stock hasn't crossed its 52w high yet — this is the
        # "truly next" setup. Rewards coiling names like NATL over already-at-high ones.
        if not row.get("HIGH"):    s += 8
        if row.get("MULTI"):       s += 8
        days = int(row.get("Days Seen") or 1)
        s -= max(0, days - 1) * 3
        return round(s, 1)

    candidates["Emergence Score"] = candidates.apply(_emergence_score, axis=1)
    return candidates.sort_values("Emergence Score", ascending=False).head(top_n)


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
            snapshot_cells = soup.find_all("td", class_="snapshot-td2")
            if not snapshot_cells:
                continue
            data = {}
            for kc, vc in zip(snapshot_cells[0::2], snapshot_cells[1::2]):
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
                          dates_found: list, strategist_note: str = "",
                          fng_data: dict = None, crypto_data: dict = None,
                          cc_results: dict = None,
                          etf_rotation_summary: dict = None,
                          positioning_html: str = "",
                          shortlist_html: str = "",
                          book_review_html: str = "") -> str:
    today      = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    out_html   = os.path.join(DATA_DIR, f"finviz_weekly_{today}.html")
    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else today

    # --- NEXT ON RADAR: emerging candidates (promoted into Leadership Map) ---
    held_tickers = set()
    try:
        positions_path = os.path.join(DATA_DIR, "positions.json")
        if os.path.exists(positions_path):
            with open(positions_path) as f:
                pdata = json.load(f)
            held_tickers = {p.get("ticker") for p in pdata.get("open_positions", []) if p.get("ticker")}
    except Exception as e:
        log.warning(f"Could not load held positions for emerging filter: {e}")
    emerging_df    = select_emerging_candidates(persistence_df, top_n=5, excluded_tickers=held_tickers)
    emerging_cards = ""
    for i, (_, row) in enumerate(emerging_df.iterrows()):
        days       = int(row.get("Days Seen") or 0)
        total      = int(row.get("Total Days") or 0)
        atr        = f"{row['Max ATR%']:.1f}%" if pd.notna(row.get("Max ATR%")) else "—"
        eps        = f"{row['Max EPS%']:.1f}%" if pd.notna(row.get("Max EPS%")) else "—"
        q_rank     = f"Q{int(row['Q Rank'])}" if pd.notna(row.get("Q Rank")) else "Q?"
        stage      = row.get("Stage", "—")
        em_score   = row.get("Emergence Score", 0)
        badges     = _build_badges(row)
        chart_url  = f"{FINVIZ_BASE}/chart.ashx?t={row['Ticker']}&ty=c&ta=1&p=w&s=m"
        fv_url     = f"{FINVIZ_BASE}/quote.ashx?t={row['Ticker']}"
        emerging_cards += (
            "<div class='focus-card emerging-card'>"
            f"<div class='focus-rank' style='color:#06b6d4'>#{i+1}</div>"
            "<div class='focus-header'>"
            f"<a href='{fv_url}' target='_blank' class='focus-ticker'>{row['Ticker']}</a>"
            f"<span class='focus-sector'>{row['Sector']}</span>"
            "</div>"
            f"<div class='focus-company'>{row['Company']}</div>"
            f"<div class='focus-badges'>{badges}</div>"
            f"<div class='focus-persist'>{days}/{total} days · setup score {em_score:.0f}</div>"
            f"<div class='focus-quality'>{q_rank} · {stage}</div>"
            f"<div class='focus-meta'>ATR {atr} · EPS {eps}</div>"
            f"<div class='focus-screeners'>{row['Screeners Hit']}</div>"
            f"<a href='{chart_url}' target='_blank'>"
            f"<img src='{chart_url}' class='focus-chart' alt='{row['Ticker']}'>"
            "</a>"
            "</div>"
        )
    emerging_html = ""
    if emerging_cards:
        emerging_html = (
            "<h2>🔭 Next on the Radar <span class='h2-sub'>"
            "— setting up, not yet broken out</span></h2>"
            "<p class='section-note'>Stage 2 + Q≥70 with a fresh catalyst (EP / IPO / "
            "CC Watch / 52w-high proximity / multi-screen). Predictive setup, not "
            "post-action coincident.</p>"
            "<div class='focus-grid'>" + emerging_cards + "</div>"
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

    # --- STRATEGIST'S NOTE (demoted — max 3 bullets, no essay) ---
    ai_html = ""
    if strategist_note:
        bullets = [b.strip().lstrip("•-").strip()
                   for b in strategist_note.split("\n") if b.strip()]
        inner = "".join(f"<li>{b}</li>" for b in bullets[:3])
        ai_html = ("<h2>🧠 Strategist's Note</h2>"
                   "<ul class='strat-note'>" + inner + "</ul>")

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
.emerging-card { border-left: 3px solid #06b6d4; }
.h2-sub        { color: #6b7280; font-weight: 400; font-size: 0.7em; }
.section-note  { color: #6b7280; font-size: 13px; margin: -8px 0 12px 0; font-style: italic; }
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
/* Strategist's Note (demoted 3-bullet) */
.strat-note { background: #eff6ff; border-left: 3px solid #2563eb; border-radius: 0 8px 8px 0;
              padding: 14px 20px 14px 36px; margin-bottom: 8px; }
.strat-note li { line-height: 1.65; color: #1e3a5f; font-size: 0.86rem; margin-bottom: 6px; }
.strat-note li:last-child { margin-bottom: 0; }
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
""" + SECTOR_SETUP_CSS + POSITIONING_CSS + SHORTLIST_CSS + BOOK_REVIEW_CSS

    html = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta http-equiv='Cache-Control' content='no-cache, no-store, must-revalidate'>"
        "<meta http-equiv='Pragma' content='no-cache'>"
        "<meta http-equiv='Expires' content='0'>"
        f"<title>Finviz Weekly — {today}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        "<button class='pdf-btn' onclick='window.print()' title='Export PDF'>⬇</button>"
        "<h1>Finviz Weekly Review</h1>"
        f"<p class='subtitle'>{week_range} · {len(persistence_df)} tickers scanned · {len(dates_found)} trading days</p>"
        # §1 Positioning & Book Risk — am I positioned right?
        + positioning_html
        # §2 Week-Ahead Shortlist — what to do next week (replaces Top 5)
        + shortlist_html
        # §3 Book Weekend Review — what to do with what I hold
        + book_review_html
        # §4 Leadership Map — what is leadership doing
        + "<h2>📊 Leadership Map <span class='h2-sub'>— sectors, emerging names, macro</span></h2>"
        + render_sector_setup_html(etf_rotation_summary)
        + emerging_html
        + macro_html
        + crypto_html
        + fng_html
        # §5 Strategist's Note — 3 bullets, demoted
        + ai_html
        # Reference (demoted): character-change alerts + recurring-names board
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
# Part 4: Strategist's Note (3 bullets — replaces the AI essay)
# ----------------------------

def generate_strategist_note(positioning_summary: dict, shortlist_cards: list,
                             market_state: str, fng_data: dict = None,
                             etf_regime: str = None) -> str:
    """Produce the demoted weekly take: MAX 3 terse bullets —
    (a) regime insight, (b) best setup + why, (c) the one risk.

    Token-capped Claude call with a deterministic 3-bullet fallback so the
    note always renders (no API key / API down). Returns a newline-joined
    string of up to 3 bullets (no leading bullet glyphs)."""

    def _fallback() -> str:
        bullets = []
        regime = market_state or "UNKNOWN"
        rb = f"Regime: {regime}"
        if etf_regime:
            rb += f" · rotation {etf_regime}"
        if fng_data:
            rb += f" · F&G {fng_data.get('score')} ({fng_data.get('rating')})"
        if market_state in ("EXTENDED", "RED", "DANGER", "BLACKOUT"):
            rb += " — no new entries; protect the book."
        elif market_state in ("CAUTION", "COOLING", "STEADY-UPTREND"):
            rb += " — half size, be selective."
        else:
            rb += " — full size on confirmed setups."
        bullets.append(rb)
        if shortlist_cards:
            c = shortlist_cards[0]
            bullets.append(
                f"Best setup: {c['ticker']} ({c.get('sector','')}) — "
                f"{c.get('source_label','')} Q{int(c.get('q',0))} RS{c.get('rs',0)}, "
                f"{c.get('trigger','')}."
            )
        else:
            bullets.append("Best setup: none cleared the gate — patience is the trade.")
        risk = "The one risk: "
        ps = positioning_summary or {}
        health = ps.get("health", {})
        if health.get("past_stop_held"):
            risk += (f"{health['past_stop_held']} held past stop "
                     f"({', '.join(health.get('leak_names', [])[:3])}) — cut the leak.")
        elif ps.get("over_cap"):
            risk += f"over cap at {ps.get('n_positions')}/{ps.get('position_cap')} — trim before adding."
        elif market_state == "EXTENDED":
            risk += "parabolic tape — chasing extension is the round-trip trap."
        else:
            risk += "forcing a trade in a thin week. Cash is a position."
        bullets.append(risk)
        return "\n".join(bullets[:3])

    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — strategist note uses deterministic fallback.")
        return _fallback()

    ps = positioning_summary or {}
    health = ps.get("health", {})
    setup_lines = []
    for c in (shortlist_cards or [])[:5]:
        setup_lines.append(
            f"{c['ticker']} ({c.get('sector','')}) {c.get('source_label','')} "
            f"Q{int(c.get('q',0))} RS{c.get('rs',0)} ATR{c.get('atr_pct',0):.1f}% "
            f"dist {c.get('dist52',0):+.0f}% trigger='{c.get('trigger','')}'"
        )
    fng_ctx = (f"F&G {fng_data.get('score')} ({fng_data.get('rating')})"
               if fng_data else "F&G n/a")
    book_ctx = (
        f"{ps.get('n_positions')}/{ps.get('position_cap')} positions"
        f"{' (OVER CAP)' if ps.get('over_cap') else ''}; "
        f"book {health.get('green',0)} green / {health.get('underwater',0)} underwater / "
        f"{health.get('past_stop_held',0)} past stop"
    )

    prompt = (
        "You are a momentum swing trader writing the Saturday weekly take for "
        "your OWN book. Output EXACTLY 3 bullets, one line each, no preamble, "
        "no markdown bullets — just 3 lines:\n"
        "Line 1 — REGIME insight (market state + rotation): one decisive sentence.\n"
        "Line 2 — BEST setup for next week and WHY (pick from the shortlist).\n"
        "Line 3 — THE ONE risk to the book right now.\n"
        "Max ~22 words per line. No disclaimers.\n\n"
        f"Market state: {market_state} · rotation regime: {etf_regime} · {fng_ctx}\n"
        f"My book: {book_ctx}\n"
        f"Shortlist:\n" + ("\n".join(setup_lines) if setup_lines else "(empty)")
    )

    for attempt in range(3):
        try:
            resp = requests.post(
                ANTHROPIC_API_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 350,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60,
            )
            if resp.status_code in (429, 529):
                time.sleep(20 * (attempt + 1))
                continue
            if not resp.ok:
                log.error(f"Strategist note HTTP {resp.status_code}: {resp.text}")
                return _fallback()
            note = resp.json()["content"][0]["text"].strip()
            log.info("Weekly strategist note generated.")
            return note or _fallback()
        except Exception as e:
            log.error(f"Strategist note failed: {e}")
            return _fallback()
    log.warning("Strategist note exhausted retries — using fallback.")
    return _fallback()


# ----------------------------
# Part 5: Slack
# ----------------------------

def send_weekly_slack(persistence_df: pd.DataFrame, macro_data: dict,
                       strategist_note: str, weekly_html: str,
                       dates_found: list, fng_data: dict = None,
                       crypto_data: dict = None,
                       etf_rotation_summary: dict = None,
                       positioning_text: str = "",
                       shortlist_text: str = "",
                       book_review_text: str = ""):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return

    week_range = f"{dates_found[0]} to {dates_found[-1]}" if dates_found else "this week"

    macro_movers = []
    for sym, m in macro_data.items():
        wk = m["perf_week"].replace("%", "")
        try:
            if abs(float(wk)) >= 2.0:
                arrow = "↑" if float(wk) > 0 else "↓"
                macro_movers.append(f"{sym} {arrow} {m['perf_week']} wk")
        except Exception:
            pass

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

    def _section(text: str):
        # Slack section text hard-limit is ~3000 chars
        if len(text) > 2900:
            text = text[:2900] + "…"
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 Weekly Review — {week_range}"}},
    ]

    # Decision-first order: §1 Positioning → §2 Shortlist → §3 Book → §4
    # Leadership → §5 Strategist's Note.
    if positioning_text:
        blocks.append(_section(positioning_text))
        blocks.append({"type": "divider"})

    if shortlist_text:
        blocks.append(_section(shortlist_text))
        blocks.append({"type": "divider"})

    if book_review_text:
        blocks.append(_section(book_review_text))
        blocks.append({"type": "divider"})

    # §4 Leadership Map — ETF rotation + macro/crypto context
    sector_setup_text = render_sector_setup_slack(etf_rotation_summary)
    macro_str = " · ".join(macro_movers) if macro_movers else ""
    lead_parts = []
    if sector_setup_text:
        lead_parts.append(sector_setup_text)
    ctx = ""
    if macro_str:
        ctx += f"*Macro movers:* {macro_str}"
    ctx += crypto_line
    if ctx.strip():
        lead_parts.append(ctx.strip())
    if lead_parts:
        blocks.append(_section("\n\n".join(lead_parts)))
        blocks.append({"type": "divider"})

    # §5 Strategist's Note — 3 bullets
    if strategist_note:
        bullets = [b.strip() for b in strategist_note.split("\n") if b.strip()][:3]
        note_txt = ":brain: *Strategist's Note*\n" + "\n".join(f"• {b}" for b in bullets)
        blocks.append(_section(note_txt + gallery_link))
    elif gallery_link:
        blocks.append(_section(gallery_link.strip()))

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

    # Load market monitor state (regime context for all sections)
    market_state = load_market_state()
    state_str = market_state.get("market_state", "UNKNOWN") if market_state else "UNKNOWN"
    if market_state:
        log.info(f"Market state: {state_str} | 5d ratio: {market_state.get('ratio_5day')}")
    else:
        log.info("Market monitor data not available — sections run without market context")

    # ETF rotation summary (drives Leadership Map + regime label)
    etf_rotation_summary = None
    etf_regime = None
    try:
        from agents.sector_rotation import regime_action as _regime_action
        rotation = load_etf_rotation(DATA_DIR)
        if rotation:
            etf_rotation_summary = summarize_etf_rotation(
                rotation, top_n=5, regime_actions_lookup=_regime_action,
            )
            etf_regime = etf_rotation_summary.get("regime")
            log.info("ETF rotation summary loaded: regime=%s, buckets=%s",
                     etf_regime,
                     {b: len(rows) for b, rows in etf_rotation_summary.get("buckets", {}).items()})
    except Exception as e:
        log.warning("ETF rotation summary build failed (non-fatal): %s", e)

    # ── Load book data (positions.json + position_history.json) ──
    open_positions, position_history, held_tickers = [], {}, set()
    try:
        with open(os.path.join(DATA_DIR, "positions.json")) as f:
            open_positions = json.load(f).get("open_positions", [])
        held_tickers = {p.get("ticker") for p in open_positions if p.get("ticker")}
    except Exception as e:
        log.warning("Could not load positions.json (non-fatal): %s", e)
    try:
        with open(os.path.join(DATA_DIR, "position_history.json")) as f:
            position_history = json.load(f).get("history", {})
    except Exception as e:
        log.warning("Could not load position_history.json (non-fatal): %s", e)

    # ── §1 Positioning & Book Risk ──
    from agents.trading.alpaca_executor import effective_max_positions
    week_start = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    positioning_summary = build_positioning_summary(
        open_positions, position_history, state_str, etf_regime,
        position_cap=effective_max_positions(state_str),
        week_start=week_start, week_end=datetime.date.today().isoformat(),
    )
    positioning_html = render_positioning_html(positioning_summary)
    positioning_text = render_positioning_slack(positioning_summary)
    log.info("§1 Positioning: %d positions, realized %.0f",
             positioning_summary["n_positions"], positioning_summary["realized"]["total"])

    # ── §2 Week-Ahead Shortlist (forward funnel) ──
    shortlist_cards = []
    try:
        latest_daily = daily_dfs.get(dates_found[-1]) if dates_found else None
        daily_lookup = {}
        if latest_daily is not None:
            for _, drow in latest_daily.iterrows():
                t = str(drow.get("Ticker", "")).strip().upper()
                if t and t not in daily_lookup:
                    daily_lookup[t] = drow.to_dict()
        emerging_for_shortlist = select_emerging_candidates(
            persistence_df, top_n=8, excluded_tickers=held_tickers)
        try:
            with open(os.path.join(DATA_DIR, "watchlist.json")) as f:
                wl_data = json.load(f)
        except Exception:
            wl_data = {}
        try:
            with open(os.path.join(DATA_DIR, "rs_leaders.json")) as f:
                rs_data = json.load(f)
        except Exception:
            rs_data = {}
        cands = select_shortlist_candidates(
            emerging_for_shortlist, wl_data, rs_data, held_tickers,
            daily_lookup, datetime.date.today().isoformat(), max_n=8)
        shortlist_cards = build_shortlist_cards(cands, state_str)
        # Terse AI setup/invalidation prose (non-fatal)
        enrich_shortlist_notes_ai(shortlist_cards, state_str, api_key=ANTHROPIC_API_KEY)
        log.info("§2 Shortlist: %d cards", len(shortlist_cards))
    except Exception as e:
        log.warning("Shortlist build failed (non-fatal): %s", e)
    shortlist_html = render_shortlist_html(shortlist_cards)
    shortlist_text = render_shortlist_slack(shortlist_cards)

    # ── §3 Book Weekend Review ──
    book_review_rows = build_book_review_rows(open_positions)
    book_review_html = render_book_review_html(book_review_rows)
    book_review_text = render_book_review_slack(book_review_rows)
    log.info("§3 Book Weekend Review: %d positions", len(book_review_rows))

    # ── §5 Strategist's Note (3 bullets, demoted) ──
    log.info("Generating strategist note (3 bullets)...")
    strategist_note = generate_strategist_note(
        positioning_summary, shortlist_cards, state_str, fng_data, etf_regime)

    weekly_html = generate_weekly_html(
        persistence_df, macro_data, dates_found, strategist_note, fng_data,
        crypto_data, cc_results, etf_rotation_summary,
        positioning_html=positioning_html, shortlist_html=shortlist_html,
        book_review_html=book_review_html)
    log.info(f"Report: {weekly_html}")

    send_weekly_slack(
        persistence_df, macro_data, strategist_note, weekly_html, dates_found,
        fng_data, crypto_data, etf_rotation_summary,
        positioning_text=positioning_text, shortlist_text=shortlist_text,
        book_review_text=book_review_text)
    log.info("=== Done ===")
