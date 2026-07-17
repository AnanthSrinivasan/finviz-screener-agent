#!/usr/bin/env python3
# ----------------------------
# Market Monitor Agent
# ----------------------------
# Standalone daily agent — runs after market close.
# Fetches breadth data from Finviz, calculates up/down ratios,
# classifies market state (THRUST/GREEN/CAUTION/DANGER/RED/BLACKOUT),
# stores rolling 30-day history, and sends Slack alerts on state changes.
# ----------------------------

import os
import re
import json
import time
import random
import logging
import datetime
import requests
import pytz
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
FINVIZ_BASE        = "https://finviz.com"
CNN_FNG_URL        = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
DATA_DIR           = os.environ.get("DATA_DIR", "data")
HISTORY_FILE       = os.path.join(DATA_DIR, "market_monitor_history.json")
TRADING_STATE_FILE = os.path.join(DATA_DIR, "trading_state.json")
from utils.events import _append_recent_event
SLACK_WEBHOOK_ALERTS = os.environ.get("SLACK_WEBHOOK_MARKET_ALERTS", "")
FETCH_DELAY        = int(os.environ.get("MONITOR_FETCH_DELAY", "7"))

# Bonde calibration: 500+ stocks up/down 4%+ = "Very High" pressure zone.
# Universe: NYSE+NASDAQ common stocks, dollar volume > $250k OR volume > 100k.
THRUST_THRESHOLD      = 500   # stocks up 4%+ entering Very High buying pressure
DANGER_DOWN_THRESHOLD = 500   # stocks down 4%+ entering Very High selling pressure

# Alpaca Data API base URL (constant — same for paper and live accounts)
ALPACA_DATA_URL = "https://data.alpaca.markets/v2"

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
# Data Storage
# ----------------------------
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load history: {e}")
    return []


