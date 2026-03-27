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
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL      = "https://api.anthropic.com/v1/messages"
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
PEEL_THRESHOLDS = {
    "low":     {"max_atr": 4.0,  "warn": 3.0, "signal": 4.0},
    "mid":     {"max_atr": 7.0,  "warn": 4.0, "signal": 6.0},
    "high":    {"max_atr": 10.0, "warn": 5.0, "signal": 8.0},
    "extreme": {"max_atr": 999,  "warn": 7.0, "signal": 10.0},
}

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
# SnapTrade Auth
# ----------------------------

def snaptrade_headers(path: str, query_params: dict = None) -> dict:
    timestamp = str(int(time.time()))
    query_str = ""
    if query_params:
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        query_str = sorted_params
    message = timestamp + path
    if query_str:
        message += "?" + query_str
    signature = hmac.new(
        SNAPTRADE_CONSUMER_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "Signature": signature,
        "timestamp": timestamp,
        "clientId": SNAPTRADE_CLIENT_ID,
        "Content-Type": "application/json",
    }

def snaptrade_get(path: str, params: dict = None) -> dict | list | None:
    full_params = {
        "userId": SNAPTRADE_USER_ID,
        "userSecret": SNAPTRADE_USER_SECRET,
    }
    if params:
        full_params.update(params)
    headers = snaptrade_headers(path, full_params)
    try:
        resp = requests.get(
            f"{SNAPTRADE_BASE}{path}",
            params=full_params,
            headers=headers,
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

def get_peel_thresholds(atr_pct: float) -> tuple:
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

        sma20 = parse_float(data.get("SMA20", "0"))
        if sma20 == 0:
            sma20 = parse_float(data.get("SMA50", "0"))

        pct_from_ma    = ((price / sma20) - 1) * 100 if sma20 > 0 else 0
        atr_multiple_ma = pct_from_ma / atr_pct if atr_pct > 0 else 0

        high_52w_raw   = data.get("52W High", "0").replace(",", "").strip()
        high_52w_match = re.match(r"^(\d+\.?\d*)", high_52w_raw)
        high_52w       = float(high_52w_match.group(1)) if high_52w_match else 0.0
        dist_from_high = ((price / high_52w) - 1) * 100 if high_52w > 0 else 0

        rel_vol = parse_float(data.get("Rel Volume", "1"), 1.0)

        result = {
            "price":          price,
            "atr":            atr,
            "atr_pct":        round(atr_pct, 2),
            "sma20":          sma20,
            "pct_from_ma":    round(pct_from_ma, 2),
            "atr_multiple_ma":round(atr_multiple_ma, 2),
            "dist_from_high": round(dist_from_high, 2),
            "rel_vol":        round(rel_vol, 2),
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

def get_ai_commentary(positions_with_metrics: list) -> str:
    if not ANTHROPIC_API_KEY or not positions_with_metrics:
        return ""
    lines = []
    for p in positions_with_metrics:
        m = p.get("metrics", {})
        lines.append(
            f"{p['ticker']}: {p['shares']:.0f} shares | avg cost ${p['avg_cost']:.2f} | "
            f"now ${m.get('price', p['current_price']):.2f} | "
            f"P&L ${p['pnl']:+.2f} ({p['pnl_pct']:+.1f}%) | "
            f"ATR mult from MA: {m.get('atr_multiple_ma', 0):.2f} | "
            f"% from MA: {m.get('pct_from_ma', 0):.2f}% | "
            f"dist from 52w high: {m.get('dist_from_high', 0):.1f}%"
        )
    newline = "\n"
    prompt = (
        f"You are a momentum trader reviewing open positions. "
        f"Exit signal fires at ATR multiple from MA = {ATR_MULTIPLE_EXIT}. "
        f"Hard stop fires at ${abs(MAX_POSITION_LOSS):,.0f} loss per position.\n\n"
        f"Current positions:\n{newline.join(lines)}\n\n"
        "Write 2-3 sentences. For each position: is the setup still valid or has the thesis broken? "
        "Flag any position approaching or past the exit thresholds. "
        "Be direct. Use ticker names. No disclaimers. Plain text only."
    )
    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if resp.ok:
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        log.warning(f"AI commentary failed: {e}")
    return ""


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


def send_daily_position_summary(positions_with_metrics: list, ai_commentary: str):
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
        peel_warn_m, peel_sig_m = get_peel_thresholds(m.get("atr_pct", 0))

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
    if ai_commentary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":brain: {ai_commentary}"}})
        blocks.append({"type": "divider"})
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


