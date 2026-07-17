#!/usr/bin/env python3
"""
Morning Brief — the one composed phone-first Slack message
(spec: docs/specs/cx-rehaul.md §5).

Posts to #daily-alerts right after the 13:00 UTC pre-market job (same run,
second message). Composes ONLY from files agents already wrote — no new
computation, no network beyond the Slack webhook. Every line is non-fatal:
a missing source omits its line, never blocks the message.

    ☀️ Brief — Tue Jul 15
    🚦 Gate: NO NEW ENTRIES · index CAUTION · cohort STRESS (34)
    💰 Money: IN Memory, Cyber · OUT Cloud sw
    📓 Book: 1 open · paper 3 · live 0
    🎯 Today: CRWD Q84 · MU Q81
    📅 ER today/tomorrow: TENB (watchlist) AMC
    ⚠️ Risk: regime late-rotation — fresh leaders only, half size
    → Cockpit: {PAGES_BASE_URL}/data/daily.html

Hard cap: 7 lines (header + 6 content) + the cockpit link.
"""

import datetime
import glob
import json
import logging
import os

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
PAGES_BASE_URL = os.environ.get(
    "PAGES_BASE_URL", "https://ananthsrinivasan.github.io/finviz-screener-agent")

MAX_LINES = 7          # header + content, hard cap (spec §5)
_RISK_REGIMES = {"late-rotation", "blow-off-risk", "correlation_phase"}


# ---------------------------------------------------------------- loaders

def _load_json(data_dir: str, name: str, default=None):
    try:
        with open(os.path.join(data_dir, name)) as f:
            return json.load(f)
    except Exception:
        return default


def _latest_market(data_dir: str) -> dict:
    files = sorted(glob.glob(os.path.join(data_dir, "market_monitor_2*.json")))
    files = [f for f in files if "history" not in f]
    if files:
        try:
            with open(files[-1]) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _latest_screener_rows(data_dir: str):
    """Rows of the newest screener CSV, or None when no CSV exists."""
    files = sorted(glob.glob(os.path.join(data_dir, "finviz_screeners_2*.csv")))
    if not files:
        return None
    try:
        import csv
        with open(files[-1]) as f:
            return list(csv.DictReader(f))
    except Exception:
        return None


# ---------------------------------------------------------------- line builders
# Each returns a str line or None (omit). All wrapped non-fatally by compose.

def _gate_line(data_dir: str):
    market = _latest_market(data_dir)
    ts = _load_json(data_dir, "trading_state.json", {}) or {}
    rotation = _load_json(data_dir, "etf_rotation.json", {}) or {}
    state = market.get("market_state") or ts.get("market_state")
    if not state:
        return None
    from utils.generators.generate_daily_cockpit import gate_decision
    gate = gate_decision(state, rotation.get("regime", ""),
                         ts.get("current_sizing_mode"))
    line = f"🚦 Gate: {gate['action']} · index {gate['state']}"
    cohort = market.get("cohort") or {}
    if cohort.get("label"):
        line += (f" · cohort {str(cohort['label']).upper()}"
                 f" ({cohort.get('cohort_score', '?')})")
    return line


def _money_line(data_dir: str):
    rotation = _load_json(data_dir, "etf_rotation.json", {}) or {}
    ml = rotation.get("money_line") or {}
    text = (ml.get("text") or "").strip() if isinstance(ml, dict) else ""
    if not text:
        return None
    return f"💰 {text}"


def _book_line(data_dir: str):
    pj = _load_json(data_dir, "positions.json")
    paper = _load_json(data_dir, "paper_stops.json")
    live = _load_json(data_dir, "live_alpaca_stops.json")
    if pj is None and paper is None and live is None:
        return None
    parts = []
    if pj is not None:
        n = len(pj.get("open_positions", []) or [])
        parts.append("flat (0 positions)" if n == 0 else f"{n} open")
    if isinstance(paper, dict):
        n = sum(1 for v in paper.values()
                if isinstance(v, dict) and not v.get("pending_close"))
        parts.append(f"paper {n}")
    if isinstance(live, dict):
        n = sum(1 for v in live.values()
                if isinstance(v, dict) and not v.get("pending_close"))
        parts.append(f"live {n}")
    return "📓 Book: " + " · ".join(parts) if parts else None


def _today_line(data_dir: str):
    rows = _latest_screener_rows(data_dir)
    if rows is None:
        return None
    from utils.generators.generate_daily_cockpit import qualify_setups
    pj = _load_json(data_dir, "positions.json", {}) or {}
    held = {p.get("ticker", "").upper()
            for p in pj.get("open_positions", []) if p.get("ticker")}
    cards = qualify_setups(rows, held)
    if not cards:
        return "🎯 Today: 0 qualify — patience"
    picks = " · ".join(f"{c['ticker']} Q{c['q']:.0f}" for c in cards[:3])
    return f"🎯 Today: {picks}"


def _earnings_line(data_dir: str):
    """Earnings state file — only when an agent has already written one
    (spec: earnings parse only if trivially available; else omit)."""
    er = _load_json(data_dir, "earnings_upcoming.json")
    if not er:
        return None
    items = er.get("upcoming", er) if isinstance(er, dict) else er
    if not isinstance(items, list) or not items:
        return None
    near = [e for e in items if isinstance(e, dict)
            and (e.get("days_until") is not None and int(e["days_until"]) <= 1)]
    if not near:
        return None
    tks = " · ".join(str(e.get("ticker", "?")) for e in near[:4])
    return f"📅 ER today/tomorrow: {tks}"


def _risk_line(data_dir: str):
    rotation = _load_json(data_dir, "etf_rotation.json", {}) or {}
    regime = (rotation.get("regime") or "").lower()
    if regime not in _RISK_REGIMES:
        return None
    try:
        from agents.utils.etf_rotation_summary import REGIME_ADVICE
        advice = REGIME_ADVICE.get(regime, "")
    except Exception:
        advice = ""
    return f"⚠️ Risk: regime {regime}" + (f" — {advice}" if advice else "")


# ---------------------------------------------------------------- compose

def compose_brief(data_dir: str = None, today: datetime.date = None) -> list:
    """The brief's lines (header + content, hard-capped at MAX_LINES).
    Every builder is non-fatal; missing sources simply omit their line."""
    d = data_dir or DATA_DIR
    today = today or datetime.date.today()
    lines = [f"☀️ Brief — {today.strftime('%a %b %-d')}"]
    for builder in (_gate_line, _money_line, _book_line, _today_line,
                    _earnings_line, _risk_line):
        try:
            line = builder(d)
        except Exception as e:
            log.warning("brief line %s failed (non-fatal): %s",
                        builder.__name__, e)
            line = None
        if line:
            lines.append(line)
    return lines[:MAX_LINES]


def build_message(data_dir: str = None, today: datetime.date = None) -> str:
    lines = compose_brief(data_dir, today)
    lines.append(f"→ Cockpit: {PAGES_BASE_URL}/data/daily.html")
    return "\n".join(lines)


def run_morning_brief(data_dir: str = None) -> bool:
    """Compose + post to Slack. Returns True when posted."""
    msg = build_message(data_dir)
    if not SLACK_WEBHOOK:
        log.warning("SLACK_WEBHOOK_URL not set — morning brief:\n%s", msg)
        return False
    try:
        import requests
        payload = {"blocks": [{"type": "section",
                               "text": {"type": "mrkdwn", "text": msg}}]}
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Morning brief sent (%d lines)", msg.count("\n") + 1)
        return True
    except Exception as e:
        log.error("Morning brief Slack send failed: %s", e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_morning_brief()
