#!/usr/bin/env python3
"""
Cohort Health Index — Phase 1 (informational only).

Spec: docs/specs/cohort-health-index.md.

market_state is 100% index-level; the user judges the tape by his growth
cohort. This module builds the cohort universe fresh daily (union of the
three books + active watchlist + optional theme_map constituents), pulls one
batched Alpaca daily-bars fetch, and computes a 0-100 cohort_score with a
HEALTHY / MIXED / STRESS / CARNAGE label. market_monitor.py persists the
result as a `cohort` block in the daily record + rolling history and fires a
⚠ COHORT DIVERGENCE Slack alert when the index is bullish while the cohort
is in STRESS/CARNAGE.

Everything here is non-fatal by design: any failure returns None and the
market monitor runs exactly as before, just without the cohort block.
Phase 1 does NOT touch market_state transitions, gate decisions, or sizing.
"""

import os
import json
import logging
import datetime

import requests

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
ALPACA_DATA_URL = "https://data.alpaca.markets/v2"

# Metric window: last ~60 trading bars (spec §2.2 — 60d-high proxy for the
# 52wk-high read). Calendar lookback is wider so 60 trading bars + the 50MA
# always fit. Explicit `start` is MANDATORY (CLAUDE.md 2026-07-10 rule —
# Alpaca bars with no start returns bars: null).
COHORT_BARS_DAYS = 60
FETCH_CALENDAR_DAYS = 130
SYMBOL_BATCH_SIZE = 200

# Score weights (spec §2.2)
W_ABOVE_20MA = 0.35
W_ABOVE_50MA = 0.25
W_NOT_DOWN4 = 0.25
W_NEAR_HIGH = 0.15

# Label thresholds (spec §2.2)
CARNAGE_DOWN4_PCT = 25.0     # pct_down4_today >= 25% forces CARNAGE
CARNAGE_SCORE = 25           # score < 25 → CARNAGE
STRESS_SCORE = 40            # score < 40 → STRESS
HEALTHY_SCORE = 65           # score >= 65 → HEALTHY

INDEX_BULLISH_STATES = {"GREEN", "THRUST", "TREND-FOLLOW", "STEADY-UPTREND"}
DIVERGENCE_LABELS = {"STRESS", "CARNAGE"}

ACTIVE_WATCHLIST_PRIORITIES = {"watching", "focus", "entry-ready"}


