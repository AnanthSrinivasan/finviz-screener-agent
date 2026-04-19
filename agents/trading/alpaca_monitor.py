#!/usr/bin/env python3
# ----------------------------
# Alpaca Paper Position Monitor
# ----------------------------
# Runs after market close (via position-monitor.yml).
# For each open Alpaca paper position:
#   - Stop hit  → market sell
#   - Stage 3/4 → market sell
#   - Otherwise → hold, log P&L to Slack

import ast
import csv
import json
import os
import logging
import datetime
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DATA_DIR          = os.environ.get("DATA_DIR", "data")
PAPER_STOPS_FILE  = os.path.join(DATA_DIR, "paper_stops.json")


def alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }


def slack_send(text: str):
    if not SLACK_WEBHOOK_URL:
        log.info("[Slack] %s", text)
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.error("Slack send failed: %s", e)


# ----------------------------
# Alpaca helpers
# ----------------------------
def get_positions() -> list:
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/positions",
            headers=alpaca_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Failed to get positions: %s", e)
        return []


def place_sell(symbol: str, qty: str) -> dict:
    try:
        resp = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            json={
                "symbol":        symbol,
                "qty":           qty,
                "side":          "sell",
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Sell order failed for %s: %s", symbol, e)
        slack_send(":x: *SELL FAILED* " + symbol + ": " + str(e))
        return {}


# ----------------------------
# Stop loss data
# ----------------------------
def load_stops() -> dict:
    if os.path.exists(PAPER_STOPS_FILE):
        try:
            with open(PAPER_STOPS_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not load stops: %s", e)
    return {}


def save_stops(stops: dict):
    with open(PAPER_STOPS_FILE, "w") as f:
        json.dump(stops, f, indent=2)
    log.info("paper_stops.json updated.")


# ----------------------------
# Stage lookup from screener CSV
# ----------------------------
def get_ticker_stage(ticker: str) -> int:
    """
    Look up the Weinstein stage for a ticker from the most recent
    daily screener CSV (searches back up to 7 days).
    Returns stage int, or 0 if not found.
    """
    today = datetime.date.today()
    for i in range(7):
        date_str = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(DATA_DIR, "finviz_screeners_" + date_str + ".csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("Ticker") or "").strip() != ticker:
                        continue
                    stage_raw = row.get("Stage", "")
                    if stage_raw and stage_raw not in ("", "nan"):
                        try:
                            stage_dict = ast.literal_eval(stage_raw)
                            return int(stage_dict.get("stage", 0))
                        except Exception:
                            return 0
        except Exception as e:
            log.warning("Could not read %s: %s", path, e)

    return 0  # Not found in screener — hold


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    log.info("=== Alpaca monitor starting — %s ===", today)

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.error("Missing Alpaca credentials — aborting.")
        slack_send(":x: *Alpaca monitor failed* — missing API credentials")
        raise SystemExit(1)

    positions = get_positions()

    if not positions:
        log.info("No open paper positions to monitor.")
        slack_send(":bar_chart: *Alpaca Monitor " + today + "* — No open positions.")
        raise SystemExit(0)

    log.info("Monitoring %d position(s)", len(positions))
    stops        = load_stops()
    sells_placed = 0

    for pos in positions:
        ticker      = pos.get("symbol", "")
        qty         = pos.get("qty", "0")
        entry_price = float(pos.get("avg_entry_price") or 0)
        current     = float(pos.get("current_price") or 0)
        pl_dollar   = float(pos.get("unrealized_pl") or 0)
        pl_pct      = float(pos.get("unrealized_plpc") or 0) * 100

        stop_info  = stops.get(ticker, {})
        stop_price = stop_info.get("stop_price")

        sell_reason = None

        # Check stop loss
        if stop_price is not None and current > 0 and current <= float(stop_price):
            sell_reason = (
                "stop hit (price $" + str(round(current, 2))
                + " <= stop $" + str(round(float(stop_price), 2)) + ")"
            )

        # Check Stage 3 or 4 deterioration
        if sell_reason is None:
            stage = get_ticker_stage(ticker)
            if stage in (3, 4):
                labels = {3: "Distribution", 4: "Downtrend"}
                sell_reason = "Stage " + str(stage) + " (" + labels[stage] + ") in screener"

        if sell_reason:
            log.info("SELL %s — %s", ticker, sell_reason)
            result = place_sell(ticker, qty)
            if result:
                sells_placed += 1
                stops.pop(ticker, None)

                pl_sign  = "+" if pl_dollar >= 0 else ""
                pct_sign = "+" if pl_pct    >= 0 else ""
                slack_send(
                    ":large_red_circle: *SELL PLACED* " + ticker + "\n"
                    "Reason: " + sell_reason + "\n"
                    "Qty: " + qty
                    + " | Entry: $" + str(round(entry_price, 2))
                    + " | Exit: ~$" + str(round(current, 2)) + "\n"
                    "P&L: " + pl_sign + str(int(pl_dollar))
                    + " (" + pct_sign + str(round(pl_pct, 1)) + "%)"
                )
        else:
            # Hold — report current P&L
            stop_str = ""
            if stop_price is not None:
                stop_str = " | Stop: $" + str(round(float(stop_price), 2))
            pl_sign  = "+" if pl_dollar >= 0 else ""
            pct_sign = "+" if pl_pct    >= 0 else ""
            slack_send(
                ":white_circle: *HOLD* " + ticker
                + " — $" + str(round(current, 2))
                + " | P&L: " + pl_sign + str(int(pl_dollar))
                + " (" + pct_sign + str(round(pl_pct, 1)) + "%)"
                + stop_str
            )

    save_stops(stops)

    slack_send(
        ":bar_chart: *Alpaca Monitor Summary — " + today + "*\n"
        "Positions monitored: " + str(len(positions)) + "\n"
        "Sells placed: " + str(sells_placed)
    )

    # Regenerate Claude model portfolio page (non-fatal).
    try:
        from utils.generators.generate_portfolio import main as _gen_portfolio
        _gen_portfolio()
    except Exception as e:
        log.warning("Portfolio page generation failed: %s", e)

    log.info("=== Alpaca monitor done — %d sell(s) placed ===", sells_placed)
