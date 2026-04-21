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
# Schema migration & rules
# ----------------------------
def migrate_stop_entry(ticker: str, entry: dict, entry_price: float) -> dict:
    """
    Bring a paper_stops entry up to the full schema.
    Idempotent — safe to call on already-migrated entries.
    Fields added (when missing):
      highest_price_seen, peak_gain_pct, breakeven_activated,
      target1, target2, target1_hit
    """
    ep = entry.get("entry_price", entry_price) or entry_price
    if ep <= 0:
        return entry  # can't migrate without entry price

    entry.setdefault("highest_price_seen", round(ep, 2))
    entry.setdefault("peak_gain_pct", 0.0)
    entry.setdefault("breakeven_activated", False)
    entry.setdefault("target1", round(ep * 1.20, 2))
    entry.setdefault("target2", round(ep * 1.40, 2))
    entry.setdefault("target1_hit", False)
    return entry


def apply_paper_rules(ticker: str, entry: dict, current_price: float,
                     day_high: float, atr_pct: float) -> tuple:
    """
    Apply trailing stop rules to a paper position. Mirrors apply_minervini_rules
    but writes to paper_stops dict shape and returns (alerts, modified).
    Rules: ATR trail (silent) → breakeven at +20% → 10% trail at +30% → fade alert at peak-1×ATR.
    """
    alerts = []
    modified = False
    entry_price = entry.get("entry_price", 0) or 0
    if entry_price <= 0 or current_price <= 0:
        return alerts, modified

    atr_dollar = entry_price * (atr_pct / 100.0) if atr_pct > 0 else 0
    # Use the higher of current price or today's intraday high
    high_candidate = max(current_price, day_high or current_price)
    prev_high = entry.get("highest_price_seen", entry_price)
    if high_candidate > prev_high:
        entry["highest_price_seen"] = round(high_candidate, 2)
        prev_high = entry["highest_price_seen"]
        modified = True

    gain_pct = (current_price - entry_price) / entry_price * 100
    peak_gain_pct = (prev_high - entry_price) / entry_price * 100
    if peak_gain_pct > entry.get("peak_gain_pct", 0.0):
        entry["peak_gain_pct"] = round(peak_gain_pct, 2)
        modified = True

    current_stop = float(entry.get("stop_price") or 0)

    # ATR trail (silent, pre-breakeven only)
    if atr_dollar > 0 and gain_pct > 0 and not entry.get("breakeven_activated"):
        atr_trail = round(current_price - 2 * atr_dollar, 2)
        if atr_trail > current_stop:
            entry["stop_price"] = atr_trail
            current_stop = atr_trail
            modified = True

    # Breakeven at +20%
    if gain_pct >= 20 and not entry.get("breakeven_activated"):
        be_stop = round(entry_price * 1.005, 2)
        if be_stop > current_stop:
            entry["stop_price"] = be_stop
            current_stop = be_stop
        entry["breakeven_activated"] = True
        modified = True
        alerts.append(
            ":lock: [PAPER] " + ticker + " +" + str(round(gain_pct, 1))
            + "% — stop moved to breakeven $" + str(be_stop)
        )

    # +30% trail from highest (10% from high)
    if gain_pct >= 30:
        trail_stop = round(prev_high * 0.90, 2)
        if trail_stop > current_stop:
            entry["stop_price"] = trail_stop
            current_stop = trail_stop
            modified = True
            alerts.append(
                ":chart_with_upwards_trend: [PAPER] " + ticker + " +"
                + str(round(gain_pct, 1)) + "% — trailing stop raised to $"
                + str(trail_stop)
            )

    # Targets
    t1 = entry.get("target1", 0) or 0
    if t1 > 0 and current_price >= t1 and not entry.get("target1_hit"):
        entry["target1_hit"] = True
        modified = True
        alerts.append(
            ":dart: [PAPER] " + ticker + " HIT TARGET 1 $" + str(t1)
            + " — consider selling half, move stop to breakeven"
        )

    t2 = entry.get("target2", 0) or 0
    if t2 > 0 and current_price >= t2:
        alerts.append(
            ":dart::dart: [PAPER] " + ticker + " HIT TARGET 2 $" + str(t2)
            + " — trail remaining position tightly"
        )

    # 1×ATR fade alert (peak >= +20% AND price dropped 1 ATR below high)
    peak_gain = entry.get("peak_gain_pct", 0.0)
    if atr_dollar > 0 and peak_gain >= 20 and current_price < (prev_high - atr_dollar):
        last_fade = entry.get("last_fade_alert_gain_pct")
        if last_fade is None or (last_fade - gain_pct) >= 5:
            given_back = peak_gain - gain_pct
            alerts.append(
                ":warning: [PAPER] " + ticker + " fading — peak +"
                + str(round(peak_gain, 1)) + "%, now +"
                + str(round(gain_pct, 1)) + "% (gave back "
                + str(round(given_back, 1)) + "pp)"
            )
            entry["last_fade_alert_gain_pct"] = round(gain_pct, 2)
            modified = True
    elif "last_fade_alert_gain_pct" in entry:
        entry.pop("last_fade_alert_gain_pct", None)
        modified = True

    return alerts, modified