# ----------------------------------------------------------------- helpers
def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _theme_map_tickers(theme_map) -> set:
    """Extract constituent tickers from theme_map.json.

    Primary shape (money-flow-dashboard spec §3.1):
        {"themes": {"T-XYZ": {"tickers": ["CRWD", ...]}, ...}}
    Tolerant by construction: recursively collects every "tickers"/
    "constituents" string list anywhere in the document, so nesting changes
    survive. The file may be created by a parallel agent — never assume it
    exists or is well-formed.
    """
    tickers: set = set()
    if theme_map is None:
        return tickers

    def _collect(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("tickers", "constituents") and isinstance(val, list):
                    for t in val:
                        if isinstance(t, str) and t.strip():
                            tickers.add(t.strip().upper())
                else:
                    _collect(val)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    _collect(theme_map)
    return tickers


# ---------------------------------------------------------- universe builder
def build_cohort_universe(data_dir: str | None = None) -> list:
    """Cohort universe per spec §2.1 — built fresh, deduped, sorted.

    Union of:
      - open positions across all 3 books (positions.json open_positions,
        paper_stops.json, live_alpaca_stops.json)
      - active watchlist rows (watching/focus/entry-ready, not archived)
      - theme_map.json constituents when the file exists (soft dependency)
    """
    data_dir = data_dir or DATA_DIR
    tickers: set = set()

    # Book 1 — manual/Robinhood (positions.json)
    positions = _load_json(os.path.join(data_dir, "positions.json"), {}) or {}
    for pos in positions.get("open_positions") or []:
        if not isinstance(pos, dict):
            continue
        tk = str(pos.get("ticker") or "").strip().upper()
        status = str(pos.get("status") or "active").lower()
        if tk and status != "closed":
            tickers.add(tk)

    # Books 2 + 3 — paper and live Alpaca stop files ({ticker: {...}})
    for fname in ("paper_stops.json", "live_alpaca_stops.json"):
        stops = _load_json(os.path.join(data_dir, fname), {}) or {}
        if isinstance(stops, dict):
            for tk, state in stops.items():
                if isinstance(state, dict) and str(tk).strip():
                    tickers.add(str(tk).strip().upper())

    # Active watchlist rows
    wl = _load_json(os.path.join(data_dir, "watchlist.json"), {}) or {}
    rows = wl.get("watchlist") if isinstance(wl, dict) else wl
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        priority = str(row.get("priority") or "").lower()
        status = str(row.get("status") or "").lower()
        tk = str(row.get("ticker") or "").strip().upper()
        if (tk and priority in ACTIVE_WATCHLIST_PRIORITIES
                and status != "archived"):
            tickers.add(tk)

    # theme_map.json constituents — optional, degrade gracefully
    theme_map = _load_json(os.path.join(data_dir, "theme_map.json"), None)
    tickers |= _theme_map_tickers(theme_map)

    return sorted(t for t in tickers if t)


# --------------------------------------------------------------- bars fetch
def fetch_cohort_bars(symbols: list,
                      days: int = COHORT_BARS_DAYS) -> dict | None:
    """ONE batched Alpaca daily-bars fetch for the whole cohort.

    Uses the multi-symbol /v2/stocks/bars endpoint, batched
    SYMBOL_BATCH_SIZE symbols per request with page_token pagination.
    ALWAYS passes an explicit `start` (2026-07-10 rule). Returns
    {symbol: [bar, ...]} with each list sliced to the last `days` bars,
    or None when keys are missing / nothing came back.
    """
    if not symbols:
        return None

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        log.warning("Alpaca keys missing — skipping cohort bars fetch")
        return None

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end = datetime.date.today()
    start = end - datetime.timedelta(days=FETCH_CALENDAR_DAYS)

    bars_by_symbol: dict = {}
    for i in range(0, len(symbols), SYMBOL_BATCH_SIZE):
        batch = symbols[i:i + SYMBOL_BATCH_SIZE]
        page_token = None
        for _ in range(20):  # pagination safety cap
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start.isoformat() + "T00:00:00Z",
                "end": end.isoformat() + "T23:59:59Z",
                "limit": 10000,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                params["page_token"] = page_token
            try:
                resp = requests.get(
                    f"{ALPACA_DATA_URL}/stocks/bars",
                    headers=headers, params=params, timeout=60,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                log.warning("Cohort bars batch %d failed: %s",
                            i // SYMBOL_BATCH_SIZE + 1, e)
                break
            for sym, bars in (payload.get("bars") or {}).items():
                if bars:
                    bars_by_symbol.setdefault(sym, []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

    if not bars_by_symbol:
        log.warning("Cohort bars fetch returned no data")
        return None

    return {sym: bars[-days:] for sym, bars in bars_by_symbol.items()}


# ------------------------------------------------------------ metrics/score
def cohort_label(score: float, pct_down4_today: float) -> str:
    """HEALTHY (>=65) / MIXED (40-64) / STRESS (25-39) / CARNAGE (<25 OR
    pct_down4 >= 25%). The down-4 override is the user's carnage read — a
    quarter of the cohort down 4%+ in one day is CARNAGE no matter what the
    MA shares say."""
    if pct_down4_today >= CARNAGE_DOWN4_PCT or score < CARNAGE_SCORE:
        return "CARNAGE"
    if score < STRESS_SCORE:
        return "STRESS"
    if score < HEALTHY_SCORE:
        return "MIXED"
    return "HEALTHY"


def compute_cohort_metrics(bars_by_symbol: dict) -> dict | None:
    """Compute cohort breadth metrics + score + label from daily bars.

    Per-metric denominators: today's-move metrics need >=2 bars, the 20MA
    share needs >=20 closes, the 50MA share needs >=50. Returns None when
    the usable universe is degenerate (<5 names with 2 bars, or no names
    with enough history for the MA shares).
    """
    if not bars_by_symbol:
        return None

    n_move = n_20 = n_50 = 0
    down4 = up4 = above20 = above50 = near_high = 0
    moves = []  # (chg_pct, dollar_move, close, ticker)

    for sym, bars in bars_by_symbol.items():
        closes = [b.get("c") for b in bars if b.get("c")]
        if len(closes) < 2:
            continue
        close = closes[-1]
        prev = closes[-2]
        if not prev:
            continue
        chg_pct = (close - prev) / prev * 100.0

        n_move += 1
        if chg_pct <= -4.0:
            down4 += 1
        elif chg_pct >= 4.0:
            up4 += 1
        moves.append((chg_pct, close - prev, close, sym))

        highs = [b.get("h") or b.get("c") for b in bars if b.get("c")]
        window_high = max(highs) if highs else close
        if window_high and close >= window_high * 0.90:
            near_high += 1

        if len(closes) >= 20:
            n_20 += 1
            if close >= sum(closes[-20:]) / 20.0:
                above20 += 1
        if len(closes) >= 50:
            n_50 += 1
            if close >= sum(closes[-50:]) / 50.0:
                above50 += 1

    if n_move < 5 or n_20 == 0 or n_50 == 0:
        log.warning("Cohort universe too thin for metrics "
                    "(move=%d, 20ma=%d, 50ma=%d)", n_move, n_20, n_50)
        return None

    f_down4 = down4 / n_move
    f_up4 = up4 / n_move
    f_20 = above20 / n_20
    f_50 = above50 / n_50
    f_high = near_high / n_move

    score = round(100.0 * (
        W_ABOVE_20MA * f_20
        + W_ABOVE_50MA * f_50
        + W_NOT_DOWN4 * (1.0 - f_down4)
        + W_NEAR_HIGH * f_high
    ))

    pct_down4_today = round(f_down4 * 100.0, 1)
    label = cohort_label(score, pct_down4_today)

    moves.sort(key=lambda m: m[0])
    worst = [
        {
            "ticker": tk,
            "chg_pct": round(chg, 1),
            "dollar_move": round(dmove, 2),
            "close": round(close, 2),
        }
        for chg, dmove, close, tk in moves[:3]
    ]

    return {
        "universe_size": n_move,
        "pct_down4_today": pct_down4_today,
        "pct_up4_today": round(f_up4 * 100.0, 1),
        "pct_above_20ma": round(f_20 * 100.0, 1),
        "pct_above_50ma": round(f_50 * 100.0, 1),
        "pct_within_10_of_52wk_high": round(f_high * 100.0, 1),
        "cohort_score": score,
        "label": label,
        "worst": worst,
    }


def compute_cohort_health(data_dir: str | None = None,
                          fetch_fn=None) -> dict | None:
    """Full pipeline: universe → one batched bars fetch → metrics.

    Returns the cohort block dict, or None on any failure (non-fatal by
    contract — caller writes the daily record without the cohort block).
    `fetch_fn` is injectable for tests.
    """
    try:
        universe = build_cohort_universe(data_dir)
        if not universe:
            log.info("Cohort universe empty — skipping cohort health")
            return None
        log.info("Cohort universe: %d symbols", len(universe))
        fetch = fetch_fn or fetch_cohort_bars
        bars = fetch(universe)
        if not bars:
            return None
        metrics = compute_cohort_metrics(bars)
        if metrics:
            log.info("Cohort health: %s (%d) · %.1f%% down-4 · "
                     "%.1f%% above 20MA",
                     metrics["label"], metrics["cohort_score"],
                     metrics["pct_down4_today"], metrics["pct_above_20ma"])
        return metrics
    except Exception as e:
        log.warning("Cohort health computation failed (non-fatal): %s", e)
        return None


# ------------------------------------------------------- divergence signal
def is_divergent(market_state: str, label: str) -> bool:
    """Index-bullish while the cohort is in STRESS/CARNAGE (spec §2.3)."""
    return ((market_state or "").upper() in INDEX_BULLISH_STATES
            and (label or "").upper() in DIVERGENCE_LABELS)


def is_resilient(market_state: str, label: str) -> bool:
    """Inverse case — index RED while the cohort is HEALTHY."""
    return ((market_state or "").upper() == "RED"
            and (label or "").upper() == "HEALTHY")


def should_alert_divergence(market_state: str, label: str,
                            last_alerted_label: str | None) -> bool:
    """Dedup per spec §2.3: alert once per label change, not per run.

    Caller persists the alerted label while divergence holds and clears it
    (None) when divergence ends, so a fresh divergence episode re-alerts and
    a STRESS→CARNAGE escalation alerts again.
    """
    if not is_divergent(market_state, label):
        return False
    return (label or "").upper() != (last_alerted_label or "").upper()


def format_cohort_line(cohort: dict) -> str:
    """One Slack line per spec §2.4:
    `Cohort: 61 MIXED · 12% down-4 · 48% above 20MA`"""
    return (
        f"Cohort: {cohort.get('cohort_score')} {cohort.get('label')} · "
        f"{cohort.get('pct_down4_today', 0):.0f}% down-4 · "
        f"{cohort.get('pct_above_20ma', 0):.0f}% above 20MA"
    )


def build_divergence_text(market_state: str, cohort: dict) -> str:
    """⚠ COHORT DIVERGENCE Slack message with the 3 worst names, $-moves."""
    worst_lines = []
    for w in cohort.get("worst", [])[:3]:
        worst_lines.append(
            f"• *{w['ticker']}* ${w['close']:.2f} "
            f"({w['dollar_move']:+.2f} today, {w['chg_pct']:+.1f}%)"
        )
    worst_block = "\n".join(worst_lines) if worst_lines else "• n/a"
    return (
        f"⚠️ *COHORT DIVERGENCE — index says {market_state}, "
        f"your cohort says {cohort.get('label')}*\n"
        f"Cohort score {cohort.get('cohort_score')} · "
        f"{cohort.get('pct_down4_today', 0):.0f}% of cohort down 4%+ today · "
        f"{cohort.get('pct_above_20ma', 0):.0f}% above 20MA · "
        f"{cohort.get('pct_above_50ma', 0):.0f}% above 50MA "
        f"(universe {cohort.get('universe_size')})\n"
        f"Worst cohort names today:\n{worst_block}\n"
        f"Index breadth looks fine but the names you actually trade are "
        f"under distribution — this is the read that hits the book first. "
        f"Phase 1: informational only, no gate change."
    )


def send_divergence_alert(market_state: str, cohort: dict,
                          webhook: str) -> None:
    """Post the divergence alert to #market-alerts. Non-fatal."""
    if not webhook:
        log.info("Market-alerts webhook not set — skipping cohort "
                 "divergence alert.")
        return
    text = build_divergence_text(market_state, cohort)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    try:
        resp = requests.post(webhook, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Cohort divergence alert sent: %s vs %s",
                 market_state, cohort.get("label"))
    except Exception as e:
        log.error("Cohort divergence alert failed: %s", e)
