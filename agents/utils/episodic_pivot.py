"""Episodic Pivot (EP / SB) detector — Pradeep Bonde framework.

Detects the quiet **Setup Bar** the day BEFORE a catalyst-driven volume explosion.
Pattern B only (pullback-reversal on drying volume). Pattern A (single-bar
high-tight drift) was tested and discarded — 0% hit rate at +15%/5d after
tightening with consecutive-bars filter.

Reference cases:
- QBTS 2026-05-20 SB → 2026-05-21 EP +33%
- AMKR/AXTI/COHU 2026-05-19/20 SB cluster (semis thematic move)

Pre-filter is cheap (Finviz snapshot only). Bar-shape gate fetches 30 daily
Alpaca bars per candidate. Production candidate set is typically 20-50 tickers
after pre-filter; bar-shape narrows to 0-3 fires/day.

Pure helpers — easy to unit test. The finviz_agent.py orchestrator calls these
and handles Slack posting, HTML render, watchlist auto-add.
"""
from __future__ import annotations

import os
import json
import glob
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Persistence + paths
EP_STATE_PATH = "data/episodic_pivots.json"
SCREENER_CSV_GLOB = "data/finviz_screeners_*.csv"

# Cooldown
COOLDOWN_TRADING_DAYS = 20

# Universe
SCREENER_HISTORY_TRADING_DAYS = 20

# Pre-filter
MIN_SMA50_PCT = 10.0          # price ≥ +10% above 50d SMA
MIN_PERF_QUARTER = 15.0       # 3-month return ≥ +15%
MAX_ATR_PCT = 12.0
MIN_AVG_VOLUME = 1_000_000
MIN_MARKET_CAP = 500_000_000
MIN_PRICE = 5.0

EXCLUDED_SECTORS = {
    "Utilities", "Energy", "Real Estate", "Basic Materials", "Consumer Defensive",
}
EXCLUDED_INDUSTRY_SUBSTRINGS = (
    "Biotechnology", "Drug Manufacturers", "Pharmaceutical",
)

# Bar-shape (Pattern B — pullback-reversal)
MAX_RVOL = 1.0                 # volume drying
MAX_RANGE_CONTRACT = 0.80      # today's range vs prior 10d avg ≤ 0.80
MAX_PRIOR_3D_CUM = -8.0        # close[-1] / close[-4] - 1 ≤ -8% (pullback)
MIN_CHG_PCT = 3.0              # reversal up-day ≥ +3%

# Expansion filter (no EP-day in last N trading days)
EXPANSION_LOOKBACK = 7
EXPANSION_RVOL = 3.0
EXPANSION_CHG = 10.0

# Sector tag thresholds
SECTOR_ROTATING_RANK_DELTA = -5
SECTOR_BUCKETS_ROTATING_IN = {"BASE", "PRE-BREAKOUT"}

# Peer co-fire window
PEER_LOOKBACK_TRADING_DAYS = 5

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


@dataclass
class EPFire:
    ticker: str
    date: str
    close: float
    chg_pct: float
    rvol: float
    atr_pct: float
    range_contract: float
    prior_3d_cum: float
    dist_52w_hi: float
    sector: str
    industry: str
    etf: Optional[str]
    tags: list[str] = field(default_factory=list)   # subset of {"SECTOR", "PEERS"}
    peers: list[str] = field(default_factory=list)  # peer tickers contributing to PEERS tag

    @property
    def emoji(self) -> str:
        s, p = "SECTOR" in self.tags, "PEERS" in self.tags
        if s and p:  return "🔥"
        if p:        return "🌊"
        if s:        return "📈"
        return "⚡"

    @property
    def tier(self) -> str:
        """Sort key — lower = higher priority."""
        return {"🔥": 0, "🌊": 1, "📈": 2, "⚡": 3}[self.emoji]

    @property
    def score(self) -> float:
        """Within-tier ranking — rewards reversal strength + drying volume."""
        return float(self.chg_pct) * (1.0 - float(self.rvol))


