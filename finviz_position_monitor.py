#!/usr/bin/env python3
# ----------------------------
# Finviz Position Monitor — Agent 4
# ----------------------------
import os
import json
import logging
import datetime
import random
import requests
import hmac
import hashlib
import time
import uuid
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
# Main
# ----------------------------

if __name__ == "__main__":
    today = datetime.date.today().isoformat()
    log.info(f"=== Position monitor starting — {today} ===")
    log.info(f"Hard stop threshold: ${MAX_POSITION_LOSS:,.0f} per position")

    if not all([SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, SNAPTRADE_USER_SECRET]):
        log.error("SnapTrade credentials missing — check GitHub secrets.")
        exit(1)

    positions = fetch_positions()
    if not positions:
        log.info("No open positions found — nothing to monitor.")
        exit(0)

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

    # Fire all alerts
    for alert_type, pos, metrics in alerts_to_fire:
        send_position_alert(pos, metrics, alert_type)

    ai_commentary = get_ai_commentary(positions_with_metrics)
    send_daily_position_summary(positions_with_metrics, ai_commentary)

    os.makedirs(DATA_DIR, exist_ok=True)
    snapshot_path = os.path.join(DATA_DIR, f"positions_{today}.json")
    with open(snapshot_path, "w") as f:
        safe = [{k: v for k, v in p.items() if k != "account_id"} for p in positions_with_metrics]
        json.dump({"date": today, "positions": safe}, f, indent=2)
    log.info(f"Snapshot saved: {snapshot_path}")
    log.info(f"=== Done — {len(positions)} positions, {len(alerts_to_fire)} alerts ===")
