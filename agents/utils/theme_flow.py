"""
Theme flow engine — Money Flow Dashboard (spec docs/specs/money-flow-dashboard.md).

Hand-curated theme baskets (data/theme_map.json) become synthetic equal-weight
base-100 indices; themes are percentile-ranked in ONE combined pool with the
45-ETF rotation universe so a theme RS is directly comparable to an ETF RS.
A stock-level rollup (`flow_score`) surfaces the names sitting inside the
hottest themes, and a one-sentence Money Line names every leading group.

Everything here is pure computation — no network, no pandas requirement.
Callers (agents/sector_rotation.py) fetch bars and pass plain dicts in.
All theme steps are non-fatal by design: a missing/invalid theme_map.json
makes `load_theme_map` return None and the caller falls through to today's
behavior exactly.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

MIN_VALID_CONSTITUENTS = 2      # min constituent daily returns for an index day to move
SPARK_SESSIONS = 60             # sparkline window (sessions)
DIVERGENCE_MIN = 5              # |theme RS − sibling ETF RS| to render the chip
FLOW_LEADERBOARD_SIZE = 15
THEME_HISTORY_RETENTION_DAYS = 180

# Money Line thresholds (spec §4.5 — reuses signals() OUT thresholds)
MONEY_IN_RS_MIN = 70
MONEY_IN_TOP_RANKS = 5
MONEY_IN_CLIMB_5D = -5
MONEY_OUT_DELTA_5D = 10
MONEY_OUT_RS_MAX = 50


# ------------------------------------------------------------------
# Theme map loading / validation
# ------------------------------------------------------------------
def load_theme_map(path: str) -> Optional[dict]:
    """
    Load + validate data/theme_map.json. Returns None on ANY problem
    (missing file, bad JSON, malformed schema) — callers treat None as
    "themes disabled" and behave exactly as before the feature existed.
    """
    try:
        with open(path) as f:
            tm = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(tm, dict):
        return None
    themes = tm.get("themes")
    ecosystems = tm.get("ecosystems")
    if not isinstance(themes, dict) or not themes or not isinstance(ecosystems, dict):
        return None
    for tid, t in themes.items():
        if not isinstance(t, dict) or not t.get("name"):
            return None
        tks = t.get("tickers")
        if not isinstance(tks, list) or len(tks) < MIN_VALID_CONSTITUENTS:
            return None
        if not all(isinstance(x, str) and x for x in tks):
            return None
    return tm


def theme_ticker_union(theme_map: dict) -> list:
    """Sorted union of all theme constituent tickers."""
    out: set = set()
    for t in theme_map.get("themes", {}).values():
        out.update(t.get("tickers", []))
    return sorted(out)


# ------------------------------------------------------------------
# Synthetic equal-weight base-100 theme index (spec §4.1)
# ------------------------------------------------------------------
def synth_theme_index(closes_by_ticker: dict,
                      min_valid: int = MIN_VALID_CONSTITUENTS) -> Optional[dict]:
    """
    Build the synthetic index for one theme.

    closes_by_ticker: {ticker: {date_iso: close}}.
    Daily theme return = equal-weight mean of constituent daily returns over
    consecutive index dates. A ticker missing either close that day (halted,
    not yet listed) is excluded for that day. A day with fewer than
    `min_valid` constituent returns carries the prior level (return 0).
    Base-100 starts at the earliest date where >= min_valid constituents
    have a close.

    Returns {"base_date", "dates", "levels"} or None when unbuildable.
    """
    series = {tk: m for tk, m in closes_by_ticker.items() if m}
    all_dates = sorted({d for m in series.values() for d in m})
    if len(all_dates) < 2:
        return None
    base_i = None
    for i, d in enumerate(all_dates):
        if sum(1 for m in series.values() if d in m) >= min_valid:
            base_i = i
            break
    if base_i is None or base_i >= len(all_dates) - 1:
        return None

    dates = [all_dates[base_i]]
    levels = [100.0]
    for i in range(base_i + 1, len(all_dates)):
        d_prev, d = all_dates[i - 1], all_dates[i]
        rets = []
        for m in series.values():
            a, b = m.get(d), m.get(d_prev)
            if a is not None and b:
                rets.append(a / b - 1)
        r = (sum(rets) / len(rets)) if len(rets) >= min_valid else 0.0
        dates.append(d)
        levels.append(levels[-1] * (1 + r))
    return {"base_date": dates[0], "dates": dates, "levels": levels}


def index_ret(levels: list, n: int) -> Optional[float]:
    """n-session return off an index level series (mirror of sector_rotation._ret)."""
    if len(levels) <= n:
        return None
    b = levels[-1 - n]
    if not b:
        return None
    return float(levels[-1] / b - 1)


# ------------------------------------------------------------------
# Theme rows (per-theme metrics)
# ------------------------------------------------------------------
def compute_theme_rows(theme_map: dict, closes_by_ticker: dict,
                       spy_ret_20d: Optional[float]) -> list:
    """
    One row per buildable theme:
      {theme_id, name, ecosystem, ecosystem_name, sibling_etf, tickers,
       ret_1d, ret_20d, ret_vs_spy_20d, index: {base_date, level, spark}}
    Themes whose constituents lack bars are skipped (logged) — non-fatal.
    """
    eco_meta = theme_map.get("ecosystems", {})
    rows: list = []
    for tid, t in theme_map.get("themes", {}).items():
        member = {tk: closes_by_ticker.get(tk) or {} for tk in t.get("tickers", [])}
        member = {tk: m for tk, m in member.items() if m}
        idx = synth_theme_index(member) if len(member) >= MIN_VALID_CONSTITUENTS else None
        if idx is None:
            log.warning("theme %s: cannot build index (%d constituents with bars)",
                        tid, len(member))
            continue
        levels = idx["levels"]
        r20 = index_ret(levels, 20)
        rvs20 = (r20 - spy_ret_20d) if (r20 is not None and spy_ret_20d is not None) else None
        tail = levels[-SPARK_SESSIONS:]
        b0 = tail[0] or 1.0
        spark = [round(v / b0 * 100, 2) for v in tail]
        eco = t.get("ecosystem", "")
        rows.append({
            "theme_id":        tid,
            "name":            t["name"],
            "ecosystem":       eco,
            "ecosystem_name":  (eco_meta.get(eco) or {}).get("name", eco),
            "sibling_etf":     t.get("sibling_etf"),
            "tickers":         list(t.get("tickers", [])),
            "ret_1d":          index_ret(levels, 1),
            "ret_20d":         r20,
            "ret_vs_spy_20d":  rvs20,
            "index": {
                "base_date": idx["base_date"],
                "level":     round(levels[-1], 2),
                "spark":     spark,
            },
        })
    return rows


# ------------------------------------------------------------------
# Combined-pool RS (spec §4.2) — themes + ETFs in ONE 0-99 space
# ------------------------------------------------------------------
def percentile_rank(values: list, target: float) -> int:
    """0-99 percentile rank of `target` within `values` (same math as sector_rotation)."""
    if not values:
        return 0
    pool = [v for v in values if v is not None]
    if not pool:
        return 0
    below = sum(1 for v in pool if v < target)
    return min(99, int(round(below / len(pool) * 99)))


def combined_pool_rs(theme_rows: list, etf_rows: list) -> None:
    """
    Percentile-rank themes and ETFs together in one combined pool.

    Mutates in place:
      - theme rows get `rs_score` (combined pool) + `rank` (among themes only)
      - ETF rows get `rs_combined` (auxiliary — the published ETF `rs_score`
        from the 45-ETF pool is NOT overwritten; existing consumers unchanged)
      - both get `combined_rank` (1 = strongest across the whole pool)
    """
    pool = [r.get("ret_vs_spy_20d") for r in (theme_rows + etf_rows)
            if r.get("ret_vs_spy_20d") is not None]
    for r in theme_rows:
        v = r.get("ret_vs_spy_20d")
        r["rs_score"] = percentile_rank(pool, v) if v is not None else 0
    for r in etf_rows:
        v = r.get("ret_vs_spy_20d")
        r["rs_combined"] = percentile_rank(pool, v) if v is not None else 0

    theme_rows.sort(key=lambda r: r.get("rs_score", 0), reverse=True)
    for i, r in enumerate(theme_rows, start=1):
        r["rank"] = i

    def _combined_score(r):
        return r.get("rs_score", 0) if "theme_id" in r else r.get("rs_combined", 0)

    all_rows = sorted(theme_rows + etf_rows, key=_combined_score, reverse=True)
    for i, r in enumerate(all_rows, start=1):
        r["combined_rank"] = i


# ------------------------------------------------------------------
# Divergence (spec §4.3)
# ------------------------------------------------------------------
def divergence_for(theme_row: dict, etf_rs_by_ticker: dict) -> Optional[int]:
    """theme.rs_score − sibling ETF combined-pool RS. None when no sibling / no data."""
    sib = theme_row.get("sibling_etf")
    if not sib:
        return None
    etf_rs = etf_rs_by_ticker.get(sib)
    if etf_rs is None:
        return None
    return int(round((theme_row.get("rs_score") or 0) - etf_rs))


# ------------------------------------------------------------------
# Stock flow rollup (spec §4.4)
# ------------------------------------------------------------------
def stock_flow_rollup(theme_rows: list) -> list:
    """
    flow_score = Σ over member themes of max(0, theme_rs − 50) / 50.
    Sub-50 themes contribute 0; multi-theme membership stacks.
    Returns rows sorted desc: {ticker, themes, theme_labels, flow_score}.
    """
    by_ticker: dict = {}
    for r in theme_rows:
        contrib = max(0.0, float(r.get("rs_score") or 0) - 50.0) / 50.0
        label = short_label(r.get("name", r.get("theme_id", "")))
        for tk in r.get("tickers", []):
            e = by_ticker.setdefault(tk, {"ticker": tk, "themes": [],
                                          "theme_labels": [], "flow_score": 0.0})
            e["themes"].append(r.get("theme_id"))
            e["theme_labels"].append(label)
            e["flow_score"] += contrib
    rows = sorted(by_ticker.values(), key=lambda e: (-e["flow_score"], e["ticker"]))
    for e in rows:
        e["flow_score"] = round(e["flow_score"], 2)
    return rows


def enrich_stock_flow(rows: list, q_by_ticker: Optional[dict] = None,
                      tier_by_ticker: Optional[dict] = None,
                      held: Optional[set] = None) -> list:
    """Pure enrichment — Q score, watchlist tier, held badge. Mutates + returns rows."""
    q_by_ticker = q_by_ticker or {}
    tier_by_ticker = tier_by_ticker or {}
    held = held or set()
    for e in rows:
        tk = e.get("ticker")
        e["q_score"] = q_by_ticker.get(tk)
        e["watchlist_tier"] = tier_by_ticker.get(tk)
        e["held"] = tk in held
    return rows


def short_label(name: str, maxlen: int = 16) -> str:
    """'Vulnerability & Exposure Mgmt' → 'Vulnerability' — compact chip/Slack label."""
    head = (name or "").split(" & ")[0].split(" and ")[0].strip()
    return head[:maxlen]


# ------------------------------------------------------------------
# Money Line (spec §4.5) — names ALL leading groups
# ------------------------------------------------------------------
def money_line(theme_rows: list, etf_rows: list,
               top_ranks: int = MONEY_IN_TOP_RANKS) -> dict:
    """
    One auto-written sentence from the combined ETF+theme pool.
    IN  : every group with combined-pool RS >= 70 that sits in the top
          `top_ranks` combined ranks OR climbed >= 5 rank spots over 5d.
    OUT : rank_delta_5d >= +10 AND RS < 50 (same thresholds as signals()).
    Sub-themes of one ecosystem are grouped: `Cyber (Endpoint #2, Vuln #4)`.
    """
    def _rs(r):
        return (r.get("rs_score") if "theme_id" in r else r.get("rs_combined")) or 0

    def _name(r):
        return r.get("name") or r.get("theme_id") or r.get("etf") or "?"

    all_rows = sorted(theme_rows + etf_rows, key=lambda r: r.get("combined_rank") or 9999)

    in_rows = [r for r in all_rows
               if _rs(r) >= MONEY_IN_RS_MIN
               and ((r.get("combined_rank") or 9999) <= top_ranks
                    or (r.get("rank_delta_5d") or 0) <= MONEY_IN_CLIMB_5D)]
    out_rows = [r for r in all_rows
                if (r.get("rank_delta_5d") or 0) >= MONEY_OUT_DELTA_5D
                and _rs(r) < MONEY_OUT_RS_MAX]

    def _ser(r):
        return {
            "id":            r.get("theme_id") or r.get("etf"),
            "kind":          "theme" if "theme_id" in r else "etf",
            "name":          _name(r),
            "ecosystem":     r.get("ecosystem_name") or r.get("ecosystem") or None,
            "rs":            _rs(r),
            "combined_rank": r.get("combined_rank"),
            "rank_delta_5d": r.get("rank_delta_5d"),
        }

    # Build the IN phrase — themes grouped under their ecosystem, ETFs standalone,
    # ordered by strongest member's combined rank. Every qualifying group named.
    parts_in: list = []
    eco_groups: dict = {}
    order: list = []
    for r in in_rows:
        if "theme_id" in r:
            eco = r.get("ecosystem_name") or r.get("ecosystem") or "Other"
            if eco not in eco_groups:
                eco_groups[eco] = []
                order.append(("eco", eco))
            eco_groups[eco].append(r)
        else:
            order.append(("etf", r))
    for kind, item in order:
        if kind == "eco":
            members = eco_groups[item]
            if len(members) == 1:
                parts_in.append(_name(members[0]))
            else:
                subs = ", ".join(
                    "{0} #{1}".format(short_label(_name(m)), m.get("combined_rank"))
                    for m in members)
                parts_in.append("{0} ({1})".format(item, subs))
        else:
            parts_in.append(_name(item))

    parts_out = [_name(r) for r in out_rows]

    if parts_in and parts_out:
        text = "Money is IN: {0}. LEAVING: {1}.".format(", ".join(parts_in), ", ".join(parts_out))
    elif parts_in:
        text = "Money is IN: {0}.".format(", ".join(parts_in))
    elif parts_out:
        text = "No group clears the money-line bar today. LEAVING: {0}.".format(", ".join(parts_out))
    else:
        text = "No group clears the money-line bar today — flow is undecided."

    return {"in": [_ser(r) for r in in_rows],
            "out": [_ser(r) for r in out_rows],
            "text": text}


# ------------------------------------------------------------------
# Theme history (spec §3.2) — mirrors sector_rotation_history.json
# ------------------------------------------------------------------
def load_theme_history(path: str) -> list:
    try:
        with open(path) as f:
            return json.load(f) or []
    except (FileNotFoundError, ValueError, OSError):
        return []


def save_theme_history(history: list, path: str,
                       retention_days: int = THEME_HISTORY_RETENTION_DAYS) -> None:
    cutoff = (datetime.date.today() - datetime.timedelta(days=retention_days)).isoformat()
    pruned = [row for row in history if row.get("date", "") >= cutoff]
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


def annotate_theme_history(rows: list, history: list, today: str) -> list:
    """
    Adds `rank_5d_ago` / `rank_delta_5d` to theme rows off theme_rotation_history.json
    (same pattern as sector_rotation.annotate_with_history — theme-only rank space).
    """
    by_date: dict = {}
    for h in history:
        by_date.setdefault(h.get("date"), {})[h.get("theme")] = h
    sorted_dates = sorted(d for d in by_date.keys() if d and d < today)
    five_back = sorted_dates[-5] if len(sorted_dates) >= 5 else (sorted_dates[0] if sorted_dates else None)
    for r in rows:
        prior5 = by_date.get(five_back, {}).get(r.get("theme_id")) if five_back else None
        r["rank_5d_ago"] = prior5.get("rank") if prior5 else None
        r["rank_delta_5d"] = (r.get("rank", 0) - prior5["rank"]) if prior5 and prior5.get("rank") else 0
    return rows


def append_theme_history(rows: list, path: str, today: str) -> None:
    """Idempotent per-day append (replaces any existing rows for today), 180d retention."""
    history = load_theme_history(path)
    history = [h for h in history if h.get("date") != today]
    for r in rows:
        history.append({
            "date":     today,
            "theme":    r.get("theme_id"),
            "rs_score": r.get("rs_score"),
            "rank":     r.get("rank"),
            "ret_1d":   r.get("ret_1d"),
        })
    save_theme_history(history, path)


# ------------------------------------------------------------------
# Inline-SVG sparkline (no external libs — strict-CSP safe)
# ------------------------------------------------------------------
def sparkline_svg(values: list, width: int = 140, height: int = 30,
                  stroke: str = "#60a5fa") -> str:
    """Single-series micro line chart of the theme index (last N sessions)."""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 2.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = pad + (hi - v) * (height - 2 * pad) / span
        pts.append("{0:.1f},{1:.1f}".format(x, y))
    pts_str = " ".join(pts)
    return (
        '<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        'preserveAspectRatio="none" role="img" aria-label="theme index, last {n} sessions">'
        '<polyline fill="none" stroke="{s}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round" points="{p}"/></svg>'
    ).format(w=width, h=height, n=n, s=stroke, p=pts_str)
