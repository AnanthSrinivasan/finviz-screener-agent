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

import ast
import csv
import json
import math
import os
import logging
import datetime
import re
import requests

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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DATA_DIR          = os.environ.get("DATA_DIR", "data")
PAPER_STOPS_FILE  = os.path.join(DATA_DIR, "paper_stops.json")
MAX_POSITIONS     = 5


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
# Step 1: Regime Check
# ----------------------------
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
            for col in ("Quality Score", "ATR%", "EPS Y/Y TTM", "Rel Volume", "Appearances"):
                try:
                    row[col] = float(row.get(col, "") or 0)
                except (ValueError, TypeError):
                    row[col] = 0.0

            rows.append(row)

    log.info("Loaded %d tickers from %s", len(rows), path)
    return rows


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
    """Get latest close price from Alpaca bars."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/stocks/{symbol}/bars",
            headers=alpaca_headers(),
            params={"timeframe": "1Day", "limit": 1, "adjustment": "raw"},
            timeout=10,
        )
        if resp.ok:
            bars = resp.json().get("bars", [])
            if bars:
                return float(bars[-1]["c"])
    except Exception as e:
        log.warning("Alpaca price failed for %s: %s", symbol, e)

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
# Step 5: Claude bull/bear thesis
# ----------------------------
def get_claude_verdict(ticker: str, row: dict) -> tuple:
    """
    Calls Claude claude-sonnet-4-6 for a bull/bear thesis.
    Returns (verdict, full_text) where verdict is 'BUY' or 'SKIP'.
    """
    if not ANTHROPIC_API_KEY:
        return "SKIP", "No ANTHROPIC_API_KEY configured"

    stage_dict = row.get("Stage", {})
    vcp_dict   = row.get("VCP", {})
    stage_str  = stage_dict.get("badge", str(stage_dict)) if isinstance(stage_dict, dict) else str(stage_dict)
    vcp_str    = vcp_dict.get("reason", str(vcp_dict))    if isinstance(vcp_dict, dict)  else str(vcp_dict)

    qs       = row.get("Quality Score", 0)
    eps      = row.get("EPS Y/Y TTM", 0)
    rvol     = row.get("Rel Volume", 1)
    atr      = row.get("ATR%", 0)
    sector   = row.get("Sector", "")
    screeners = row.get("Screeners", "")

    prompt = (
        "You are a momentum trader. Argue bull and bear case for " + ticker + ". "
        "Context: Quality Score " + str(int(qs)) + ", Stage " + stage_str + ", "
        "VCP " + vcp_str + ", Sector " + sector + ", "
        "EPS Y/Y " + str(round(eps, 1)) + "%, RVol " + str(round(rvol, 1)) + "x, "
        "ATR% " + str(round(atr, 1)) + ", Screeners: " + screeners + ".\n"
        "Rules: Weinstein Stage 2 required, Minervini VCP preferred, regime is GREEN.\n"
        "End your response with exactly one line: VERDICT: BUY or VERDICT: SKIP"
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            text  = resp.json()["content"][0]["text"].strip()
            lines = text.splitlines()
            last  = lines[-1].strip() if lines else ""
            if "VERDICT: BUY" in last:
                return "BUY", text
            if "VERDICT: SKIP" in last:
                return "SKIP", text
            log.warning("Unexpected verdict line for %s: %r — defaulting SKIP", ticker, last)
            return "SKIP", text
        else:
            log.error("Claude API HTTP %s for %s", resp.status_code, ticker)
    except Exception as e:
        log.error("Claude verdict failed for %s: %s", ticker, e)

    return "SKIP", "Claude API error"


# ----------------------------
# Step 6: Place order
# ----------------------------
def place_order(symbol: str, qty: int) -> dict:
    try:
        resp = requests.post(
            f"{ALPACA_BASE_URL}/orders",
            headers=alpaca_headers(),
            json={
                "symbol":        symbol,
                "qty":           str(qty),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
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
# Main
# ----------------------------
if __name__ == "__main__":
    today = datetime.date.today().strftime("%Y-%m-%d")
    log.info("=== Alpaca executor starting — %s ===", today)

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.error("ALPACA_API_KEY or ALPACA_SECRET_KEY not set — aborting.")
        slack_send(":x: *Alpaca executor failed* — missing API credentials")
        raise SystemExit(1)

    # Step 1: Regime check
    regime, spy_price, sma200 = get_spy_regime()
    if regime == "RED":
        msg = (
            ":red_circle: *REGIME RED* — SPY ("
            + str(round(spy_price, 2))
            + ") below 200-day SMA ("
            + str(round(sma200, 2))
            + "). No new buys today."
        )
        log.info(msg)
        slack_send(msg)
        raise SystemExit(0)

    log.info("Regime GREEN — SPY %.2f above SMA200 %.2f", spy_price, sma200)

    # Step 2: Load screener CSV
    rows = load_screener_csv(today)
    if not rows:
        slack_send(":x: *Alpaca executor* — no screener data for " + today)
        raise SystemExit(1)

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
        len(open_positions), MAX_POSITIONS, portfolio_equity, buying_power,
    )

    if len(open_positions) >= MAX_POSITIONS:
        msg = (
            ":no_entry: *MAX POSITIONS REACHED* — "
            + str(len(open_positions))
            + "/"
            + str(MAX_POSITIONS)
            + " open. No new buys today."
        )
        log.info(msg)
        slack_send(msg)
        raise SystemExit(0)

    # Step 4–7: Evaluate and trade
    stops           = load_stops()
    orders_placed   = 0
    total_deployed  = 0.0
    pending_positions = set(open_positions)  # track adds during this run

    # Sort by Quality Score descending
    sorted_rows = sorted(rows, key=lambda r: r.get("Quality Score", 0), reverse=True)

    for row in sorted_rows:
        ticker = (row.get("Ticker") or "").strip()
        if not ticker:
            continue

        if ticker in pending_positions:
            log.info("Skipping %s — already held", ticker)
            continue

        if len(pending_positions) >= MAX_POSITIONS:
            log.info("Max positions reached — stopping evaluation")
            break

        qs = row.get("Quality Score", 0)

        # Step 4: Quality gate — must reach "strong conviction" bar
        if qs < 60:
            log.info("Skipping %s — Q=%.0f below 60 (min for auto-execution)", ticker, qs)
            continue

        # Stage 2 gate
        stage_dict = row.get("Stage", {})
        stage_num  = stage_dict.get("stage", 0) if isinstance(stage_dict, dict) else 0
        if stage_num != 2:
            log.info("Skipping %s — Stage %d, not Stage 2", ticker, stage_num)
            continue

        # Compute allocation
        vcp_dict     = row.get("VCP", {})
        dollar_alloc = compute_allocation(qs, vcp_dict, portfolio_equity)
        if dollar_alloc <= 0:
            log.info("Skipping %s — no allocation (Q=%.0f)", ticker, qs)
            continue

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

        # Step 5: Claude verdict
        verdict, reasoning = get_claude_verdict(ticker, row)

        if verdict == "SKIP":
            slack_send(
                ":yellow_circle: *SKIPPED* " + ticker
                + " (Q=" + str(int(qs)) + ") — Claude SKIP\n"
                + "_" + reasoning[:300] + "_"
            )
            log.info("Claude SKIP for %s", ticker)
            continue

        # Step 6: Place order
        log.info(
            "Placing BUY %s: %d shares @ ~$%.2f = $%.0f",
            ticker, shares, price, dollar_actual,
        )
        order_result = place_order(ticker, shares)
        if not order_result:
            continue

        orders_placed  += 1
        total_deployed += dollar_actual
        buying_power   -= dollar_actual
        pending_positions.add(ticker)

        # Step 7: Stop reference (2×ATR below entry)
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

        # Step 8: Per-trade Slack alert
        slack_send(
            ":large_green_circle: *BUY PLACED* " + ticker + "\n"
            "Shares: " + str(shares) + " @ ~$" + str(round(price, 2))
            + " = *$" + str(int(dollar_actual)) + "*\n"
            "Stop: $" + str(stop_price)
            + " (2×ATR = $" + str(round(atr_dollar, 2)) + ")\n"
            "Q=" + str(int(qs)) + " | Stage 2 | VCP=" + str(vcp_ok) + "\n"
            "_" + reasoning[:300] + "_"
        )

    # Step 8: Summary
    slack_send(
        ":bar_chart: *Alpaca Executor Summary — " + today + "*\n"
        "Regime: GREEN (SPY " + str(round(spy_price, 2))
        + " > SMA200 " + str(round(sma200, 2)) + ")\n"
        "Positions opened today: *" + str(orders_placed) + "*\n"
        "Total deployed: *$" + str(int(total_deployed)) + "*\n"
        "Cash remaining: *$" + str(int(buying_power)) + "*"
    )

    log.info("=== Alpaca executor done — %d orders placed ===", orders_placed)
