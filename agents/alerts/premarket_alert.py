#!/usr/bin/env python3
# ----------------------------
# Pre-Market Gap-Up Alert (BGU)
# ----------------------------
# Runs at 9:00 AM ET (13:00 UTC) Mon-Fri.
# Scans tickers from the last 5 days of screener CSVs and fires a Slack
# alert for any that are gapping up 5%+ in pre-market. BGU = Before Gap-Up.
#
# Tickers must have appeared on radar for >= 2 days OR hit >= 2 screeners.
# Open positions are skipped (already on radar, no new alert needed).
# Alert is suppressed when market state is RED or BLACKOUT.
# ----------------------------

import os
import json
import logging
import datetime
import requests
import pandas as pd
from glob import glob
from glob import glob as _glob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SLACK_WEBHOOK        = os.environ.get("SLACK_WEBHOOK_URL", "")
DATA_DIR             = os.environ.get("DATA_DIR", "data")
PREMARKET_THRESHOLD  = float(os.environ.get("PREMARKET_THRESHOLD", "5.0"))
TRADING_STATE_FILE   = os.path.join(DATA_DIR, "trading_state.json")
POSITIONS_FILE       = os.path.join(DATA_DIR, "positions.json")
ALPACA_API_KEY       = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY    = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL      = "https://data.alpaca.markets"


# ----------------------------
# Market state gate
# ----------------------------
def load_market_state() -> str:
    """Return current market state string, or 'UNKNOWN' if unavailable."""
    try:
        with open(TRADING_STATE_FILE) as f:
            ts = json.load(f)
        return ts.get("market_state", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


# ----------------------------
# Pre-market price
# ----------------------------
def get_premarket_change(ticker: str) -> tuple:
    """
    Returns (premarket_pct_change, premarket_price) via Alpaca snapshot.
    Uses latestTrade.p as current price and prevDailyBar.c as previous close.
    Returns (0.0, 0.0) if data is unavailable.
    """
    if not ALPACA_API_KEY:
        return 0.0, 0.0
    try:
        resp = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/snapshot",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY},
            params={"feed": "iex"},
            timeout=10,
        )
        if not resp.ok:
            return 0.0, 0.0
        snap = resp.json()
        pre  = snap.get("latestTrade", {}).get("p")
        prev = snap.get("prevDailyBar", {}).get("c")
        if pre and prev and prev > 0:
            pct = (pre - prev) / prev * 100
            return round(pct, 2), round(pre, 2)
    except Exception as e:
        log.warning("Alpaca snapshot failed for %s: %s", ticker, e)
    return 0.0, 0.0