# ── Persistence ───────────────────────────────────────────────────────────────

def load_ep_history(path: str = EP_STATE_PATH) -> dict:
    """{ticker: {last_fire_date, last_fire_tags, fire_count, peers}}"""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ep_history(history: dict, path: str = EP_STATE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(history, f, indent=2, sort_keys=True)


def is_in_cooldown(ticker: str, today: str, history: dict,
                   cooldown_days: int = COOLDOWN_TRADING_DAYS) -> bool:
    """Returns True if ticker last fired EP within cooldown window.
    Uses calendar days × 1.5 as a trading-day proxy (cheaper than maintaining
    a trading-day calendar).
    """
    rec = history.get(ticker)
    if not rec:
        return False
    last = rec.get("last_fire_date")
    if not last:
        return False
    try:
        d_today = datetime.strptime(today, "%Y-%m-%d").date()
        d_last = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (d_today - d_last).days < int(cooldown_days * 1.5)


# ── Universe build (tickers seen in screeners last 20 trading days) ───────────

def build_recent_screener_universe(today: str,
                                   history_days: int = SCREENER_HISTORY_TRADING_DAYS,
                                   pattern: str = SCREENER_CSV_GLOB) -> set[str]:
    """Returns the set of tickers that appeared in any
    `finviz_screeners_YYYY-MM-DD.csv` within the last `history_days` trading days.
    Uses calendar days × 1.5 as a trading-day proxy.
    """
    try:
        d_today = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return set()
    cutoff = d_today - timedelta(days=int(history_days * 1.5))
    tickers: set[str] = set()
    for path in glob.glob(pattern):
        try:
            datestr = os.path.basename(path).replace("finviz_screeners_", "").replace(".csv", "")
            d = datetime.strptime(datestr, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff or d > d_today:
            continue
        try:
            import pandas as pd
            df = pd.read_csv(path, usecols=["Ticker"])
            tickers.update(str(t).strip().upper() for t in df["Ticker"].dropna().tolist())
        except Exception as e:
            log.warning("EP universe: skip %s — %s", path, e)
            continue
    return tickers


# ── Pre-filter (cheap, Finviz snapshot row only) ──────────────────────────────

def passes_pre_filter(row, universe: set[str], open_positions: set[str],
                      exclude_already_surfaced: set[str]) -> tuple[bool, str]:
    """Returns (passes, reason). reason is empty if passes."""
    import pandas as pd

    def _f(key, default=None):
        v = row.get(key) if hasattr(row, "get") else default
        if v is None:
            return default
        try:
            if pd.isna(v):
                return default
        except (TypeError, ValueError):
            pass
        return v

    ticker = _f("Ticker")
    if not ticker:
        return False, "no ticker"
    ticker = str(ticker).strip().upper()
    if ticker in open_positions:
        return False, "held"
    if ticker in exclude_already_surfaced:
        return False, "already surfaced"
    if universe and ticker not in universe:
        return False, "not in screener universe"

    sma50 = _f("SMA50%")
    if sma50 is None or float(sma50) < MIN_SMA50_PCT:
        return False, f"SMA50% {sma50}"
    perf_q = _f("Perf Quarter")
    if perf_q is None or float(perf_q) < MIN_PERF_QUARTER:
        return False, f"perf_q {perf_q}"
    atr = _f("ATR%")
    if atr is None or float(atr) <= 0 or float(atr) > MAX_ATR_PCT:
        return False, f"atr {atr}"

    # Avg Volume and Market Cap — both stored as humanized strings in some
    # paths and raw numbers elsewhere. Use the same parser as the screener.
    def _parse(v):
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).replace(",", "").replace("$", "").strip()
        if not s or s == "-":
            return 0.0
        mult = 1.0
        if s.endswith("K"): mult = 1e3; s = s[:-1]
        elif s.endswith("M"): mult = 1e6; s = s[:-1]
        elif s.endswith("B"): mult = 1e9; s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return 0.0

    avg_vol = _parse(_f("Avg Volume") or _f("Volume"))
    if avg_vol < MIN_AVG_VOLUME:
        return False, f"avg_vol {avg_vol:.0f}"

    mcap = _parse(_f("Market Cap"))
    if mcap < MIN_MARKET_CAP:
        return False, f"mcap {mcap:.0f}"

    price = _f("Price") or _f("Close")
    if price is None or float(price) < MIN_PRICE:
        return False, f"price {price}"

    sector = str(_f("Sector") or "").strip()
    if sector in EXCLUDED_SECTORS:
        return False, f"sector {sector}"
    industry = str(_f("Industry") or "").strip()
    if any(sub in industry for sub in EXCLUDED_INDUSTRY_SUBSTRINGS):
        return False, f"industry {industry}"

    return True, ""


# ── Bar-shape gate (Pattern B) ────────────────────────────────────────────────

def compute_bar_metrics(rows: list[dict]) -> Optional[dict]:
    """Compute today's bar-shape metrics from raw Alpaca daily bars.
    Returns None if insufficient data (need ≥ EXPANSION_LOOKBACK + 11 bars).
    """
    if not rows or len(rows) < EXPANSION_LOOKBACK + 11:
        return None
    rows = sorted(rows, key=lambda b: b.get("t", ""))
    closes = [float(b.get("c") or 0) for b in rows]
    highs = [float(b.get("h") or 0) for b in rows]
    lows = [float(b.get("l") or 0) for b in rows]
    vols = [float(b.get("v") or 0) for b in rows]
    if closes[-1] <= 0:
        return None

    chg_pct = (closes[-1] / closes[-2] - 1.0) * 100.0 if closes[-2] > 0 else 0.0
    range_pct = (highs[-1] - lows[-1]) / closes[-1] * 100.0

    # Prior 10-day range avg (excluding today)
    prior_ranges = [(highs[i] - lows[i]) / closes[i] * 100.0
                    for i in range(-11, -1) if closes[i] > 0]
    range_avg10 = sum(prior_ranges) / len(prior_ranges) if prior_ranges else 0.0
    range_contract = range_pct / range_avg10 if range_avg10 > 0 else 99.0

    # Prior 20-day average volume (excluding today)
    prior_vols = vols[-21:-1] if len(vols) >= 21 else vols[:-1]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    rvol = vols[-1] / avg_vol if avg_vol > 0 else 0.0

    # 3-day pullback up to yesterday: close[-2] vs close[-5]
    prior_3d_cum = ((closes[-2] / closes[-5]) - 1.0) * 100.0 if closes[-5] > 0 else 0.0

    # Expansion in last N (excluding today)
    has_expansion = False
    for i in range(-EXPANSION_LOOKBACK - 1, -1):
        if i + 1 >= 0 or abs(i) > len(closes) or abs(i - 1) > len(closes):
            continue
        try:
            c_chg = (closes[i] / closes[i - 1] - 1.0) * 100.0 if closes[i - 1] > 0 else 0.0
            r = vols[i] / avg_vol if avg_vol > 0 else 0.0
        except (IndexError, ZeroDivisionError):
            continue
        if r >= EXPANSION_RVOL or c_chg >= EXPANSION_CHG:
            has_expansion = True
            break

    # 52w high distance (using available window — caller should supply Finviz value
    # for accuracy; this is a fallback from the bars we have)
    hi_window = max(highs) if highs else 0.0
    dist_52w_hi = (closes[-1] / hi_window - 1.0) * 100.0 if hi_window > 0 else 0.0

    return {
        "close": closes[-1],
        "chg_pct": chg_pct,
        "range_pct": range_pct,
        "range_contract": range_contract,
        "rvol": rvol,
        "prior_3d_cum": prior_3d_cum,
        "has_expansion_recent": has_expansion,
        "dist_window_hi": dist_52w_hi,
    }


def passes_bar_shape(metrics: dict) -> tuple[bool, str]:
    """Pattern B gates. Returns (passes, reason)."""
    if metrics.get("has_expansion_recent"):
        return False, "recent expansion"
    rvol = metrics.get("rvol", 99)
    if rvol > MAX_RVOL:
        return False, f"rvol {rvol:.2f}"
    rc = metrics.get("range_contract", 99)
    if rc > MAX_RANGE_CONTRACT:
        return False, f"range_contract {rc:.2f}"
    p3 = metrics.get("prior_3d_cum", 0)
    if p3 > MAX_PRIOR_3D_CUM:
        return False, f"prior_3d_cum {p3:.1f}"
    chg = metrics.get("chg_pct", 0)
    if chg < MIN_CHG_PCT:
        return False, f"chg_pct {chg:.1f}"
    return True, ""


def fetch_bars_batch(tickers: list[str], days: int = 40) -> dict[str, list[dict]]:
    """Multi-symbol bars fetch. Returns {ticker: [bar_rows]}.
    Skips hyphen/dot tickers (Alpaca rejects).
    """
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        log.warning("EP bars: missing Alpaca creds")
        return {}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=int(days * 1.8) + 10)
    start_iso = start.strftime("%Y-%m-%d")

    out: dict[str, list[dict]] = {}
    BATCH = 100
    clean = [t for t in tickers if t and "-" not in t and "." not in t]
    for i in range(0, len(clean), BATCH):
        batch = clean[i: i + BATCH]
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Day",
            "start": start_iso,
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex",
        }
        try:
            r = requests.get(ALPACA_BARS_URL, params=params, headers=headers, timeout=30)
        except Exception as e:
            log.warning("EP bars: request error %s", e)
            continue
        if r.status_code != 200:
            log.warning("EP bars: HTTP %s — %s", r.status_code, r.text[:200])
            continue
        bars_by_t = r.json().get("bars", {}) or {}
        for tk, rows in bars_by_t.items():
            out[tk] = rows
    return out


