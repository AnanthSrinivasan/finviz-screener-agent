"""Weekly Review §3 — Book Weekend Review.

Per-open-position weekend verdict: cur% / peak% / dist-to-stop / verdict
(✅ hold · 🟢 trail tighter · ⚠️ trim ⅓ · 🚨 cut). Reuses the /pos-review
verdict logic from utils/generators/generate_live_portfolio.py::verdict_for —
single source of truth for the verdict ladder.

The weekly agent passes in the open positions (positions.json open_positions,
refreshed every monitor run) plus an optional technicals lookup (ATR% / SMA20%
/ stage). When technicals are unavailable the verdict still renders from the
gain ladder alone.

Pure fns + html/slack renderers. Light theme only. Plain English.
"""

from __future__ import annotations

from utils.generators.generate_live_portfolio import verdict_for


def _f(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _current_price(pos: dict) -> float:
    cp = pos.get("current_price")
    if cp:
        try:
            return float(cp)
        except (TypeError, ValueError):
            pass
    entry = _f(pos.get("entry_price"))
    gain = pos.get("current_gain_pct")
    if entry > 0 and gain is not None:
        return entry * (1 + _f(gain) / 100.0)
    return entry


def build_book_review_rows(positions: list, tech_lookup=None) -> list:
    """Build per-position weekend-review rows.

    positions:   positions.json open_positions list.
    tech_lookup: optional callable(ticker) -> {atr, s20, stage} (Finviz). When
                 absent the verdict uses the gain ladder with neutral
                 technicals (atr=0, s20=0, stage='2').

    Returns rows sorted worst-verdict-first (cut → trim → trail → hold) then by
    current gain ascending, so the names needing action surface at the top.
    """
    rows = []
    for pos in positions or []:
        ticker = pos.get("ticker", "?")
        entry = _f(pos.get("entry_price"))
        gain = _f(pos.get("current_gain_pct"))
        peak = _f(pos.get("peak_gain_pct"))
        cp = _current_price(pos)
        stop = _f(pos.get("stop_price"))
        dist_to_stop = ((cp - stop) / cp * 100.0) if (cp > 0 and stop > 0) else None

        tech = (tech_lookup(ticker) if tech_lookup else None) or {}
        atr = _f(tech.get("atr"))
        s20 = _f(tech.get("s20"))
        stage = tech.get("stage", "2")
        verdict = verdict_for(gain, atr, s20, stage)

        rows.append({
            "ticker": ticker,
            "entry": entry,
            "current_price": cp,
            "gain_pct": round(gain, 1),
            "peak_pct": round(peak, 1),
            "stop_price": stop,
            "dist_to_stop_pct": round(dist_to_stop, 1) if dist_to_stop is not None else None,
            "atr_pct": round(atr, 1),
            "s20_pct": round(s20, 1),
            "stage": stage,
            "verdict": verdict,
            "_rank": _verdict_rank(verdict),
        })

    rows.sort(key=lambda r: (r["_rank"], r["gain_pct"]))
    return rows


def _verdict_rank(verdict: str) -> int:
    """Lower = more urgent (surfaces first)."""
    v = verdict.lower()
    if "cut" in v:
        return 0
    if "trim" in v or "peel" in v:
        return 1
    if "trail" in v:
        return 2
    if "dead weight" in v:
        return 3
    if "sleeping" in v:
        return 4
    return 5  # working / hold / watch


# ----------------------------
# HTML render (light theme)
# ----------------------------

BOOK_REVIEW_CSS = """
.bwr-table { width: 100%; border-collapse: collapse; font-size: 0.8rem;
             background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
             overflow: hidden; margin-bottom: 28px; }
.bwr-table th { text-align: left; padding: 9px 11px; color: #6b7280; font-weight: 500;
                border-bottom: 1px solid #e5e7eb; text-transform: uppercase;
                font-size: 0.64rem; letter-spacing: .05em; background: #f9fafb; }
.bwr-table td { padding: 9px 11px; border-bottom: 1px solid #f3f4f6; color: #111827; }
.bwr-table tr:last-child td { border-bottom: none; }
.bwr-table tr:hover td { background: #f9fafb; }
.bwr-mono { font-variant-numeric: tabular-nums; }
.bwr-pos { color: #16a34a; font-weight: 600; }
.bwr-neg { color: #dc2626; font-weight: 600; }
.bwr-near { color: #b45309; font-weight: 600; }
"""


def _pct_cls(v: float) -> str:
    return "bwr-pos" if v >= 0 else "bwr-neg"


def render_book_review_html(rows: list) -> str:
    if not rows:
        return (
            "<h2>📋 Book Weekend Review</h2>"
            "<p class='lb-note'>No open positions — flat book into the weekend.</p>"
        )
    body = ""
    for r in rows:
        d2s = r["dist_to_stop_pct"]
        if d2s is None:
            d2s_html = "<td class='bwr-mono dim'>—</td>"
        else:
            cls = "bwr-near" if d2s < 3 else "bwr-mono"
            d2s_html = f"<td class='{cls}'>{d2s:+.1f}%</td>"
        fv = f"https://finviz.com/quote.ashx?t={r['ticker']}"
        body += (
            "<tr>"
            f"<td class='bold'><a href='{fv}' target='_blank'>{r['ticker']}</a></td>"
            f"<td class='bwr-mono {_pct_cls(r['gain_pct'])}'>{r['gain_pct']:+.1f}%</td>"
            f"<td class='bwr-mono'>{r['peak_pct']:+.1f}%</td>"
            f"<td class='bwr-mono'>${r['stop_price']:.2f}</td>"
            f"{d2s_html}"
            f"<td>{r['verdict']}</td>"
            "</tr>"
        )
    return (
        "<h2>📋 Book Weekend Review</h2>"
        "<p class='lb-note'>Per-position verdict (reuses /pos-review ladder). "
        "Sorted action-first — cut / trim at the top, working names below.</p>"
        "<table class='bwr-table'><thead><tr>"
        "<th>Ticker</th><th>Cur %</th><th>Peak %</th><th>Stop</th>"
        "<th>To stop</th><th>Verdict</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
    )


# ----------------------------
# Slack render
# ----------------------------

def render_book_review_slack(rows: list, max_rows: int = 12) -> str:
    if not rows:
        return "📋 *Book Weekend Review*\n_No open positions — flat into the weekend._"
    lines = ["📋 *Book Weekend Review* — action-first"]
    for r in rows[:max_rows]:
        d2s = (f"{r['dist_to_stop_pct']:+.1f}% to stop"
               if r["dist_to_stop_pct"] is not None else "no stop")
        lines.append(
            f"*{r['ticker']}*  {r['gain_pct']:+.1f}% (pk {r['peak_pct']:+.1f}%) · "
            f"{d2s} · {r['verdict']}"
        )
    if len(rows) > max_rows:
        lines.append(f"_+{len(rows) - max_rows} more — see full report_")
    return "\n".join(lines)