# ----------------------------
# Screener CSV aggregation
# ----------------------------
def collect_recent_screener_tickers(lookback_days: int = 5) -> dict:
    """
    Returns dict: {ticker: {"days_seen": N, "screens": N, "sector": str, "stage": str}}
    aggregated from the last N days of finviz_screeners_YYYY-MM-DD.csv files.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
    files = sorted(glob(os.path.join(DATA_DIR, "finviz_screeners_*.csv")))
    recent = []
    for f in files:
        try:
            date_str = os.path.basename(f).replace("finviz_screeners_", "").replace(".csv", "")
            if datetime.date.fromisoformat(date_str) >= cutoff:
                recent.append(f)
        except ValueError:
            continue

    ticker_data = {}
    for fpath in recent:
        try:
            df = pd.read_csv(fpath)
            for _, row in df.iterrows():
                t = str(row.get("Ticker", row.get("ticker", ""))).strip()
                if not t or t == "nan":
                    continue
                screens_val = int(row.get("Appearances", row.get("screener_count", row.get("screens_hit", 1))) or 1)
                if t not in ticker_data:
                    ticker_data[t] = {
                        "days_seen": 0,
                        "screens":   screens_val,
                        "sector":    str(row.get("Sector", row.get("sector", "")) or ""),
                        "stage":     str(row.get("stage", "") or ""),
                    }
                ticker_data[t]["days_seen"] += 1
                ticker_data[t]["screens"] = max(ticker_data[t]["screens"], screens_val)
        except Exception as e:
            log.warning("Could not read %s: %s", fpath, e)

    return ticker_data


# ----------------------------
# Open positions
# ----------------------------
def load_open_position_tickers() -> set:
    """Returns set of tickers in open positions (rules engine state)."""
    try:
        with open(POSITIONS_FILE) as f:
            pos = json.load(f)
        return {p["ticker"] for p in pos.get("open_positions", []) if p.get("ticker")}
    except Exception:
        return set()


# ----------------------------
# Focus list entry scan
# ----------------------------

def _fetch_daily_bars(ticker: str, limit: int = 60) -> pd.DataFrame:
    """Fetch daily OHLCV bars from Alpaca. Returns DataFrame with close prices."""
    if not ALPACA_API_KEY:
        return pd.DataFrame()
    try:
        resp = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY},
            params={"timeframe": "1Day", "limit": limit, "feed": "iex", "adjustment": "raw"},
            timeout=10,
        )
        if not resp.ok:
            return pd.DataFrame()
        bars = resp.json().get("bars", [])
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df = df.sort_values("t").reset_index(drop=True)
        return df
    except Exception as e:
        log.warning("Bars fetch failed for %s: %s", ticker, e)
        return pd.DataFrame()


def _load_conviction(ticker: str) -> tuple:
    """Return (q_rank, appearances) from latest daily_quality JSON and screener CSV."""
    q_rank = 0
    appearances = 0
    try:
        quality_files = sorted(_glob(os.path.join(DATA_DIR, "daily_quality_*.json")))
        if quality_files:
            with open(quality_files[-1]) as f:
                q_data = json.load(f)
            q_rank = q_data.get(ticker, {}).get("q_rank", 0)
    except Exception:
        pass
    try:
        csv_files = sorted(_glob(os.path.join(DATA_DIR, "finviz_screeners_*.csv")))
        if csv_files:
            df = pd.read_csv(csv_files[-1])
            row = df[df["Ticker"] == ticker]
            if not row.empty:
                appearances = int(row.iloc[0].get("Appearances", 1) or 1)
    except Exception:
        pass
    return q_rank, appearances


def _sizing_label(q_rank: int, appearances: int) -> str:
    if q_rank >= 85 or appearances >= 4:
        return "AGGRESSIVE (8-10%)"
    elif q_rank >= 70 or appearances >= 2:
        return "NORMAL (4-6%)"
    else:
        return "REDUCED (2-3%)"


def scan_focus_list(market_state: str):
    """
    Scans the focus list every morning at 9am ET.
    Always sends a Slack summary of ALL focus tickers with:
    - Setup type + sizing suggestion if near entry (21 EMA PB / 50 SMA PB / Breakout)
    - Distance from key levels if not at entry yet
    """
    log.info("=== Focus list entry scan starting ===")
    watchlist_path = os.path.join(DATA_DIR, "watchlist.json")
    try:
        with open(watchlist_path) as f:
            wl_data = json.load(f)
        focus_tickers = [
            e["ticker"] for e in wl_data.get("watchlist", [])
            if e.get("priority") == "focus" and e.get("status") != "archived"
        ]
    except Exception as e:
        log.warning("Could not load watchlist: %s", e)
        return

    if not focus_tickers:
        log.info("Focus list is empty — no scan needed.")
        return

    open_tickers = load_open_position_tickers()
    log.info("Scanning %d focus tickers: %s", len(focus_tickers), focus_tickers)

    at_setup = []   # ready to enter
    watching = []   # tracked but not at entry yet

    for ticker in focus_tickers:
        if ticker in open_tickers:
            log.debug("Skipping %s — already in open position", ticker)
            continue

        bars = _fetch_daily_bars(ticker, limit=60)
        if bars.empty or len(bars) < 22:
            log.debug("Not enough bars for %s", ticker)
            continue

        closes = bars["c"]
        ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
        sma50 = closes.rolling(window=min(50, len(closes))).mean().iloc[-1]
        high20 = bars["h"].iloc[-21:-1].max() if len(bars) >= 21 else bars["h"].max()

        pct_change, price = get_premarket_change(ticker)
        if not price:
            price = closes.iloc[-1]

        pct_from_ema21  = (price - ema21)  / ema21  * 100
        pct_from_sma50  = (price - sma50)  / sma50  * 100
        pct_from_high20 = (price - high20) / high20 * 100
        premarket_str   = f" (pre-mkt {pct_change:+.1f}%)" if abs(pct_change) >= 1 else ""

        q_rank, appearances = _load_conviction(ticker)
        sizing = _sizing_label(q_rank, appearances)

        setup_type = None
        if -5 <= pct_from_ema21 <= 5:
            setup_type = "21 EMA PB"
            entry_note = f"${price:.2f} near 21 EMA ${ema21:.2f} ({pct_from_ema21:+.1f}%)"
        elif 0 <= pct_from_sma50 <= 5:
            setup_type = "50 SMA PB"
            entry_note = f"${price:.2f} near 50 SMA ${sma50:.2f} ({pct_from_sma50:+.1f}%)"
        elif 0 <= pct_from_high20 <= 2:
            setup_type = "Breakout"
            entry_note = f"${price:.2f} within {pct_from_high20:.1f}% of 20d high ${high20:.2f}"

        if setup_type:
            at_setup.append({
                "ticker": ticker, "setup_type": setup_type, "entry_note": entry_note,
                "sizing": sizing, "q_rank": q_rank, "premarket": premarket_str,
            })
            log.info("%s: AT SETUP %s — %s | sizing %s", ticker, setup_type, entry_note, sizing)
        else:
            watching.append({
                "ticker": ticker, "price": round(price, 2),
                "ema21": round(ema21, 2), "sma50": round(sma50, 2),
                "pct_from_ema21": round(pct_from_ema21, 1),
                "pct_from_sma50": round(pct_from_sma50, 1),
                "sizing": sizing, "q_rank": q_rank, "premarket": premarket_str,
            })
            log.info("%s: watching — EMA21 %+.1f%%, SMA50 %+.1f%%", ticker, pct_from_ema21, pct_from_sma50)

    if not at_setup and not watching:
        log.info("Focus scan: no tickers to report.")
        return

    today_str = datetime.date.today().isoformat()
    lines = [f":dart: *FOCUS LIST — {today_str}* ({market_state})\n"]

    if at_setup:
        lines.append("*:fire: AT ENTRY:*")
        for s in at_setup:
            setup_emoji = ":arrow_down_small:" if "PB" in s["setup_type"] else ":rocket:"
            sizing_emoji = ":large_green_circle:" if "AGGRESSIVE" in s["sizing"] else (":large_yellow_circle:" if "NORMAL" in s["sizing"] else ":white_circle:")
            lines.append(
                f"{setup_emoji} *{s['ticker']}* — {s['setup_type']}{s['premarket']}\n"
                f"  {s['entry_note']}\n"
                f"  {sizing_emoji} Size: *{s['sizing']}*  Q:{s['q_rank']}"
            )
        lines.append("")

    if watching:
        lines.append("*:eyes: WATCHING:*")
        for s in watching:
            sizing_emoji = ":large_green_circle:" if "AGGRESSIVE" in s["sizing"] else (":large_yellow_circle:" if "NORMAL" in s["sizing"] else ":white_circle:")
            lines.append(
                f":white_small_square: *{s['ticker']}* ${s['price']}{s['premarket']}  "
                f"21EMA {s['pct_from_ema21']:+.1f}% (${s['ema21']})  "
                f"50SMA {s['pct_from_sma50']:+.1f}% (${s['sma50']})\n"
                f"  {sizing_emoji} Size when ready: *{s['sizing']}*  Q:{s['q_rank']}"
            )

    payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]}

    if not SLACK_WEBHOOK:
        log.warning("SLACK_WEBHOOK_URL not set — focus tickers: %s", focus_tickers)
        return
    try:
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Focus list alert sent: %d at-setup, %d watching", len(at_setup), len(watching))
    except Exception as e:
        log.error("Focus list Slack send failed: %s", e)


# ----------------------------
# Main
# ----------------------------
def run_premarket_alert():
    log.info("=== Pre-Market Gap-Up Alert (BGU) starting ===")

    # Market state gate — suppress in RED / BLACKOUT
    market_state = load_market_state()
    if market_state in ("RED", "BLACKOUT"):
        log.info("Market state is %s — suppressing pre-market alert. No entries allowed.", market_state)
        return

    ticker_data   = collect_recent_screener_tickers(lookback_days=5)
    open_tickers  = load_open_position_tickers()
    log.info("Checking %d tickers (last 5 screener days)", len(ticker_data))

    alerts = []
    for ticker, meta in ticker_data.items():
        # Must have meaningful screener presence
        if meta["days_seen"] < 2 and meta["screens"] < 2:
            continue
        # Skip if already in an open position
        if ticker in open_tickers:
            continue

        pct, price = get_premarket_change(ticker)
        if pct >= PREMARKET_THRESHOLD:
            alerts.append({
                "ticker":    ticker,
                "pct":       pct,
                "price":     price,
                "days_seen": meta["days_seen"],
                "screens":   meta["screens"],
                "sector":    meta["sector"],
                "stage":     meta["stage"],
            })

    if not alerts:
        log.info("No pre-market gap-up alerts today.")
    else:
        # Sort by gap size
        alerts.sort(key=lambda x: x["pct"], reverse=True)

    if alerts:
        today_str  = datetime.date.today().isoformat()
        thresh_str = str(int(PREMARKET_THRESHOLD))
        lines = [
            f":zap: *PRE-MARKET GAP-UP ALERT — {today_str}*",
            f"_Screener-tracked stocks gapping +{thresh_str}%+ before open_\n",
        ]

        for a in alerts:
            lines.append(
                f"*{a['ticker']}* +{a['pct']}% pre-market (est. ${a['price']})\n"
                f"  Screeners: {a['screens']} | Days on radar: {a['days_seen']}"
                + (f" | {a['sector']}" if a["sector"] else "")
                + f"\n  :chart_with_upwards_trend: https://finviz.com/quote.ashx?t={a['ticker']}"
            )

        lines.append("\n_Act at open — not mid-day chase. Confirm volume at bell._")

        payload = {
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }]
        }

        if not SLACK_WEBHOOK:
            log.warning("SLACK_WEBHOOK_URL not set — would have alerted: %s", [a["ticker"] for a in alerts])
        else:
            try:
                resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
                resp.raise_for_status()
                log.info("Pre-market alert sent: %s", [a["ticker"] for a in alerts])
            except Exception as e:
                log.error("Slack send failed: %s", e)

    # ── Focus list entry scan ──────────────────────────────────────────────────
    scan_focus_list(market_state)

    # ── SetupOfDay tweet via EventBridge ──────────────────────────────────────
    # Always fires at 9am ET (unless RED/BLACKOUT — already gated at top).
    # Reads yesterday's screener CSV, picks the top quality ticker.
    try:
        import glob as _glob
        import json as _json

        _csv_files = sorted(_glob.glob(os.path.join(DATA_DIR, "finviz_screeners_*.csv")))
        if _csv_files:
            import pandas as _pd
            _df = _pd.read_csv(_csv_files[-1])
            # Exclude open positions — don't tweet what's already held
            _open = load_open_position_tickers()
            _candidates = _df[~_df["Ticker"].isin(_open)]
            if _candidates.empty:
                _candidates = _df  # fallback: every result is held
            # Exclude tickers flagged out by 10% gate
            _candidates = _candidates[_candidates.get("_10pct_excluded", False) != True]
            # Exclude overextended tickers (ATR multiple from 50MA > 5x = bad entry)
            if "SMA50%" in _candidates.columns and "ATR%" in _candidates.columns:
                _atr_mult = _candidates["SMA50%"].astype(float) / _candidates["ATR%"].astype(float).replace(0, float("nan"))
                _not_extended = _candidates[_atr_mult <= 5.0]
                if not _not_extended.empty:
                    _candidates = _not_extended
            if not _candidates.empty:
                _best = _candidates.nlargest(1, "Quality Score").iloc[0]
                _ticker   = str(_best["Ticker"])
                _sma50pct = float(_best.get("SMA50%") or 0)

                # Fetch current price from Alpaca (pre-market available at 9am ET)
                _pct, _price = get_premarket_change(_ticker)
                if not _price:
                    # Fall back to previous close from prevDailyBar if latestTrade not available
                    _price = 0.0

                _stop = round(_price / (1 + _sma50pct / 100), 2) if _price and _sma50pct else 0.0
                _vcp_raw = _best.get("VCP", {}) or {}
                _vcp_ok  = bool(_vcp_raw.get("vcp_possible", False)) if isinstance(_vcp_raw, dict) else False

                _top_pick = {
                    "ticker":        _ticker,
                    "quality_score": int(_best.get("Quality Score") or 0),
                    "section":       "stage2",
                    "rel_vol":       round(float(_best.get("Rel Volume") or 1.0), 1),
                    "vcp":           _vcp_ok,
                    "entry_price":   _price,
                    "stop_price":    _stop,
                }

                _ts = {}
                try:
                    with open(os.path.join(DATA_DIR, "trading_state.json")) as _f:
                        _ts = _json.load(_f)
                except Exception:
                    pass
                _market_state = _ts.get("market_state", "RED")
                _fear_greed   = int(_ts.get("fng") or 0)

                base_url = os.environ.get(
                    "PAGES_BASE_URL",
                    "https://ananthsrinivasan.github.io/finviz-screener-agent",
                )
                _date = os.path.basename(_csv_files[-1]).replace("finviz_screeners_", "").replace(".csv", "")

                from agents.publishing.event_publisher import publish_screener_completed
                publish_screener_completed(
                    date=_date,
                    market_state=_market_state,
                    fear_greed=_fear_greed,
                    top_pick=_top_pick,
                    total_tickers=len(_df),
                    preview_report_url=f"{base_url}/preview/{_date}.html",
                    full_report_url=f"{base_url}/reports/{_date}.html",
                )
        else:
            log.info("SetupOfDay: no screener CSV found — skipping tweet")
    except Exception as _e:
        log.warning("SetupOfDay EventBridge publish skipped (non-fatal): %s", _e)


if __name__ == "__main__":
    run_premarket_alert()