# ── Context tags ──────────────────────────────────────────────────────────────

def compute_context_tags(ticker: str, etf: Optional[str],
                         etf_rotation_data: dict, sector_snapshot: dict,
                         industry: str, history: dict, today: str,
                         peer_lookback: int = PEER_LOOKBACK_TRADING_DAYS,
                         ) -> tuple[list[str], list[str]]:
    """Returns (tags, peers).

    tags subset of {"SECTOR", "PEERS"}.
    peers = list of tickers in same industry that fired EP in last `peer_lookback`
            trading days (calendar days × 1.5 proxy).
    """
    tags: list[str] = []

    # SECTOR tag
    if etf:
        etf_meta = (etf_rotation_data.get("etfs_by_symbol") or {}).get(etf, {})
        bucket = (etf_meta.get("bucket") or "").upper()
        rotating_by_bucket = bucket in SECTOR_BUCKETS_ROTATING_IN
        rank_delta = (sector_snapshot.get(etf) or {}).get("rank_delta_5d")
        rotating_by_rank = rank_delta is not None and rank_delta <= SECTOR_ROTATING_RANK_DELTA
        if rotating_by_bucket or rotating_by_rank:
            tags.append("SECTOR")

    # PEERS tag — scan history for same-industry fires within window
    peers: list[str] = []
    try:
        d_today = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        d_today = None

    for other_t, rec in history.items():
        if other_t == ticker:
            continue
        if rec.get("industry", "") != industry or not industry:
            continue
        last = rec.get("last_fire_date")
        if not last or not d_today:
            continue
        try:
            d_last = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            continue
        days_ago = (d_today - d_last).days
        if 0 <= days_ago <= int(peer_lookback * 1.5):
            peers.append(other_t)

    if peers:
        tags.append("PEERS")

    return tags, peers


