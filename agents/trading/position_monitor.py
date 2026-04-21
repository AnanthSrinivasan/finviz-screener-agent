#!/usr/bin/env python3
# ----------------------------
# Finviz Position Monitor — Agent 4
# ----------------------------
import os
import sys
import json
import logging
import datetime
import random
import requests
import hmac
import hashlib
import time
import uuid
import base64
import glob as globmod
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# Config
# ----------------------------
SNAPTRADE_CLIENT_ID    = os.environ.get("SNAPTRADE_CLIENT_ID", "")
SNAPTRADE_CONSUMER_KEY = os.environ.get("SNAPTRADE_CONSUMER_KEY", "")
SNAPTRADE_USER_ID      = os.environ.get("SNAPTRADE_USER_ID", "ananth919")
SNAPTRADE_USER_SECRET  = os.environ.get("SNAPTRADE_USER_SECRET", "")
SLACK_WEBHOOK_URL      = os.environ.get("SLACK_WEBHOOK_URL", "")

FINVIZ_BASE            = "https://finviz.com"
DATA_DIR               = os.environ.get("DATA_DIR", "data")

# --- Exit thresholds (negative side) ---
ATR_MULTIPLE_EXIT = float(os.environ.get("ATR_MULTIPLE_EXIT", "-1.5"))
ATR_MULTIPLE_WARN = float(os.environ.get("ATR_MULTIPLE_WARN", "-1.0"))

# --- Hard position loss cap ---
# SLV Feb 2026: held through Stage 3 distribution, lost $11K on one position.
# This rule caps any single position at -$4,500 regardless of ATR calculation.
# If a position hits this threshold the alert fires immediately with GET OUT NOW.
# Override via environment variable if needed — but think twice before raising it.
MAX_POSITION_LOSS = float(os.environ.get("MAX_POSITION_LOSS", "-4500"))

# --- Peel thresholds (positive side) ---
# warn fires when ~75% of the way to signal — close enough to be actionable, not noise.
PEEL_THRESHOLDS = {
    "low":     {"max_atr": 4.0,  "warn": 3.0, "signal": 4.0},
    "mid":     {"max_atr": 7.0,  "warn": 5.0, "signal": 6.0},
    "high":    {"max_atr": 10.0, "warn": 6.5, "signal": 8.0},
    "extreme": {"max_atr": 999,  "warn": 8.5, "signal": 10.0},
}

# Cache for peel_calibration.json — loaded once per process
_PEEL_CALIBRATION_CACHE: dict | None = None

# --- Dynamic stop loss ---
STOP_LOSS_BASE_PCT = float(os.environ.get("STOP_LOSS_BASE_PCT", "5.0"))
STOP_LOSS_ATR_MULT = float(os.environ.get("STOP_LOSS_ATR_MULT", "0.5"))

SNAPTRADE_BASE = "https://api.snaptrade.com/api/v1"

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
# SnapTrade Auth (matching SDK's sign_request pattern)
# ----------------------------
from base64 import b64encode
from urllib.parse import urlencode

def snaptrade_get(path: str, params: dict = None) -> dict | list | None:
    """Authenticated GET using SnapTrade's signature scheme."""
    try:
        query_params = {
            "clientId": SNAPTRADE_CLIENT_ID,
            "timestamp": str(int(time.time())),
            "userId": SNAPTRADE_USER_ID,
            "userSecret": SNAPTRADE_USER_SECRET,
        }
        if params:
            query_params.update(params)

        # Signature: HMAC-SHA256 over JSON {"content":null,"path":...,"query":...}
        request_path = f"/api/v1{path}"
        query_string = urlencode(query_params)
        sig_object = json.dumps(
            {"content": None, "path": request_path, "query": query_string},
            separators=(",", ":"), sort_keys=True,
        )
        sig_digest = hmac.new(
            SNAPTRADE_CONSUMER_KEY.encode(),
            sig_object.encode(),
            hashlib.sha256,
        ).digest()
        signature = b64encode(sig_digest).decode()

        resp = requests.get(
            f"{SNAPTRADE_BASE}{path}",
            params=query_params,
            headers={"Signature": signature},
            timeout=15,
        )
        if not resp.ok:
            log.error(f"SnapTrade {path} failed: {resp.status_code} — {resp.text}")
            return None
        return resp.json()
    except Exception as e:
        log.error(f"SnapTrade request failed: {e}")
        return None


# ----------------------------
# Helper: Dynamic peel thresholds
# ----------------------------

def get_peel_thresholds(atr_pct: float, ticker: str = None) -> tuple:
    global _PEEL_CALIBRATION_CACHE
    if ticker:
        if _PEEL_CALIBRATION_CACHE is None:
            cal_path = os.path.join(DATA_DIR, "peel_calibration.json")
            try:
                with open(cal_path) as fh:
                    _PEEL_CALIBRATION_CACHE = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError):
                _PEEL_CALIBRATION_CACHE = {}
        entry = _PEEL_CALIBRATION_CACHE.get(ticker, {})
        if entry.get("calibrated"):
            return entry["warn"], entry["signal"]
    for tier in PEEL_THRESHOLDS.values():
        if atr_pct <= tier["max_atr"]:
            return tier["warn"], tier["signal"]
    return 7.0, 10.0


# ----------------------------
# Part 1: Fetch Live Positions
# ----------------------------

def fetch_positions() -> list:
    log.info("Fetching positions from SnapTrade...")
    accounts = snaptrade_get("/accounts")
    if not accounts:
        log.error("No accounts returned from SnapTrade.")
        return []
    log.info(f"Found {len(accounts)} account(s)")

    all_positions = []
    for account in accounts:
        account_id = account.get("id")
        if not account_id:
            continue
        positions = snaptrade_get(f"/accounts/{account_id}/positions")
        if not positions:
            continue

        for pos in positions:
            try:
                symbol_data = pos.get("symbol", {})
                symbol      = symbol_data.get("symbol", {})
                ticker      = symbol.get("symbol", "")
                if not ticker:
                    ticker = pos.get("symbol", {}).get("local_id", "")
                units     = float(pos.get("units", 0))
                avg_cost  = float(pos.get("average_purchase_price", 0) or 0)
                price     = float(pos.get("price", 0) or 0)
                if not ticker or units <= 0:
                    continue
                pnl     = (price - avg_cost) * units
                pnl_pct = ((price / avg_cost) - 1) * 100 if avg_cost > 0 else 0
                all_positions.append({
                    "ticker":        ticker,
                    "shares":        units,
                    "avg_cost":      avg_cost,
                    "current_price": price,
                    "market_value":  price * units,
                    "pnl":           pnl,
                    "pnl_pct":       round(pnl_pct, 2),
                    "account_id":    account_id,
                })
                log.info(f"Position: {ticker} {units} shares @ ${avg_cost:.2f} | now ${price:.2f} | P&L ${pnl:+.2f}")
            except Exception as e:
                log.warning(f"Could not parse position: {e} — {pos}")

    log.info(f"Total positions: {len(all_positions)}")
    return all_positions


# ----------------------------
# Part 2: Fetch Metrics from Finviz
# ----------------------------

