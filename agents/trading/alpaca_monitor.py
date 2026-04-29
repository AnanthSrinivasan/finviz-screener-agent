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

from agents.trading import rules

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
PAPER_TRADING_STATE_FILE = os.path.join(DATA_DIR, "paper_trading_state.json")
MARKET_HISTORY_FILE = os.path.join(DATA_DIR, "market_monitor_history.json")


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
    """Thin shim — delegates to the shared rules engine."""
    return rules.apply_position_rules(ticker, entry, current_price, day_high,
                                      atr_pct, label_prefix="PAPER")


# ----------------------------
# Market state (read-only — written by market_monitor)
# ----------------------------
def load_market_state() -> str:
    """Return latest persisted market_state, or 'GREEN' if unknown (don't gate exits)."""
    try:
        with open(MARKET_HISTORY_FILE) as f:
            h = json.load(f)
        hist = h if isinstance(h, list) else h.get("history", [])
        if hist:
            return hist[-1].get("market_state") or "GREEN"
    except Exception as e:
        log.warning("Could not load market_monitor_history: %s", e)
    return "GREEN"


# ----------------------------
# Paper trading state (streaks / sizing — separate from live trading_state.json)
# ----------------------------
def load_paper_trading_state() -> dict:
    if os.path.exists(PAPER_TRADING_STATE_FILE):
        try:
            with open(PAPER_TRADING_STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not load paper_trading_state: %s", e)
    return {
        "consecutive_wins": 0, "consecutive_losses": 0,
        "total_wins": 0, "total_losses": 0,
        "current_sizing_mode": "normal", "sizing_override": None,
        "last_updated": "", "recent_trades": [],
    }


def save_paper_trading_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PAPER_TRADING_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("paper_trading_state.json saved (mode=%s, w=%d l=%d).",
             state.get("current_sizing_mode"),
             state.get("consecutive_wins", 0),
             state.get("consecutive_losses", 0))


# ----------------------------
# Recent SELL fills — for close-detection result recording
# ----------------------------
def fetch_recent_sell_fill(ticker: str, lookback_days: int = 7) -> float:
    """Return filled_avg_price of the most recent filled SELL for ticker, or 0.0."""
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)
        resp = requests.get(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            params={
                "status":    "closed",
                "limit":     100,
                "direction": "desc",
                "symbols":   ticker,
                "after":     cutoff.isoformat(),
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning("Could not fetch closed orders for %s: %s", ticker, resp.status_code)
            return 0.0
        for o in resp.json():
            if o.get("side") == "sell" and o.get("filled_avg_price"):
                return float(o["filled_avg_price"])
    except Exception as e:
        log.warning("fetch_recent_sell_fill(%s) failed: %s", ticker, e)
    return 0.0


# ----------------------------
# MA trail (post-close alert layer — Layer 1b)
# ----------------------------
def fetch_daily_closes(ticker: str, limit: int = 30) -> list:
    """Last N completed daily closes from Alpaca, oldest first."""
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/" + ticker + "/bars",
            params={"timeframe": "1Day", "limit": limit, "feed": "iex", "adjustment": "raw"},
            headers=alpaca_headers(),
            timeout=8,
        )
        if not resp.ok:
            return []
        bars = resp.json().get("bars", []) or []
        return [float(b["c"]) for b in bars if b.get("c") is not None]
    except Exception as e:
        log.warning("fetch_daily_closes(%s) failed: %s", ticker, e)
        return []


def is_post_close_run() -> bool:
    """True once per weekday after 21:00 UTC (matches the 22:00 UTC monitor tick)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.weekday() < 5 and now.hour >= 21


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

    positions     = get_positions()
    market_state  = load_market_state()
    stops         = load_stops()
    paper_state   = load_paper_trading_state()
    open_symbols  = {p.get("symbol", "") for p in positions}

    # Close-detection: tickers in stops but no longer in Alpaca positions.
    # Record win/loss/breakeven into paper_trading_state and pop from stops.
    closed_records = []
    for ticker in list(stops.keys()):
        if ticker in open_symbols:
            continue
        info       = stops[ticker]
        entry_px   = float(info.get("entry_price") or 0)
        if entry_px <= 0:
            stops.pop(ticker, None)
            continue
        exit_px = fetch_recent_sell_fill(ticker)
        source  = "fill"
        if exit_px <= 0:
            exit_px = float(info.get("highest_price_seen") or entry_px)
            source  = "peak_fallback"
        result_pct = (exit_px - entry_px) / entry_px * 100
        rules.record_trade_result(paper_state, ticker, result_pct, today, "SELL", source)
        closed_records.append((ticker, entry_px, exit_px, result_pct, source))
        stops.pop(ticker, None)

    for ticker, entry_px, exit_px, result_pct, src in closed_records:
        sign = "+" if result_pct >= 0 else ""
        slack_send(
            ":checkered_flag: *[PAPER] CLOSED* " + ticker
            + " entry $" + str(round(entry_px, 2))
            + " → exit $" + str(round(exit_px, 2))
            + " (" + sign + str(round(result_pct, 1)) + "%, " + src + ")"
        )

    sizing_alerts = rules.update_sizing_mode(paper_state, market_state)
    for a in sizing_alerts:
        slack_send("[PAPER] " + a)

    if not positions:
        log.info("No open paper positions to monitor.")
        slack_send(
            ":bar_chart: *Alpaca Monitor " + today + "* — No open positions. "
            "Mode: " + paper_state.get("current_sizing_mode", "normal")
        )
        save_paper_trading_state(paper_state)
        save_stops(stops)
        raise SystemExit(0)

    log.info("Monitoring %d position(s) — market %s, mode %s",
             len(positions), market_state, paper_state.get("current_sizing_mode"))
    sells_placed = 0
    post_close   = is_post_close_run()

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
        rule_events, _ = apply_paper_rules(ticker, stop_info, current, day_high,
                                           stop_info.get("atr_pct", atr_pct))
        for event in rule_events:
            slack_send(event["message"])

        # Layer 1b — post-close MA trail alert (alert-only, human decides)
        if post_close:
            closes = fetch_daily_closes(ticker, limit=60)
            ma_alert = rules.check_ma_trail_alert(
                closes, market_state,
                atr_pct=stop_info.get("atr_pct", atr_pct),
                highest_price_seen=stop_info.get("highest_price_seen", 0.0),
            )
            if ma_alert:
                slack_send(
                    ":warning: *[PAPER] MA TRAIL* " + ticker
                    + " — close $" + str(ma_alert["last_close"])
                    + " < " + ma_alert["ma_type"] + " $" + str(ma_alert["last_ema"])
                    + " (" + ma_alert["tier"] + ", " + str(ma_alert["consecutive"])
                    + "x consec) — your call"
                )

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
                # Mark for close-detection on next run; entry kept so we can
                # compute result_pct from the actual fill price (not pop now).
                stop_info["pending_close"] = True

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
    save_paper_trading_state(paper_state)

    slack_send(
        ":bar_chart: *Alpaca Monitor Summary — " + today + "*\n"
        "Market: " + market_state + " | Mode: "
        + paper_state.get("current_sizing_mode", "normal")
        + " (W:" + str(paper_state.get("consecutive_wins", 0))
        + " L:" + str(paper_state.get("consecutive_losses", 0)) + ")\n"
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