def sync_snaptrade_with_rules(snaptrade_positions: list, positions_data: dict) -> list:
    """
    Reconcile SnapTrade (source of truth for what exists) with positions.json
    (source of truth for rules-specific fields like stops/targets/gain protection).

    Returns list of warning messages.
    """
    warnings = []
    snap_tickers = {p["ticker"] for p in snaptrade_positions}
    rules_tickers = {p["ticker"] for p in positions_data["open_positions"]}

    # Tickers in SnapTrade but NOT in positions.json
    for ticker in snap_tickers - rules_tickers:
        warnings.append(
            f"\u26a0\ufe0f {ticker} found in SnapTrade but NOT in positions.json — "
            f"add manually or trigger via workflow_dispatch BUY"
        )
        log.warning(f"Sync: {ticker} in SnapTrade but not in positions.json")

    # Tickers in positions.json but NOT in SnapTrade — closed externally
    closed_externally = []
    for pos in positions_data["open_positions"]:
        if pos["ticker"] not in snap_tickers:
            pos["status"] = "closed_external"
            pos["close_date"] = datetime.date.today().isoformat()
            positions_data["closed_positions"].append(pos)
            closed_externally.append(pos["ticker"])
            warnings.append(
                f"\u26a0\ufe0f {pos['ticker']} in positions.json but NOT in SnapTrade — "
                f"moved to closed_positions (closed_external)"
            )
            log.warning(f"Sync: {pos['ticker']} closed externally")

    # Remove closed positions from open list
    if closed_externally:
        positions_data["open_positions"] = [
            p for p in positions_data["open_positions"]
            if p["ticker"] not in closed_externally
        ]

    return warnings


def apply_minervini_rules(position: dict, current_price: float) -> tuple:
    """
    Apply Minervini rules to a single position.
    Returns (alerts_list, position_was_modified).

    Rules applied:
      1. Stop loss check (positions.json stop)
      5. Gain protection (breakeven at +20%, trailing at +30%, fade warning)
      Targets: alert on target1/target2 hits
    """
    alerts = []
    modified = False
    ticker = position["ticker"]
    entry = position["entry_price"]

    # Update tracking fields
    prev_high = position.get("highest_price_seen", entry)
    if current_price > prev_high:
        position["highest_price_seen"] = round(current_price, 2)
        modified = True

    gain_pct = (current_price - entry) / entry * 100
    if round(gain_pct, 2) != position.get("current_gain_pct", 0):
        position["current_gain_pct"] = round(gain_pct, 2)
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

    # Gain fading warning
    if position.get("breakeven_stop_activated", False) and gain_pct < 5:
        alerts.append(
            f"\u26a0\ufe0f {ticker} gain fading \u2014 was +20%+, now +{gain_pct:.1f}% \u2014 watch closely"
        )

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

        # Calculate initial stop via yfinance 50MA
        initial_stop = round(price * 0.90, 2)  # fallback: 10% below entry
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="60d")
            if len(hist) >= 50:
                ma50 = round(hist["Close"].rolling(50).mean().iloc[-1], 2)
                if ma50 > 0:
                    initial_stop = ma50
        except Exception as e:
            log.warning(f"yfinance 50MA lookup failed for {ticker}: {e} — using 10% stop")

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

        sizing_note = ""
        if market_state == "CAUTION":
            sizing_note = "\n\u26a0\ufe0f Market is CAUTION \u2014 half sizing applies."

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
        stop = rp.get("stop", 0)
        t1 = rp.get("target1", 0)
        pos_lines.append(
            f"{ticker}  {rp['shares']} @ ${rp['entry_price']:.2f} \u2192 ${cur_price:.2f} "
            f"({gain:+.1f}%) | Stop ${stop:.2f} | Target ${t1:.2f}"
        )

    sizing = trading_state["current_sizing_mode"].upper()
    wins = trading_state["consecutive_wins"]
    losses = trading_state["consecutive_losses"]

    sections = [f"\U0001f4cb Position Monitor \u2014 {time_str}\nMarket: {state_emoji.get(market_state, '\u26aa')} {market_state}"]

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

    if not all([SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, SNAPTRADE_USER_SECRET]):
        log.error("SnapTrade credentials missing — check GitHub secrets.")
        exit(1)

    positions = fetch_positions()
    if not positions and not has_trade_input:
        log.info("No open positions found — nothing to monitor.")
        exit(0)

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
            stop_loss_pct = STOP_LOSS_BASE_PCT + (atr_pct * STOP_LOSS_ATR_MULT)

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
            peel_warn_mult, peel_signal_mult = get_peel_thresholds(atr_pct)
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

    # === EXISTING: AI commentary ===
    ai_commentary = get_ai_commentary(positions_with_metrics)

    # === NEW: RULES ENGINE — runs after existing checks, before Slack ===
    os.makedirs(DATA_DIR, exist_ok=True)

    # Step 8: Load rules engine state
    positions_data = load_positions_json()
    trading_state = load_trading_state()
    market_state = load_latest_market_state()
    rules_alerts = []

    # Step 9: Sync SnapTrade positions with positions.json
    sync_warnings = sync_snaptrade_with_rules(positions, positions_data)
    rules_alerts.extend(sync_warnings)

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
            pos_alerts, modified = apply_minervini_rules(rpos, cur_price)
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
    if rules_state_modified or sync_warnings:
        save_positions(positions_data)
    save_trading_state(trading_state)

    # === EXISTING: Fire all per-position alerts (hard stop, ATR, peel etc.) ===
    for alert_type, pos, metrics in alerts_to_fire:
        send_position_alert(pos, metrics, alert_type)

    # === EXISTING: Daily position summary ===
    send_daily_position_summary(positions_with_metrics, ai_commentary)

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
