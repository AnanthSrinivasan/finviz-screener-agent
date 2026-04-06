#!/usr/bin/env python3
"""Market Pulse — intraday watchlist scanner.

Runs 4x daily during market hours.  Fetches current price + EMAs for
each ticker in data/watchlist.json, checks for actionable setups, and
sends a single Slack message when something triggers.  Silent otherwise.
"""

import json, glob, logging, os, sys
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf
import pandas as pd

log = logging.getLogger("market_pulse")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = os.environ.get("DATA_DIR", "data")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

STATE_EMOJI = {
    "THRUST": "🟢", "GREEN": "🟢", "CAUTION": "🟡",
    "DANGER": "🟠", "RED": "🔴", "BLACKOUT": "⚫",
}

# ── helpers ──────────────────────────────────────────────────────────

def load_watchlist() -> list[dict]:
    path = os.path.join(DATA_DIR, "watchlist.json")
    with open(path) as f:
        return json.load(f)["watchlist"]


def load_latest_json(prefix: str) -> dict | None:
    pattern = os.path.join(DATA_DIR, f"{prefix}*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    # exclude history file
    files = [f for f in files if "history" not in f]
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def compute_emas(ticker: str) -> dict | None:
    """Return current price, 10 EMA and 21 EMA using 60 days of daily data."""
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="3mo")
        if hist.empty or len(hist) < 21:
            return None
        close = hist["Close"]
        ema10 = close.ewm(span=10, adjust=False).mean().iloc[-1]
        ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
        price = close.iloc[-1]
        prev_close = close.iloc[-2] if len(close) >= 2 else price
        return {"price": price, "ema10": ema10, "ema21": ema21, "prev_close": prev_close}
    except Exception as e:
        log.warning("yfinance error for %s: %s", ticker, e)
        return None


def check_alerts(item: dict, data: dict) -> list[str]:
    """Return list of alert strings for one watchlist ticker."""
    alerts = []
    price = data["price"]
    ema10 = data["ema10"]
    ema21 = data["ema21"]
    prev_close = data["prev_close"]
    stop = item.get("stop")

    # Stop hit
    if stop is not None and price <= stop:
        alerts.append(f"🚨 *STOP HIT* — ${price:.2f} ≤ stop ${stop}")
        return alerts  # stop dominates

    # 21 EMA proximity (within 2%)
    if ema21 > 0 and abs(price - ema21) / ema21 <= 0.02:
        alerts.append(f"touching 21 EMA (${ema21:.2f})")

    # 10 EMA proximity (within 2%)
    if ema10 > 0 and abs(price - ema10) / ema10 <= 0.02:
        alerts.append(f"near 10 EMA (${ema10:.2f})")

    # New intraday high vs yesterday close
    if price > prev_close and (price - prev_close) / prev_close > 0.005:
        alerts.append(f"up {(price - prev_close) / prev_close:.1%} vs prev close")

    return alerts


def format_time_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%-I:%M%p ET")


def build_message(triggered: list[tuple[dict, dict, list[str]]],
                  market: dict | None, quality: dict | None) -> str:
    """Build the Slack message body."""
    time_str = format_time_et()
    lines = [f"📊 *Market Pulse — {time_str}*"]

    if market:
        state = market.get("market_state", "?")
        emoji = STATE_EMOJI.get(state, "⚪")
        fg = market.get("fg", "?")
        lines.append(f"State: {emoji} {state} (F&G {fg})")

    lines.append("")

    for item, data, alerts in triggered:
        ticker = item["ticker"]
        price = data["price"]
        stop = item.get("stop")
        thesis = item.get("thesis", "")

        alert_str = ", ".join(alerts)
        lines.append(f"🎯 *{ticker}* — ${price:.2f} {alert_str}")

        detail_parts = []
        if thesis:
            detail_parts.append(f"Thesis: {thesis}")
        if stop:
            detail_parts.append(f"Stop: ${stop}")
        if quality and ticker in quality:
            q = quality[ticker]
            detail_parts.append(f"Q{q.get('q_rank', '?')} {q.get('stage_label', '')}")
        if detail_parts:
            lines.append(f"   {'. '.join(detail_parts)}")
        lines.append("")

    return "\n".join(lines).strip()


def send_slack(text: str):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — printing to stdout.")
        print(text)
        return
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    resp.raise_for_status()
    log.info("Slack message sent (%d chars).", len(text))


# ── main ─────────────────────────────────────────────────────────────

def main():
    watchlist = load_watchlist()
    active = [w for w in watchlist if w.get("status") == "watching"]
    if not active:
        log.info("Watchlist empty — nothing to do.")
        return

    market = load_latest_json("market_monitor_")
    quality = load_latest_json("daily_quality_")

    triggered: list[tuple[dict, dict, list[str]]] = []

    for item in active:
        ticker = item["ticker"]
        data = compute_emas(ticker)
        if data is None:
            log.warning("Skipping %s — no data.", ticker)
            continue
        alerts = check_alerts(item, data)
        if alerts:
            triggered.append((item, data, alerts))
            log.info("%s: %s", ticker, alerts)
        else:
            log.info("%s: $%.2f — no alerts.", ticker, data["price"])

    if not triggered:
        log.info("Silent run — no actionable alerts.")
        return

    msg = build_message(triggered, market, quality)
    send_slack(msg)


if __name__ == "__main__":
    main()
