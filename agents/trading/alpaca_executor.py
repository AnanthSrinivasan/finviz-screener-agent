#!/usr/bin/env python3
# ----------------------------
# Alpaca Paper Trading Executor
# ----------------------------
# Triggered after Daily Finviz Screener completes.
# Steps:
#   1. Regime check (SPY vs 200-day SMA)
#   2. Load today's screener CSV
#   3. Get open positions + account from Alpaca
#   4. Size each qualifying ticker
#   5. Claude bull/bear thesis + VERDICT
#   6. Place market buy orders
#   7. Persist stop-loss reference to data/paper_stops.json
#   8. Slack summary

import argparse
import ast
import csv
import json
import math
import os
import logging
import datetime
import re
import requests
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
ALPACA_DATA_URL   = "https://data.alpaca.markets/v2"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DATA_DIR          = os.environ.get("DATA_DIR", "data")
PAPER_STOPS_FILE  = os.path.join(DATA_DIR, "paper_stops.json")
PAPER_TRADING_STATE_FILE = os.path.join(DATA_DIR, "paper_trading_state.json")
MARKET_HISTORY_FILE = os.path.join(DATA_DIR, "market_monitor_history.json")
WATCHLIST_FILE    = os.path.join(DATA_DIR, "watchlist.json")
def effective_max_positions(market_state: str) -> int:
    if market_state in ("GREEN", "THRUST"):
        return 10
    if market_state == "CAUTION":
        return 7
    return 5  # COOLING, DANGER, RED, BLACKOUT


# Market state → (block_entries, size_multiplier)
# Mirrors the live position_monitor's Rule 6 / regime-conditioning policy.
_MARKET_GATE = {
    "BLACKOUT": (True,  0.0),  # seasonal freeze
    "DANGER":   (True,  0.0),
    "RED":      (True,  0.0),
    "COOLING":  (False, 0.5),  # trim/tighten regime — half size
    "CAUTION":  (False, 0.5),  # half size
    "GREEN":    (False, 1.0),
    "THRUST":   (False, 1.0),
}

# ATR% tier fallback for peel warn — mirrors PEEL_THRESHOLDS in position_monitor.py.
# Used only when the ticker is not present (or not calibrated) in peel_calibration.json.
PEEL_WARN_TIERS = [
    (4.0,  3.0),   # low
    (7.0,  5.0),   # mid
    (10.0, 6.5),   # high
    (999,  8.5),   # extreme
]

_PEEL_CALIBRATION_CACHE: dict | None = None


def get_entry_peel_warn(atr_pct: float, ticker: str) -> tuple:
    """
    Return (warn_multiple, source) for entry gating.
    source == 'calibrated' when drawn from per-ticker p75×0.75 in
    data/peel_calibration.json; else 'tier' (ATR% band fallback).
    """
    global _PEEL_CALIBRATION_CACHE
    if _PEEL_CALIBRATION_CACHE is None:
        cal_path = os.path.join(DATA_DIR, "peel_calibration.json")
        try:
            with open(cal_path) as fh:
                _PEEL_CALIBRATION_CACHE = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            _PEEL_CALIBRATION_CACHE = {}
    entry = _PEEL_CALIBRATION_CACHE.get(ticker, {})
    if entry.get("calibrated") and entry.get("warn"):
        return float(entry["warn"]), "calibrated"
    for max_atr, warn in PEEL_WARN_TIERS:
        if atr_pct <= max_atr:
            return warn, "tier"
    return 8.5, "tier"


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
# Step 1: Market state (breadth) — single source of truth
# ----------------------------
def load_market_state() -> str:
    """Read latest market_state from market_monitor_history.json.
    Defaults to RED on failure (safe-by-default — same posture as old SPY/SMA200 fallback)."""
    try:
        with open(MARKET_HISTORY_FILE) as f:
            h = json.load(f)
        hist = h if isinstance(h, list) else h.get("history", [])
        if hist:
            state = hist[-1].get("market_state") or "RED"
            log.info("Market state (from market_monitor_history): %s", state)
            return state
    except Exception as e:
        log.warning("Could not load market_monitor_history: %s — defaulting RED", e)
    return "RED"