def fetch_day_high_atr(ticker: str) -> tuple:
    """Pull today's (day_high, atr_pct) via position_monitor's Finviz parser.
    Returns (0.0, 0.0) if unavailable — rules gracefully skip ATR-dependent branches."""
    try:
        from agents.trading.position_monitor import fetch_position_metrics
        m = fetch_position_metrics(ticker) or {}
        return float(m.get("day_high") or 0.0), float(m.get("atr_pct") or 0.0)
    except Exception as e:
        log.warning("day_high/ATR fetch failed for %s: %s", ticker, e)
        return 0.0, 0.0


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

        # Migrate schema for existing entries, seed defaults for new ones
        stop_info = stops.setdefault(ticker, {"entry_price": entry_price})
        if not stop_info.get("entry_price"):
            stop_info["entry_price"] = entry_price
        stop_info = migrate_stop_entry(ticker, stop_info, entry_price)

        # Apply trailing rules: ATR trail, breakeven, +30% trail, targets, fade
        day_high, atr_pct = fetch_day_high_atr(ticker)
        if atr_pct > 0 and "atr_pct" not in stop_info:
            stop_info["atr_pct"] = atr_pct
        rule_alerts, _ = apply_paper_rules(ticker, stop_info, current, day_high,
                                           stop_info.get("atr_pct", atr_pct))
        for msg in rule_alerts:
            slack_send(msg)

        stop_price = stop_info.get("stop_price")
        sell_reason = None

        # Check stop loss (now reflects any trailing raises applied above)
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
                    ":large_red_circle: *[PAPER] SELL PLACED* " + ticker + "\n"
                    "Reason: " + sell_reason + "\n"
                    "Qty: " + qty
                    + " | Entry: $" + str(round(entry_price, 2))
                    + " | Exit: ~$" + str(round(current, 2)) + "\n"
                    "P&L: " + pl_sign + str(int(pl_dollar))
                    + " (" + pct_sign + str(round(pl_pct, 1)) + "%)"
                )
        else:
            # Hold — report current P&L with T1/T2/peak context
            stop_str = ""
            if stop_price is not None:
                be_suffix = " BE" if stop_info.get("breakeven_activated") else ""
                stop_str = " | Stop: $" + str(round(float(stop_price), 2)) + be_suffix
            t1_mark = ":white_check_mark:" if stop_info.get("target1_hit") else ":hourglass_flowing_sand:"
            t2 = stop_info.get("target2", 0) or 0
            t2_hit = t2 > 0 and current >= t2
            t2_mark = ":white_check_mark:" if t2_hit else ":hourglass_flowing_sand:"
            peak = stop_info.get("peak_gain_pct", 0) or 0
            peak_str = ""
            if peak > pl_pct + 0.1:
                peak_str = ", peak +" + str(round(peak, 1)) + "%"
            pl_sign  = "+" if pl_dollar >= 0 else ""
            pct_sign = "+" if pl_pct    >= 0 else ""
            slack_send(
                ":white_circle: *[PAPER] HOLD* " + ticker
                + " — $" + str(round(current, 2))
                + " | P&L: " + pl_sign + str(int(pl_dollar))
                + " (" + pct_sign + str(round(pl_pct, 1)) + "%" + peak_str + ")"
                + stop_str
                + " | T1 " + t1_mark + " $" + str(round(stop_info.get("target1", 0), 2))
                + " | T2 " + t2_mark + " $" + str(round(t2, 2))
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