def save_history(history: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    log.info(f"History saved — {len(history)} days.")


def save_daily(record: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"market_monitor_{record['date']}.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    log.info(f"Daily record saved: {path}")


def update_trading_state(record: dict, new_consecutive_weak_days: int = 0):
    """Save market state and metrics to data/trading_state.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    existing = {}
    if os.path.exists(TRADING_STATE_FILE):
        try:
            with open(TRADING_STATE_FILE) as f:
                existing = json.load(f)
        except Exception as e:
            log.warning("Could not load trading_state.json: %s", e)

    # Compute spy_200ma from price and sma200_pct
    spy_price = record.get("spy_price")
    sma200_pct = record.get("spy_sma200_pct")
    spy_200ma = None
    if spy_price and sma200_pct is not None:
        spy_200ma = round(spy_price / (1 + sma200_pct / 100), 2)

    existing.update({
        "market_state":          record["market_state"],
        "up_4pct_count":         record["up_4_today"],
        "down_4pct_count":       record["down_4_today"],
        "5d_ratio":              record["ratio_5day"],
        "10d_ratio":             record["ratio_10day"],
        "spy_price":             spy_price,
        "spy_200ma":             spy_200ma,
        "fng":                   record.get("fg"),
        "consecutive_weak_days": new_consecutive_weak_days,
        "extended_since_date":   record.get("extended_since_date"),
        "days_below_21ema":      record.get("days_below_21ema", 0),
    })

    if record["market_state"] == "THRUST":
        existing["last_thrust_date"] = record["date"]

    # Cohort divergence dedup state (spec §2.3 — alert once per label change).
    # Only touched when the cohort block computed this run; a failed cohort
    # step leaves the previous dedup state intact.
    cohort = record.get("cohort")
    if cohort is not None:
        existing["cohort_last_divergence_label"] = cohort.get(
            "divergence_alerted_label")

    fg_val = record.get("fg") or 0
    if fg_val > 74:
        existing["last_extreme_greed_date"] = record["date"]
    if fg_val < 25:
        existing["last_extreme_fear_date"] = record["date"]

    with open(TRADING_STATE_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    log.info("trading_state.json updated: %s", record["market_state"])


# ----------------------------
# Finviz Screener Count Fetcher
# ----------------------------
def fetch_screener_count(session: requests.Session, url: str, label: str = "") -> int:
    """
    Fetch the total result count for a Finviz screener URL.

    Strategy:
      1. Fetch page 1 and try to parse the result counter (fast path).
         Finviz renders the count in a <td class="count-text"> element.
         Formats seen: "1 - 20 / 1234"  "of 1234"  "Total: 1234"
         The old code only matched "Total: N" and silently fell back to
         counting the 20 visible rows, which capped every result at 20.
      2. If the header parse fails (e.g. 0 results or HTML change),
         paginate: r=1, r=21, r=41 ... until a page returns fewer than 20
         unique tickers.  Max 30 pages (600 results) — more than enough
         for the up/down-4% screeners; large screeners (total_universe,
         above_40ma) will always succeed via step 1.
    """
    first_page_soup = None

    # --- Step 1: fast path — parse result counter from page 1 ---
    try:
        resp = session.get(url + "&r=1", timeout=15)
        if not resp.ok:
            log.warning("HTTP %s fetching %s", resp.status_code, label)
            return 0
        soup = BeautifulSoup(resp.text, "html.parser")
        first_page_soup = soup

        count_td = soup.find("td", class_="count-text")
        if count_td:
            text = count_td.get_text(strip=True)
            # Match "/ 1234", "of 1234", "Total: 1234", "Total 1234"
            match = re.search(r'(?:/\s*|of\s+|[Tt]otal:?\s*)([\d,]+)', text)
            if not match:
                # Last number in the string as final attempt
                match = re.search(r'([\d,]+)\s*$', text)
            if match:
                count = int(match.group(1).replace(',', ''))
                log.info("%s: %d (from count-text header)", label, count)
                return count

        # Broader scan of full page text
        page_text = soup.get_text()
        match = re.search(r'(?:/\s*|of\s+|[Tt]otal:?\s*)([\d,]+)', page_text)
        if match:
            count = int(match.group(1).replace(',', ''))
            log.info("%s: %d (from page text scan)", label, count)
            return count

    except Exception as e:
        log.error("Failed to fetch %s: %s", label, e)
        return 0

    # --- Step 2: pagination fallback ---
    log.debug("%s: count header not found — paginating", label)
    seen: set = set()

    # Reuse already-fetched first page
    if first_page_soup is not None:
        for row in first_page_soup.select('tr[valign="top"]'):
            cols = row.find_all('td')
            if len(cols) >= 2:
                ticker = cols[1].text.strip()
                if ticker:
                    seen.add(ticker)
        if len(seen) < 20:
            # Fewer than a full page — we have everything
            log.info("%s: %d (pagination, 1 page)", label, len(seen))
            return len(seen)

    for page in range(2, 31):  # pages 2-30  (max 600 results)
        r = 1 + (page - 1) * 20
        try:
            time.sleep(1)
            resp = session.get(url + "&r=" + str(r), timeout=15)
            if not resp.ok:
                log.warning("%s: HTTP %s on page %d", label, resp.status_code, page)
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select('tr[valign="top"]')
            new_this_page = 0
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 2:
                    ticker = cols[1].text.strip()
                    if ticker and ticker not in seen:
                        seen.add(ticker)
                        new_this_page += 1
            if new_this_page == 0 or new_this_page < 20:
                break
        except Exception as e:
            log.error("%s: page %d failed: %s", label, page, e)
            break

    log.info("%s: %d (from pagination)", label, len(seen))
    return len(seen)



# ----------------------------
# Breadth Source — Alpaca 4%-Filtered (Primary)
# ----------------------------
def fetch_breadth_alpaca() -> dict | None:
    """
    True 4%-filtered advance/decline counts via Alpaca market data API.
    Uses ALPACA_API_KEY / ALPACA_SECRET_KEY (already configured as repo secrets).

    Universe: NYSE + NASDAQ active tradable equities.
    Filters applied to each snapshot:
      - dollar volume (close * volume) > $250k  OR  volume > 100k  (Bonde's filter)
      - close > $3  (noise filter)

    Steps:
      1. GET /v2/assets — all active NYSE+NASDAQ equities (broker API)
      2. GET /v2/stocks/snapshots — batched 1000/call (data API)
      3. Count tickers where (close - prev_close) / prev_close >= +4% or <= -4%

    Returns None if keys are missing or both counts come back zero with no valid universe.
    """
    alpaca_key    = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "")
    alpaca_broker = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")

    if not alpaca_key or not alpaca_secret:
        log.warning("Alpaca keys not configured — skipping 4pct breadth")
        return None

    headers = {
        "APCA-API-KEY-ID":     alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
    }

    # Step 1 — get all active NYSE+NASDAQ equities
    try:
        resp = requests.get(
            f"{alpaca_broker}/assets",
            headers=headers,
            params={"status": "active", "asset_class": "us_equity"},
            timeout=30,
        )
        resp.raise_for_status()
        assets = resp.json()
    except Exception as e:
        log.error("Alpaca assets fetch failed: %s", e)
        return None

    tickers = [
        a["symbol"] for a in assets
        if a.get("exchange") in ("NYSE", "NASDAQ")
        and a.get("tradable", False)
        and a.get("status") == "active"
    ]
    log.info("Alpaca: %d active NYSE+NASDAQ tickers to check", len(tickers))
    if not tickers:
        log.error("Alpaca returned no tickers")
        return None

    # Step 2 — batch snapshots, 1000 per call
    up_4    = 0
    down_4  = 0
    checked = 0

    for i in range(0, len(tickers), 1000):
        batch = tickers[i:i + 1000]
        try:
            resp = requests.get(
                f"{ALPACA_DATA_URL}/stocks/snapshots",
                headers=headers,
                params={"symbols": ",".join(batch), "feed": "iex"},
                timeout=30,
            )
            resp.raise_for_status()
            snapshots = resp.json()
        except Exception as e:
            log.error("Alpaca snapshots batch %d failed: %s", i // 1000 + 1, e)
            continue

        for snap in snapshots.values():
            daily      = snap.get("dailyBar") or {}
            prev_daily = snap.get("prevDailyBar") or {}

            close      = daily.get("c") or 0
            prev_close = prev_daily.get("c") or 0
            volume     = daily.get("v") or 0

            # Bonde filter: dollar vol > $250k OR volume > 100k; plus price > $3
            if close <= 3 or prev_close <= 0:
                continue
            if (close * volume) < 250_000 and volume < 100_000:
                continue

            pct = (close - prev_close) / prev_close * 100
            checked += 1

            if pct >= 4.0:
                up_4 += 1
            elif pct <= -4.0:
                down_4 += 1

    log.info(
        "Alpaca 4pct breadth: up=%d down=%d (universe=%d after filters)",
        up_4, down_4, checked,
    )

    if checked < 100:
        log.warning("Alpaca breadth universe < 100 — market likely closed or API issue")
        return None

    return {
        "up_4_today":    up_4,
        "down_4_today":  down_4,
        "breadth_source": "alpaca_4pct",
        "universe_size":  checked,
    }


# ----------------------------
# Data Fetchers
# ----------------------------
def fetch_breadth_data(session: requests.Session) -> dict:
    """
    Fetch all breadth data + SPY + F&G.
    Up/Down 4% breadth via Alpaca snapshots API (primary). Zero fallback on failure.
    Finviz fetch_screener_count() used for quarterly/SMA supplemental metrics.
    """

    # --- BREADTH: True 4%-filtered counts (Alpaca, primary) ---
    alpaca_breadth = fetch_breadth_alpaca()
    if alpaca_breadth:
        up_4          = alpaca_breadth["up_4_today"]
        down_4        = alpaca_breadth["down_4_today"]
        breadth_source = "alpaca_4pct"
        universe_size  = alpaca_breadth.get("universe_size", 0)
    else:
        log.error("Alpaca 4pct breadth failed — up/down 4pct counts unavailable")
        up_4          = 0
        down_4        = 0
        breadth_source = "none"
        universe_size  = 0

    adv_total = None
    dec_total = None

    base_filters = "geo_usa,sh_avgvol_o500,sh_price_o5,exch_nysenasd"

    # Fetch 3 — Stocks up 25%+ in a quarter
    url_up25q = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_perf_13w30o"
    )
    up_25_quarter = fetch_screener_count(session, url_up25q, "Up 25%+ quarter")
    time.sleep(FETCH_DELAY)

    # Fetch 4 — Stocks down 25%+ in a quarter
    url_down25q = (
        f"{FINVIZ_BASE}/screener.ashx?v=111"
        f"&f={base_filters},ta_perf_13w30u"
    )
    down_25_quarter = fetch_screener_count(session, url_down25q, "Down 25%+ quarter")
    time.sleep(FETCH_DELAY)

    # SPY snapshot for price + SMA data
    spy_data = fetch_spy_data(session)

    # Fear & Greed
    fg = fetch_fng()

    # Extension metrics — SPY + QQQ ATR multiples / SMA50% via Alpaca daily bars.
    # Used to detect parabolic / "no chase" regime (EXTENDED state) and the
    # v3 TREND-FOLLOW trend regime detector (slope, 20d-high distance).
    spy_ext = fetch_index_extension("SPY")
    qqq_ext = fetch_index_extension("QQQ")
    vix     = fetch_vix_snapshot()

    today_partial = {
        "universe_size":  universe_size,
        "up_25_quarter":  up_25_quarter,
    }
    pct_above_50ma = compute_participation_proxy(today_partial)

    return {
        "up_4_today":         up_4,
        "down_4_today":       down_4,
        "breadth_source":     breadth_source,
        "universe_size":      universe_size,
        "adv_total":          adv_total,
        "dec_total":          dec_total,
        "up_25_quarter":      up_25_quarter,
        "down_25_quarter":    down_25_quarter,
        "spy_price":          spy_data.get("price"),
        "spy_sma200_pct":     spy_data.get("sma200_pct"),
        "spy_sma50_pct":      spy_ext.get("sma50_pct") if spy_ext else None,
        "spy_atr_mult_50":    spy_ext.get("atr_mult_50") if spy_ext else None,
        "spy_sma50_slope_10d": spy_ext.get("sma50_slope_10d") if spy_ext else None,
        "spy_pct_from_20d_high": spy_ext.get("pct_from_20d_high") if spy_ext else None,
        "spy_close":          spy_ext.get("close") if spy_ext else None,
        "spy_sma50":          spy_ext.get("sma50") if spy_ext else None,
        "spy_21ema":          spy_ext.get("ema21") if spy_ext else None,
        "spy_20d_high":       spy_ext.get("close_20d_high") if spy_ext else None,
        "qqq_sma50_pct":      qqq_ext.get("sma50_pct") if qqq_ext else None,
        "qqq_atr_mult_50":    qqq_ext.get("atr_mult_50") if qqq_ext else None,
        "pct_above_50ma":     pct_above_50ma,
        "vix_close":          vix.get("vix_close") if vix else None,
        "vix_change_pct":     vix.get("vix_change_pct") if vix else None,
        "fg":                 fg,
    }


def fetch_index_extension(ticker: str) -> dict | None:
    """Fetch SPY/QQQ daily bars from Alpaca and compute extension metrics.

    Returns dict with sma50_pct, atr_mult_50, sma200_pct or None on failure.
    ATR% Multiple formula matches utils/calibrate_peel.py / TradingView:
        (close - sma50) * close / (sma50 * atr14)
    """
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        log.warning("Alpaca keys missing — cannot compute %s extension metrics", ticker)
        return None

    try:
        from utils.calibrate_peel import wilder_atr, compute_sma
    except Exception as e:
        log.warning("Could not import indicator helpers: %s", e)
        return None

    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=320)
        bars: list = []
        page_token = None
        for _ in range(5):
            params = {
                "timeframe": "1Day",
                "start": start.isoformat() + "T00:00:00Z",
                "end":   end.isoformat()   + "T23:59:59Z",
                "limit": 1000,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{ALPACA_DATA_URL}/stocks/{ticker}/bars",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                params=params,
                timeout=15,
            )
            if not resp.ok:
                log.warning("Alpaca bars failed for %s: %s", ticker, resp.status_code)
                return None
            payload = resp.json()
            page_bars = payload.get("bars") or []
            bars.extend(page_bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        if len(bars) < 50:
            log.warning("Insufficient %s bars (%d) for extension calc", ticker, len(bars))
            return None
        closes = [b["c"] for b in bars]
        highs = [b["h"] for b in bars]
        sma50_series = compute_sma(closes, 50)
        sma50 = sma50_series[-1]
        sma200 = compute_sma(closes, 200)[-1] if len(closes) >= 200 else None
        atr14 = wilder_atr(bars)[-1]
        close = closes[-1]
        if sma50 and atr14:
            atr_mult_50 = round((close - sma50) * close / (sma50 * atr14), 2)
            sma50_pct = round((close - sma50) / sma50 * 100, 2)
        else:
            atr_mult_50 = None
            sma50_pct = None
        sma200_pct = round((close - sma200) / sma200 * 100, 2) if sma200 else None
        # v3 trend-follow inputs
        sma50_slope_10d = compute_sma50_slope_10d(sma50_series)
        pct_from_20d_high_val = pct_from_20d_high(highs[:-1] + [close], closes)
        # v4 EXTENDED stickiness inputs — 21 EMA + 20-day close high
        ema21_series = compute_ema(closes, 21)
        ema21 = ema21_series[-1] if ema21_series else None
        close_20d_high = max(closes[-20:]) if len(closes) >= 20 else None
        log.info(
            "%s extension: sma50%%=%s atr_mult_50=%s sma200%%=%s slope_10d=%s pct_from_20d_high=%s ema21=%s close_20d_high=%s",
            ticker, sma50_pct, atr_mult_50, sma200_pct,
            sma50_slope_10d, pct_from_20d_high_val,
            round(ema21, 2) if ema21 else None,
            round(close_20d_high, 2) if close_20d_high else None,
        )
        return {
            "close": close,
            "sma50": sma50,
            "sma50_pct": sma50_pct,
            "atr_mult_50": atr_mult_50,
            "sma200_pct": sma200_pct,
            "sma50_slope_10d": sma50_slope_10d,
            "pct_from_20d_high": pct_from_20d_high_val,
            "ema21": ema21,
            "close_20d_high": close_20d_high,
        }
    except Exception as e:
        log.warning("Extension fetch failed for %s: %s", ticker, e)
        return None


def compute_ema(values: list, period: int) -> list:
    """Compute EMA over a list of floats. Returns list with None padding.

    Seeds with SMA of the first `period` values, then applies the standard
    smoothing factor k = 2 / (period + 1).
    """
    n = len(values)
    if n < period:
        return [None] * n
    result: list = [None] * (period - 1)
    seed = sum(values[:period]) / period
    result.append(seed)
    k = 2.0 / (period + 1)
    for v in values[period:]:
        prev = result[-1]
        result.append(v * k + prev * (1 - k))
    return result


def compute_sma50_slope_10d(sma50_series: list) -> float | None:
    """Return percent change in SMA50 over the last 10 sessions.

    `sma50_series` is the list from compute_sma(closes, 50). Returns None when
    there are not enough non-None entries.
    """
    if not sma50_series or len(sma50_series) < 11:
        return None
    today = sma50_series[-1]
    prior = sma50_series[-11]
    if today is None or prior is None or prior == 0:
        return None
    return round((today - prior) / prior * 100, 2)


def pct_from_20d_high(highs: list, closes: list) -> float | None:
    """Percent distance of latest close from trailing 20-day high.

    Negative when below; near zero when at or above the 20d high. Returns None
    if there are fewer than 20 highs.
    """
    if not highs or not closes or len(highs) < 20:
        return None
    window = highs[-20:]
    hi = max(window)
    close = closes[-1]
    if hi == 0:
        return None
    return round((close - hi) / hi * 100, 2)


def fetch_vix_snapshot() -> dict | None:
    """Fetch VIX close + day change via Yahoo Finance (^VIX).

    Returns dict with keys vix_close, vix_change_pct, or None on failure.
    Alpaca does not carry ^VIX, so Yahoo is the lightweight choice.
    """
    try:
        import yfinance as yf  # local import — only loaded when needed
        hist = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if hist is None or hist.empty or len(hist) < 2:
            log.warning("VIX history empty or too short")
            return None
        close = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        change_pct = round((close - prev) / prev * 100, 2) if prev else None
        result = {
            "vix_close":      round(close, 2),
            "vix_change_pct": change_pct,
        }
        log.info("VIX: %.2f (Δ %s%%)", close, change_pct)
        return result
    except Exception as e:
        log.warning("VIX fetch failed: %s", e)
        return None


def compute_participation_proxy(today_data: dict) -> float | None:
    """Cheap participation proxy: up_25_quarter / universe_size.

    Spec ships the proxy first; true %above-50MA can swap in later. Returns the
    ratio as a percent (0–100) or None when inputs are missing.
    """
    universe = today_data.get("universe_size") or 0
    up_25 = today_data.get("up_25_quarter") or 0
    if universe <= 0:
        return None
    return round(up_25 / universe * 100, 2)


def is_trend_follow(today_data: dict, fg: float | None,
                    prev_state: str | None = None) -> bool:
    """All TREND-FOLLOW gates pass.

    v4 (May 2026) — added two rejects:
      - prev_state guard: TREND-FOLLOW is a *continuation* path; reject when
        prev in {EXTENDED, RED, DANGER, BLACKOUT, COOLING}. Path out of those
        must run through CAUTION → GREEN/THRUST first.
      - breadth sanity: reject when today's dn4 ≥ 2 × up4 (heavy distribution).
        A grind-up trend day does not print 535 vs 110.

    Base gates:
      - MA stack: SPY > SMA50 > SMA200
      - 50MA slope: rising over last 10 sessions
      - Near 20d high: SPY within 3%
      - Participation: proxy >= 8% (up_25_quarter / universe)
      - Vol calm: VIX < 25 OR VIX down on the day
      - Not EXTENDED (caller enforces priority)
    """
    if prev_state in ("EXTENDED", "RED", "DANGER", "BLACKOUT", "COOLING"):
        return False

    up4 = today_data.get("up_4_today") or 0
    dn4 = today_data.get("down_4_today") or 0
    if dn4 >= 2 * max(up4, 1):
        return False

    spy_sma50_pct  = today_data.get("spy_sma50_pct")
    spy_sma200_pct = today_data.get("spy_sma200_pct")
    if spy_sma50_pct is None or spy_sma200_pct is None:
        return False
    if not (spy_sma50_pct > 0 and spy_sma200_pct > 0):
        return False

    slope = today_data.get("spy_sma50_slope_10d")
    if slope is None or slope <= 0:
        return False

    pct_high = today_data.get("spy_pct_from_20d_high")
    if pct_high is None or pct_high < -3.0:
        return False

    part = today_data.get("pct_above_50ma")
    # Threshold lowered from 10% → 8% (May 2026 calibration). Apr 24–29
    # backtest hovered 8.3–9.0% on a clearly trending tape. Spec note in
    # docs/specs/state-machine-v3-trend-follow.md flags participation proxy
    # as the likely too-strict gate when backtest under-fires.
    if part is None or part < 8.0:
        return False

    vix_close  = today_data.get("vix_close")
    vix_change = today_data.get("vix_change_pct")
    vix_calm = False
    if vix_close is not None and vix_close < 25:
        vix_calm = True
    elif vix_change is not None and vix_change < 0:
        vix_calm = True
    if not vix_calm:
        return False

    return True


def is_extended(spy_atr_mult_50: float | None,
                spy_sma50_pct: float | None,
                qqq_atr_mult_50: float | None) -> bool:
    """Parabolic / blow-off guardrail.

    Fires when the index is stretched far above its 50MA. Any one trigger is
    enough — SPY ATR mult ≥ 7, SPY %above 50 ≥ 8, or QQQ ATR mult ≥ 9.
    """
    if spy_atr_mult_50 is not None and spy_atr_mult_50 >= 7.0:
        return True
    if spy_sma50_pct is not None and spy_sma50_pct >= 8.0:
        return True
    if qqq_atr_mult_50 is not None and qqq_atr_mult_50 >= 9.0:
        return True
    return False


def fetch_spy_data(session: requests.Session) -> dict:
    """Fetch SPY price and SMA data from Finviz quote page."""
    try:
        resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": "SPY"}, timeout=10)
        if not resp.ok:
            log.warning(f"SPY fetch failed: HTTP {resp.status_code}")
            return {}
        soup = BeautifulSoup(resp.content, "html.parser")
        snapshot_cells = soup.find_all("td", class_="snapshot-td2")
        if not snapshot_cells:
            return {}
        data = {}
        for k, v in zip(snapshot_cells[0::2], snapshot_cells[1::2]):
            data[k.get_text(strip=True).rstrip(".")] = v.get_text(strip=True)

        price_raw = data.get("Price", "").replace(",", "")
        sma200_raw = data.get("SMA200", "").replace("%", "")

        result = {}
        try:
            result["price"] = round(float(price_raw), 2)
        except (ValueError, TypeError):
            result["price"] = None
        try:
            result["sma200_pct"] = round(float(sma200_raw), 2)
        except (ValueError, TypeError):
            result["sma200_pct"] = None

        log.info(f"SPY: ${result.get('price')} | SMA200: {result.get('sma200_pct')}%")
        return result
    except Exception as e:
        log.error(f"SPY snapshot failed: {e}")
        return {}




def fetch_fng() -> float | None:
    """Fetch current CNN Fear & Greed score."""
    try:
        resp = make_session().get(CNN_FNG_URL, timeout=10)
        if resp.ok:
            score = resp.json()["fear_and_greed"]["score"]
            log.info(f"F&G: {score:.1f}")
            return round(float(score), 1)
    except Exception as e:
        log.error(f"F&G fetch failed: {e}")
    return None


# ----------------------------
# Calculations
# ----------------------------
def calculate_metrics(history: list, today_data: dict) -> dict:
    """Calculate breadth ratios, thrust detection, and T2108 equivalent."""
    up_4 = today_data["up_4_today"]
    down_4 = today_data["down_4_today"]

    # Daily ratio
    ratio_today = up_4 / max(down_4, 1)

    # 5-day ratio — include today in the window
    recent = history[-(5 - 1):] + [today_data]  # last 4 from history + today
    last_5 = recent[-5:]
    ratio_5day = (
        sum(d["up_4_today"] for d in last_5) /
        max(sum(d["down_4_today"] for d in last_5), 1)
    )

    # 10-day ratio
    recent_10 = history[-(10 - 1):] + [today_data]
    last_10 = recent_10[-10:]
    ratio_10day = (
        sum(d["up_4_today"] for d in last_10) /
        max(sum(d["down_4_today"] for d in last_10), 1)
    )

    # Thrust detection
    thrust = up_4 >= THRUST_THRESHOLD

    # SPY above 200-day SMA (conservative proxy for 20-week MA)
    spy_sma200_pct = today_data.get("spy_sma200_pct")
    spy_above_200d = spy_sma200_pct is not None and spy_sma200_pct > 0

    return {
        "ratio_today": round(ratio_today, 2),
        "ratio_5day": round(ratio_5day, 2),
        "ratio_10day": round(ratio_10day, 2),
        "thrust": thrust,
        "spy_above_200d": spy_above_200d,
    }


# ----------------------------
# Market State Classification
# ----------------------------
def is_blackout(date: datetime.date) -> bool:
    """Check if date falls in seasonal no-trade blackout period.

    Blackout months: February (1–end of month) and September (1–30).
    Matches CLAUDE.md / SYSTEM_DOCS.
    """
    return date.month in (2, 9)


def classify_market_state(metrics: dict, fg: float | None,
                          spy_price: float | None,
                          spy_above_200d: bool,
                          today_data: dict,
                          date: datetime.date,
                          prev_state: str | None = None,
                          last_thrust_date: str | None = None,
                          consecutive_weak_days: int = 0,
                          extended_since_date: str | None = None,
                          days_below_21ema: int = 0) -> tuple[str, str, dict]:
    """
    Classify market into one of 9 states, checked in priority order:
      BLACKOUT → DANGER → EXTENDED → COOLING → THRUST → GREEN → CAUTION
      → STEADY-UPTREND → RED

    Returns (state, message, context) where context = {
        post_thrust_floor_active: bool,
        confidence_context: "high_confidence_recovery" | "extreme_greed_caution" | None,
    }

    Two confidence layers sit on top of the base classification:
      Layer 1 — Post-THRUST floor: RED → CAUTION for 3 days after any THRUST day.
                DANGER still fires immediately (bypasses floor).
      Layer 2a — Extreme greed (F&G > 74): skip 2-day COOLING sustain buffer;
                from COOLING prev-state → RED in 1 day instead of 2.
      Layer 2b — Extreme fear (F&G < 25) + THRUST + prev in RED/DANGER:
                override to CAUTION + high_confidence_recovery tag.
    """
    fg_val = fg if fg is not None else 0
    ctx: dict = {
        "post_thrust_floor_active": False,
        "confidence_context": None,
        "extended_since_date": extended_since_date,
        "days_below_21ema": days_below_21ema,
    }

    # 1. Seasonal blackout — always overrides
    if is_blackout(date):
        return "BLACKOUT", "Seasonal no-trade period active", ctx

    # 2. DANGER — bypasses all floors/overrides (checked before THRUST so a
    #    collapse day with 500+ down doesn't accidentally fire THRUST).
    #    v4: catastrophic single-day distribution (dn4 ≥ 3 × up4) also fires
    #    DANGER even when 5d hasn't deteriorated yet (05-15 reference).
    if (today_data["down_4_today"] >= DANGER_DOWN_THRESHOLD
            and (metrics["ratio_5day"] < 0.5
                 or today_data["down_4_today"]
                    >= 3 * max(today_data["up_4_today"], 1))):
        return "DANGER", "Major breadth deterioration", ctx

    extreme_greed = fg_val > 74
    extreme_fear  = fg_val < 25

    # EXTENDED — parabolic / blow-off guardrail (May 2026, SNDK reference case).
    # v4 (May 2026) stickiness: once EXTENDED trips, stay EXTENDED while SPY
    # respects the 21 EMA / 50 SMA structure even after the ATR-mult metric
    # cools. Pulls to 8/21 EMA that bounce are healthy Stage 2 digestion, not
    # an exit. Exits are structural: 3 closes below 21 EMA → COOLING; any close
    # below 50 SMA → RED. Re-entry from COOLING/CAUTION needs metric trip +
    # new 20d close high.
    spy_atr_mult_50 = today_data.get("spy_atr_mult_50")
    spy_sma50_pct   = today_data.get("spy_sma50_pct")
    qqq_atr_mult_50 = today_data.get("qqq_atr_mult_50")
    spy_close       = today_data.get("spy_close")
    spy_21ema       = today_data.get("spy_21ema")
    spy_50sma       = today_data.get("spy_sma50")
    spy_20d_high    = today_data.get("spy_20d_high")

    metric_trip = is_extended(spy_atr_mult_50, spy_sma50_pct, qqq_atr_mult_50)

    def _extended_message(days_below: int) -> str:
        spy_part = (
            f"SPY ATR mult {spy_atr_mult_50:.1f}× / +{spy_sma50_pct:.1f}% above 50MA"
            if spy_atr_mult_50 is not None and spy_sma50_pct is not None
            else "SPY extension data unavailable"
        )
        qqq_part = (
            f"QQQ ATR mult {qqq_atr_mult_50:.1f}×"
            if qqq_atr_mult_50 is not None else ""
        )
        msg = "Parabolic tape — no new entries, tighten stops. " + spy_part
        if qqq_part:
            msg += " · " + qqq_part
        if days_below > 0:
            msg += f" · {days_below}d under 21 EMA (3 = exit to COOLING)"
        return msg

    # Currently in EXTENDED — apply stickiness rules
    if prev_state == "EXTENDED":
        below_50 = (spy_close is not None and spy_50sma is not None
                    and spy_close < spy_50sma)
        if below_50:
            ctx["extended_since_date"] = None
            ctx["days_below_21ema"] = 0
            return ("RED",
                    "EXTENDED → RED: SPY closed below 50 SMA (trend damage)",
                    ctx)

        below_21 = (spy_close is not None and spy_21ema is not None
                    and spy_close < spy_21ema)
        new_days_below = days_below_21ema + 1 if below_21 else 0
        if new_days_below >= 3:
            ctx["extended_since_date"] = None
            ctx["days_below_21ema"] = 0
            return ("COOLING",
                    "EXTENDED → COOLING: 3 closes below 21 EMA, "
                    "leadership ended",
                    ctx)

        ctx["extended_since_date"] = extended_since_date or date.isoformat()
        ctx["days_below_21ema"] = new_days_below
        return "EXTENDED", _extended_message(new_days_below), ctx

    # Re-entry from COOLING/CAUTION — metric trip + new 20d close high
    if prev_state in ("COOLING", "CAUTION") and metric_trip:
        if (spy_close is not None and spy_20d_high is not None
                and spy_close >= spy_20d_high):
            ctx["extended_since_date"] = date.isoformat()
            ctx["days_below_21ema"] = 0
            return ("EXTENDED",
                    "Re-entered EXTENDED: new 20d high + parabolic metrics. "
                    + _extended_message(0),
                    ctx)

    # Fresh trip from any non-bearish prev state
    if metric_trip and prev_state not in ("RED", "DANGER", "BLACKOUT"):
        ctx["extended_since_date"] = date.isoformat()
        ctx["days_below_21ema"] = 0
        return "EXTENDED", _extended_message(0), ctx

    # Layer 2b — Extreme fear + THRUST from RED/DANGER: high-confidence recovery.
    # Override THRUST → CAUTION so executor can size in immediately; tag the event.
    if (extreme_fear
            and prev_state in ("RED", "DANGER")
            and metrics["thrust"]):
        ctx["confidence_context"] = "high_confidence_recovery"
        msg = (
            f"⚡ High-confidence recovery — THRUST during Extreme Fear "
            f"(F&G {fg_val:.0f}). Reversal signal. "
            f"Watch for 2nd THRUST to confirm GREEN."
        )
        return "CAUTION", msg, ctx

    # Reusable GREEN condition check
    green_conditions = (
        metrics["ratio_5day"] >= 2.0
        and metrics["ratio_10day"] >= 1.5
        and fg_val >= 35
        and spy_above_200d
    )

    # 3. COOLING — market fading FROM GREEN (sell-off phase, tighten stops).
    #    Fires on every deterioration from GREEN regardless of F&G regime.
    if prev_state == "GREEN" and not green_conditions:
        if extreme_greed:
            ctx["confidence_context"] = "extreme_greed_caution"
        return "COOLING", "Market cooling from GREEN — trim and tighten", ctx

    # 4. THRUST — single-day breadth explosion (Bonde signal)
    if metrics["thrust"]:
        return "THRUST", f"Breadth thrust — {today_data['up_4_today']} stocks up 4%", ctx

    # 5. GREEN — full bull, all conditions met
    if green_conditions:
        return "GREEN", "Full conditions met", ctx

    # 6. TREND-FOLLOW — trend-persistence path to full size (v3, May 2026).
    #    Reads MA stack + 50MA slope + 20d-high proximity + participation +
    #    vol calm. Independent of the 5d/10d thrust ratio so steady grind-up
    #    tapes are not dumped to RED. Checked before COOLING-sustain so a
    #    recovered trend escapes the 2-day buffer. EXTENDED already escaped.
    if is_trend_follow(today_data, fg, prev_state):
        vix_close = today_data.get("vix_close")
        vix_str = f"VIX {vix_close:.1f}" if vix_close is not None else "VIX n/a"
        part = today_data.get("pct_above_50ma")
        part_str = f"participation {part:.1f}%" if part is not None else "participation n/a"
        msg = (
            f"Steady uptrend — full size, entries allowed. "
            f"SMA50 rising · near 20d high · {part_str} · {vix_str}"
        )
        return "TREND-FOLLOW", msg, ctx

    # 6b. Sustain COOLING for a 2nd consecutive weak day (normal F&G range only).
    #     Adds a 1-day buffer before allowing RED from COOLING.
    #     Only applies when conditions are RED-level (not CAUTION) — CAUTION recovery
    #     is always allowed immediately. Extreme greed bypasses the buffer.
    caution_conditions = (
        metrics["ratio_5day"] >= 1.5
        and fg_val >= 25
        and spy_above_200d
    )
    if (prev_state == "COOLING"
            and not green_conditions
            and not caution_conditions
            and not extreme_greed
            and consecutive_weak_days < 2):
        return "COOLING", "Market still cooling — 2-day confirmation buffer", ctx

    # 7. CAUTION — recovering/building phase (going UP toward GREEN)
    if (metrics["ratio_5day"] >= 1.5
            and fg_val >= 25
            and spy_above_200d):
        return "CAUTION", "Recovering — build watchlist, half size", ctx

    # 6b. STEADY-UPTREND — trend tape between thrust days.
    #     SPY > 200d AND SPY > 50d AND F&G ≥ 50 AND up4 ≥ dn4 AND 5d_ratio ≥ 0.9
    #     AND prev_state ∉ {RED, DANGER, BLACKOUT, EXTENDED} (don't auto-rescue
    #     a bear bounce; path out of RED stays RED → THRUST → CAUTION → GREEN).
    #     Half size, entries allowed.
    spy_above_50d = (spy_sma50_pct is not None and spy_sma50_pct > 0)
    if (spy_above_200d
            and spy_above_50d
            and fg_val >= 50
            and today_data["up_4_today"] >= today_data["down_4_today"]
            and metrics["ratio_5day"] >= 0.9
            and prev_state not in ("RED", "DANGER", "BLACKOUT", "EXTENDED", None)):
        return ("STEADY-UPTREND",
                "Steady uptrend — half size, entries allowed", ctx)

    # 7. RED — check Layer 1 post-THRUST floor before returning RED.
    #    After any THRUST, enforce minimum state = CAUTION for 3 trading days.
    #    DANGER already escaped above; floor only overrides RED.
    if last_thrust_date:
        try:
            thrust_dt = datetime.date.fromisoformat(last_thrust_date)
            days_since = (date - thrust_dt).days
            if 0 < days_since <= 3:
                ctx["post_thrust_floor_active"] = True
                return "CAUTION", (
                    f"Post-THRUST floor — {days_since}d since THRUST "
                    f"({last_thrust_date}). Minimum CAUTION for 3 days."
                ), ctx
        except Exception:
            pass

    return "RED", "Bearish — no new trades", ctx


# ----------------------------
# Record Builder
# ----------------------------
def build_daily_record(date: datetime.date, today_data: dict, metrics: dict,
                       state: str, message: str,
                       classify_ctx: dict | None = None) -> dict:
    """Build the complete daily record for storage."""
    fg_val = today_data.get("fg") or 0
    fg_regime = (
        "extreme_greed" if fg_val > 74
        else ("extreme_fear" if fg_val < 25 else "normal")
    )
    ctx = classify_ctx or {}
    return {
        "date":                   date.isoformat(),
        "up_4_today":             today_data["up_4_today"],
        "down_4_today":           today_data["down_4_today"],
        "breadth_source":         today_data.get("breadth_source", "unknown"),
        "universe_size":          today_data.get("universe_size", 0),
        "adv_total":              today_data.get("adv_total"),
        "dec_total":              today_data.get("dec_total"),
        "ratio_today":            metrics["ratio_today"],
        "ratio_5day":             metrics["ratio_5day"],
        "ratio_10day":            metrics["ratio_10day"],
        "up_25_quarter":          today_data.get("up_25_quarter", 0),
        "down_25_quarter":        today_data.get("down_25_quarter", 0),
        "thrust_detected":        metrics["thrust"],
        "fg":                     today_data.get("fg"),
        "spy_price":              today_data.get("spy_price"),
        "spy_sma200_pct":         today_data.get("spy_sma200_pct"),
        "spy_sma50_pct":          today_data.get("spy_sma50_pct"),
        "spy_atr_mult_50":        today_data.get("spy_atr_mult_50"),
        "spy_sma50_slope_10d":    today_data.get("spy_sma50_slope_10d"),
        "spy_pct_from_20d_high":  today_data.get("spy_pct_from_20d_high"),
        "qqq_sma50_pct":          today_data.get("qqq_sma50_pct"),
        "qqq_atr_mult_50":        today_data.get("qqq_atr_mult_50"),
        "pct_above_50ma":         today_data.get("pct_above_50ma"),
        "vix_close":              today_data.get("vix_close"),
        "vix_change_pct":         today_data.get("vix_change_pct"),
        "trend_follow_active":    state == "TREND-FOLLOW",
        "spy_above_200d":         metrics["spy_above_200d"],
        "market_state":           state,
        "state_message":          message,
        "blackout":               is_blackout(date),
        "fg_regime":              fg_regime,
        "post_thrust_floor_active": ctx.get("post_thrust_floor_active", False),
        "confidence_context":     ctx.get("confidence_context"),
        "extended_since_date":    ctx.get("extended_since_date"),
        "extended_days_active":   (
            (date - datetime.date.fromisoformat(ctx["extended_since_date"])).days
            if ctx.get("extended_since_date") else 0
        ),
        "days_below_21ema":       ctx.get("days_below_21ema", 0),
        "spy_close":              today_data.get("spy_close"),
        "spy_21ema":              today_data.get("spy_21ema"),
        "spy_20d_high":           today_data.get("spy_20d_high"),
    }


# ----------------------------
# Slack Alerts
# ----------------------------
def send_thrust_alert(record: dict):
    """Send dedicated THRUST alert when 500+ stocks up 4%+."""
    if not SLACK_WEBHOOK_ALERTS:
        log.info("SLACK_WEBHOOK_MARKET_ALERTS not set — skipping THRUST alert.")
        return

    up_count = record["up_4_today"]
    ratio = record["ratio_5day"]
    spy_price = record.get("spy_price")
    sma200_pct = record.get("spy_sma200_pct")

    spy_200ma = None
    if spy_price and sma200_pct is not None:
        spy_200ma = round(spy_price / (1 + sma200_pct / 100), 2)

    spy_str = f"${spy_price:.0f}" if spy_price else "n/a"
    ma_str = f"${spy_200ma:.0f}" if spy_200ma else "n/a"

    text = (
        f"🚀 THRUST DAY — {up_count} stocks up 4%+\n"
        f"This is Pradeep Bonde's tide-turning signal.\n"
        f"Market breadth has exploded to the upside.\n"
        f"Watch for follow-through over next 2-3 days.\n"
        f"5d ratio: {ratio} | SPY: {spy_str} vs 200MA: {ma_str}\n"
        f"Regime will flip GREEN if SPY reclaims 200MA with sustained breadth."
    )

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    try:
        resp = requests.post(SLACK_WEBHOOK_ALERTS, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("THRUST alert sent: %d stocks up 4%%+", up_count)
    except Exception as e:
        log.error("THRUST alert failed: %s", e)


def _build_cycle_chain(history: list) -> str:
    """Build a deduplicated state progression string from history.
    e.g. RED › THRUST › GREEN › *THRUST*  (current state bolded)
    """
    # Deduplicate consecutive identical states
    chain = []
    for entry in history:
        s = entry.get("market_state", "")
        if not s:
            continue
        if not chain or chain[-1] != s:
            chain.append(s)
    # Keep last 5 distinct states for readability
    chain = chain[-5:]
    if not chain:
        return ""
    parts = [f"*{s}*" if i == len(chain) - 1 else s for i, s in enumerate(chain)]
    return "Cycle: " + " › ".join(parts)


def send_state_change_alert(record: dict, prev_state: str | None, history: list | None = None):
    """Send state change alert to #market-alerts."""
    if not SLACK_WEBHOOK_ALERTS:
        log.info("SLACK_WEBHOOK_MARKET_ALERTS not set — skipping state change alert.")
        return

    state = record["market_state"]
    state_emoji = {
        "THRUST": "🚨", "GREEN": "✅", "CAUTION": "🟡",
        "COOLING": "🧊", "DANGER": "⚠️", "RED": "🔴", "BLACKOUT": "⛔",
        "EXTENDED": "🌡️", "STEADY-UPTREND": "🟢",
        "TREND-FOLLOW": "🌊",
    }
    emoji = state_emoji.get(state, "📊")

    prev_str = prev_state or "UNKNOWN"
    fg_str = f"{record['fg']:.1f}" if record["fg"] is not None else "n/a"
    spy_str = f"${record['spy_price']:.2f}" if record["spy_price"] is not None else "n/a"

    if state == "THRUST":
        action = (
            "ACTION: Start building watchlist.\n"
            "Watch for 5-day ratio > 1.5 to confirm entry.\n"
            "Do NOT size full yet."
        )
    elif state == "GREEN":
        action = (
            "ACTION: Full conditions met.\n"
            "Size at 10-15% for high conviction.\n"
            "Current watchlist candidates: check weekly report."
        )
    elif state == "CAUTION":
        action = (
            "ACTION: Market recovering — half size only.\n"
            "Build watchlist now. Highest conviction setups only.\n"
            "Wait for GREEN before sizing full."
        )
    elif state == "COOLING":
        action = (
            "ACTION: Market fading from GREEN — sell-off phase.\n"
            "Trim extended positions. Tighten stops on all holdings.\n"
            "Do NOT add new positions. Wait for re-entry signal."
        )
    elif state == "DANGER":
        action = (
            "ACTION: No new entries.\n"
            "Raise stops on all open positions.\n"
            "Consider peeling weak names."
        )
    elif state == "EXTENDED":
        action = (
            "ACTION: Parabolic tape — NO new entries.\n"
            "Tighten stops on all holdings. Trim extended names.\n"
            "Hold what's working; don't chase. Re-engage when SPY pulls back to ATR mult < 7."
        )
    elif state == "STEADY-UPTREND":
        action = (
            "ACTION: Steady uptrend between thrust days — half size only.\n"
            "Entries allowed on confirmed RS leaders.\n"
            "Watch for THRUST or GREEN confirmation to upsize."
        )
    elif state == "TREND-FOLLOW":
        action = (
            "ACTION: Trend-persistence regime — full size, entries allowed.\n"
            "SMA50 rising · near 20d high · participation healthy · vol calm.\n"
            "5d ratio shown as thrust-strength gauge only — not a state gate."
        )
    elif state == "BLACKOUT":
        action = (
            "ACTION: Seasonal no-trade period.\n"
            "No new entries until blackout ends.\n"
            "Existing positions: trail stops only."
        )
    else:
        action = (
            "ACTION: No new trades.\n"
            "Wait for breadth confirmation.\n"
            "Monitor daily for state change."
        )

    adv = record.get("adv_total")
    dec = record.get("dec_total")
    ad_str = f"{adv:,} / {dec:,}" if adv is not None and dec is not None else "n/a"

    cycle_line = _build_cycle_chain(history) if history else ""

    # Cohort Health line (spec §2.4) + inverse-divergence resilient note
    # (spec §2.3 — index RED, cohort HEALTHY → one-liner, no separate alert).
    cohort_line = ""
    cohort = record.get("cohort")
    if cohort:
        try:
            from agents.market import cohort_health
            cohort_line = "\n" + cohort_health.format_cohort_line(cohort)
            if cohort_health.is_resilient(state, cohort.get("label")):
                cohort_line += ("\n🟢 Cohort resilient — your names are "
                                "holding up better than the index.")
        except Exception as e:
            log.warning("Cohort Slack line skipped: %s", e)

    # Confidence context annotation
    confidence_context = record.get("confidence_context")
    post_thrust_floor = record.get("post_thrust_floor_active", False)
    fg_raw = record.get("fg") or 0
    context_line = ""
    if confidence_context == "high_confidence_recovery":
        context_line = (
            f"\n⚡ HIGH-CONFIDENCE THRUST — F&G at {fg_raw:.0f} (Extreme Fear). "
            f"Reversal signal. Watch for 2nd THRUST to confirm GREEN."
        )
    elif confidence_context == "extreme_greed_caution":
        context_line = (
            f"\n⚠️ EXTREME GREED ({fg_raw:.0f}) + breadth deteriorating — "
            f"downgrade confirmed without 2-day wait. Risk is asymmetric."
        )
    elif post_thrust_floor:
        context_line = "\n⚡ Post-THRUST floor applied — minimum CAUTION maintained."

    text = (
        f"{emoji} *MARKET MONITOR — STATE CHANGE*\n"
        f"{record['date']}\n\n"
        f"Previous: {prev_str} → Now: *{state}*\n"
        + (f"{cycle_line}\n" if cycle_line else "")
        + (f"{context_line}\n" if context_line else "")
        + f"\nStocks up 4%+ today: {record['up_4_today']} | Down 4%+: {record['down_4_today']}\n"
        f"Adv / Dec (all movers): {ad_str}\n"
        f"5-day ratio: {record['ratio_5day']}\n"
        f"10-day ratio: {record['ratio_10day']}\n"
        f"F&G: {fg_str} | SPY: {spy_str}"
        + cohort_line
        + f"\n\n{action}"
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    try:
        resp = requests.post(SLACK_WEBHOOK_ALERTS, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info(f"State change alert sent: {prev_str} → {state}")
    except Exception as e:
        log.error(f"State change alert failed: {e}")


def send_confirmation_alert(record: dict):
    """Send confirmation alert when transitioning to GREEN from THRUST/CAUTION."""
    if not SLACK_WEBHOOK_ALERTS:
        return

    fg_str = f"{record['fg']:.1f}" if record["fg"] is not None else "n/a"

    text = (
        f"✅ *MARKET MONITOR — CONFIRMED RECOVERY*\n"
        f"{record['date']}\n\n"
        f"5-day ratio: {record['ratio_5day']} ✅\n"
        f"10-day ratio: {record['ratio_10day']} ✅\n"
        f"F&G: {fg_str} ✅\n"
        f"SPY above 200d MA: {'✅' if record['spy_above_200d'] else '❌'}\n\n"
        f"ACTION: Full conditions met.\n"
        f"Size at 10-15% for high conviction.\n"
        f"Current watchlist candidates: check weekly report."
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]

    try:
        resp = requests.post(SLACK_WEBHOOK_ALERTS, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Confirmation alert sent.")
    except Exception as e:
        log.error(f"Confirmation alert failed: {e}")


# ----------------------------
# Main
# ----------------------------
def run_market_monitor(date: datetime.date | None = None):
    """Main entry point for the market monitor."""
    if date is None:
        et = pytz.timezone('US/Eastern')
        date = datetime.datetime.now(et).date()

    log.info(f"=== Market Monitor starting — {date.isoformat()} ===")

    # Load history
    history = load_history()
    log.info(f"Loaded {len(history)} days of history")

    # Determine previous state
    prev_state = history[-1]["market_state"] if history else None
    log.info(f"Previous market state: {prev_state or 'UNKNOWN'}")

    # Load last_thrust_date and consecutive_weak_days from trading_state.json
    last_thrust_date = None
    consecutive_weak_days = 0
    extended_since_date = None
    days_below_21ema = 0
    cohort_last_divergence_label = None
    if os.path.exists(TRADING_STATE_FILE):
        try:
            with open(TRADING_STATE_FILE) as f:
                ts = json.load(f)
            last_thrust_date = ts.get("last_thrust_date")
            consecutive_weak_days = ts.get("consecutive_weak_days", 0)
            extended_since_date = ts.get("extended_since_date")
            days_below_21ema = ts.get("days_below_21ema", 0)
            cohort_last_divergence_label = ts.get("cohort_last_divergence_label")
        except Exception:
            pass

    # Fetch all breadth data
    session = make_session()
    today_data = fetch_breadth_data(session)

    log.info(
        "Up 4%%: %d | Down 4%%: %d | Universe: %d | Adv/Dec: %s/%s",
        today_data["up_4_today"], today_data["down_4_today"],
        today_data.get("universe_size", 0),
        today_data.get("adv_total", "n/a"), today_data.get("dec_total", "n/a"),
    )
    log.info(f"Up 25% qtr: {today_data['up_25_quarter']} | Down 25% qtr: {today_data['down_25_quarter']}")

    # Calculate metrics
    metrics = calculate_metrics(history, today_data)
    log.info(f"Ratios — today: {metrics['ratio_today']} | 5d: {metrics['ratio_5day']} | 10d: {metrics['ratio_10day']}")
    log.info(f"Thrust: {metrics['thrust']} | SPY above 200d: {metrics['spy_above_200d']}")

    # Classify market state
    state, message, classify_ctx = classify_market_state(
        metrics, today_data.get("fg"), today_data.get("spy_price"),
        metrics["spy_above_200d"], today_data, date, prev_state,
        last_thrust_date=last_thrust_date,
        consecutive_weak_days=consecutive_weak_days,
        extended_since_date=extended_since_date,
        days_below_21ema=days_below_21ema,
    )
    log.info(f"Market state: {state} — {message}")

    # Compute updated consecutive_weak_days: reset on GREEN/THRUST/BLACKOUT/STEADY/EXTENDED, else increment
    new_consecutive_weak_days = (
        0 if state in ("GREEN", "THRUST", "BLACKOUT", "STEADY-UPTREND", "EXTENDED", "TREND-FOLLOW")
        else consecutive_weak_days + 1
    )

    # ── Cohort Health Index (Phase 1 — informational only, spec
    # docs/specs/cohort-health-index.md). NON-FATAL by contract: any failure
    # → record written without the cohort block, monitor otherwise unchanged.
    # Does NOT touch market_state, gate decisions, or sizing.
    cohort = None
    fire_cohort_divergence = False
    try:
        from agents.market import cohort_health
        cohort = cohort_health.compute_cohort_health()
        if cohort:
            divergent = cohort_health.is_divergent(state, cohort["label"])
            fire_cohort_divergence = cohort_health.should_alert_divergence(
                state, cohort["label"], cohort_last_divergence_label)
            cohort["divergence"] = divergent
            # Dedup state: keep the alerted label while divergence holds;
            # clear when it ends so a fresh episode re-alerts.
            cohort["divergence_alerted_label"] = (
                cohort["label"] if divergent else None)
    except Exception as e:
        log.warning("Cohort health step failed (non-fatal): %s", e)
        cohort = None
        fire_cohort_divergence = False

    # Build and save daily record
    record = build_daily_record(date, today_data, metrics, state, message, classify_ctx)
    if cohort:
        record["cohort"] = cohort
    save_daily(record)

    # Update rolling history (keep last 30 trading days)
    history.append(record)
    history = history[-30:]
    save_history(history)

    # Update trading_state.json (track last_thrust_date before saving)
    if state == "THRUST":
        last_thrust_date = record["date"]
    update_trading_state(record, new_consecutive_weak_days=new_consecutive_weak_days)

    # Send Slack alerts
    state_changed = prev_state is not None and state != prev_state

    if state == "THRUST":
        send_thrust_alert(record)

    if state_changed:
        send_state_change_alert(record, prev_state, history)
        # Append to dashboard recent-events feed.
        try:
            _append_recent_event(
                category="market_state",
                title=f"Market: {prev_state} → {state}",
                date=record.get("date"),
                severity={"RED": "high", "DANGER": "high", "BLACKOUT": "high",
                          "EXTENDED": "high",
                          "COOLING": "med", "CAUTION": "med",
                          "STEADY-UPTREND": "low", "TREND-FOLLOW": "low",
                          "GREEN": "low", "THRUST": "low"}.get(state, "med"),
            )
        except Exception as e:
            log.warning(f"Failed to append market_state event: {e}")
        # Send confirmation alert when moving to GREEN from THRUST, CAUTION, or COOLING
        if state == "GREEN" and prev_state in ("THRUST", "CAUTION", "COOLING"):
            send_confirmation_alert(record)

    # ⚠ COHORT DIVERGENCE — index bullish while cohort STRESS/CARNAGE
    # (spec §2.3). Deduped once per label change; non-fatal.
    if fire_cohort_divergence and cohort:
        try:
            from agents.market import cohort_health
            cohort_health.send_divergence_alert(
                state, cohort, SLACK_WEBHOOK_ALERTS)
        except Exception as e:
            log.warning("Cohort divergence alert failed (non-fatal): %s", e)

    # ── EventBridge: MarketDailySummary ──────────────────────────────────
    # Fires end-of-day market state to finviz-events bus.
    # XPublisher currently skips this event (no-op).
    #
    # TODO: Future subscribers on MarketDailySummary:
    #   - SlackPublisher Lambda → replaces direct send_daily_summary() webhook calls
    #   - DiscordPublisher Lambda → Discord channel
    #
    # TODO: PreMarketPulse (morning tweet, 8am ET) should be fired from
    #   premarket_alert.py instead — it has Alpaca pre-market data and
    #   runs at the right time. Wire publish_pre_market_pulse() there
    #   when connecting premarket_alert.py to the bus.
    try:
        from agents.publishing.event_publisher import publish_market_daily_summary
        publish_market_daily_summary(
            date=record["date"],
            market_state=record["market_state"],
            fear_greed=int(today_data.get("fg") or 0),
            spy_above_200ma=record["spy_above_200d"],
        )
    except Exception as e:
        log.warning(f"MarketDailySummary publish skipped (non-fatal): {e}")

    log.info(f"=== Market Monitor complete — {state} ===")
    return record


if __name__ == "__main__":
    run_market_monitor()