def fetch_sma50_price(ticker: str, fallback_pct: float = 0.90) -> float:
    """Return the 50MA dollar price derived from Finviz's SMA50 % field.
    Falls back to price * fallback_pct if Finviz is unavailable."""
    session = make_session()
    try:
        from bs4 import BeautifulSoup
        import re
        resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": ticker}, timeout=10)
        if not resp.ok:
            return 0.0
        soup = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", class_="snapshot-table2")
        if not table:
            return 0.0
        data = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                data[key_cell.get_text(strip=True).rstrip(".")] = val_cell.get_text(strip=True)
        price_raw = data.get("Price", "0").replace(",", "").strip()
        price = float(re.match(r"^[\d.]+", price_raw).group()) if re.match(r"^[\d.]+", price_raw) else 0.0
        sma50_raw = data.get("SMA50") or data.get("SMA20") or ""
        if not sma50_raw or sma50_raw == "-":
            return 0.0
        pct = float(sma50_raw.replace("%", "").replace(",", "").strip())
        sma50_price = round(price / (1 + pct / 100), 2) if price > 0 else 0.0
        return sma50_price if sma50_price > 0 else 0.0
    except Exception as e:
        log.warning(f"{ticker}: Finviz SMA50 fetch failed — {e}")
        return 0.0


