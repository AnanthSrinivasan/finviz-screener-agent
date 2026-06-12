#!/usr/bin/env python3
# ----------------------------
# Alpaca Position Monitor (paper by default; TRADING_PROFILE=live for the
# dedicated live Alpaca account — see docs/specs/live-alpaca-executor.md)
# ----------------------------
# Runs inside the position-monitor schedule.
# For each open Alpaca position:
#   - Stop hit  → market sell
#   - Stage 3/4 → market sell
#   - Live only: +30% hard full take-profit · foreign-position refusal ·
#     no T1/T2 peels (full exits only) · EOD unfilled-order log
#   - Otherwise → hold, log P&L to Slack

import ast
import csv
import json
import os
import logging
import datetime
import requests

from agents.trading import rules
from agents.trading import trading_profile as tp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config — resolved per TRADING_PROFILE (paper default | live)
# ----------------------------
PROFILE = tp.resolve_profile()
IS_LIVE      = PROFILE["is_live"]
LIVE_DRY_RUN = PROFILE["dry_run"]
TAG          = PROFILE["slack_tag"]          # "[PAPER]" / "[LIVE 🔴]"
# Quiet mode (30-min critical runs): suppress HOLD/summary chatter, keep
# sells and alerts. Set by position-critical.yml so live stop coverage runs
# intraday without spamming #positions.
MONITOR_QUIET = (os.environ.get("MONITOR_QUIET") or "").strip() == "1"
ALPACA_API_KEY    = PROFILE["api_key"]
ALPACA_SECRET_KEY = PROFILE["secret_key"]
ALPACA_BASE_URL   = PROFILE["base_url"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DATA_DIR          = os.environ.get("DATA_DIR", "data")
PAPER_STOPS_FILE  = os.path.join(DATA_DIR, PROFILE["stops_filename"])
PAPER_TRADING_STATE_FILE = os.path.join(DATA_DIR, PROFILE["state_filename"])
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


def place_sell(symbol: str, qty: str, client_order_id: str = "") -> dict:
    if IS_LIVE and LIVE_DRY_RUN:
        msg = ":test_tube: " + TAG + " DRY RUN — would SELL " + symbol + " qty " + str(qty)
        log.info(msg)
        slack_send(msg)
        return {}
    payload = {
        "symbol":        symbol,
        "qty":           qty,
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
    try:
        resp = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Sell order failed for %s: %s", symbol, e)
        slack_send(":x: " + (TAG + " " if IS_LIVE else "") + "*SELL FAILED* " + symbol + ": " + str(e))
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
    entry.setdefault("t1_peeled", False)
    entry.setdefault("t2_peeled", False)
    return entry


# ----------------------------
# Target peel + stale cull (paper auto-execute)
# ----------------------------
PEEL_MIN_NOTIONAL = 50.0  # don't pay commission on sub-$50 lots


def process_target_peels(ticker: str, events: list, stop_info: dict,
                         qty: int, current_price: float,
                         sell_fn, slack_fn) -> tuple:
    """Consume target1/target2 events; place SELL for half, mutate stop_info.

    Returns (sells_placed, remaining_qty). Falls through to slack_fn(event['message'])
    for non-peel events (so caller doesn't need to re-handle them).
    """
    remaining = int(qty)
    sells = 0
    for event in events:
        kind = event.get("kind")
        msg = event.get("message", "")
        if kind == "target1" and not stop_info.get("t1_peeled"):
            peel_qty = remaining // 2
            if remaining <= 1 or peel_qty < 1:
                slack_fn(msg)
                continue
            if peel_qty * current_price < PEEL_MIN_NOTIONAL:
                log.info("%s: T1 peel skipped — notional $%.0f < $%.0f",
                         ticker, peel_qty * current_price, PEEL_MIN_NOTIONAL)
                slack_fn(msg)
                continue
            result = sell_fn(ticker, str(peel_qty))
            if not result:
                slack_fn(msg)
                continue
            stop_info["t1_peeled"] = True
            entry_price = float(stop_info.get("entry_price", 0) or 0)
            be_floor = round(entry_price * 1.005, 2)
            if be_floor > float(stop_info.get("stop_price", 0) or 0):
                stop_info["stop_price"] = be_floor
            remaining -= peel_qty
            sells += 1
            slack_fn(
                ":dart: *[PAPER] T1 AUTO-PEEL* " + ticker
                + " — sold " + str(peel_qty) + " sh @ ~$" + str(round(current_price, 2))
                + ", stop → $" + str(stop_info["stop_price"])
                + " (breakeven), " + str(remaining) + " sh continue"
            )
        elif kind == "target2" and not stop_info.get("t2_peeled"):
            peel_qty = remaining // 2
            if remaining <= 1 or peel_qty < 1:
                slack_fn(msg)
                continue
            if peel_qty * current_price < PEEL_MIN_NOTIONAL:
                log.info("%s: T2 peel skipped — notional $%.0f < $%.0f",
                         ticker, peel_qty * current_price, PEEL_MIN_NOTIONAL)
                slack_fn(msg)
                continue
            result = sell_fn(ticker, str(peel_qty))
            if not result:
                slack_fn(msg)
                continue
            stop_info["t2_peeled"] = True
            remaining -= peel_qty
            sells += 1
            slack_fn(
                ":dart::dart: *[PAPER] T2 AUTO-PEEL* " + ticker
                + " — sold " + str(peel_qty) + " sh @ ~$" + str(round(current_price, 2))
                + ", " + str(remaining) + " sh runner"
            )
        else:
            slack_fn(msg)
    return sells, remaining


def check_stale_position(stop_info: dict, today: datetime.date | None = None) -> tuple:
    """Return (is_stale, days_open, peak_gain_pct).

    Stale = days_open >= STALE_DAYS AND peak < STALE_PEAK_THRESHOLD AND not t1_peeled
    AND not in a losing position (stops handle those). Used by paper auto-cull.
    """
    if stop_info.get("t1_peeled"):
        return False, 0, 0.0
    entry_date = stop_info.get("entry_date")
    if not entry_date:
        return False, 0, 0.0
    try:
        ed = datetime.datetime.strptime(entry_date, "%Y-%m-%d").date()
    except Exception:
        return False, 0, 0.0
    today_d = today or datetime.date.today()
    days_open = (today_d - ed).days
    peak = float(stop_info.get("peak_gain_pct", 0) or 0)
    if days_open >= rules.STALE_DAYS and peak < rules.STALE_PEAK_THRESHOLD:
        return True, days_open, peak
    return False, days_open, peak


def apply_paper_rules(ticker: str, entry: dict, current_price: float,
                     day_high: float, atr_pct: float) -> tuple:
    """Thin shim — delegates to the shared rules engine."""
    return rules.apply_position_rules(ticker, entry, current_price, day_high,
                                      atr_pct, label_prefix=PROFILE["label_prefix"])


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
# Live EOD unfilled-order log — day-TIF buys that expired without a fill
# ----------------------------
def report_expired_live_buys(state: dict):
    """Slack a "no chase" line for each agent-placed live BUY that expired or
    was cancelled with zero fill. Dedup across runs via last_expired_check_ts."""
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        resp = requests.get(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            params={"status": "closed", "limit": 100, "direction": "desc",
                    "after": cutoff.isoformat()},
            timeout=10,
        )
        if not resp.ok:
            log.warning("Could not fetch closed orders for EOD report: %s", resp.status_code)
            return
        since = state.get("last_expired_check_ts", "")
        for o in tp.filter_expired_unfilled(resp.json(), since):
            slack_send(
                ":hourglass: " + TAG + " *UNFILLED EOD* — " + str(o.get("symbol"))
                + " buy order " + str(o.get("status"))
                + " unfilled (limit $" + str(o.get("limit_price")) + ") — no chase"
            )
        state["last_expired_check_ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    except Exception as e:
        log.warning("report_expired_live_buys failed: %s", e)


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
    log.info("=== Alpaca monitor starting — %s (profile=%s%s) ===",
             today, PROFILE["name"], ", DRY RUN" if LIVE_DRY_RUN else "")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.error("Missing Alpaca credentials — aborting.")
        slack_send(":x: " + (TAG + " " if IS_LIVE else "") + "*Alpaca monitor failed* — missing API credentials")
        raise SystemExit(1)

    paper_state = load_paper_trading_state()

    # Live profile stays silent until the user has armed it with the first
    # manual executor dispatch — no positions can exist before that.
    if IS_LIVE and not paper_state.get("first_run_verified"):
        log.info("Live profile not yet armed (first_run_verified false) — nothing to monitor.")
        raise SystemExit(0)

    positions     = get_positions()
    market_state  = load_market_state()
    stops         = load_stops()
    open_symbols  = {p.get("symbol", "") for p in positions}

    if IS_LIVE:
        # Hard boundary: the live agent refuses to manage positions it didn't
        # open (one discretionary trade contaminates the experiment). Alert
        # every run until the foreign position is gone.
        foreign = [p for p in positions if p.get("symbol", "") not in stops]
        for p in foreign:
            slack_send(
                ":bangbang: " + TAG + " *FOREIGN POSITION* " + str(p.get("symbol"))
                + " — qty " + str(p.get("qty")) + " not opened by the agent. "
                "Not managed: no stops, no exits. Close it manually or move it out."
            )
        if foreign:
            positions = [p for p in positions if p.get("symbol", "") in stops]
            open_symbols = {p.get("symbol", "") for p in positions}

        report_expired_live_buys(paper_state)

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
            ":checkered_flag: *" + TAG + " CLOSED* " + ticker
            + " entry $" + str(round(entry_px, 2))
            + " → exit $" + str(round(exit_px, 2))
            + " (" + sign + str(round(result_pct, 1)) + "%, " + src + ")"
        )

    sizing_alerts = rules.update_sizing_mode(paper_state, market_state)
    for a in sizing_alerts:
        slack_send(TAG + " " + a)

    if not positions:
        log.info("No open %s positions to monitor.", PROFILE["name"])
        if not MONITOR_QUIET:
            slack_send(
                ":bar_chart: *" + (TAG + " " if IS_LIVE else "") + "Alpaca Monitor " + today
                + "* — No open positions. "
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
        try:
            qty_int = int(float(qty))
        except (TypeError, ValueError):
            qty_int = 0
        if IS_LIVE:
            # Full exits only (user decision 2026-06-12) — no T1/T2 peels.
            # Forward the engine's alerts; the tight ≥+20% tier trail plus the
            # +30% hard take-profit below realize winners in one order.
            for event in rule_events:
                slack_send(event.get("message", ""))
        else:
            peel_sells, qty_after_peel = process_target_peels(
                ticker, rule_events, stop_info, qty_int, current,
                sell_fn=place_sell, slack_fn=slack_send,
            )
            if peel_sells:
                sells_placed += peel_sells
                stop_info["pending_close"] = True
                qty = str(qty_after_peel)
                if qty_after_peel <= 0:
                    continue

        # Live hard take-profit: +30% gain → the entire position exits now.
        if IS_LIVE and tp.should_full_take_profit(entry_price, current):
            result = place_sell(ticker, qty,
                                client_order_id=tp.make_client_order_id(today, ticker, side="sell"))
            if result:
                sells_placed += 1
                stop_info["pending_close"] = True
                stop_info["close_source"] = "take_profit_30"
                gain = (current - entry_price) / entry_price * 100
                slack_send(
                    ":moneybag: " + TAG + " *TAKE PROFIT +30%* " + ticker
                    + " — sold full " + str(qty) + " sh @ ~$" + str(round(current, 2))
                    + " (+" + str(round(gain, 1)) + "%) — winner realized, full exit"
                )
                continue

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
                    ":warning: *" + TAG + " MA TRAIL* " + ticker
                    + " — close $" + str(ma_alert["last_close"])
                    + " < " + ma_alert["ma_type"] + " $" + str(ma_alert["last_ema"])
                    + " (" + ma_alert["tier"] + ", " + str(ma_alert["consecutive"])
                    + "x consec) — your call"
                )

        stop_price = stop_info.get("stop_price")
        sell_reason = None

        # Stale cull — auto-sell positions that drifted flat for 14d with peak < +4%
        sell_coid = tp.make_client_order_id(today, ticker, side="sell") if IS_LIVE else ""
        is_stale, stale_days, stale_peak = check_stale_position(stop_info)
        if is_stale and current > 0:
            log.info("%s: STALE — %dd open, peak +%.1f%% — auto-cull",
                     ticker, stale_days, stale_peak)
            result = place_sell(ticker, qty, client_order_id=sell_coid)
            if result:
                sells_placed += 1
                stop_info["pending_close"] = True
                stop_info["close_source"] = "stale_cull"
                slack_send(
                    ":zzz: *" + TAG + " STALE CULL* " + ticker
                    + " — " + str(stale_days) + "d open, peak only +"
                    + str(round(stale_peak, 1)) + "% · sold " + str(qty) + " sh "
                    + "@ ~$" + str(round(current, 2))
                    + " · freeing capital for next signal"
                )
                continue  # position will close-detect on next run

        # Check stop loss (now reflects any trailing raises applied above)
        if stop_price is not None and current > 0 and current <= float(stop_price):
            effective_atr_pct = stop_info.get("atr_pct", atr_pct)
            if effective_atr_pct <= 5.0:
                closes = fetch_daily_closes(ticker, limit=5)
                if rules.price_above_sma5(closes, current):
                    log.info(
                        "%s: stop hit but price $%.2f >= SMA5 — skipping sell this run (low-vol, trend intact)",
                        ticker, current,
                    )
                else:
                    sell_reason = (
                        "stop hit (price $" + str(round(current, 2))
                        + " <= stop $" + str(round(float(stop_price), 2)) + ")"
                    )
            else:
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
            result = place_sell(ticker, qty, client_order_id=sell_coid)
            if result:
                sells_placed += 1
                # Mark for close-detection on next run; entry kept so we can
                # compute result_pct from the actual fill price (not pop now).
                stop_info["pending_close"] = True

                pl_sign  = "+" if pl_dollar >= 0 else ""
                pct_sign = "+" if pl_pct    >= 0 else ""
                slack_send(
                    ":large_red_circle: *" + TAG + " SELL PLACED* " + ticker + "\n"
                    "Reason: " + sell_reason + "\n"
                    "Qty: " + qty
                    + " | Entry: $" + str(round(entry_price, 2))
                    + " | Exit: ~$" + str(round(current, 2)) + "\n"
                    "P&L: " + pl_sign + str(int(pl_dollar))
                    + " (" + pct_sign + str(round(pl_pct, 1)) + "%)"
                )
        elif not MONITOR_QUIET:
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
                ":white_circle: *" + TAG + " HOLD* " + ticker
                + " — $" + str(round(current, 2))
                + " | P&L: " + pl_sign + str(int(pl_dollar))
                + " (" + pct_sign + str(round(pl_pct, 1)) + "%" + peak_str + ")"
                + stop_str
                + " | T1 " + t1_mark + " $" + str(round(stop_info.get("target1", 0), 2))
                + " | T2 " + t2_mark + " $" + str(round(t2, 2))
            )

    save_stops(stops)
    save_paper_trading_state(paper_state)

    if not MONITOR_QUIET or sells_placed:
        slack_send(
            ":bar_chart: *" + (TAG + " " if IS_LIVE else "") + "Alpaca Monitor Summary — " + today + "*\n"
            "Market: " + market_state + " | Mode: "
            + paper_state.get("current_sizing_mode", "normal")
            + " (W:" + str(paper_state.get("consecutive_wins", 0))
            + " L:" + str(paper_state.get("consecutive_losses", 0)) + ")\n"
            "Positions monitored: " + str(len(positions)) + "\n"
            "Sells placed: " + str(sells_placed)
        )

    # Regenerate Claude model portfolio page (paper book only, non-fatal).
    if not IS_LIVE:
        try:
            from utils.generators.generate_portfolio import main as _gen_portfolio
            _gen_portfolio()
        except Exception as e:
            log.warning("Portfolio page generation failed: %s", e)

    log.info("=== Alpaca monitor done — %d sell(s) placed ===", sells_placed)