def load_paper_trading_state() -> dict:
    if os.path.exists(PAPER_TRADING_STATE_FILE):
        try:
            with open(PAPER_TRADING_STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not load paper_trading_state: %s", e)
    return {
        "consecutive_wins": 0, "consecutive_losses": 0,
        "current_sizing_mode": "normal", "recent_trades": [],
    }


def get_spy_regime() -> tuple:
    """
    Returns (regime, spy_price, sma200_approx).
    regime is 'GREEN' (SPY > SMA200) or 'RED' (SPY <= SMA200).
    Uses Finviz quote page (same source as market_monitor). Returns RED on failure.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; finviz-screener-agent/1.0)"}
        resp = requests.get(
            "https://finviz.com/quote.ashx",
            params={"t": "SPY"},
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            log.warning("Finviz SPY fetch HTTP %s", resp.status_code)
        else:
            # Extract Price and SMA200 from snapshot table
            price_match = re.search(r'"Price"[^>]*>([0-9,\.]+)<', resp.text)
            sma200_match = re.search(r'SMA200[^%]*?([-\d\.]+)%', resp.text)
            if not price_match:
                # fallback: grab from table cell after "Price" label
                price_match = re.search(r'>Price<[^>]*>[^<]*<[^>]*>([\d,\.]+)<', resp.text)
            try:
                price_m = re.search(r'quote-price_wrapper_price">([\d,\.]+)<', resp.text)
                spy_price = float(price_m.group(1).replace(',', '')) if price_m else None
            except Exception:
                spy_price = None
            try:
                sma200_m = re.search(r'SMA200.{0,200}?([-\d\.]+)%', resp.text, re.DOTALL)
                sma200_pct = float(sma200_m.group(1)) if sma200_m else None
            except Exception:
                sma200_pct = None

            if spy_price and sma200_pct is not None:
                # sma200_pct is % distance from SMA200 (positive = above)
                sma200_approx = round(spy_price / (1 + sma200_pct / 100), 2)
                regime = "GREEN" if sma200_pct > 0 else "RED"
                log.info("SPY (Finviz): %.2f | SMA200%%: %.2f%% | Regime: %s", spy_price, sma200_pct, regime)
                return regime, spy_price, sma200_approx
            log.warning("Could not parse SPY price or SMA200 from Finviz")
    except Exception as e:
        log.error("Finviz SPY fetch failed: %s", e)

    log.error("Could not determine regime — defaulting RED (safe)")
    return "RED", 0.0, 0.0


# ----------------------------
# Step 2: Load screener CSV
# ----------------------------
def load_screener_csv(today: str) -> list:
    """
    Load today's enriched CSV. Returns list of row dicts with parsed
    Stage (dict), VCP (dict), and numeric fields as floats.
    """
    path = os.path.join(DATA_DIR, "finviz_screeners_" + today + ".csv")
    if not os.path.exists(path):
        log.error("Screener CSV not found: %s", path)
        return []

    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse Stage and VCP from their string-repr dict values
            for col in ("Stage", "VCP"):
                val = row.get(col, "")
                if val and val not in ("", "nan"):
                    try:
                        row[col] = ast.literal_eval(val)
                    except Exception:
                        row[col] = {}
                else:
                    row[col] = {}

            # Parse numeric fields
            for col in ("Quality Score", "ATR%", "SMA50%", "EPS Y/Y TTM", "Rel Volume", "Appearances"):
                try:
                    row[col] = float(row.get(col, "") or 0)
                except (ValueError, TypeError):
                    row[col] = 0.0

            rows.append(row)

    log.info("Loaded %d tickers from %s", len(rows), path)
    return rows


# ----------------------------
# Step 2b: Merge watchlist tickers from daily_quality
# ----------------------------
def load_watchlist_tickers() -> set:
    """Return set of ticker symbols currently on the watchlist."""
    if not os.path.exists(WATCHLIST_FILE):
        return set()
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return {e["ticker"] for e in data.get("watchlist", []) if e.get("ticker")}
    except Exception as e:
        log.warning("Could not load watchlist: %s", e)
        return set()


def load_daily_quality(today: str) -> dict:
    """Return {ticker: quality_data} from today's daily_quality JSON."""
    path = os.path.join(DATA_DIR, "daily_quality_" + today + ".json")
    if not os.path.exists(path):
        log.warning("daily_quality not found for %s", today)
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        # Keyed by ticker
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {r["Ticker"]: r for r in data if r.get("Ticker")}
    except Exception as e:
        log.warning("Could not load daily_quality: %s", e)
    return {}


def merge_watchlist_rows(screener_rows: list, today: str) -> list:
    """
    Add watchlist tickers that appear in daily_quality but not already
    in the screener rows. This ensures high-Q watchlist names get evaluated.
    """
    existing_tickers = {r.get("Ticker", "").strip() for r in screener_rows}
    watchlist = load_watchlist_tickers()
    quality = load_daily_quality(today)

    added = 0
    for ticker in watchlist:
        if ticker in existing_tickers:
            continue
        if ticker not in quality:
            continue
        q = quality[ticker]
        # Build a minimal row compatible with the executor's field expectations
        row = {
            "Ticker":        ticker,
            "Quality Score": float(q.get("Quality Score") or q.get("quality_score") or 0),
            "ATR%":          float(q.get("ATR%") or q.get("atr_pct") or 0),
            "EPS Y/Y TTM":   float(q.get("EPS Y/Y TTM") or 0),
            "Rel Volume":    float(q.get("Rel Volume") or 1),
            "Appearances":   float(q.get("Appearances") or 1),
            "Sector":        q.get("Sector") or "",
            "Screeners":     q.get("Screeners") or "",
            "Stage":         q.get("Stage") or {},
            "VCP":           q.get("VCP") or {},
            "_source":       "watchlist",
        }
        screener_rows.append(row)
        added += 1

    if added:
        log.info("Merged %d watchlist tickers from daily_quality into candidate pool", added)
    return screener_rows


# ----------------------------
# Step 3: Alpaca account + positions
# ----------------------------
def get_open_positions() -> set:
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/positions",
            headers=alpaca_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return {p["symbol"] for p in resp.json()}
    except Exception as e:
        log.error("Failed to get positions: %s", e)
        return set()


def get_account() -> dict:
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/account",
            headers=alpaca_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Failed to get account: %s", e)
        return {}


def get_current_price(symbol: str) -> float:
    """Get latest price from Alpaca data API (latest trade, fallback to last bar close)."""
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    # Try latest trade first (works during + after market hours)
    try:
        resp = requests.get(
            ALPACA_DATA_URL + "/stocks/" + symbol + "/trades/latest",
            headers=headers,
            params={"feed": "iex"},
            timeout=10,
        )
        if resp.ok:
            trade = resp.json().get("trade", {})
            price = float(trade.get("p", 0) or 0)
            if price > 0:
                return price
    except Exception as e:
        log.warning("Alpaca latest trade failed for %s: %s", symbol, e)

    # Fallback: last daily bar close
    try:
        resp = requests.get(
            ALPACA_DATA_URL + "/stocks/" + symbol + "/bars",
            headers=headers,
            params={"timeframe": "1Day", "limit": 1, "adjustment": "raw", "feed": "iex"},
            timeout=10,
        )
        if resp.ok:
            bars = resp.json().get("bars", [])
            if bars:
                return float(bars[-1]["c"])
    except Exception as e:
        log.warning("Alpaca bar fallback failed for %s: %s", symbol, e)

    return 0.0


# ----------------------------
# Step 4: Position sizing
# ----------------------------
def compute_allocation(quality_score: float, vcp_dict: dict, portfolio_equity: float) -> float:
    """
    Returns dollar allocation based on Quality Score tier.
    Thresholds match the system's own conviction levels (see SYSTEM_DOCS §3.1):
      Q < 60       → skip — not good enough to auto-execute
      Q 60–79      → 15% of equity (standard conviction)
      Q 80–89      → 20% of equity (strong conviction)
      Q 90+ + VCP  → 25% of equity (highest conviction)
    Q=35 would mean Stage 2 (+25) + ~10pts more — one screener, weak volume.
    That is not a trade. Earnings alert floor is Q>50; we set the bar higher.
    """
    vcp_confirmed = isinstance(vcp_dict, dict) and vcp_dict.get("vcp_possible", False)

    if quality_score >= 90 and vcp_confirmed:
        pct = 0.25
    elif quality_score >= 80:
        pct = 0.20
    elif quality_score >= 60:
        pct = 0.15
    else:
        return 0.0

    return portfolio_equity * pct


# ----------------------------
# Step 5: Place order
# ----------------------------
def place_order(symbol: str, qty: int, limit_price: float) -> dict:
    """Place a GTC limit order at last close price. Fills at open if price is there."""
    try:
        resp = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            json={
                "symbol":        symbol,
                "qty":           str(qty),
                "side":          "buy",
                "type":          "limit",
                "limit_price":   str(round(limit_price, 2)),
                "time_in_force": "gtc",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Order failed for %s: %s", symbol, e)
        slack_send(":x: *ORDER FAILED* " + symbol + " qty=" + str(qty) + ": " + str(e))
        return {}


# ----------------------------
# Step 6: Cancel stale GTC orders
# ----------------------------
def cancel_stale_gtc_orders(max_age_days: int = 2):
    """Cancel open GTC buy orders older than max_age_days to avoid stale fills."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            params={"status": "open", "limit": 50, "direction": "desc"},
            timeout=10,
        )
        if not resp.ok:
            log.warning("Could not fetch open orders: %s", resp.status_code)
            return

        orders = resp.json()
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
        cancelled = []

        for order in orders:
            if order.get("side") != "buy" or order.get("time_in_force") != "gtc":
                continue
            created = order.get("created_at", "")
            try:
                created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                continue
            if created_dt < cutoff:
                cancel_resp = requests.delete(
                    f"{ALPACA_BASE_URL}/orders/" + order["id"],
                    headers=alpaca_headers(),
                    timeout=10,
                )
                if cancel_resp.ok or cancel_resp.status_code == 204:
                    cancelled.append(order["symbol"])
                    log.info("Cancelled stale GTC order: %s (placed %s)", order["symbol"], created[:10])
                else:
                    log.warning("Failed to cancel order %s: %s", order["id"], cancel_resp.status_code)

        if cancelled:
            slack_send(":wastebasket: *Stale GTC orders cancelled:* " + ", ".join(cancelled))

    except Exception as e:
        log.error("cancel_stale_gtc_orders failed: %s", e)


# ----------------------------
# Step 7: Stop-loss persistence
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
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PAPER_STOPS_FILE, "w") as f:
        json.dump(stops, f, indent=2)
    log.info("paper_stops.json saved.")


# ----------------------------
# Cancel orders for a specific ticker
# ----------------------------
def cancel_orders_for_ticker(ticker: str) -> int:
    """Cancel all open orders for a specific ticker. Returns number cancelled."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            params={"status": "open", "limit": 50},
            timeout=10,
        )
        if not resp.ok:
            log.error("Could not fetch open orders: %s", resp.status_code)
            return 0
        orders = [o for o in resp.json() if o.get("symbol", "").upper() == ticker.upper()]
        if not orders:
            log.info("No open orders found for %s", ticker)
            slack_send(f":information_source: *Cancel {ticker}* — no open orders found")
            return 0
        cancelled = 0
        for order in orders:
            del_resp = requests.delete(
                f"{ALPACA_BASE_URL}/orders/{order['id']}",
                headers=alpaca_headers(),
                timeout=10,
            )
            if del_resp.ok or del_resp.status_code == 204:
                log.info("Cancelled order %s: %s %s %s@%s",
                         order['id'], order['side'], order['qty'],
                         ticker, order.get('limit_price', 'mkt'))
                cancelled += 1
            else:
                log.warning("Failed to cancel order %s: %s", order['id'], del_resp.status_code)
        if cancelled:
            slack_send(f":wastebasket: *Cancelled {cancelled} order(s) for {ticker}* (manual cancel)")
        return cancelled
    except Exception as e:
        log.error("cancel_orders_for_ticker failed: %s", e)
        return 0


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cancel-ticker", metavar="TICKER",
                        help="Cancel all open orders for a ticker and exit (e.g. --cancel-ticker MRVL)")
    args = parser.parse_args()

    today = datetime.date.today().strftime("%Y-%m-%d")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.error("ALPACA_API_KEY or ALPACA_SECRET_KEY not set — aborting.")
        slack_send(":x: *Alpaca executor failed* — missing API credentials")
        raise SystemExit(1)

    # --cancel-ticker mode: cancel orders for a specific ticker and exit
    cancel_env = os.environ.get("CANCEL_TICKER", "").strip().upper()
    cancel_ticker = (args.cancel_ticker or cancel_env or "").strip().upper()
    if cancel_ticker:
        log.info("=== Cancel mode: %s ===", cancel_ticker)
        n = cancel_orders_for_ticker(cancel_ticker)
        log.info("Cancelled %d order(s) for %s", n, cancel_ticker)
        sys.exit(0)

    # Weekend guard — executor must never trade on non-market days
    if datetime.date.today().weekday() >= 5:
        msg = f":information_source: Alpaca executor skipped — weekend ({today}). No trades."
        log.info(msg)
        slack_send(msg)
        raise SystemExit(0)

    log.info("=== Alpaca executor starting — %s ===", today)

    # Step 1: Market state gate (breadth-based — single source of truth)
    market_state    = load_market_state()
    block, size_mul = _MARKET_GATE.get(market_state, (True, 0.0))
    max_pos         = effective_max_positions(market_state)
    paper_state     = load_paper_trading_state()
    sizing_mode     = paper_state.get("current_sizing_mode", "normal")

    # Sizing mode overlays on top of market gate
    if sizing_mode == "suspended":
        block = True
    elif sizing_mode == "reduced":
        size_mul = min(size_mul, 0.25)  # 5% of equity ≈ ¼ of normal 20% base
    elif sizing_mode == "aggressive" and size_mul == 1.0:
        size_mul = 1.25

    # Step 2: Load screener CSV + merge watchlist tickers
    rows = load_screener_csv(today)
    if not rows:
        slack_send(":x: *Alpaca executor* — no screener data for " + today)
        raise SystemExit(1)
    rows = merge_watchlist_rows(rows, today)

    # Step 3: Positions + account
    open_positions = get_open_positions()
    account        = get_account()

    if not account:
        slack_send(":x: *Alpaca executor* — could not fetch Alpaca account")
        raise SystemExit(1)

    portfolio_equity = float(account.get("equity", 0) or 0)
    buying_power     = float(account.get("buying_power", 0) or 0)

    if portfolio_equity <= 0:
        slack_send(":x: *Alpaca executor* — portfolio equity is zero or missing")
        raise SystemExit(1)

    log.info(
        "Open positions: %d/%d | Equity: $%.2f | BP: $%.2f",
        len(open_positions), max_pos, portfolio_equity, buying_power,
    )

    if len(open_positions) >= max_pos:
        msg = (
            ":no_entry: *MAX POSITIONS REACHED* — "
            + str(len(open_positions))
            + "/"
            + str(max_pos)
            + " open. No new buys today."
        )
        log.info(msg)
        slack_send(msg)
        raise SystemExit(0)

    # Step 4–7: Evaluate and trade
    cancel_stale_gtc_orders(max_age_days=2)
    stops           = load_stops()
    orders_placed   = 0
    total_deployed  = 0.0
    pending_positions = set(open_positions)  # track adds during this run

    # Sort by Quality Score descending, cap at top 10 candidates for Claude evaluation
    sorted_rows = sorted(rows, key=lambda r: r.get("Quality Score", 0), reverse=True)
    MAX_CANDIDATES = 10
    slots_needed = max_pos - len(open_positions)
    # Pre-filter to Q≥60 + Stage 2 before capping, so we cap meaningful candidates only
    pre_filtered = [
        r for r in sorted_rows
        if r.get("Quality Score", 0) >= 60
        and (r.get("Stage", {}) if isinstance(r.get("Stage"), dict) else {}).get("stage", 0) == 2
        and (r.get("Ticker", "").strip() not in open_positions)
    ]
    log.info("%d candidates after Q≥60 + Stage 2 filter, evaluating top %d", len(pre_filtered), MAX_CANDIDATES)
    sorted_rows = pre_filtered[:MAX_CANDIDATES]

    # Market-state gate: in RED/DANGER/BLACKOUT (or sizing=suspended), don't trade —
    # but post a Slack alert listing the would-have-bought candidates so the human
    # can decide. Mirrors live's "no entries in RED" rule with an informational layer.
    if block:
        if sorted_rows:
            lines = [
                ":no_entry: *PAPER ENTRIES BLOCKED* — market " + market_state
                + (", sizing " + sizing_mode if sizing_mode != "normal" else "")
                + ". " + str(len(sorted_rows)) + " candidate(s) — your call:"
            ]
            for r in sorted_rows[:5]:
                tk = r.get("Ticker", "").strip()
                qs = int(r.get("Quality Score", 0))
                atr = round(r.get("ATR%", 0) or 0, 1)
                lines.append("• " + tk + " — Q=" + str(qs) + " ATR=" + str(atr) + "%")
            slack_send("\n".join(lines))
        else:
            slack_send(
                ":no_entry: *PAPER ENTRIES BLOCKED* — market " + market_state
                + ". No qualifying candidates today."
            )
        raise SystemExit(0)

    log.info("Market %s | sizing %s | size_mul=%.2f", market_state, sizing_mode, size_mul)

    for row in sorted_rows:
        ticker = (row.get("Ticker") or "").strip()
        if not ticker:
            continue

        if ticker in pending_positions:
            log.info("Skipping %s — already held", ticker)
            continue

        if len(pending_positions) >= max_pos:
            log.info("Max positions reached — stopping evaluation")
            break

        qs = row.get("Quality Score", 0)

        # Compute allocation
        vcp_dict     = row.get("VCP", {})
        dollar_alloc = compute_allocation(qs, vcp_dict, portfolio_equity) * size_mul
        if dollar_alloc <= 0:
            log.info("Skipping %s — no allocation (Q=%.0f)", ticker, qs)
            continue

        # ATR% multiple from MA gate — no new positions when stock is already in peel territory.
        # Multiple = SMA50% / ATR% (same formula as position_monitor).
        # Threshold = peel_warn from per-ticker calibration when available, else ATR% tier fallback.
        atr_pct_raw  = row.get("ATR%", 0)
        sma50_pct    = row.get("SMA50%", None)  # None = unknown (watchlist rows from daily_quality)
        if sma50_pct is not None and atr_pct_raw > 0:
            atr_multiple_entry = sma50_pct / atr_pct_raw
            peel_warn, peel_src = get_entry_peel_warn(atr_pct_raw, ticker)
            if atr_multiple_entry > peel_warn:
                msg = (
                    ":no_entry_sign: *SKIPPED* " + ticker
                    + " — ATR multiple *" + str(round(atr_multiple_entry, 2)) + "x*"
                    + " > peel warn " + str(round(peel_warn, 1)) + "x (" + peel_src + ")"
                )
                log.info(
                    "Skipping %s — ATR multiple %.2f > peel warn %.2f (%s)",
                    ticker, atr_multiple_entry, peel_warn, peel_src,
                )
                slack_send(msg)
                continue
        elif sma50_pct is None:
            log.info("%s — SMA50%% unknown (watchlist row), skipping ATR multiple check", ticker)

        # Fetch price
        price = get_current_price(ticker)
        if price <= 0:
            log.warning("Skipping %s — price fetch failed", ticker)
            slack_send(":warning: *" + ticker + "* — price fetch failed, skipped")
            continue

        shares = math.floor(dollar_alloc / price)
        if shares < 1:
            log.info(
                "Skipping %s — alloc $%.0f / price $%.2f = %.2f shares < 1",
                ticker, dollar_alloc, price, dollar_alloc / price,
            )
            continue

        dollar_actual = shares * price

        if dollar_actual > buying_power:
            log.warning(
                "Skipping %s — $%.0f exceeds buying power $%.0f",
                ticker, dollar_actual, buying_power,
            )
            slack_send(
                ":warning: *" + ticker + "* — insufficient buying power "
                "($" + str(int(buying_power)) + " available), skipped"
            )
            continue

        # Step 5: Place GTC limit order at close price
        log.info(
            "Placing BUY %s: %d shares limit $%.2f = $%.0f (GTC)",
            ticker, shares, price, dollar_actual,
        )
        order_result = place_order(ticker, shares, price)
        if not order_result:
            continue

        orders_placed  += 1
        total_deployed += dollar_actual
        buying_power   -= dollar_actual
        pending_positions.add(ticker)

        # Step 6: Stop reference (2×ATR below entry)
        atr_pct    = row.get("ATR%", 0)
        atr_dollar = (atr_pct / 100) * price
        stop_price = round(price - (2 * atr_dollar), 2)

        stops[ticker] = {
            "stop_price":  stop_price,
            "entry_price": price,
            "atr_pct":     atr_pct,
            "entry_date":  today,
        }
        save_stops(stops)

        vcp_ok = vcp_dict.get("vcp_possible", False) if isinstance(vcp_dict, dict) else False

        # Step 7: Per-trade Slack alert
        slack_send(
            ":large_green_circle: *LIMIT ORDER PLACED* " + ticker + " (GTC)\n"
            "Shares: " + str(shares) + " @ limit $" + str(round(price, 2))
            + " = *$" + str(int(dollar_actual)) + "*\n"
            "Stop: $" + str(stop_price)
            + " (2×ATR = $" + str(round(atr_dollar, 2)) + ")\n"
            "Q=" + str(int(qs)) + " | Stage 2 | VCP=" + str(vcp_ok) + "\n"
            "_Fills at open if price ≤ limit — no chase if gap-up_"
        )

    # Step 8: Summary
    slack_send(
        ":bar_chart: *Alpaca Executor Summary — " + today + "*\n"
        "Market: " + market_state + " | Mode: " + sizing_mode
        + " | Size multiplier: " + str(size_mul) + "x\n"
        "Positions opened today: *" + str(orders_placed) + "*\n"
        "Total deployed: *$" + str(int(total_deployed)) + "*\n"
        "Cash remaining: *$" + str(int(buying_power)) + "*"
    )

    log.info("=== Alpaca executor done — %d orders placed ===", orders_placed)