# ── ETF rotation loader ───────────────────────────────────────────────────────

def load_etf_rotation(path: str = "data/etf_rotation.json") -> dict:
    """Load etf_rotation.json. Returns dict with `etfs_by_symbol` keyed by ticker."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"etfs_by_symbol": {}, "regime": "unknown"}
    by_sym = {}
    for etf in data.get("etfs", []) or []:
        sym = etf.get("ticker")
        if sym:
            by_sym[sym] = etf
    data["etfs_by_symbol"] = by_sym
    return data


# ── Slack formatting ──────────────────────────────────────────────────────────

def format_momentum_slack_blocks(fires: list[EPFire], today: str,
                                 gallery_url: Optional[str] = None) -> list[dict]:
    """Builds Slack blocks for the #momentum-alerts channel."""
    if not fires:
        return []

    fires_sorted = sorted(fires, key=lambda f: (f.tier, -f.score))
    tier_counts = {"🔥": 0, "🌊": 0, "📈": 0, "⚡": 0}
    for f in fires_sorted:
        tier_counts[f.emoji] += 1
    summary_bits = [f"{e} {n}" for e, n in tier_counts.items() if n > 0]

    blocks: list[dict] = [{
        "type": "header",
        "text": {"type": "plain_text",
                 "text": f"⚡ Episodic Pivot — {today} ({len(fires_sorted)} setups)"}
    }, {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": (f"*Pradeep SB lane:* drying volume + 3-day pullback + reversal up-day\n"
                          f"*Counts:* {' · '.join(summary_bits)}\n"
                          f"*Sizing:* 🔥/🌊 full · 📈 half (leader risk) · ⚡ quarter (no edge)")}
    }, {"type": "divider"}]

    for f in fires_sorted:
        tag_str = ""
        if f.tags:
            parts = []
            if "SECTOR" in f.tags and f.etf:
                parts.append(f"[SECTOR ↑ {f.etf}]")
            if "PEERS" in f.tags:
                parts.append(f"[PEERS: {', '.join(f.peers[:3])}]")
            tag_str = " " + " ".join(parts)
        else:
            tag_str = " [STANDALONE]"

        line = (
            f"{f.emoji} *{f.ticker}* · ${f.close:.2f} · {f.chg_pct:+.1f}% · "
            f"RVol {f.rvol:.2f} · ATR {f.atr_pct:.1f}%{tag_str}\n"
            f"3d pullback {f.prior_3d_cum:+.1f}% → reversal · "
            f"dist 52w-hi {f.dist_52w_hi:+.1f}% · "
            f"`/stock-research {f.ticker}`"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

    if gallery_url:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": f"<{gallery_url}|📊 Full chart gallery>"}})
    return blocks


def format_daily_teaser(fires: list[EPFire]) -> Optional[str]:
    """One-line teaser appended to the main #daily-alerts post."""
    if not fires:
        return None
    n = len(fires)
    n_hot = sum(1 for f in fires if f.emoji == "🔥")
    hot_str = f" ({n_hot} 🔥 sector-confirmed)" if n_hot else ""
    return f"⚡ {n} EP setup{'s' if n != 1 else ''} today{hot_str} — see #momentum-alerts"


# ── Slack posting ─────────────────────────────────────────────────────────────

def post_to_momentum_channel(blocks: list[dict]) -> bool:
    """Post EP blocks to #momentum-alerts. Returns True on success."""
    if not blocks:
        return False
    url = os.getenv("SLACK_WEBHOOK_MOMENTUM", "")
    if not url:
        log.info("SLACK_WEBHOOK_MOMENTUM not set — skipping momentum post")
        return False
    try:
        r = requests.post(url, json={"blocks": blocks}, timeout=10)
        r.raise_for_status()
        log.info("EP momentum post sent (%d blocks)", len(blocks))
        return True
    except Exception as e:
        log.error("EP momentum post failed: %s", e)
        return False


# ── HTML formatting ───────────────────────────────────────────────────────────

EP_HTML_CSS = """
<style>
.ep-section { margin: 24px 0; }
.ep-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.ep-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
.ep-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.ep-emoji { font-size: 20px; }
.ep-ticker { font-weight: 700; font-size: 16px; color: #111827; }
.ep-meta { font-size: 12px; color: #6b7280; margin-left: auto; }
.ep-tag { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-right: 4px; }
.ep-tag-sector { background: #fee2e2; color: #b91c1c; }
.ep-tag-peers { background: #dbeafe; color: #1d4ed8; }
.ep-tag-standalone { background: #f3f4f6; color: #6b7280; }
.ep-metrics { font-size: 12px; color: #374151; margin: 6px 0; }
.ep-metrics strong { color: #111827; }
.ep-card img { width: 100%; border-radius: 4px; }
</style>
"""


def format_html_section(fires: list[EPFire], finviz_chart_url: str = "https://finviz.com/chart.ashx?t={ticker}&p=d&s=l") -> str:
    """Render the ⚡ Episodic Pivots collapsible section for the chart grid HTML."""
    if not fires:
        return ""
    fires_sorted = sorted(fires, key=lambda f: (f.tier, -f.score))
    cards = []
    for f in fires_sorted:
        tag_html = []
        if "SECTOR" in f.tags and f.etf:
            tag_html.append(f'<span class="ep-tag ep-tag-sector">SECTOR ↑ {f.etf}</span>')
        if "PEERS" in f.tags:
            tag_html.append(f'<span class="ep-tag ep-tag-peers">PEERS: {", ".join(f.peers[:3])}</span>')
        if not f.tags:
            tag_html.append('<span class="ep-tag ep-tag-standalone">STANDALONE</span>')
        cards.append(f"""
<div class="ep-card">
  <div class="ep-card-head">
    <span class="ep-emoji">{f.emoji}</span>
    <span class="ep-ticker">{f.ticker}</span>
    <span class="ep-meta">${f.close:.2f} · {f.chg_pct:+.1f}%</span>
  </div>
  <div>{"".join(tag_html)}</div>
  <div class="ep-metrics">
    <strong>RVol</strong> {f.rvol:.2f} ·
    <strong>ATR</strong> {f.atr_pct:.1f}% ·
    <strong>3d</strong> {f.prior_3d_cum:+.1f}% ·
    <strong>52w-hi</strong> {f.dist_52w_hi:+.1f}%
  </div>
  <img src="{finviz_chart_url.format(ticker=f.ticker)}" alt="{f.ticker}" loading="lazy"/>
</div>""")
    return f"""
{EP_HTML_CSS}
<details open class="ep-section">
  <summary><strong>⚡ Episodic Pivots — Pullback Reversal ({len(fires_sorted)})</strong></summary>
  <p style="font-size: 13px; color: #6b7280;">Pradeep SB lane: drying volume + 3-day pullback + reversal up-day. 🔥 sector+peers · 🌊 peers · 📈 leader · ⚡ standalone.</p>
  <div class="ep-cards">{"".join(cards)}</div>
</details>
"""


# ── State update ──────────────────────────────────────────────────────────────

def update_ep_history(fires: list[EPFire], today: str, history: dict) -> dict:
    """Returns updated history dict (mutates in place + returns)."""
    for f in fires:
        rec = history.get(f.ticker, {})
        rec["last_fire_date"] = today
        rec["last_fire_tags"] = list(f.tags)
        rec["industry"] = f.industry
        rec["sector"] = f.sector
        rec["etf"] = f.etf
        rec["fire_count"] = int(rec.get("fire_count", 0)) + 1
        history[f.ticker] = rec
    return history
