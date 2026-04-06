#!/usr/bin/env python3
# ----------------------------
# Pre-Market Gap-Up Alert (BGU)
# ----------------------------
# Runs at 9:00 AM ET (14:00 UTC) Mon-Fri.
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
        return

    # Sort by gap size
    alerts.sort(key=lambda x: x["pct"], reverse=True)

    today_str = datetime.date.today().isoformat()
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
        return

    try:
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Pre-market alert sent: %s", [a["ticker"] for a in alerts])
    except Exception as e:
        log.error("Slack send failed: %s", e)


if __name__ == "__main__":
    run_premarket_alert()