def fetch_alpaca_day_high(ticker: str) -> float:
    """Today's intraday high via Alpaca snapshot. Returns 0.0 if unavailable.
    Finviz snapshot has no intraday range, so we use Alpaca for the trailing-stop high."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_sec = os.environ.get("ALPACA_SECRET_KEY", "")
    base    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    if not api_key or not api_sec:
        return 0.0
    try:
        data_host = "https://data.alpaca.markets"
        resp = requests.get(
            f"{data_host}/v2/stocks/{ticker}/snapshot",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_sec},
            timeout=8,
        )
        if not resp.ok:
            return 0.0
        snap = resp.json() or {}
        day_bar = snap.get("dailyBar") or {}
        high = float(day_bar.get("h") or 0)
        return high
    except Exception as e:
        log.warning(f"{ticker}: Alpaca day_high fetch failed — {e}")
        return 0.0


def fetch_position_metrics(ticker: str) -> dict:
    session = make_session()
    try:
        resp = session.get(f"{FINVIZ_BASE}/quote.ashx", params={"t": ticker}, timeout=10)
        if not resp.ok:
            log.warning(f"{ticker}: Finviz fetch failed {resp.status_code}")
            return {}

        from bs4 import BeautifulSoup
        import re

        soup  = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", class_="snapshot-table2")
        if not table:
            log.warning(f"{ticker}: snapshot table not found")
            return {}

        data = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            for key_cell, val_cell in zip(cells[0::2], cells[1::2]):
                data[key_cell.get_text(strip=True).rstrip(".")] = val_cell.get_text(strip=True)

        def parse_float(raw, default=0.0):
            if not raw or raw in ('-', ''):
                return default
            raw = raw.replace(',', '').replace('%', '').replace('x', '').strip()
            m = re.match(r'^([\d.]+)([KMBkmb]?)', raw)
            if not m:
                return default
            val    = float(m.group(1))
            suffix = m.group(2).upper()
            if suffix == 'K': val *= 1_000
            elif suffix == 'M': val *= 1_000_000
            elif suffix == 'B': val *= 1_000_000_000
            return val

        price   = parse_float(data.get("Price", "0").replace(',', ''))
        atr     = parse_float(data.get("ATR (14)", "0"))
        atr_pct = (atr / price * 100) if price > 0 else 0

        # Finviz SMA50/SMA20 fields are already % distance from MA (e.g. "47.31%" or "-5.32%").
        # Parse as signed float directly — do NOT treat as a dollar price.
        def parse_signed_pct(raw):
            if not raw or raw == '-':
                return None
            try:
                return float(raw.replace('%', '').replace(',', '').strip())
            except ValueError:
                return None

        pct_from_ma = parse_signed_pct(data.get("SMA50")) \
                   or parse_signed_pct(data.get("SMA20")) \
                   or 0.0
        atr_multiple_ma = pct_from_ma / atr_pct if atr_pct > 0 else 0

        high_52w_raw   = data.get("52W High", "0").replace(",", "").strip()
        high_52w_match = re.match(r"^(\d+\.?\d*)", high_52w_raw)
        high_52w       = float(high_52w_match.group(1)) if high_52w_match else 0.0
        dist_from_high = ((price / high_52w) - 1) * 100 if high_52w > 0 else 0

        # Intraday high via Alpaca snapshot (Finviz snapshot has no intraday range).
        # Falls back to 0 — caller treats 0 as "use current_price".
        day_high = fetch_alpaca_day_high(ticker)
        day_low = 0.0

        rel_vol = parse_float(data.get("Rel Volume", "1"), 1.0)

        result = {
            "price":          price,
            "atr":            atr,
            "atr_pct":        round(atr_pct, 2),
            "pct_from_ma":    round(pct_from_ma, 2),
            "atr_multiple_ma":round(atr_multiple_ma, 2),
            "dist_from_high": round(dist_from_high, 2),
            "rel_vol":        round(rel_vol, 2),
            "day_high":       day_high,
            "day_low":        day_low,
        }
        log.info(
            f"{ticker} metrics: price={price} ATR%={atr_pct:.2f}% "
            f"pct_from_MA={pct_from_ma:.2f}% ATR_mult={atr_multiple_ma:.2f}"
        )
        return result

    except Exception as e:
        log.error(f"{ticker}: metrics fetch failed — {e}")
        return {}


# ----------------------------
# Part 3: AI Commentary
# ----------------------------



# ----------------------------
# Part 4: Slack Alerts
# ----------------------------

def send_position_alert(position: dict, metrics: dict, alert_type: str):
    if not SLACK_WEBHOOK_URL:
        return

    ticker    = position["ticker"]
    shares    = position["shares"]
    avg_cost  = position["avg_cost"]
    price     = metrics.get("price", position["current_price"])
    pnl       = (price - avg_cost) * shares
    pnl_pct   = ((price / avg_cost) - 1) * 100 if avg_cost > 0 else 0
    atr_mult  = metrics.get("atr_multiple_ma", 0)
    pct_ma    = metrics.get("pct_from_ma", 0)
    atr_pct   = metrics.get("atr_pct", 0)
    stop_thresh = position.get("stop_loss_pct", 5.0)

    configs = {
        # ── HARD STOP — fires first, absolute dollar cap ──────────────────────
        "HARD_STOP": (
            "🚨", "#dc2626",
            f"HARD STOP — {ticker} down ${abs(pnl):,.0f}",
            f"Position is down *${abs(pnl):,.0f}* — breaching the *${abs(MAX_POSITION_LOSS):,.0f} hard stop*.\n"
            f"This is not an ATR signal. This is the absolute loss cap.\n"
            f"*Get out now. No exceptions.*\n\n"
            f"_SLV Feb 2026 rule: no single position loses more than ${abs(MAX_POSITION_LOSS):,.0f}. "
            f"That trade cost $11K. This alert exists so it never happens again._"
        ),
        # ── ATR EXIT ──────────────────────────────────────────────────────────
        "EXIT_ATR": (
            "🔴", "#f87171",
            f"EXIT — {ticker} ATR multiple hit {atr_mult:.2f}",
            f"ATR multiple from MA has breached *{ATR_MULTIPLE_EXIT}* (now {atr_mult:.2f}).\n"
            f"Your exit indicator has fired. This is the signal you built the system for."
        ),
        "EXIT_STOP": (
            "🔴", "#f87171",
            f"STOP LOSS — {ticker} down {pnl_pct:.1f}%",
            f"Position is down *{pnl_pct:.1f}%* from your entry, breaching the -{stop_thresh:.1f}% dynamic stop.\n"
            f"Dynamic stop = {STOP_LOSS_BASE_PCT}% base + {atr_pct:.1f}% ATR x {STOP_LOSS_ATR_MULT} = {stop_thresh:.1f}%."
        ),
        # ── WARNINGS ──────────────────────────────────────────────────────────
        "WARN_ATR": (
            "🟡", "#facc15",
            f"WARNING — {ticker} ATR multiple at {atr_mult:.2f}",
            f"ATR multiple from MA is {atr_mult:.2f} — approaching exit threshold of {ATR_MULTIPLE_EXIT}.\n"
            f"Watch closely. Exit signal has not fired yet."
        ),
        "WARN_STOP": (
            "🟡", "#facc15",
            f"STOP WARNING — {ticker} down {pnl_pct:.1f}%",
            f"Position is down {pnl_pct:.1f}% — approaching dynamic stop of -{stop_thresh:.1f}%.\n"
            f"No action required yet but monitor closely."
        ),
        # ── PEEL SIGNALS ──────────────────────────────────────────────────────
        "PEEL": (
            "🟢", "#4ade80",
            f"PEEL SIGNAL — {ticker} ATR multiple at {atr_mult:.2f}x",
            f"ATR multiple from MA has hit *{atr_mult:.2f}x* — your peel signal threshold.\n"
            f"Price is {pct_ma:.1f}% above the MA. Extension is real — consider reducing position.\n"
            f"Threshold scales with ATR%: {atr_pct:.1f}% ATR → signal at {position.get('peel_signal_mult', 10):.1f}x"
        ),
        "PEEL_WARN": (
            "🔵", "#60a5fa",
            f"PEEL WARNING — {ticker} ATR multiple at {atr_mult:.2f}x",
            f"ATR multiple from MA is {atr_mult:.2f}x — approaching peel threshold of {position.get('peel_signal_mult', 10):.1f}x.\n"
            f"Price is {pct_ma:.1f}% above the MA. Extension building — tighten stop."
        ),
    }

    emoji, color, title, reason = configs.get(
        alert_type, ("⚪", "#64748b", f"Alert — {ticker}", "")
    )

    body = (
        f"*Position:* {shares:.0f} shares | avg ${avg_cost:.2f} | now ${price:.2f}\n"
        f"*P&L:* ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
        f"*% from MA:* {pct_ma:.2f}% | *ATR multiple:* {atr_mult:.2f} | ATR%: {atr_pct:.2f}%\n\n"
        f"{reason}"
    )

    payload = {
        "attachments": [{
            "color": color,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            ]
        }]
    }

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Alert sent: {title}")
    except Exception as e:
        log.error(f"Slack alert failed: {e}")


def send_daily_position_summary(positions_with_metrics: list):
    if not SLACK_WEBHOOK_URL or not positions_with_metrics:
        return

    today      = datetime.date.today().isoformat()
    lines      = []
    total_pnl  = 0

    for p in positions_with_metrics:
        m       = p.get("metrics", {})
        price   = m.get("price", p["current_price"])
        pnl     = (price - p["avg_cost"]) * p["shares"]
        pnl_pct = ((price / p["avg_cost"]) - 1) * 100 if p["avg_cost"] > 0 else 0
        total_pnl += pnl

        atr_mult    = m.get("atr_multiple_ma", 0)
        stop_thresh = p.get("stop_loss_pct", 5.0)
        peel_warn_m, peel_sig_m = get_peel_thresholds(m.get("atr_pct", 0), p.get("ticker"))

        # Hard stop check for display
        if pnl <= MAX_POSITION_LOSS:
            status = "🚨"
        elif atr_mult <= ATR_MULTIPLE_EXIT or pnl_pct <= -stop_thresh:
            status = "🔴"
        elif atr_mult <= ATR_MULTIPLE_WARN or pnl_pct <= -(stop_thresh * 0.6):
            status = "🟡"
        elif atr_mult >= peel_sig_m:
            status = "🟢"
        elif atr_mult >= peel_warn_m:
            status = "🔵"
        else:
            status = "⚪"

        lines.append(
            f"{status} *{p['ticker']}* · {p['shares']:.0f} shares · "
            f"avg ${p['avg_cost']:.2f} → ${price:.2f} · "
            f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}%) · "
            f"ATR mult {atr_mult:.2f}"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 Position Monitor — {today}"}},
    ]
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "\n".join(lines) +
                f"\n\n*Total P&L: ${total_pnl:+.2f}*\n"
                f"🚨 Hard stop (>${abs(MAX_POSITION_LOSS):,.0f} loss) "
                f"🔴 Exit/Stop · 🟡 Warning · ⚪ Healthy · 🔵 Peel warn · 🟢 Peel signal"
            )
        }
    })

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Daily position summary sent.")
    except Exception as e:
        log.error(f"Daily summary Slack failed: {e}")


# ----------------------------
# Rules Engine — Minervini 6 Rules
# ----------------------------

def load_positions_json() -> dict:
    """Load positions.json rules engine state."""
    path = os.path.join(DATA_DIR, "positions.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"open_positions": [], "closed_positions": []}


def load_trading_state() -> dict:
    """Load trading_state.json streak/sizing state."""
    path = os.path.join(DATA_DIR, "trading_state.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "consecutive_wins": 0, "consecutive_losses": 0,
        "total_wins": 0, "total_losses": 0,
        "current_sizing_mode": "normal", "sizing_override": None,
        "last_updated": "", "recent_trades": []
    }


def save_positions(positions_data: dict):
    """Save positions.json."""
    path = os.path.join(DATA_DIR, "positions.json")
    with open(path, "w") as f:
        json.dump(positions_data, f, indent=2)
    log.info(f"Saved positions.json ({len(positions_data['open_positions'])} open, {len(positions_data['closed_positions'])} closed)")


def save_trading_state(trading_state: dict):
    """Save trading_state.json."""
    path = os.path.join(DATA_DIR, "trading_state.json")
    with open(path, "w") as f:
        json.dump(trading_state, f, indent=2)
    log.info(f"Saved trading_state.json (sizing: {trading_state['current_sizing_mode']})")


def load_latest_market_state() -> str:
    """Load the latest market_monitor_*.json and return market_state string."""
    pattern = os.path.join(DATA_DIR, "market_monitor_*.json")
    files = sorted(globmod.glob(pattern))
    # Exclude history file
    files = [f for f in files if "history" not in f]
    if not files:
        log.warning("No market_monitor files found — defaulting to CAUTION")
        return "CAUTION"
    try:
        with open(files[-1]) as f:
            data = json.load(f)
        state = data.get("market_state", "CAUTION")
        log.info(f"Market state from {os.path.basename(files[-1])}: {state}")
        return state
    except Exception as e:
        log.warning(f"Could not load market state: {e} — defaulting to CAUTION")
        return "CAUTION"


def sync_snaptrade_with_rules(snaptrade_positions: list, positions_data: dict,
                              trading_state: dict, market_state: str) -> list:
    """
    Reconcile SnapTrade (source of truth for what exists) with positions.json
    (source of truth for rules-specific fields like stops/targets/gain protection).

    Auto-adds new positions detected in SnapTrade with sensible defaults.
    Auto-closes positions gone from SnapTrade and updates win/loss streak.

    Returns list of alert messages.
    """
    alerts = []
    today = datetime.date.today().isoformat()
    snap_tickers = {p["ticker"] for p in snaptrade_positions}
    snap_by_ticker = {p["ticker"]: p for p in snaptrade_positions}
    rules_tickers = {p["ticker"] for p in positions_data["open_positions"]}

    # --- AUTO-ADD: Tickers in SnapTrade but NOT in positions.json ---
    for ticker in snap_tickers - rules_tickers:
        snap = snap_by_ticker[ticker]
        entry_price = round(snap["avg_cost"], 2)
        shares = snap["shares"]

        # Calculate initial stop via Finviz SMA50
        initial_stop = round(entry_price * 0.93, 2)  # fallback: 7% below entry
        ma50 = fetch_sma50_price(ticker, fallback_pct=0.93)
        if ma50 > 0:
            initial_stop = ma50

        new_position = {
            "ticker": ticker,
            "shares": int(shares) if shares == int(shares) else shares,
            "entry_price": entry_price,
            "entry_date": today,
            "stop": initial_stop,
            "stop_type": "auto_50ma",
            "breakeven_stop_activated": False,
            "target1": round(entry_price * 1.20, 2),
            "target1_hit": False,
            "target2": round(entry_price * 1.40, 2),
            "thesis": "Auto-detected from SnapTrade — update thesis via workflow_dispatch",
            "status": "active",
            "highest_price_seen": round(snap["current_price"], 2),
            "current_gain_pct": round(snap["pnl_pct"], 2),
        }
        positions_data["open_positions"].append(new_position)

        alerts.append(
            f"\U0001f7e2 AUTO-DETECTED NEW POSITION: {ticker}\n"
            f"   {int(shares)} shares @ ${entry_price:.2f} (from SnapTrade)\n"
            f"   Auto-stop: ${initial_stop:.2f} | T1: ${new_position['target1']:.2f} | T2: ${new_position['target2']:.2f}\n"
            f"   \u2139\ufe0f Update thesis/stop via workflow_dispatch if needed"
        )
        log.info(f"Sync: auto-added {ticker} — {int(shares)} shares @ ${entry_price:.2f}")

    # --- AUTO-CLOSE: Tickers in positions.json but NOT in SnapTrade ---
    closed_externally = []
    for pos in positions_data["open_positions"]:
        if pos["ticker"] not in snap_tickers:
            ticker = pos["ticker"]
            entry_price = pos["entry_price"]

            # Use last known price for result calculation
            last_price = pos.get("highest_price_seen", entry_price)
            # Try to get current price from a recent metric fetch
            result_pct = (last_price - entry_price) / entry_price * 100 if entry_price else 0

            pos["status"] = "closed_external"
            pos["close_date"] = today
            pos["close_price"] = round(last_price, 2)
            pos["result_pct"] = round(result_pct, 2)
            positions_data["closed_positions"].append(pos)
            closed_externally.append(ticker)

            # Update win/loss streak
            if result_pct > 0:
                trading_state["total_wins"] += 1
                trading_state["consecutive_wins"] += 1
                trading_state["consecutive_losses"] = 0
                result_label = "WIN"
            else:
                trading_state["total_losses"] += 1
                trading_state["consecutive_losses"] += 1
                trading_state["consecutive_wins"] = 0
                result_label = "LOSS"

            # Record trade
            trading_state["recent_trades"].append({
                "ticker": ticker,
                "result": "win" if result_pct > 0 else "loss",
                "result_pct": round(result_pct, 2),
                "date": today,
                "side": "SELL",
                "source": "auto_detected",
            })
            trading_state["recent_trades"] = trading_state["recent_trades"][-20:]
            trading_state["last_updated"] = today

            alerts.append(
                f"\U0001f534 AUTO-DETECTED CLOSE: {ticker}\n"
                f"   Entry ${entry_price:.2f} \u2192 ${last_price:.2f} ({result_pct:+.1f}%) — {result_label}\n"
                f"   Streak: {trading_state['consecutive_wins']}W / {trading_state['consecutive_losses']}L"
            )
            log.info(f"Sync: auto-closed {ticker} — {result_pct:+.1f}% ({result_label})")

    # Remove closed positions from open list
    if closed_externally:
        positions_data["open_positions"] = [
            p for p in positions_data["open_positions"]
            if p["ticker"] not in closed_externally
        ]
        # Recalculate sizing mode after closes
        sizing_alerts = update_sizing_mode(trading_state, market_state)
        alerts.extend(sizing_alerts)

    return alerts


def apply_minervini_rules(position: dict, current_price: float, atr: float = 0.0,
                          day_high: float | None = None) -> tuple:
    """
    Apply Minervini rules to a single position.
    Returns (alerts_list, position_was_modified).

    `day_high` (when provided) is used to capture intraday peaks that the
    hourly snap price misses. Trailing stops and peak_gain_pct use
    max(current_price, day_high, prior highest_price_seen).
    """
    alerts = []
    modified = False
    ticker = position["ticker"]
    entry = position["entry_price"]

    # Use intraday high if caller supplied one (fixes stale highest_price_seen)
    high_candidate = current_price
    if day_high is not None and day_high > high_candidate:
        high_candidate = day_high

    prev_high = position.get("highest_price_seen", entry)
    if high_candidate > prev_high:
        position["highest_price_seen"] = round(high_candidate, 2)
        modified = True

    gain_pct = (current_price - entry) / entry * 100
    if round(gain_pct, 2) != position.get("current_gain_pct", 0):
        position["current_gain_pct"] = round(gain_pct, 2)
        modified = True

    # peak_gain_pct — tracks the best gain ever seen (intraday-aware)
    high_gain_pct = (position["highest_price_seen"] - entry) / entry * 100
    prev_peak = position.get("peak_gain_pct", 0.0)
    if high_gain_pct > prev_peak:
        position["peak_gain_pct"] = round(high_gain_pct, 2)
        modified = True

    # Rule 1 — Stop loss check (positions.json stop, tighter than ATR may be)
    stop = position.get("stop", 0)
    if stop > 0 and current_price <= stop:
        alerts.append(
            f"\U0001f6a8 STOP HIT: {ticker} @ ${current_price:.2f} \u2014 "
            f"exit immediately (stop ${stop:.2f})"
        )
        position["status"] = "stop_hit"
        modified = True
        log.warning(f"{ticker}: Rules engine STOP HIT — ${current_price:.2f} <= ${stop:.2f}")

    # Rule 5 — Gain protection

    # ATR trailing stop — raises incrementally from entry onwards (price - 2×ATR).
    # Silent (no Slack alert) — just moves the floor up automatically every run.
    # Only fires when price is profitable and stop would move higher than current stop.
    if atr > 0 and gain_pct > 0 and not position.get("breakeven_stop_activated", False):
        atr_trail = round(current_price - 2 * atr, 2)
        if atr_trail > position.get("stop", 0):
            position["stop"] = atr_trail
            position["stop_type"] = "atr_trail"
            modified = True
            log.info(f"{ticker}: ATR trail stop raised to ${atr_trail:.2f} (price=${current_price:.2f}, 2×ATR=${2*atr:.2f})")

    # Breakeven stop activation at +20%
    if gain_pct >= 20 and not position.get("breakeven_stop_activated", False):
        new_stop = round(entry * 1.005, 2)  # just above breakeven (+0.5%)
        if new_stop > position.get("stop", 0):
            position["stop"] = new_stop
        position["breakeven_stop_activated"] = True
        modified = True
        alerts.append(
            f"\U0001f512 {ticker} +{gain_pct:.1f}% \u2014 stop moved to breakeven ${new_stop:.2f}"
        )
        log.info(f"{ticker}: Breakeven stop activated at ${new_stop:.2f}")

    # Trailing stop at +30%
    if gain_pct >= 30:
        trail_stop = round(position["highest_price_seen"] * 0.90, 2)  # 10% trail from high
        if trail_stop > position.get("stop", 0):
            position["stop"] = trail_stop
            modified = True
            alerts.append(
                f"\U0001f4c8 {ticker} +{gain_pct:.1f}% \u2014 trailing stop raised to ${trail_stop:.2f}"
            )
            log.info(f"{ticker}: Trailing stop raised to ${trail_stop:.2f}")

    # Gain fading warning — fires when peak ≥ +20% AND price has dropped 1×ATR
    # below the highest_price_seen. Alert (not exit); the stop is the exit.
    # Dedup: suppress re-alert unless current_gain has fallen another 5pp since last alert.
    peak_gain = position.get("peak_gain_pct", gain_pct)
    high = position.get("highest_price_seen", entry)
    fade_trigger_price = high - atr if atr > 0 else None
    in_fade_zone = (
        peak_gain >= 20
        and fade_trigger_price is not None
        and current_price < fade_trigger_price
    )
    if in_fade_zone:
        last_alert_gain = position.get("last_fade_alert_gain_pct")
        should_fire = (
            last_alert_gain is None
            or (last_alert_gain - gain_pct) >= 5
        )
        if should_fire:
            given_back = peak_gain - gain_pct
            alerts.append(
                f"\u26a0\ufe0f {ticker} fading \u2014 peak +{peak_gain:.1f}%, "
                f"now +{gain_pct:.1f}% (gave back {given_back:.1f}pp, "
                f"price ${current_price:.2f} < high ${high:.2f} \u2212 1\u00d7ATR ${atr:.2f})"
            )
            position["last_fade_alert_gain_pct"] = round(gain_pct, 2)
            modified = True
    elif "last_fade_alert_gain_pct" in position:
        # Recovered (or out of zone) — clear dedup so next fade fires cleanly
        position.pop("last_fade_alert_gain_pct", None)
        modified = True

    # Target alerts
    target1 = position.get("target1", 0)
    if target1 > 0 and current_price >= target1 and not position.get("target1_hit", False):
        position["target1_hit"] = True
        modified = True
        alerts.append(
            f"\U0001f3af {ticker} HIT TARGET 1 ${target1:.2f} \u2014 "
            f"consider selling half, move stop to breakeven"
        )
        log.info(f"{ticker}: Target 1 hit at ${target1:.2f}")
        today_str = datetime.date.today().isoformat()
        _save_winner_chart(ticker, "T1", today_str)

    target2 = position.get("target2", 0)
    if target2 > 0 and current_price >= target2:
        alerts.append(
            f"\U0001f3af\U0001f3af {ticker} HIT TARGET 2 ${target2:.2f} \u2014 "
            f"trail remaining position tightly"
        )
        log.info(f"{ticker}: Target 2 hit at ${target2:.2f}")

    return alerts, modified


def update_sizing_mode(trading_state: dict, market_state: str) -> list:
    """
    Recalculate sizing mode based on streak and market state.
    Returns list of alerts if mode changed.
    """
    alerts = []
    old_mode = trading_state["current_sizing_mode"]

    if trading_state["consecutive_losses"] >= 3:
        trading_state["current_sizing_mode"] = "suspended"
    elif trading_state["consecutive_losses"] == 2:
        trading_state["current_sizing_mode"] = "reduced"
    elif trading_state["consecutive_wins"] >= 2 and market_state in ("GREEN", "THRUST"):
        trading_state["current_sizing_mode"] = "aggressive"
    else:
        trading_state["current_sizing_mode"] = "normal"

    new_mode = trading_state["current_sizing_mode"]
    if new_mode != old_mode:
        if new_mode == "suspended":
            alerts.append(
                "\U0001f6a8 SIZING SUSPENDED \u2014 3 consecutive losses. "
                "Paper trade only until 2 consecutive wins."
            )
        elif new_mode == "reduced":
            alerts.append(
                "\u26a0\ufe0f SIZING REDUCED \u2014 2 consecutive losses. "
                "Max 5% position size until streak breaks."
            )
        log.info(f"Sizing mode changed: {old_mode} -> {new_mode}")

    return alerts


def handle_trade_input(ticker: str, shares: int, price: float, side: str,
                       positions_data: dict, trading_state: dict,
                       market_state: str) -> list:
    """
    Process BUY/SELL from workflow_dispatch.
    Returns list of alert strings.
    """
    alerts = []
    today = datetime.date.today().isoformat()

    if side == "BUY":
        # Rule 6 — Market state gate
        if market_state in ("RED", "BLACKOUT"):
            alerts.append(
                f"\u274c BLOCKED: Market is {market_state} \u2014 "
                f"no new entries. Rule 6: no forced trades."
            )
            return alerts

        # Rule 4 — No averaging down
        existing = next(
            (p for p in positions_data["open_positions"] if p["ticker"] == ticker),
            None,
        )
        if existing:
            if price < existing["entry_price"]:
                alerts.append(
                    f"\u274c BLOCKED: Cannot average down on {ticker}. "
                    f"Current ${price:.2f} < entry ${existing['entry_price']:.2f}."
                )
                return alerts

        # Sizing mode check
        if trading_state["current_sizing_mode"] == "suspended":
            alerts.append(
                "\u274c BLOCKED: Sizing suspended (3 consecutive losses). Paper trade only."
            )
            return alerts

        # Calculate initial stop via Finviz SMA50
        initial_stop = round(price * 0.90, 2)  # fallback: 10% below entry
        ma50 = fetch_sma50_price(ticker, fallback_pct=0.90)
        if ma50 > 0:
            initial_stop = ma50

        sizing_note = ""
        if market_state == "CAUTION":
            sizing_note = "\n\u26a0\ufe0f Market is CAUTION \u2014 half sizing applies."

        # --- Averaging up: merge into existing position ---
        if existing:
            old_shares = existing["shares"]
            old_cost   = existing["entry_price"]
            new_total  = old_shares + shares
            new_avg    = round((old_shares * old_cost + shares * price) / new_total, 2)
            existing["shares"]       = new_total
            existing["entry_price"]  = new_avg
            existing["avg_cost"]     = new_avg
            # Recalculate targets from new avg cost
            existing["target1"]      = round(new_avg * 1.20, 2)
            existing["target2"]      = round(new_avg * 1.40, 2)
            existing["target1_hit"]  = False  # reset — targets shift up
            # Keep stop unchanged (already trailed up), but raise if new stop is higher
            if initial_stop > existing.get("stop", 0):
                existing["stop"]      = initial_stop
                existing["stop_type"] = "50ma_add"
            existing["highest_price_seen"] = max(existing.get("highest_price_seen", price), price)
            alerts.append(
                f"\U0001f7e2 ADDED TO {ticker}: +{shares} shares @ ${price:.2f}\n"
                f"   Total: {new_total} shares | New avg: ${new_avg:.2f}\n"
                f"   Stop: ${existing['stop']:.2f} | T1: ${existing['target1']:.2f} | T2: ${existing['target2']:.2f}"
                f"{sizing_note}"
            )
            return alerts

        # --- New position ---
        target1 = round(price * 1.20, 2)
        target2 = round(price * 1.40, 2)

        new_position = {
            "ticker": ticker,
            "shares": shares,
            "entry_price": round(price, 2),
            "entry_date": today,
            "stop": initial_stop,
            "stop_type": "50ma",
            "breakeven_stop_activated": False,
            "target1": target1,
            "target1_hit": False,
            "target2": target2,
            "thesis": "Added via workflow_dispatch",
            "status": "active",
            "highest_price_seen": round(price, 2),
            "current_gain_pct": 0.0,
        }
        positions_data["open_positions"].append(new_position)

        alerts.append(
            f"\U0001f7e2 NEW POSITION: {ticker} {shares} shares @ ${price:.2f}\n"
            f"   Auto-stop: ${initial_stop} (50MA)\n"
            f"   Target 1: ${target1} (+20%)\n"
            f"   Target 2: ${target2} (+40%)\n"
            f"   Sizing mode: {trading_state['current_sizing_mode']}"
            f"{sizing_note}"
        )

    elif side == "SELL":
        position = next(
            (p for p in positions_data["open_positions"] if p["ticker"] == ticker),
            None,
        )
        if not position:
            alerts.append(f"\u26a0\ufe0f {ticker} not found in positions.json \u2014 logging close anyway")
            result_pct = 0.0
        else:
            result_pct = (price - position["entry_price"]) / position["entry_price"] * 100
            position["status"] = "closed"
            position["close_price"] = round(price, 2)
            position["close_date"] = today
            position["result_pct"] = round(result_pct, 2)
            positions_data["open_positions"].remove(position)
            positions_data["closed_positions"].append(position)

        # Save chart if this is a winning exit
        if result_pct > 0:
            _save_winner_chart(ticker, "exit_win", today)

        # Update streak
        if result_pct > 0:
            trading_state["total_wins"] += 1
            trading_state["consecutive_wins"] += 1
            trading_state["consecutive_losses"] = 0
        else:
            trading_state["total_losses"] += 1
            trading_state["consecutive_losses"] += 1
            trading_state["consecutive_wins"] = 0

        # Record trade
        trading_state["recent_trades"].append({
            "ticker": ticker,
            "result_pct": round(result_pct, 2),
            "date": today,
            "side": "SELL",
        })
        trading_state["recent_trades"] = trading_state["recent_trades"][-20:]
        trading_state["last_updated"] = today

        # Recalculate sizing mode
        sizing_alerts = update_sizing_mode(trading_state, market_state)
        alerts.extend(sizing_alerts)

        alerts.append(
            f"\U0001f534 POSITION CLOSED: {ticker} @ ${price:.2f}\n"
            f"   Result: {result_pct:+.1f}%\n"
            f"   Streak: {trading_state['consecutive_wins']}W / {trading_state['consecutive_losses']}L\n"
            f"   Sizing mode: {trading_state['current_sizing_mode']}"
        )

    return alerts


def send_rules_engine_alerts(alerts: list, positions_data: dict, trading_state: dict,
                             market_state: str, positions_with_metrics: list):
    """Send rules engine alerts to Slack as a unified message."""
    if not SLACK_WEBHOOK_URL:
        return
    if not alerts and not positions_data["open_positions"]:
        return

    today = datetime.date.today().isoformat()
    now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
    time_str = now_et.strftime("%-I:%M%p ET")

    state_emoji = {
        "THRUST": "\U0001f7e2", "GREEN": "\U0001f7e2", "CAUTION": "\U0001f7e1",
        "DANGER": "\U0001f7e0", "RED": "\U0001f534", "BLACKOUT": "\u26ab",
    }

    # Build position lines
    pos_lines = []
    for rp in positions_data["open_positions"]:
        ticker = rp["ticker"]
        # Find matching SnapTrade data for current price
        snap = next((p for p in positions_with_metrics if p["ticker"] == ticker), None)
        cur_price = snap["metrics"].get("price", snap["current_price"]) if snap and snap.get("metrics") else rp.get("highest_price_seen", rp["entry_price"])
        gain = (cur_price - rp["entry_price"]) / rp["entry_price"] * 100
        peak = rp.get("peak_gain_pct", gain)
        stop = rp.get("stop", 0)
        t1 = rp.get("target1", 0)
        t2 = rp.get("target2", 0)
        t1_mark = "\u2705" if rp.get("target1_hit") else "\u23f3"
        t2_mark = "\u2705" if (t2 > 0 and cur_price >= t2) else "\u23f3"
        be_suffix = " BE" if rp.get("breakeven_stop_activated") else ""
        peak_str = f", peak +{peak:.1f}%" if peak > gain + 0.1 else ""
        pos_lines.append(
            f"{ticker}  {rp['shares']} @ ${rp['entry_price']:.2f} \u2192 ${cur_price:.2f} "
            f"({gain:+.1f}%{peak_str}) | Stop ${stop:.2f}{be_suffix} | "
            f"T1 {t1_mark} ${t1:.2f} | T2 {t2_mark} ${t2:.2f}"
        )

    # Daily reminder for positions in T1→T2 holding zone
    for rp in positions_data["open_positions"]:
        ticker = rp["ticker"]
        if not rp.get("target1_hit"):
            continue
        t2 = rp.get("target2", 0)
        snap = next((p for p in positions_with_metrics if p["ticker"] == ticker), None)
        cur_price = snap["metrics"].get("price", snap["current_price"]) if snap and snap.get("metrics") else rp.get("highest_price_seen", rp["entry_price"])
        if t2 > 0 and cur_price >= t2:
            continue  # T2 already hit → different zone
        gain = (cur_price - rp["entry_price"]) / rp["entry_price"] * 100
        reminder = (
            f"\U0001f3af {ticker} T1 locked at +20% \u2014 watching T2 ${t2:.2f} "
            f"(now +{gain:.1f}%)"
        )
        if reminder not in alerts:
            alerts.append(reminder)

    sizing = trading_state["current_sizing_mode"].upper()
    wins = trading_state["consecutive_wins"]
    losses = trading_state["consecutive_losses"]

    default_emoji = "\u26aa"
    sections = [f"\U0001f4cb Position Monitor \u2014 {time_str}\nMarket: {state_emoji.get(market_state, default_emoji)} {market_state}"]

    if pos_lines:
        sections.append("*Open Positions:*\n" + "\n".join(pos_lines))

    if alerts:
        sections.append("*\u26a0\ufe0f Alerts:*\n" + "\n".join(alerts))

    sections.append(f"Sizing: {sizing} ({wins}W / {losses}L streak)")

    text = "\n\n".join(sections)

    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=10,
        )
        resp.raise_for_status()
        log.info(f"Rules engine Slack sent ({len(alerts)} alerts)")
    except Exception as e:
        log.error(f"Rules engine Slack failed: {e}")


# ----------------------------
# Winner chart capture
# ----------------------------

FINVIZ_CHART_URL = "https://finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d"
CHART_PATTERNS_DIR = os.path.join("data", "chart_patterns", "winners")


def _save_winner_chart(ticker: str, label: str, today: str):
    """
    Download today's Finviz daily chart for a winning trade and save to
    data/chart_patterns/winners/{ticker}_{today}_{label}.png

    label: "T1", "T2", "exit_win" etc.
    Non-fatal — a failed download never blocks the monitor.
    """
    try:
        os.makedirs(CHART_PATTERNS_DIR, exist_ok=True)
        url = FINVIZ_CHART_URL.format(ticker=ticker)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finviz.com/",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if not resp.ok:
            log.warning("Chart download failed for %s: HTTP %s", ticker, resp.status_code)
            return
        filename = f"{ticker}_{today}_{label}.png"
        path = os.path.join(CHART_PATTERNS_DIR, filename)
        with open(path, "wb") as f:
            f.write(resp.content)
        log.info("Winner chart saved: %s (%d bytes)", path, len(resp.content))
    except Exception as e:
        log.warning("Chart capture failed for %s: %s", ticker, e)


# ----------------------------
# Watchlist management
# ----------------------------

def _handle_watchlist_action(ticker: str, action: str, today: str):
    """
    Handle watchlist_action dispatch inputs from GitHub Actions.

    Actions:
      focus    — promote ticker to focus priority (actionable this week)
      archive  — manually archive ticker
      unarchive — restore archived ticker to watching
    """
    watchlist_path = os.path.join(DATA_DIR, "watchlist.json")
    try:
        with open(watchlist_path) as f:
            wl_data = json.load(f)
    except Exception:
        log.error("Cannot open watchlist.json for action=%s ticker=%s", action, ticker)
        return

    entries = wl_data.get("watchlist", [])
    match = next((e for e in entries if e.get("ticker") == ticker), None)

    if action == "focus":
        if match is None:
            log.warning("Watchlist: %s not found — adding as focus entry", ticker)
            entries.append({
                "ticker": ticker, "entry_note": "Manually promoted to focus",
                "entry_price": None, "stop": None, "thesis": "",
                "added": today, "status": "watching", "priority": "focus", "source": "manual",
            })
        else:
            if match.get("status") == "archived":
                match["status"] = "watching"
                match.pop("archive_reason", None)
                match.pop("archived_date", None)
            match["priority"] = "focus"
        log.info("Watchlist: %s → FOCUS", ticker)
        msg = f"📌 *Watchlist — Focus promoted*: {ticker} moved to Focus List"

    elif action == "archive":
        if match is None:
            log.warning("Watchlist: %s not found — nothing to archive", ticker)
            return
        match["status"] = "archived"
        match["archive_reason"] = "manual"
        match["archived_date"] = today
        match["priority"] = "watching"
        log.info("Watchlist: %s → ARCHIVED (manual)", ticker)
        msg = f"🗑️ *Watchlist — Archived*: {ticker} removed from active watchlist"

    elif action == "unarchive":
        if match is None:
            log.warning("Watchlist: %s not found", ticker)
            return
        match["status"] = "watching"
        match["priority"] = "watching"
        match.pop("archive_reason", None)
        match.pop("archived_date", None)
        log.info("Watchlist: %s → UNARCHIVED", ticker)
        msg = f"♻️ *Watchlist — Restored*: {ticker} back to watchlist"

    else:
        log.error("Unknown watchlist_action: %s (valid: focus|archive|unarchive)", action)
        return

    wl_data["watchlist"] = entries
    with open(watchlist_path, "w") as f:
        json.dump(wl_data, f, indent=2)

    # Send Slack confirmation
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if webhook:
        try:
            requests.post(webhook, json={"text": msg}, timeout=10)
        except Exception as e:
            log.warning("Slack watchlist action notify failed: %s", e)


# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    today = datetime.date.today().isoformat()
    log.info(f"=== Position monitor starting — {today} ===")
    log.info(f"Hard stop threshold: ${MAX_POSITION_LOSS:,.0f} per position")

    # Read workflow_dispatch inputs from environment (set by GitHub Actions)
    wd_ticker = os.environ.get("INPUT_TICKER", "").strip().upper()
    wd_shares = os.environ.get("INPUT_SHARES", "").strip()
    wd_price  = os.environ.get("INPUT_PRICE", "").strip()
    wd_side   = os.environ.get("INPUT_SIDE", "").strip().upper()
    has_trade_input = bool(wd_ticker and wd_side)

    # Watchlist management dispatch (separate from trade inputs)
    wd_wl_action = os.environ.get("INPUT_WATCHLIST_ACTION", "").strip().lower()
    wd_wl_ticker = os.environ.get("INPUT_WATCHLIST_TICKER", "").strip().upper()
    if wd_wl_action and wd_wl_ticker:
        _handle_watchlist_action(wd_wl_ticker, wd_wl_action, today)
        # watchlist-only dispatch: skip full monitor run
        if not has_trade_input:
            exit(0)

    if not all([SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, SNAPTRADE_USER_SECRET]):
        log.error("SnapTrade credentials missing — check GitHub secrets.")
        exit(1)

    positions = fetch_positions()
    if not positions and not has_trade_input:
        log.info("No open positions found — nothing to monitor.")
        exit(0)

    market_state = load_latest_market_state()

    # === EXISTING FLOW: Fetch metrics, check hard stop / ATR / dynamic stop / peel ===
    positions_with_metrics = []
    alerts_to_fire         = []

    for pos in positions:
        ticker  = pos["ticker"]
        metrics = fetch_position_metrics(ticker)
        pos["metrics"] = metrics

        if metrics:
            atr_mult   = metrics.get("atr_multiple_ma", 0)
            pct_from_ma = metrics.get("pct_from_ma", 0)
            atr_pct    = metrics.get("atr_pct", 0)
            price      = metrics.get("price", pos["current_price"])
            avg_cost   = pos["avg_cost"]
            shares     = pos["shares"]

            pnl        = (price - avg_cost) * shares
            pnl_pct    = ((price / avg_cost) - 1) * 100 if avg_cost > 0 else 0
            # Tighten stop base in bear market conditions
            base_pct = 3.0 if market_state in ("RED", "DANGER") else STOP_LOSS_BASE_PCT
            stop_loss_pct = base_pct + (atr_pct * STOP_LOSS_ATR_MULT)

            pos["stop_loss_pct"] = round(stop_loss_pct, 1)
            pos["pnl_pct"]       = round(pnl_pct, 2)
            pos["pnl"]           = round(pnl, 2)

            # ── HARD STOP — checked first, absolute priority ──────────────
            if pnl <= MAX_POSITION_LOSS:
                alerts_to_fire.append(("HARD_STOP", pos, metrics))
                log.warning(
                    f"{ticker}: HARD STOP — P&L ${pnl:+.2f} breached "
                    f"${MAX_POSITION_LOSS:,.0f} hard cap"
                )

            # ── ATR EXIT ─────────────────────────────────────────────────
            elif atr_mult <= ATR_MULTIPLE_EXIT:
                alerts_to_fire.append(("EXIT_ATR", pos, metrics))
                log.warning(f"{ticker}: EXIT — ATR mult {atr_mult:.2f} breached {ATR_MULTIPLE_EXIT}")

            elif pnl_pct <= -stop_loss_pct:
                alerts_to_fire.append(("EXIT_STOP", pos, metrics))
                log.warning(f"{ticker}: STOP LOSS — down {pnl_pct:.1f}% (threshold -{stop_loss_pct:.1f}%)")

            elif atr_mult <= ATR_MULTIPLE_WARN:
                alerts_to_fire.append(("WARN_ATR", pos, metrics))
                log.warning(f"{ticker}: WARNING — ATR mult {atr_mult:.2f}")

            elif pnl_pct <= -(stop_loss_pct * 0.6):
                alerts_to_fire.append(("WARN_STOP", pos, metrics))
                log.warning(f"{ticker}: STOP WARNING — down {pnl_pct:.1f}%")

            # ── PEEL ─────────────────────────────────────────────────────
            peel_warn_mult, peel_signal_mult = get_peel_thresholds(atr_pct, ticker)
            pos["peel_warn_mult"]   = peel_warn_mult
            pos["peel_signal_mult"] = peel_signal_mult

            if atr_mult >= peel_signal_mult:
                alerts_to_fire.append(("PEEL", pos, metrics))
                log.info(f"{ticker}: PEEL SIGNAL — ATR mult {atr_mult:.2f}")
            elif atr_mult >= peel_warn_mult:
                alerts_to_fire.append(("PEEL_WARN", pos, metrics))
                log.info(f"{ticker}: PEEL WARNING — ATR mult {atr_mult:.2f}")
            else:
                log.info(f"{ticker}: healthy — ATR mult {atr_mult:.2f} | {pct_from_ma:.1f}% from MA")

        positions_with_metrics.append(pos)
        time.sleep(1)

    # === RULES ENGINE — runs after existing checks, before Slack ===
    os.makedirs(DATA_DIR, exist_ok=True)

    # Step 8: Load rules engine state
    positions_data = load_positions_json()
    trading_state = load_trading_state()
    rules_alerts = []

    # Step 9: Sync SnapTrade positions with positions.json (auto-add/auto-close)
    sync_alerts = sync_snaptrade_with_rules(positions, positions_data, trading_state, market_state)
    rules_alerts.extend(sync_alerts)

    # Steps 10-12: Apply Minervini rules per position
    rules_state_modified = False
    for rpos in positions_data["open_positions"]:
        ticker = rpos["ticker"]
        # Get current price from the SnapTrade/Finviz data already fetched
        snap = next((p for p in positions_with_metrics if p["ticker"] == ticker), None)
        if snap:
            cur_price = snap.get("metrics", {}).get("price", snap.get("current_price", 0))
        else:
            cur_price = rpos.get("highest_price_seen", rpos["entry_price"])
            log.warning(f"{ticker}: no SnapTrade data — using last known price ${cur_price:.2f}")

        if cur_price > 0:
            metrics = snap.get("metrics", {}) if snap else {}
            atr = metrics.get("atr", 0.0)
            day_high = metrics.get("day_high") or None
            pos_alerts, modified = apply_minervini_rules(
                rpos, cur_price, atr=atr, day_high=day_high
            )
            rules_alerts.extend(pos_alerts)
            if modified:
                rules_state_modified = True

    # Step 13: Check progressive sizing state
    sizing_alerts = update_sizing_mode(trading_state, market_state)
    rules_alerts.extend(sizing_alerts)

    # Step 14: Handle workflow_dispatch BUY/SELL inputs
    if has_trade_input:
        log.info(f"workflow_dispatch trade input: {wd_side} {wd_ticker} {wd_shares} @ {wd_price}")
        try:
            trade_shares = int(wd_shares) if wd_shares else 0
            trade_price = float(wd_price) if wd_price else 0.0
        except ValueError:
            log.error(f"Invalid trade input — shares={wd_shares} price={wd_price}")
            trade_shares = 0
            trade_price = 0.0

        if trade_shares > 0 and trade_price > 0:
            trade_alerts = handle_trade_input(
                wd_ticker, trade_shares, trade_price, wd_side,
                positions_data, trading_state, market_state,
            )
            rules_alerts.extend(trade_alerts)
            rules_state_modified = True
        else:
            log.error("Trade input missing shares or price — skipping")

    # Step 15: Save updated state files
    if rules_state_modified or sync_alerts:
        save_positions(positions_data)
    save_trading_state(trading_state)

    # === EXISTING: Fire all per-position alerts (hard stop, ATR, peel etc.) ===
    for alert_type, pos, metrics in alerts_to_fire:
        send_position_alert(pos, metrics, alert_type)

    # === EXISTING: Daily position summary ===
    send_daily_position_summary(positions_with_metrics)

    # Step 16: Send rules engine alerts (only if there are actionable alerts)
    if rules_alerts:
        send_rules_engine_alerts(
            rules_alerts, positions_data, trading_state,
            market_state, positions_with_metrics,
        )

    # === EXISTING: Save snapshot ===
    snapshot_path = os.path.join(DATA_DIR, f"positions_{today}.json")
    with open(snapshot_path, "w") as f:
        safe = [{k: v for k, v in p.items() if k != "account_id"} for p in positions_with_metrics]
        json.dump({"date": today, "positions": safe}, f, indent=2)
    log.info(f"Snapshot saved: {snapshot_path}")
    log.info(f"=== Done — {len(positions)} positions, {len(alerts_to_fire)} existing alerts, {len(rules_alerts)} rules alerts ===")
