"""
Sector Rotation Tracker — daily ETF RS snapshot + trend signals.

Pulls ~210 days of daily bars for the ETF universe (sectors + thematics +
benchmarks). For each ETF computes 1d/5d/20d returns, returns relative to
SPY, a 0-99 RS score (percentile rank of 20d-vs-SPY within the universe),
and a rank. Cross-checks today's rank against `sector_rotation_history.json`
to compute `rank_5d_ago`, `rank_delta_5d`, `decay_streak_days`, and the
20d-RS-high flag. Universe-level dispersion (stdev of 1d returns) is
percentile-ranked against the last 180 days for regime classification.

Trend signals (Slack):
  - Leadership change IN  : rank_delta_5d <= -10 AND rs_score >= 70
  - Leadership decay      : rank_delta_5d >= +10 AND rs_score <  50
  - Anticipation          : ret_vs_spy_20d at 20d high AND rs_score < 60
                            (must hold 2 consecutive days — confirmed via history)

Slack roll-up gated to Mon/Thu in `main()`. Snapshot + history persist daily.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import statistics
import sys
from typing import Optional

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR             = os.environ.get("DATA_DIR", "data")
ETF_MAP_FILE         = os.path.join(DATA_DIR, "sector_etf_map.json")
HISTORY_FILE         = os.path.join(DATA_DIR, "sector_rotation_history.json")
SLACK_WEBHOOK        = os.environ.get("SLACK_WEBHOOK_URL", "")
ALPACA_API_KEY       = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY    = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL      = "https://data.alpaca.markets"
HISTORY_RETENTION_DAYS = 180
# Below this many days of history, the dispersion percentile is meaningless
# (collapses to "today vs itself") so we tag the regime as `bootstrapping`
# and surface a neutral action block.
MIN_HISTORY_DAYS_FOR_REGIME = 20


# Regime → action map (Phase 1 — informational only).
# Each tag in classify_regime()'s value space must have an entry here.
REGIME_ACTIONS = {
    "bootstrapping": {
        "headline": "Regime bootstrapping — insufficient history",
        "sizing":   "Use market_state for sizing — ignore regime tag.",
        "entries":  "Trust the screener; sector signal not yet calibrated.",
        "held":     "Manage by existing rules.",
    },
    "correlation_phase": {
        "headline": "Beta tape — no sector edge",
        "sizing":   "Size down. Trade SPY/QQQ if anything.",
        "entries":  "No new sector entries.",
        "held":     "Hold, no adds.",
    },
    "early-rotation": {
        "headline": "Leadership forming",
        "sizing":   "Normal size.",
        "entries":  "Build watchlist in emerging RS leaders. Wait 5d confirm before chasing.",
        "held":     "Hold.",
    },
    "mid-rotation": {
        "headline": "Best entry tape",
        "sizing":   "Full size in GREEN/THRUST · half in CAUTION.",
        "entries":  "Press confirmed RS leaders.",
        "held":     "Add to leaders, hold others.",
    },
    "late-rotation": {
        "headline": "Leadership narrowing",
        "sizing":   "Reduce new-entry size 50%.",
        "entries":  "New entries only in fresh RS-rising leaders. Skip extended names.",
        "held":     "Trim names ≥+25% from entry. No adds to leaders.",
    },
    "blow-off-risk": {
        "headline": "Risk-off",
        "sizing":   "No new entries.",
        "entries":  "Skip all entries.",
        "held":     "Tighten stops · trim aggressively · cash is a position.",
    },
}


def regime_action(regime: str):
    """Returns the action dict for a regime tag, or None for unknown tags."""
    return REGIME_ACTIONS.get(regime)


# ------------------------------------------------------------------
# Universe loading
# ------------------------------------------------------------------
def load_universe() -> dict:
    """Returns the full ETF map (sectors + thematics + benchmarks)."""
    with open(ETF_MAP_FILE) as f:
        return json.load(f)


def universe_symbols(universe: dict) -> list:
    """Symbols to compute RS for (sectors + thematics; SPY excluded — it's the benchmark)."""
    return list(universe.get("sectors", {}).keys()) + list(universe.get("thematics", {}).keys())


# ------------------------------------------------------------------
# Bars fetch
# ------------------------------------------------------------------
def fetch_bars(symbols: list, days: int = 210) -> dict:
    """
    Multi-symbol daily bars from Alpaca. Returns {symbol: DataFrame[t,o,h,l,c,v]}.
    """
    if not ALPACA_API_KEY:
        log.error("ALPACA_API_KEY not set")
        return {}

    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).isoformat()
    headers = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    out: dict = {}
    page_token = None
    while True:
        params = {
            "symbols":   ",".join(symbols),
            "timeframe": "1Day",
            "start":     start,
            "limit":     10000,
            "adjustment": "split",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(f"{ALPACA_DATA_URL}/v2/stocks/bars",
                                headers=headers, params=params, timeout=30)
            if not resp.ok:
                log.error("Alpaca bars failed: %s %s", resp.status_code, resp.text[:200])
                return out
            payload = resp.json()
        except Exception as e:
            log.error("Alpaca bars exception: %s", e)
            return out

        bars_by_sym = payload.get("bars", {}) or {}
        for sym, bars in bars_by_sym.items():
            if not bars:
                continue
            df = pd.DataFrame(bars)
            df["t"] = pd.to_datetime(df["t"])
            if sym in out:
                out[sym] = pd.concat([out[sym], df], ignore_index=True)
            else:
                out[sym] = df

        page_token = payload.get("next_page_token")
        if not page_token:
            break

    for sym in list(out.keys()):
        out[sym] = out[sym].sort_values("t").drop_duplicates("t").reset_index(drop=True)
    return out


# ------------------------------------------------------------------
# Per-ETF return calcs
# ------------------------------------------------------------------
def _ret(closes: pd.Series, n: int) -> Optional[float]:
    if len(closes) <= n:
        return None
    a, b = closes.iloc[-1], closes.iloc[-1 - n]
    if not b:
        return None
    return float(a / b - 1)


def compute_returns(bars: dict, spy_closes: pd.Series) -> list:
    """For each ETF, compute 1d/5d/20d returns and ret-vs-SPY at 5d/20d."""
    spy_5  = _ret(spy_closes, 5)
    spy_20 = _ret(spy_closes, 20)
    rows: list = []
    for sym, df in bars.items():
        c = df["c"]
        r1   = _ret(c, 1)
        r5   = _ret(c, 5)
        r20  = _ret(c, 20)
        rvs5  = (r5  - spy_5)  if (r5 is not None and spy_5 is not None) else None
        rvs20 = (r20 - spy_20) if (r20 is not None and spy_20 is not None) else None
        # 20d-high check on ret_vs_spy_20d: rolling 20-day window of relative perf
        is_20d_high = False
        if len(c) >= 41 and rvs20 is not None:
            window: list = []
            spy_window = spy_closes.iloc[-21:]
            sym_window = c.iloc[-21:]
            for offset in range(20):
                # ret_vs_spy_20d evaluated `offset` days ago
                if len(c) > 20 + offset and len(spy_closes) > 20 + offset:
                    sym_r = c.iloc[-1 - offset] / c.iloc[-21 - offset] - 1
                    spy_r = spy_closes.iloc[-1 - offset] / spy_closes.iloc[-21 - offset] - 1
                    window.append(sym_r - spy_r)
            if window and rvs20 >= max(window):
                is_20d_high = True
        rows.append({
            "etf":             sym,
            "close":           float(c.iloc[-1]),
            "ret_1d":          r1,
            "ret_5d":          r5,
            "ret_20d":         r20,
            "ret_vs_spy_5d":   rvs5,
            "ret_vs_spy_20d":  rvs20,
            "is_20d_rs_high":  is_20d_high,
        })
    return rows


# ------------------------------------------------------------------
# RS score + rank
# ------------------------------------------------------------------
def percentile_rank(values: list, target: float) -> int:
    """0-99 percentile rank of `target` within `values` (lower than target / total)."""
    if not values:
        return 0
    pool = [v for v in values if v is not None]
    if not pool:
        return 0
    below = sum(1 for v in pool if v < target)
    return min(99, int(round(below / len(pool) * 99)))


def assign_rs_and_rank(rows: list) -> list:
    """Mutates rows in place with `rs_score` (0-99) and `rank` (1=best)."""
    pool = [r["ret_vs_spy_20d"] for r in rows if r["ret_vs_spy_20d"] is not None]
    for r in rows:
        rvs20 = r["ret_vs_spy_20d"]
        r["rs_score"] = percentile_rank(pool, rvs20) if rvs20 is not None else 0
    rows.sort(key=lambda r: r["rs_score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


# ------------------------------------------------------------------
# History (rank_delta_5d, decay streak, anticipation 2d-confirm)
# ------------------------------------------------------------------
def load_history() -> list:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f) or []
    except FileNotFoundError:
        return []


def save_history(history: list) -> None:
    cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    pruned = [row for row in history if row["date"] >= cutoff]
    with open(HISTORY_FILE, "w") as f:
        json.dump(pruned, f, indent=2)


def annotate_with_history(rows: list, history: list, today: str) -> list:
    """
    Adds `rank_5d_ago`, `rank_delta_5d`, `decay_streak_days`, `anticipation_confirmed`
    to each row using prior daily entries from `history`.
    """
    by_date: dict[str, dict[str, dict]] = {}
    for h in history:
        by_date.setdefault(h["date"], {})[h["etf"]] = h

    sorted_dates = sorted([d for d in by_date.keys() if d < today])
    five_back = sorted_dates[-5] if len(sorted_dates) >= 5 else (sorted_dates[0] if sorted_dates else None)

    for r in rows:
        etf = r["etf"]
        prior5 = by_date.get(five_back, {}).get(etf) if five_back else None
        r["rank_5d_ago"]    = prior5["rank"] if prior5 else None
        r["rank_delta_5d"]  = (r["rank"] - prior5["rank"]) if prior5 else 0

        # Decay streak: consecutive days where rank worsened (delta > 0) day over day,
        # walking backwards, while also rs_score < 50.
        streak = 0
        prev_rank: Optional[int] = r["rank"]
        for d in reversed(sorted_dates):
            entry = by_date.get(d, {}).get(etf)
            if not entry:
                break
            if prev_rank is not None and prev_rank > entry["rank"] and r["rs_score"] < 50:
                streak += 1
                prev_rank = entry["rank"]
            else:
                break
        r["decay_streak_days"] = streak

        # Anticipation 2d-confirm: today is a 20d-RS-high AND yesterday was too
        prior_day = sorted_dates[-1] if sorted_dates else None
        prior_entry = by_date.get(prior_day, {}).get(etf) if prior_day else None
        r["anticipation_confirmed"] = bool(
            r["is_20d_rs_high"]
            and r["rs_score"] < 60
            and prior_entry
            and prior_entry.get("is_20d_rs_high")
        )

    return rows


# ------------------------------------------------------------------
# Dispersion + regime
# ------------------------------------------------------------------
def universe_dispersion(rows: list) -> float:
    rets = [r["ret_1d"] for r in rows if r["ret_1d"] is not None]
    if len(rets) < 2:
        return 0.0
    return float(statistics.pstdev(rets))


def dispersion_percentile_180d(history: list, today_disp: float) -> float:
    """Percentile of today's dispersion within historical daily dispersions."""
    by_date: dict[str, list[float]] = {}
    for h in history:
        if h.get("ret_1d") is not None:
            by_date.setdefault(h["date"], []).append(h["ret_1d"])
    daily = [statistics.pstdev(v) for v in by_date.values() if len(v) >= 2]
    if not daily:
        return 0.5
    below = sum(1 for d in daily if d < today_disp)
    return round(below / len(daily), 2)


def history_days_count(history: list, today: str) -> int:
    """Number of distinct prior dates in history (excludes today)."""
    return len({h["date"] for h in history if h["date"] < today})


def classify_regime(rows: list, dispersion_pct: float, spy_at_20d_high: bool,
                    history_days: Optional[int] = None) -> str:
    if history_days is not None and history_days < MIN_HISTORY_DAYS_FOR_REGIME:
        return "bootstrapping"
    if dispersion_pct < 0.20:
        return "correlation_phase"
    top_themes = {r.get("theme") for r in rows[:5] if r.get("rs_score", 0) >= 70}
    if dispersion_pct < 0.50 and len(top_themes) <= 3:
        return "early-rotation"
    if dispersion_pct < 0.80:
        return "mid-rotation"
    # late-rotation by default at p80+
    if dispersion_pct >= 0.80 and spy_at_20d_high:
        return "blow-off-risk"
    return "late-rotation"


# ------------------------------------------------------------------
# Snapshot orchestration
# ------------------------------------------------------------------
def build_snapshot(today: Optional[str] = None) -> dict:
    today = today or datetime.date.today().isoformat()
    universe = load_universe()
    syms = universe_symbols(universe)
    benchmarks = list(universe.get("benchmarks", {}).keys())
    bars = fetch_bars(sorted(set(syms + benchmarks)))
    if "SPY" not in bars:
        log.error("SPY bars missing — cannot compute RS vs SPY")
        return {}

    spy_closes = bars["SPY"]["c"]
    spy_ret_1d = _ret(spy_closes, 1)
    spy_at_20d_high = bool(len(spy_closes) >= 21 and spy_closes.iloc[-1] >= spy_closes.iloc[-21:].max())

    etf_bars = {s: bars[s] for s in syms if s in bars}
    rows = compute_returns(etf_bars, spy_closes)
    rows = assign_rs_and_rank(rows)

    # Decorate with name/theme
    meta: dict[str, dict] = {}
    meta.update(universe.get("sectors", {}))
    meta.update(universe.get("thematics", {}))
    for r in rows:
        m = meta.get(r["etf"], {})
        r["name"]  = m.get("name", r["etf"])
        r["theme"] = m.get("theme", "")

    history = load_history()
    rows = annotate_with_history(rows, history, today)

    disp = universe_dispersion(rows)
    disp_pct = dispersion_percentile_180d(history, disp)
    hist_days = history_days_count(history, today)
    regime = classify_regime(rows, disp_pct, spy_at_20d_high, history_days=hist_days)

    snapshot = {
        "date":                       today,
        "universe_size":              len(rows),
        "spy_ret_1d":                 spy_ret_1d,
        "dispersion_1d_stdev":        round(disp, 5),
        "dispersion_percentile_180d": disp_pct,
        "regime":                     regime,
        "etfs":                       rows,
    }
    return snapshot


# ------------------------------------------------------------------
# ETF setup metrics + buckets (for etf_rotation.html dashboard)
# ------------------------------------------------------------------
def _sma(vs: list, n: int) -> Optional[float]:
    if len(vs) < n:
        return None
    return sum(vs[-n:]) / n


def _ema(vs: list, n: int) -> Optional[float]:
    if len(vs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vs[:n]) / n
    for v in vs[n:]:
        e = v * k + e * (1 - k)
    return e


def _atr14(rows: list[dict]) -> Optional[float]:
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h = rows[i]["h"]; l = rows[i]["l"]; pc = rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    return atr


def compute_etf_setup(df: pd.DataFrame) -> Optional[dict]:
    """Compute setup metrics for a single ETF given Alpaca daily bars DataFrame."""
    if df is None or len(df) < 200:
        return None
    rows = df.to_dict("records")
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    vols = [r["v"] for r in rows]
    last = closes[-1]
    s50 = _sma(closes, 50)
    s200 = _sma(closes, 200)
    e8 = _ema(closes, 8)
    e21 = _ema(closes, 21)
    atr = _atr14(rows)
    if not all((s50, s200, e8, e21, atr)) or last <= 0:
        return None
    atr_pct = atr / last * 100
    mult50 = (last - s50) * last / (s50 * atr)
    pct50 = (last - s50) / s50 * 100
    hi252 = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    dist52 = (last - hi252) / hi252 * 100
    s50_10ago = _sma(closes[:-10], 50)
    s200_10ago = _sma(closes[:-10], 200)
    s50_rising = s50_10ago is not None and s50 > s50_10ago
    s200_rising = s200_10ago is not None and s200 > s200_10ago
    range20 = (max(highs[-20:]) - min(lows[-20:])) / last * 100
    ret20 = (last - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else None
    ema21d = (last - e21) / last * 100
    av20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
    rvol = vols[-1] / av20 if av20 else None
    return {
        "last":        round(last, 2),
        "atr_pct":     round(atr_pct, 2),
        "mult50":      round(mult50, 2),
        "pct50":       round(pct50, 1),
        "dist52":      round(dist52, 1),
        "s50_rising":  bool(s50_rising),
        "s200_rising": bool(s200_rising),
        "range20":     round(range20, 1),
        "ret20":       round(ret20, 1) if ret20 is not None else None,
        "ema21d":      round(ema21d, 2),
        "rvol":        round(rvol, 2) if rvol else None,
    }


def assign_bucket(m: dict) -> str:
    """Bucket an ETF by setup state. Order matters — BROKEN/EXTENDED checked first."""
    if m["mult50"] < -1 or not m["s200_rising"]:
        return "BROKEN"
    if m["mult50"] > 5 or m["dist52"] > -2:
        return "EXTENDED"
    if (m["s50_rising"] and m["s200_rising"] and m["mult50"] < 3
            and m["range20"] < 12 and -10 < m["dist52"] < -2):
        return "BASE"
    if (m["s50_rising"] and m["s200_rising"] and m["mult50"] < 4
            and -10 <= m["dist52"] <= 0):
        return "PRE-BREAKOUT"
    return "NEUTRAL"


def compute_etf_setups(bars: dict, universe: dict) -> list[dict]:
    """Compute setup metrics + bucket for each ETF in the universe.
    Returns list of {ticker, name, theme, kind (sector|thematic), bucket, metrics}.
    """
    meta_sectors = universe.get("sectors", {})
    meta_thematics = universe.get("thematics", {})
    out: list[dict] = []
    for tk, df in bars.items():
        if tk in meta_sectors:
            kind = "sector"; meta = meta_sectors[tk]
        elif tk in meta_thematics:
            kind = "thematic"; meta = meta_thematics[tk]
        else:
            continue
        m = compute_etf_setup(df)
        if m is None:
            out.append({"ticker": tk, "name": meta.get("name", tk),
                        "theme": meta.get("theme", ""), "kind": kind,
                        "bucket": "NEUTRAL", "metrics": None, "note": "insufficient bars"})
            continue
        out.append({"ticker": tk, "name": meta.get("name", tk),
                    "theme": meta.get("theme", ""), "kind": kind,
                    "bucket": assign_bucket(m), "metrics": m})
    return out


def _score(m: dict) -> float:
    """Ranking score for BASE/PRE-BREAKOUT: lower mult50 + tighter range = higher score."""
    return (10 - m["mult50"]) + (15 - m["range20"]) + (2 if m["s50_rising"] else 0)


def render_etf_rotation_html(snapshot: dict, etf_setups: list[dict]) -> str:
    """Render etf_rotation.html — one-page actionable ETF setup state."""
    regime = snapshot.get("regime", "unknown")
    action = regime_action(regime) or {}
    today = snapshot.get("date", "")

    by_bucket = {"BASE": [], "PRE-BREAKOUT": [], "EXTENDED": [], "BROKEN": [], "NEUTRAL": []}
    for e in etf_setups:
        by_bucket[e.get("bucket", "NEUTRAL")].append(e)
    for b in ("BASE", "PRE-BREAKOUT"):
        by_bucket[b].sort(key=lambda e: -_score(e["metrics"]) if e["metrics"] else 0)
    # EXTENDED sorted by mult50 desc (most extended first), BROKEN by dist52 asc
    by_bucket["EXTENDED"].sort(key=lambda e: -(e["metrics"]["mult50"] if e["metrics"] else 0))
    by_bucket["BROKEN"].sort(key=lambda e: (e["metrics"]["dist52"] if e["metrics"] else 0))

    def _fv(tk: str) -> str:
        return f'https://finviz.com/quote.ashx?t={tk}'

    def _rs_badge(e: dict) -> str:
        rs = e.get("rs_score")
        rk = e.get("rs_rank")
        d5 = e.get("rank_delta_5d")
        if rs is None and rk is None:
            return ""
        bg = "#16a34a" if (rs or 0) >= 70 else ("#2563eb" if (rs or 0) >= 50 else ("#f59e0b" if (rs or 0) >= 30 else "#dc2626"))
        rk_txt = f" #{rk}" if rk else ""
        d5_txt = ""
        if d5 is not None:
            sign = "+" if d5 > 0 else ""
            # Negative Δrank = rank improving = green; positive = rank worsening = red
            d5_color = "#16a34a" if d5 < 0 else ("#dc2626" if d5 > 0 else "#6b7280")
            d5_txt = f' <span style="color:{d5_color};font-weight:700;margin-left:6px">Δrank {sign}{d5}</span>'
        return (
            f'<span class="rs-chip" style="background:{bg};color:#fff;font-size:11px;'
            f'font-weight:700;padding:2px 8px;border-radius:4px;margin-right:6px">'
            f'RS {rs if rs is not None else "—"}{rk_txt}</span>{d5_txt}'
        )

    def _full_row(e: dict) -> str:
        """Shared schema for BOTH the RS leaderboard and the full metrics table."""
        m = e.get("metrics")
        tk_html = f'<a href="{_fv(e["ticker"])}" target="_blank"><b>{e["ticker"]}</b></a>'
        rs = e.get("rs_score")
        rk = e.get("rs_rank")
        d5 = e.get("rank_delta_5d")
        rs_cell = f'{rs}' if rs is not None else "—"
        rk_cell = f'#{rk}' if rk else "—"
        d5_cell = "—"
        if d5 is not None:
            sign = "+" if d5 > 0 else ""
            d5_color = "#16a34a" if d5 < 0 else ("#dc2626" if d5 > 0 else "#6b7280")
            d5_cell = f'<span style="color:{d5_color};font-weight:600">{sign}{d5}</span>'
        # Momentum-sweet-spot row tint: RS 60-80 = "just being noticed" band (Qullamaggie zone)
        row_cls = ' class="row-momentum"' if (rs is not None and 60 <= rs <= 80) else ''
        if m is None:
            return (
                f'<tr{row_cls}><td>{rk_cell}</td><td>{tk_html}</td><td>{e["name"]}</td>'
                f'<td><b>{rs_cell}</b></td><td>{d5_cell}</td>'
                f'<td colspan="9" style="color:#9ca3af">{e.get("note","—")}</td>'
                f'<td><span class="bucket-tag b-{e["bucket"]}">{e["bucket"]}</span></td></tr>'
            )
        return (
            f'<tr{row_cls}><td>{rk_cell}</td><td>{tk_html}</td><td>{e["name"]}</td>'
            f'<td><b>{rs_cell}</b></td><td>{d5_cell}</td>'
            f'<td>{m["atr_pct"]}%</td><td>{m["mult50"]}</td><td>{m["dist52"]}%</td>'
            f'<td>{m["range20"]}%</td><td>{m["ret20"]}%</td><td>{m["ema21d"]}%</td>'
            f'<td>{m["rvol"]}x</td>'
            f'<td>{"✓" if m["s50_rising"] else "—"}/{"✓" if m["s200_rising"] else "—"}</td>'
            f'<td><span class="bucket-tag b-{e["bucket"]}">{e["bucket"]}</span></td></tr>'
        )

    def _card(e: dict, color: str) -> str:
        m = e["metrics"] or {}
        return (
            f'<div class="etf-card" style="border-left:4px solid {color}">'
            f'<div class="etf-head">'
            f'<a class="etf-tk" href="{_fv(e["ticker"])}" target="_blank">{e["ticker"]}</a>'
            f'<span class="etf-name">{e["name"]}</span>'
            f'<span class="etf-theme">{e["theme"]}</span></div>'
            f'<div class="etf-rs">{_rs_badge(e)}</div>'
            f'<div class="etf-metrics">'
            f'mult50 <b>{m.get("mult50","—")}</b> · '
            f'range20 <b>{m.get("range20","—")}%</b> · '
            f'dist52 <b>{m.get("dist52","—")}%</b> · '
            f'ret20 <b>{m.get("ret20","—")}%</b> · '
            f'RVol <b>{m.get("rvol","—")}x</b>'
            f'</div></div>'
        )

    base_cards = "".join(_card(e, "#16a34a") for e in by_bucket["BASE"])
    pre_cards = "".join(_card(e, "#2563eb") for e in by_bucket["PRE-BREAKOUT"])
    ext_cards = "".join(_card(e, "#f59e0b") for e in by_bucket["EXTENDED"])
    broken_cards = "".join(_card(e, "#dc2626") for e in by_bucket["BROKEN"])
    neutral_cards = "".join(_card(e, "#9ca3af") for e in by_bucket["NEUTRAL"])

    # Global RS leaderboard — top 10 by rs_rank, same schema as full table
    _ranked = [e for e in etf_setups if e.get("rs_rank")]
    _ranked.sort(key=lambda e: e.get("rs_rank") or 99)

    SHARED_HEADER = (
        '<thead><tr>'
        '<th title="Universe rank — #1 is strongest">Rank</th>'
        '<th>Ticker</th><th>Name</th>'
        '<th title="Relative Strength 0-99 vs SPY (20d-vs-SPY percentile)">RS</th>'
        '<th title="Rank change vs 5 trading days ago — negative is BETTER (rotating in)">Δrank ↓=better</th>'
        '<th>ATR%</th><th>mult50</th><th>dist52</th><th>range20</th>'
        '<th>ret20</th><th>EMA21 dist</th><th>RVol</th>'
        '<th>50/200 ↑</th><th>Bucket</th>'
        '</tr></thead>'
    )
    leaders_html = "".join(_full_row(e) for e in _ranked[:10])
    rs_leaderboard_html = ""
    if _ranked:
        rs_leaderboard_html = (
            '<h2>🏆 RS Leaderboard — top 10 by relative strength</h2>'
            '<div class="subtitle">Same columns as the full table below. '
            '<b>Δrank ↓=better</b>: negative = rank improving (rotating in, green) · positive = rank deteriorating (rotating out, red). '
            'Amber row tint = RS 60–80 momentum sweet spot (just being noticed).</div>'
            f'<table class="sortable">{SHARED_HEADER}<tbody>{leaders_html}</tbody></table>'
        )

    # SMH ↔ IGV rotation banner — semi/software is the most-asked pair
    rotation_banner = ""
    _by_etf = {e["ticker"]: e for e in etf_setups if e.get("rs_rank")}
    smh = _by_etf.get("SMH"); igv = _by_etf.get("IGV")
    if smh and igv:
        smh_d5 = smh.get("rank_delta_5d") or 0
        igv_d5 = igv.get("rank_delta_5d") or 0
        # SMH rank worsening + IGV rank improving = possible rotation
        if smh_d5 >= 3 and igv_d5 <= -3:
            rotation_banner = (
                '<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;'
                'padding:12px 16px;margin:12px 0;font-size:14px;color:#92400e">'
                f'🔄 <b>Semi/Software RS shift</b>: SMH Δrank +{smh_d5} (weakening) · '
                f'IGV Δrank {igv_d5} (strengthening) — possible rotation. '
                'NOT proof of money flow; just relative strength.'
                '</div>'
            )
        elif igv_d5 >= 3 and smh_d5 <= -3:
            rotation_banner = (
                '<div style="background:#dbeafe;border:1px solid #2563eb;border-radius:8px;'
                'padding:12px 16px;margin:12px 0;font-size:14px;color:#1e40af">'
                f'🔄 <b>Software/Semi RS shift</b>: IGV Δrank +{igv_d5} (weakening) · '
                f'SMH Δrank {smh_d5} (strengthening) — possible rotation back to semis.'
                '</div>'
            )

    # Full table: bucket-grouped by default (most actionable order). Columns are
    # click-sortable in the browser via the tiny JS at the bottom of the page.
    table_order = ["BASE", "PRE-BREAKOUT", "EXTENDED", "BROKEN", "NEUTRAL"]
    ordered_setups = [e for b in table_order for e in by_bucket.get(b, [])]
    full_table_rows = "".join(_full_row(e) for e in ordered_setups)

    headline = action.get("headline", "")
    sizing = action.get("sizing", "")
    entries = action.get("entries", "")
    held = action.get("held", "")

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>ETF Rotation — {today}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#fff;color:#111827;padding:24px;max-width:1280px;margin:0 auto}}
h1{{color:#111827;margin-bottom:4px}}
h2{{color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:6px;margin-top:28px}}
.subtitle{{color:#6b7280;font-size:14px;margin-bottom:24px}}
.regime{{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:24px}}
.regime-tag{{display:inline-block;background:#2563eb;color:#fff;padding:4px 12px;border-radius:4px;font-weight:bold;margin-bottom:8px;font-size:13px}}
.regime-head{{font-weight:bold;color:#111827;margin-bottom:6px}}
.action-row{{display:flex;gap:24px;flex-wrap:wrap;font-size:13px;color:#374151}}
.action-row b{{color:#111827}}
.etf-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin:12px 0}}
.etf-card{{background:#fff;border:1px solid #e5e7eb;border-radius:6px;padding:12px}}
.etf-head{{display:flex;gap:8px;align-items:baseline;margin-bottom:6px}}
.etf-tk{{font-weight:bold;font-size:16px;color:#2563eb;text-decoration:none}}
.etf-tk:hover{{text-decoration:underline}}
.etf-name{{color:#374151;font-size:13px}}
.etf-theme{{margin-left:auto;color:#9ca3af;font-size:11px}}
.etf-metrics{{font-size:12px;color:#4b5563}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin-top:12px}}
th,td{{border:1px solid #e5e7eb;padding:5px 8px;text-align:left}}
th{{background:#f3f4f6;color:#111827;position:sticky;top:0}}
.bucket-tag{{padding:2px 6px;border-radius:3px;font-size:10px;font-weight:bold}}
.b-BASE{{background:#d1fae5;color:#065f46}}
.b-PRE-BREAKOUT{{background:#dbeafe;color:#1e40af}}
.b-EXTENDED{{background:#fef3c7;color:#92400e}}
.b-BROKEN{{background:#fee2e2;color:#991b1b}}
.b-NEUTRAL{{background:#f3f4f6;color:#4b5563}}
.rs-board{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px}}
.rs-col-head{{font-weight:bold;color:#374151;margin-bottom:4px;font-size:13px}}
.rs-table{{width:100%;font-size:12px}}
.rs-table th{{background:#f3f4f6;color:#111827;text-align:left;padding:4px 8px;border:1px solid #e5e7eb}}
.rs-table td{{padding:4px 8px;border:1px solid #e5e7eb}}
.etf-rs{{margin:4px 0 6px 0}}
.row-momentum td{{background:#fffbeb}}
table.sortable th:hover{{background:#e5e7eb}}
table.sortable th[data-sort="asc"]::after{{content:" ▲";color:#2563eb;font-size:10px}}
table.sortable th[data-sort="desc"]::after{{content:" ▼";color:#2563eb;font-size:10px}}
@media (max-width:760px){{.rs-board{{grid-template-columns:1fr}}}}
details{{margin-top:12px}}
summary{{cursor:pointer;font-weight:bold;color:#374151;padding:4px 0}}
a{{color:#2563eb}}
</style></head><body>
<h1>📊 ETF Rotation Dashboard</h1>
<div class="subtitle">{today} · {len(etf_setups)} ETFs · regime auto-classified daily from <a href="https://github.com/AnanthSrinivasan/finviz-screener-agent/blob/main/agents/sector_rotation.py">sector_rotation.py</a></div>

<div class="regime">
<div class="regime-tag">{regime}</div>
<div class="regime-head">{headline}</div>
<div class="action-row">
<div><b>Sizing:</b> {sizing}</div>
<div><b>Entries:</b> {entries}</div>
<div><b>Held:</b> {held}</div>
</div>
</div>

{rotation_banner}
{rs_leaderboard_html}

<div class="subtitle" style="margin-top:18px">
Bucket counts:
<span class="bucket-tag b-BASE">BASE {len(by_bucket['BASE'])}</span>
<span class="bucket-tag b-PRE-BREAKOUT">PRE-BREAKOUT {len(by_bucket['PRE-BREAKOUT'])}</span>
<span class="bucket-tag b-EXTENDED">EXTENDED {len(by_bucket['EXTENDED'])}</span>
<span class="bucket-tag b-BROKEN">BROKEN {len(by_bucket['BROKEN'])}</span>
<span class="bucket-tag b-NEUTRAL">NEUTRAL {len(by_bucket['NEUTRAL'])}</span>
</div>

<h2>📋 Full metrics — all {len(etf_setups)} ETFs</h2>
<div class="subtitle">Click any column header to sort. Default: grouped by bucket (BASE → PRE-BREAKOUT → EXTENDED → BROKEN → NEUTRAL).</div>
<table id="full-table" class="sortable">{SHARED_HEADER}
<tbody>{full_table_rows}</tbody>
</table>

<script>
(function() {{
  document.querySelectorAll('table.sortable').forEach(function(table) {{
    var headers = table.querySelectorAll('thead th');
    headers.forEach(function(th, idx) {{
      th.style.cursor = 'pointer';
      th.style.userSelect = 'none';
      var sortAsc = true;
      th.addEventListener('click', function() {{
        var tbody = table.tBodies[0];
        var rows = Array.from(tbody.rows);
        // Strip non-numeric chars (#, %, x, +) for numeric compare; fall back to string
        function key(row) {{
          var c = row.cells[idx];
          if (!c) return '';
          var t = (c.innerText || c.textContent || '').trim();
          var n = parseFloat(t.replace(/[#%x,+]/g, ''));
          return isNaN(n) ? t.toLowerCase() : n;
        }}
        rows.sort(function(a, b) {{
          var ka = key(a), kb = key(b);
          if (ka < kb) return sortAsc ? -1 : 1;
          if (ka > kb) return sortAsc ? 1 : -1;
          return 0;
        }});
        rows.forEach(function(r) {{ tbody.appendChild(r); }});
        headers.forEach(function(h) {{ h.removeAttribute('data-sort'); }});
        th.setAttribute('data-sort', sortAsc ? 'asc' : 'desc');
        sortAsc = !sortAsc;
      }});
    }});
  }});
}})();
</script>
</body></html>"""


def write_etf_rotation_html(snapshot: dict, etf_setups: list[dict]) -> str:
    fpath = os.path.join(DATA_DIR, "etf_rotation.html")
    html = render_etf_rotation_html(snapshot, etf_setups)
    with open(fpath, "w") as f:
        f.write(html)
    return fpath


def write_etf_rotation_json(snapshot: dict, etf_setups: list[dict]) -> str:
    fpath = os.path.join(DATA_DIR, "etf_rotation.json")
    payload = {
        "date":   snapshot.get("date"),
        "regime": snapshot.get("regime"),
        "etfs":   etf_setups,
    }
    with open(fpath, "w") as f:
        json.dump(payload, f, indent=2)
    return fpath


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------
def write_snapshot(snapshot: dict) -> str:
    fpath = os.path.join(DATA_DIR, f"sector_rotation_{snapshot['date']}.json")
    with open(fpath, "w") as f:
        json.dump(snapshot, f, indent=2)
    return fpath


def append_to_history(snapshot: dict) -> None:
    history = load_history()
    today = snapshot["date"]
    # idempotent — replace any existing rows for today
    history = [h for h in history if h["date"] != today]
    for r in snapshot["etfs"]:
        history.append({
            "date":            today,
            "etf":             r["etf"],
            "rs_score":        r["rs_score"],
            "rank":            r["rank"],
            "is_20d_rs_high":  r.get("is_20d_rs_high", False),
            "ret_1d":          r.get("ret_1d"),
        })
    save_history(history)


# ------------------------------------------------------------------
# Trend signal extraction (for Slack)
# ------------------------------------------------------------------
def signals(snapshot: dict) -> dict:
    rows = snapshot["etfs"]
    in_  = [r for r in rows if (r.get("rank_delta_5d") or 0) <= -10 and r["rs_score"] >= 70]
    out  = [r for r in rows if (r.get("rank_delta_5d") or 0) >= 10  and r["rs_score"] <  50]
    anti = [r for r in rows if r.get("anticipation_confirmed")]
    decay = [r for r in rows if (r.get("decay_streak_days") or 0) >= 2]
    in_.sort(key=lambda r: r["rs_score"], reverse=True)
    out.sort(key=lambda r: r["rs_score"])
    return {"in": in_[:8], "out": out[:8], "anticipation": anti[:5], "decay": decay}


# ------------------------------------------------------------------
# Slack roll-up
# ------------------------------------------------------------------
def format_slack(snapshot: dict, sig: dict) -> str:
    date = snapshot["date"]
    regime = snapshot["regime"]
    disp_pct = int(round(snapshot["dispersion_percentile_180d"] * 100))
    lines = [
        f"*Sector Rotation — {date}*",
        f"Phase: `{regime}` · Dispersion p{disp_pct}",
    ]
    action = regime_action(regime)
    if action:
        lines.append(f"*{action['headline']}*")
        lines.append(f"  • Sizing:  {action['sizing']}")
        lines.append(f"  • Entries: {action['entries']}")
        lines.append(f"  • Held:    {action['held']}")
    lines.append("")

    def fmt_row(r, show_decay=False):
        rs   = r["rs_score"]
        rank = r["rank"]
        delta = r.get("rank_delta_5d") or 0
        sign = f"{delta:+d}" if delta else "±0"
        decay = f" · {r.get('decay_streak_days', 0)}d decay" if show_decay and (r.get("decay_streak_days") or 0) else ""
        return f"  • `{r['etf']:<5}` {r['name']:<22} rank {rank:>2} ({sign})  RS {rs}{decay}"

    if sig["in"]:
        lines.append("*IN (5d RS rising)*")
        for r in sig["in"]:
            lines.append(fmt_row(r))
        lines.append("")
    if sig["out"]:
        lines.append("*OUT (5d RS falling)*")
        for r in sig["out"]:
            lines.append(fmt_row(r, show_decay=True))
        lines.append("")
    if sig["anticipation"]:
        lines.append("*Anticipation (confirmed 2d)*")
        for r in sig["anticipation"]:
            lines.append(f"  • `{r['etf']:<5}` {r['name']:<22} RS {r['rs_score']} (20d-RS high)")
        lines.append("")

    if not (sig["in"] or sig["out"] or sig["anticipation"]):
        lines.append("_No leadership changes today — universe in equilibrium._")
    return "\n".join(lines)


def post_slack(text: str) -> None:
    if not SLACK_WEBHOOK:
        log.info("SLACK_WEBHOOK_URL not set — skipping post")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
        if not resp.ok:
            log.warning("Slack post failed: %s %s", resp.status_code, resp.text[:120])
    except Exception as e:
        log.warning("Slack post exception: %s", e)


# ------------------------------------------------------------------
# Backfill
# ------------------------------------------------------------------
def backfill(days: int = 60) -> None:
    """
    One-time historical backfill — replays the snapshot calc for each prior trading day
    using cached bars. Writes per-day rows to history (rs_score + rank only).
    """
    universe = load_universe()
    syms = universe_symbols(universe)
    benchmarks = list(universe.get("benchmarks", {}).keys())
    bars = fetch_bars(sorted(set(syms + benchmarks)), days=days + 60)
    if "SPY" not in bars:
        log.error("SPY missing — cannot backfill")
        return

    spy = bars["SPY"]
    history: list = []
    # iterate trailing `days` daily indices, oldest → newest
    for i in range(-days, 0):
        try:
            spy_slice = spy["c"].iloc[: i] if i != -1 else spy["c"]
        except IndexError:
            continue
        if len(spy_slice) < 22:
            continue
        date_str = spy["t"].iloc[i if i != -1 else len(spy) - 1].date().isoformat()
        rows = []
        for sym, df in bars.items():
            if sym not in syms:
                continue
            try:
                slc = df["c"].iloc[: i] if i != -1 else df["c"]
            except IndexError:
                continue
            if len(slc) < 22:
                continue
            r20 = _ret(slc, 20)
            spy20 = _ret(spy_slice, 20)
            r1 = _ret(slc, 1)
            if r20 is None or spy20 is None:
                continue
            rows.append({"etf": sym, "ret_vs_spy_20d": r20 - spy20, "ret_1d": r1})
        if not rows:
            continue
        pool = [r["ret_vs_spy_20d"] for r in rows]
        for r in rows:
            r["rs_score"] = percentile_rank(pool, r["ret_vs_spy_20d"])
        rows.sort(key=lambda r: r["rs_score"], reverse=True)
        for j, r in enumerate(rows, start=1):
            r["rank"] = j
        for r in rows:
            history.append({
                "date":            date_str,
                "etf":             r["etf"],
                "rs_score":        r["rs_score"],
                "rank":            r["rank"],
                "is_20d_rs_high":  False,
                "ret_1d":          r["ret_1d"],
            })

    existing = load_history()
    seen = {(h["date"], h["etf"]) for h in existing}
    for h in history:
        if (h["date"], h["etf"]) not in seen:
            existing.append(h)
    save_history(existing)
    log.info("Backfill wrote %d rows into history", len(history))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main() -> int:
    if os.environ.get("BACKFILL", "false").lower() == "true" or "--backfill" in sys.argv:
        days = int(os.environ.get("BACKFILL_DAYS", "60"))
        log.info("Running history backfill for %d days", days)
        backfill(days=days)
    snap = build_snapshot()
    if not snap:
        return 1
    fpath = write_snapshot(snap)
    append_to_history(snap)
    log.info("Wrote snapshot %s (regime=%s, dispersion p%.0f)",
             fpath, snap["regime"], snap["dispersion_percentile_180d"] * 100)

    # ETF setup dashboard — re-uses same Alpaca bars universe for setup metrics.
    try:
        universe = load_universe()
        syms = universe_symbols(universe)
        # Re-fetch with enough history for SMA200 (>= 280 calendar days)
        etf_bars = fetch_bars(sorted(set(syms)), days=280)
        etf_setups = compute_etf_setups(etf_bars, universe)
        # Merge per-ETF RS rank + rs_score from the snapshot so dashboard surfaces them.
        _rs_by_etf = {e.get("etf"): e for e in snap.get("etfs", []) if e.get("etf")}
        for s in etf_setups:
            r = _rs_by_etf.get(s["ticker"])
            if r is None:
                continue
            s["rs_score"]      = r.get("rs_score")
            s["rs_rank"]       = r.get("rank")
            s["rank_delta_5d"] = r.get("rank_delta_5d")
            s["is_20d_rs_high"] = r.get("is_20d_rs_high")
            s["decay_streak_days"] = r.get("decay_streak_days")
        json_path = write_etf_rotation_json(snap, etf_setups)
        html_path = write_etf_rotation_html(snap, etf_setups)
        bucket_counts: dict[str, int] = {}
        for e in etf_setups:
            bucket_counts[e["bucket"]] = bucket_counts.get(e["bucket"], 0) + 1
        log.info("ETF rotation: wrote %s and %s — buckets %s",
                 json_path, html_path, bucket_counts)
    except Exception as e:
        log.error("ETF rotation dashboard failed (non-fatal): %s", e)

    weekday = datetime.date.fromisoformat(snap["date"]).weekday()  # Mon=0, Thu=3
    force_slack = os.environ.get("FORCE_SLACK", "false").lower() == "true"
    if weekday in (0, 3) or force_slack:
        sig = signals(snap)
        text = format_slack(snap, sig)
        post_slack(text)
        log.info("Slack posted (%d in / %d out / %d anti / %d decay)",
                 len(sig["in"]), len(sig["out"]), len(sig["anticipation"]), len(sig["decay"]))
    else:
        log.info("Skipping Slack (not Mon/Thu)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
