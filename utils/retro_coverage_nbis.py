"""Retro coverage audit — NBIS-class setups.

Validates that the screener's gates would have flagged historical NBIS-class
winners on their key entry dates. Produces a coverage matrix per ticker × date
× block (technical-only — Finviz fundamentals are not reconstructable).

Spec: docs/specs/retro-coverage-nbis-class.md
Output: docs/research/retro_coverage_nbis_class.{html,json}
        docs/research/retro_coverage_nbis_class_dates.json
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "research"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"

# ---------------------------------------------------------------------------
# Basket — 20 tickers across recent (2025-2026) and prior bull markets (2020, 2024)
# Dates picked by chart inspection. D1=reclaim, D2=tight-flag/breakout, D3=21EMA PB.
# ---------------------------------------------------------------------------
BASKET: dict[str, list[tuple[str, str]]] = {
    # === Recent 2025-2026 (NBIS-class) ===
    "NBIS": [("2026-04-09", "D1_reclaim"), ("2026-04-14", "D2_flag"), ("2026-05-05", "D3_ema21_pb")],
    "RKLB": [("2026-04-16", "D1_reclaim"), ("2026-04-22", "D2_flag")],
    "DOCN": [("2026-04-06", "D1_reclaim"), ("2026-04-13", "D2_flag")],
    "INDV": [("2026-04-20", "D1_reclaim"), ("2026-04-28", "D2_flag")],
    "ARWR": [("2026-04-30", "D2_flag")],
    "ANET": [("2026-04-08", "D2_flag"), ("2026-04-22", "D3_ema21_pb")],
    "FSLY": [("2026-04-15", "D1_reclaim"), ("2026-04-28", "D2_flag")],
    "RMBS": [("2026-04-23", "D2_flag")],
    "MTSI": [("2026-04-22", "D2_flag")],
    "ALAB": [("2026-04-10", "D2_flag"), ("2026-04-28", "D3_ema21_pb")],
    # === Local winners (current closed/active) ===
    "AAOI": [("2026-03-20", "D1_reclaim"), ("2026-04-08", "D2_flag"), ("2026-05-01", "D3_ema21_pb")],
    "MU":   [("2026-03-25", "D2_flag"), ("2026-04-15", "D3_ema21_pb")],
    "NVDA": [("2026-03-19", "D2_flag"), ("2026-04-15", "D3_ema21_pb")],
    "Z":    [("2026-03-15", "D1_reclaim"), ("2026-04-08", "D2_flag")],
    # === 2024 AI rip classics ===
    "SMCI": [("2024-01-17", "D2_flag"), ("2024-02-13", "D3_ema21_pb")],
    "VRT":  [("2024-01-25", "D2_flag"), ("2024-02-22", "D3_ema21_pb")],
    "APP":  [("2024-08-08", "D2_flag"), ("2024-09-09", "D3_ema21_pb")],
    # === 2020 post-COVID classics ===
    "CRWD": [("2020-06-04", "D2_flag"), ("2020-07-15", "D3_ema21_pb")],
    "DDOG": [("2020-05-12", "D2_flag"), ("2020-06-22", "D3_ema21_pb")],
    "ENPH": [("2020-05-06", "D1_reclaim"), ("2020-07-29", "D3_ema21_pb")],
}


# ---------------------------------------------------------------------------
# Alpaca fetch
# ---------------------------------------------------------------------------
def _alpaca_headers() -> dict:
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_bars(ticker: str, end_date: str, lookback_days: int = 400) -> list[dict]:
    """Fetch up to lookback_days of daily bars ending on end_date (inclusive)."""
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = end - timedelta(days=int(lookback_days * 1.6) + 10)
    params = {
        "symbols": ticker,
        "timeframe": "1Day",
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "limit": 10000,
        "adjustment": "split",
        "feed": "iex",
    }
    try:
        r = requests.get(ALPACA_BARS_URL, params=params, headers=_alpaca_headers(), timeout=30)
    except Exception as e:
        log.warning("fetch_bars %s %s error: %s", ticker, end_date, e)
        return []
    if r.status_code != 200:
        log.warning("fetch_bars %s %s HTTP %s", ticker, end_date, r.status_code)
        return []
    rows = r.json().get("bars", {}).get(ticker, []) or []
    return sorted(rows, key=lambda b: b.get("t", ""))


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------
def sma(vs: list[float], n: int) -> Optional[float]:
    if len(vs) < n:
        return None
    return sum(vs[-n:]) / n


def ema(vs: list[float], n: int) -> Optional[float]:
    if len(vs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vs[:n]) / n
    for v in vs[n:]:
        e = v * k + e * (1 - k)
    return e


def atr14(rows: list[dict]) -> Optional[float]:
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h = rows[i]["h"]; l = rows[i]["l"]; pc = rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < 14:
        return None
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    return atr


def compute_metrics(rows: list[dict]) -> Optional[dict]:
    """Compute all technical inputs from a list of bars ending on the eval date."""
    if len(rows) < 60:
        return None
    closes = [b["c"] for b in rows]
    highs = [b["h"] for b in rows]
    lows = [b["l"] for b in rows]
    vols = [b["v"] for b in rows]
    last = closes[-1]
    s20 = sma(closes, 20); s50 = sma(closes, 50); s200 = sma(closes, 200)
    e8 = ema(closes, 8); e21 = ema(closes, 21)
    atr = atr14(rows)
    atr_pct = (atr / last * 100) if atr and last else None
    atr_mult_50 = ((last - s50) * last / (s50 * atr)) if (s50 and atr) else None
    pct_from_50 = ((last - s50) / s50 * 100) if s50 else None
    pct_from_20 = ((last - s20) / s20 * 100) if s20 else None
    hi52 = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    dist_52 = (last - hi52) / hi52 * 100
    # "perfect" (strict): full ladder, no tolerance
    stage2_perfect = (
        all(v is not None for v in (e8, e21, s50, s200))
        and last > e8 > e21 > s50 > s200
    )
    # "loose": uptrend (price>50>200) + fast EMA confirms uptrend (EMA21>SMA50).
    # Tolerates EMA8/21/price bunching on reclaim days.
    stage2_loose = (
        s50 is not None and s200 is not None and e21 is not None
        and last > s50 > s200 and e21 > s50
    )
    # "pullback-friendly": trend up but allows price to dip to/below EMA8/EMA21.
    # Required: EMA21 > SMA50 > SMA200 AND price > SMA50.
    # Don't require price > EMA8 — pullbacks to EMA21 by definition violate that.
    stage2_pullback = (
        s50 is not None and s200 is not None and e21 is not None
        and e21 > s50 > s200 and last > s50
    )
    s50_10ago = sma(closes[:-10], 50) if len(closes) >= 60 else None
    s200_10ago = sma(closes[:-10], 200) if len(closes) >= 210 else None
    s50_rising = s50 is not None and s50_10ago is not None and s50 > s50_10ago
    s200_rising = s200 is not None and s200_10ago is not None and s200 > s200_10ago
    av20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
    rvol = vols[-1] / av20 if av20 else None
    sw_high = max(highs[-90:-5]) if len(highs) >= 95 else None
    dist_swing = ((last - sw_high) / sw_high * 100) if sw_high else None
    ret_20d = ((last - closes[-21]) / closes[-21] * 100) if len(closes) >= 21 else None
    ema21_dist_pct = ((last - e21) / last * 100) if e21 else None
    return {
        "last": last, "atr_pct": atr_pct, "atr_mult_50": atr_mult_50,
        "pct_from_50": pct_from_50, "pct_from_20": pct_from_20,
        "dist_52": dist_52, "dist_swing": dist_swing,
        "stage2_perfect": stage2_perfect, "stage2_loose": stage2_loose,
        "stage2_pullback": stage2_pullback,
        "s50_rising": s50_rising, "s200_rising": s200_rising,
        "rvol": rvol, "ret_20d": ret_20d, "ema21_dist_pct": ema21_dist_pct,
    }


# ---------------------------------------------------------------------------
# Block evaluators — return (pass, reason)
# ---------------------------------------------------------------------------
USE_LOOSE = False  # toggled by run() for second pass


def _stage_ok(m: dict) -> bool:
    return m["stage2_loose"] if USE_LOOSE else m["stage2_perfect"]


def _stage_label() -> str:
    return "Stage2 loose" if USE_LOOSE else "Stage2 perfect"


def eval_rte(m: dict) -> tuple[bool, str]:
    if not _stage_ok(m): return False, f"not {_stage_label()}"
    if m["atr_pct"] is None or m["atr_pct"] > 7: return False, f"ATR%>7 ({m['atr_pct']:.1f})"
    if not (-12 <= m["dist_52"] <= -1): return False, f"dist {m['dist_52']:.1f}% outside [-12,-1]"
    if m["rvol"] is None or m["rvol"] > 1.2: return False, f"RVol>{m['rvol']:.2f}"
    return True, "pass"


def eval_fb(m: dict) -> tuple[bool, str]:
    if not m["stage2_loose"]: return False, "not Stage2"
    if m["atr_pct"] is None or m["atr_pct"] > 8: return False, f"ATR%>8 ({m['atr_pct']:.1f})"
    if not (-12 <= m["dist_52"] <= 0): return False, f"dist {m['dist_52']:.1f}% outside [-12,0]"
    if m["pct_from_20"] is None or m["pct_from_20"] <= 0: return False, "SMA20% ≤0"
    if m["pct_from_50"] is None or not (0 < m["pct_from_50"] <= 25):
        return False, f"SMA50% {m['pct_from_50']:.1f} outside (0,25]"
    rvol_ok = m["rvol"] is not None and m["rvol"] >= 1.2
    if not rvol_ok: return False, f"RVol<1.2 ({m['rvol']})"
    return True, "pass"


def eval_htf_br(m: dict) -> tuple[bool, str]:
    # HTF-BR uses pullback-friendly trend (reclaim days have price just clearing
    # but EMA8/EMA21 still tight or below).
    if not m["stage2_pullback"]: return False, "trend not up (need EMA21>SMA50>SMA200)"
    if m["atr_pct"] is None or m["atr_pct"] > 7: return False, f"ATR%>7 ({m['atr_pct']:.1f})"
    if m["dist_52"] >= -12: return False, f"dist {m['dist_52']:.1f}% ≥ -12 (not HTF)"
    if not (m["s50_rising"] and m["s200_rising"]): return False, "MA stack not rising"
    if m["rvol"] is None or m["rvol"] < 1.0: return False, f"RVol<1.0"
    if m["dist_swing"] is None or m["dist_swing"] < -10:
        return False, f"swing dist {m['dist_swing']} <-10"
    return True, "pass"


def eval_rs_leader(m: dict) -> tuple[bool, str]:
    if not _stage_ok(m): return False, f"not {_stage_label()}"
    if m["atr_pct"] is None or m["atr_pct"] > 8: return False, f"ATR%>8 ({m['atr_pct']:.1f})"
    if not (-10 <= m["dist_52"] <= 2): return False, f"dist {m['dist_52']:.1f}% outside [-10,+2]"
    if not (m["s50_rising"] and m["s200_rising"]): return False, "MA stack not rising"
    if m["rvol"] is None or m["rvol"] > 1.5: return False, f"RVol>1.5"
    return True, "pass"


def eval_bb(m: dict) -> tuple[bool, str]:
    if not m["stage2_loose"]: return False, "not Stage2"
    if m["atr_pct"] is None or m["atr_pct"] > 7: return False, f"ATR%>7"
    if not (-25 <= m["dist_52"] <= -12): return False, f"dist {m['dist_52']:.1f}% outside [-25,-12]"
    return True, "pass"


def eval_ema21_pb(m: dict) -> tuple[bool, str]:
    """Proposed lane. Tight pullback to 21 EMA after a meaningful prior run."""
    # Use pullback-friendly trend — by definition price has dipped to EMA21,
    # so requiring price > EMA8 would kill every legitimate setup.
    if not m["stage2_pullback"]: return False, "trend not up (need EMA21>SMA50>SMA200)"
    if m["atr_pct"] is None or m["atr_pct"] > 6: return False, f"ATR%>6 ({m['atr_pct']:.1f})"
    if m["ema21_dist_pct"] is None or abs(m["ema21_dist_pct"]) > 2:
        return False, f"|price-EMA21|/price >2% ({m['ema21_dist_pct']:.1f})"
    if m["ret_20d"] is None or m["ret_20d"] < 15:
        return False, f"prior 20d ret <15% ({m['ret_20d']})"
    # RVol declining = today < 20d avg → rvol < 1.0
    if m["rvol"] is None or m["rvol"] >= 1.0:
        return False, f"RVol not declining ({m['rvol']})"
    return True, "pass"


BLOCKS = [
    ("RTE-tech",     eval_rte),
    ("FB-tech",      eval_fb),
    ("HTF-BR-tech",  eval_htf_br),
    ("RS-Leader",    eval_rs_leader),
    ("BB-tech",      eval_bb),
    ("21EMA-PB*",    eval_ema21_pb),  # * = proposed new
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run() -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for ticker, dates in BASKET.items():
        for date_str, label in dates:
            bars = fetch_bars(ticker, date_str, lookback_days=400)
            # Trim bars to those <= eval date (inclusive) — Alpaca end is inclusive
            bars = [b for b in bars if b.get("t", "")[:10] <= date_str]
            if not bars:
                results.append({
                    "ticker": ticker, "date": date_str, "label": label,
                    "metrics": None, "blocks": {}, "any_pass": False,
                    "note": "no bars",
                })
                continue
            m = compute_metrics(bars)
            if m is None:
                results.append({
                    "ticker": ticker, "date": date_str, "label": label,
                    "metrics": None, "blocks": {}, "any_pass": False,
                    "note": f"insufficient history ({len(bars)} bars)",
                })
                continue
            block_results = {}
            for name, fn in BLOCKS:
                ok, reason = fn(m)
                block_results[name] = {"pass": ok, "reason": reason}
            any_pass = any(b["pass"] for b in block_results.values())
            results.append({
                "ticker": ticker, "date": date_str, "label": label,
                "metrics": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()},
                "blocks": block_results, "any_pass": any_pass,
            })
            log.info("%-6s %s %-15s  any=%s  %s",
                     ticker, date_str, label, "✓" if any_pass else "✗",
                     " ".join(n for n, b in block_results.items() if b["pass"]))

    # --- Coverage stats
    total_rows = len(results)
    passed_rows = sum(1 for r in results if r["any_pass"])
    tickers_total = len(BASKET)
    tickers_passed = len({r["ticker"] for r in results if r["any_pass"]})
    per_block_hits = {}
    for name, _ in BLOCKS:
        hits = sum(1 for r in results if r["blocks"].get(name, {}).get("pass"))
        per_block_hits[name] = hits
    # Most common failure reasons across misses
    miss_reasons: dict[str, int] = {}
    for r in results:
        if not r["any_pass"]:
            for name, b in r["blocks"].items():
                key = f"{name}: {b['reason']}"
                miss_reasons[key] = miss_reasons.get(key, 0) + 1
    top_reasons = sorted(miss_reasons.items(), key=lambda kv: -kv[1])[:15]

    summary = {
        "tickers_total": tickers_total, "tickers_passed": tickers_passed,
        "rows_total": total_rows, "rows_passed": passed_rows,
        "per_block_hits": per_block_hits, "top_miss_reasons": top_reasons,
    }

    # --- Write outputs
    out_json = OUT_DIR / "retro_coverage_nbis_class.json"
    out_json.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    (OUT_DIR / "retro_coverage_nbis_class_dates.json").write_text(json.dumps(BASKET, indent=2))
    out_html = OUT_DIR / "retro_coverage_nbis_class.html"
    out_html.write_text(_render_html(summary, results))
    log.info("\nWrote %s and %s", out_json, out_html)
    return summary


# ---------------------------------------------------------------------------
# HTML render — light theme (per project convention)
# ---------------------------------------------------------------------------
def _render_html(summary: dict, results: list[dict]) -> str:
    rows_html = []
    for r in results:
        cells = []
        for name, _ in BLOCKS:
            b = r["blocks"].get(name, {})
            ok = b.get("pass")
            if ok:
                cells.append('<td style="background:#d1fae5;color:#065f46;text-align:center">✓</td>')
            else:
                reason = b.get("reason", "")
                cells.append(f'<td style="background:#fee2e2;color:#991b1b;font-size:11px;padding:4px">{reason}</td>')
        m = r["metrics"] or {}
        metric_str = ""
        if m:
            metric_str = (f"ATR%={m.get('atr_pct')} "
                          f"mult50={m.get('atr_mult_50')} "
                          f"dist52={m.get('dist_52')} "
                          f"rvol={m.get('rvol')}")
        row_bg = "#ecfdf5" if r["any_pass"] else "#fef2f2"
        rows_html.append(
            f'<tr style="background:{row_bg}">'
            f'<td><b>{r["ticker"]}</b></td>'
            f'<td>{r["date"]}</td>'
            f'<td style="font-size:11px;color:#6b7280">{r["label"]}</td>'
            + "".join(cells)
            + f'<td style="font-size:11px;color:#6b7280">{metric_str}</td>'
            + f'<td style="text-align:center">{"✅" if r["any_pass"] else "❌"}</td>'
            + "</tr>"
        )
    header_cells = "".join(f'<th>{name}</th>' for name, _ in BLOCKS)
    block_stats = "".join(
        f'<li><b>{name}</b>: {hits}/{summary["rows_total"]} rows</li>'
        for name, hits in summary["per_block_hits"].items()
    )
    miss_list = "".join(
        f'<li>{reason} — {count}×</li>' for reason, count in summary["top_miss_reasons"]
    )
    pass_pct = (summary["rows_passed"] / summary["rows_total"] * 100) if summary["rows_total"] else 0
    ticker_pct = (summary["tickers_passed"] / summary["tickers_total"] * 100) if summary["tickers_total"] else 0
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Retro Coverage — NBIS-class</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#111827;padding:24px;max-width:1400px;margin:0 auto}}
h1{{color:#111827}} h2{{color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:6px;margin-top:32px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}}
th{{background:#f3f4f6;color:#111827}}
.summary{{background:#f9fafb;padding:16px;border-radius:8px;border:1px solid #e5e7eb}}
.big{{font-size:32px;font-weight:bold;color:#16a34a}}
.bad{{color:#dc2626}}
a{{color:#2563eb}}
</style></head><body>
<h1>Retro Coverage Audit — NBIS-class Setups</h1>
<p style="color:#6b7280">Spec: <a href="../specs/retro-coverage-nbis-class.md">retro-coverage-nbis-class.md</a></p>

<div class="summary">
<div class="big {('' if ticker_pct >= 80 else 'bad')}">{summary["tickers_passed"]}/{summary["tickers_total"]} tickers ({ticker_pct:.0f}%)</div>
<div>caught by at least one block at at least one entry date</div>
<div style="margin-top:8px">Row-level: <b>{summary["rows_passed"]}/{summary["rows_total"]}</b> ticker-dates ({pass_pct:.0f}%)</div>
</div>

<h2>Per-block hit rate</h2>
<ul>{block_stats}</ul>
<p style="color:#6b7280;font-size:12px">* 21EMA-PB is proposed — not in production. If high hit rate among misses, propose implementation.</p>

<h2>Top miss reasons (across rows where no block fired)</h2>
<ul>{miss_list}</ul>

<h2>Coverage matrix</h2>
<table>
<thead><tr><th>Ticker</th><th>Date</th><th>Label</th>{header_cells}<th>Metrics</th><th>Any</th></tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
</body></html>"""


if __name__ == "__main__":
    s = run()
    print("\nSUMMARY:", json.dumps(s, indent=2))
