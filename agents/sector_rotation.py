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


# Regime → action map (Phase 1 — informational only).
# Each tag in classify_regime()'s value space must have an entry here.
REGIME_ACTIONS = {
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


def classify_regime(rows: list, dispersion_pct: float, spy_at_20d_high: bool) -> str:
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
    regime = classify_regime(rows, disp_pct, spy_at_20d_high)

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
    snap = build_snapshot()
    if not snap:
        return 1
    fpath = write_snapshot(snap)
    append_to_history(snap)
    log.info("Wrote snapshot %s (regime=%s, dispersion p%.0f)",
             fpath, snap["regime"], snap["dispersion_percentile_180d"] * 100)

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
