"""Weekly review helper — load and summarize daily ETF rotation snapshot.

Reads `data/etf_rotation.json` (written daily by `agents/sector_rotation.py`)
and translates it into a structure the weekly agent renders into HTML + Slack.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


# Plain-English "what this means for you this week" — one sentence per regime.
REGIME_ADVICE = {
    "correlation_phase": "trade the index, not stocks. Single-name edge is fragile this tape — wait for dispersion to widen.",
    "early-rotation":    "leadership is forming. Build watchlist now, take only confirmed RS leaders. Patience pays.",
    "mid-rotation":      "this is the best-entry tape. Trade the leaders, not the laggards. The market is paying you to be selective, not to fish in broken groups.",
    "late-rotation":     "leadership is narrowing. Trim ≥+25% positions, skip extended names, take only fresh RS-rising leaders with peel-warn room.",
    "blow-off-risk":     "parabolic tape. No new entries. Tighten stops, trim aggressively, cash is a position. The next clean trade is on the other side.",
    "bootstrapping":     "regime tag not yet calibrated — use market_state for sizing decisions this week.",
}

# Buckets we surface in the weekly review (NEUTRAL is skipped — too noisy).
ACTIONABLE_BUCKETS = ["BASE", "PRE-BREAKOUT", "EXTENDED", "BROKEN"]

# Bucket → (emoji, one-line interpretation suffix for HTML/Slack lines).
BUCKET_HINTS = {
    "BASE":         ("🎯", "in BASE → tight, ready to break out. Start screening constituents this weekend — that's where next leadership likely shows up."),
    "PRE-BREAKOUT": ("🟦", "pre-breakout → approaching highs with room. Watchlist trigger if these clear pivot on volume."),
    "EXTENDED":     ("🚀", "extended — don't chase. Wait for 21 EMA pullback on the names you want in these themes."),
    "BROKEN":       ("❌", "broken → if a name from these groups screens well this week, the group is wrong. Skip or wait."),
}

# Slack-only short hints (one line each — table is too noisy for Slack).
SLACK_BUCKET_HINTS = {
    "BASE":         ("🎯", "screen constituents"),
    "PRE-BREAKOUT": ("🟦", "watch for pivot break"),
    "EXTENDED":     ("🚀", "wait for PB, don't chase"),
    "BROKEN":       ("❌", "skip names from these groups"),
}


def load_etf_rotation(data_dir: str) -> dict | None:
    """Load `data/etf_rotation.json` (the daily snapshot). Returns None if missing/invalid."""
    path = os.path.join(data_dir, "etf_rotation.json")
    if not os.path.exists(path):
        log.warning("etf_rotation.json not found at %s — weekly sector section skipped", path)
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning("Failed to load etf_rotation.json: %s", e)
        return None


def _regime_paragraph(regime: str, regime_actions: dict | None) -> str:
    """Compose the regime paragraph from REGIME_ACTIONS + REGIME_ADVICE."""
    advice = REGIME_ADVICE.get(regime, REGIME_ADVICE["bootstrapping"])
    if not regime_actions:
        return f"Regime: **{regime}**. **What this means for you this week:** {advice}"
    headline = regime_actions.get("headline", "")
    sizing   = regime_actions.get("sizing", "")
    entries  = regime_actions.get("entries", "")
    held     = regime_actions.get("held", "")
    parts = [p for p in (sizing, entries, held) if p]
    body = " · ".join(parts)
    return (
        f"Regime: **{regime}** — {headline}. {body} "
        f"**What this means for you this week:** {advice}"
    )


def summarize_etf_rotation(rotation: dict, top_n: int = 5,
                           regime_actions_lookup=None) -> dict:
    """Bucket the rotation snapshot into the weekly-review structure.

    Args:
        rotation: parsed `etf_rotation.json` dict — must have `regime` + `etfs` list.
        top_n: tickers per bucket to surface (default 5).
        regime_actions_lookup: callable(regime_tag) -> action dict, or None.

    Returns dict with regime, regime_paragraph, regime_advice, buckets, table_rows.
    Empty buckets are omitted from `buckets`.
    """
    regime = (rotation or {}).get("regime", "bootstrapping")
    etfs = (rotation or {}).get("etfs", []) or []

    regime_actions = None
    if regime_actions_lookup is not None:
        try:
            regime_actions = regime_actions_lookup(regime)
        except Exception:
            regime_actions = None

    by_bucket: dict = {b: [] for b in ACTIONABLE_BUCKETS}
    for e in etfs:
        b = e.get("bucket")
        if b in by_bucket:
            by_bucket[b].append(e)

    # Sorting per bucket — surface the most actionable first.
    def _ret20(e): return (e.get("metrics") or {}).get("ret20") or 0
    def _mult(e):  return (e.get("metrics") or {}).get("mult50") or 0
    def _dist(e):  return (e.get("metrics") or {}).get("dist52") or 0
    if "BASE" in by_bucket:         by_bucket["BASE"].sort(key=lambda e: -_ret20(e))
    if "PRE-BREAKOUT" in by_bucket: by_bucket["PRE-BREAKOUT"].sort(key=lambda e: _dist(e))  # closest to highs (least negative)
    if "EXTENDED" in by_bucket:     by_bucket["EXTENDED"].sort(key=lambda e: -_mult(e))
    if "BROKEN" in by_bucket:       by_bucket["BROKEN"].sort(key=lambda e: _dist(e))      # most broken first

    buckets_out: dict = {}
    for b in ACTIONABLE_BUCKETS:
        rows = by_bucket.get(b, [])
        if not rows:
            continue
        buckets_out[b] = rows[:top_n]

    # Compact metrics table — actionable buckets only (skip BROKEN — table is for active opportunity).
    table_rows = []
    for b in ("BASE", "PRE-BREAKOUT", "EXTENDED"):
        for e in by_bucket.get(b, []):
            m = e.get("metrics") or {}
            table_rows.append({
                "ticker":  e.get("ticker"),
                "name":    e.get("name"),
                "bucket":  b,
                "mult50":  m.get("mult50"),
                "dist52":  m.get("dist52"),
                "range20": m.get("range20"),
                "ret20":   m.get("ret20"),
                "rvol":    m.get("rvol"),
            })

    return {
        "regime":            regime,
        "regime_actions":    regime_actions,
        "regime_advice":     REGIME_ADVICE.get(regime, REGIME_ADVICE["bootstrapping"]),
        "regime_paragraph":  _regime_paragraph(regime, regime_actions),
        "buckets":           buckets_out,
        "table_rows":        table_rows,
    }


# ----------------------------
# Renderers — HTML + Slack
# ----------------------------

def _fmt_pct(v) -> str:
    if v is None: return "—"
    try: return f"{float(v):+.1f}%"
    except Exception: return "—"


def _fmt_num(v, decimals=2) -> str:
    if v is None: return "—"
    try: return f"{float(v):.{decimals}f}"
    except Exception: return "—"


def render_sector_setup_html(summary: dict | None) -> str:
    """Build the weekly `📊 Sector Setup This Week` HTML section.

    Returns "" when summary is None or has no actionable buckets — caller can
    concat unconditionally.
    """
    if not summary or not summary.get("buckets"):
        return ""

    # Convert markdown-style **bold** in regime paragraph to <strong>.
    para = summary["regime_paragraph"]
    while "**" in para:
        para = para.replace("**", "<strong>", 1)
        if "**" in para:
            para = para.replace("**", "</strong>", 1)

    bucket_lines_html = ""
    for bucket, rows in summary["buckets"].items():
        emoji, hint = BUCKET_HINTS.get(bucket, ("", ""))
        tickers = ", ".join(f"<strong>{r.get('ticker')}</strong>" for r in rows)
        bucket_lines_html += (
            f"<li class='bucket-row'>"
            f"<span class='bucket-icon'>{emoji}</span> "
            f"<span class='bucket-tickers'>{tickers}</span> {hint}"
            f"</li>"
        )

    table_rows = summary.get("table_rows", [])
    table_html = ""
    if table_rows:
        body = ""
        for r in table_rows:
            body += (
                "<tr>"
                f"<td><strong>{r['ticker']}</strong></td>"
                f"<td>{r.get('name') or ''}</td>"
                f"<td>{r['bucket']}</td>"
                f"<td>{_fmt_num(r.get('mult50'))}</td>"
                f"<td>{_fmt_pct(r.get('dist52'))}</td>"
                f"<td>{_fmt_pct(r.get('range20'))}</td>"
                f"<td>{_fmt_pct(r.get('ret20'))}</td>"
                f"<td>{_fmt_num(r.get('rvol'))}</td>"
                "</tr>"
            )
        table_html = (
            "<table class='sector-setup-table'><thead><tr>"
            "<th>Ticker</th><th>Name</th><th>Bucket</th>"
            "<th>mult50</th><th>dist52</th><th>range20</th><th>ret20</th><th>RVol</th>"
            "</tr></thead><tbody>" + body + "</tbody></table>"
        )

    return (
        "<section class='sector-setup-section'>"
        "<h2>📊 Sector Setup This Week</h2>"
        f"<p class='sector-setup-regime'>{para}</p>"
        f"<ul class='sector-setup-buckets'>{bucket_lines_html}</ul>"
        + table_html
        + "<p class='sector-setup-footer'>View full daily dashboard → "
        "<a href='etf_rotation.html'>data/etf_rotation.html</a></p>"
        "</section>"
    )


SECTOR_SETUP_CSS = """
.sector-setup-section { margin: 18px 0 24px; padding: 14px 16px; background: #f9fafb;
    border: 1px solid #e5e7eb; border-radius: 6px; }
.sector-setup-section h2 { margin: 0 0 8px; font-size: 1.05rem; color: #111827; }
.sector-setup-regime { margin: 0 0 10px; color: #111827; font-size: 0.9rem; line-height: 1.45; }
.sector-setup-buckets { list-style: none; padding: 0; margin: 0 0 12px; }
.sector-setup-buckets .bucket-row { padding: 6px 0; color: #111827; font-size: 0.88rem; line-height: 1.4; }
.sector-setup-buckets .bucket-icon { display: inline-block; width: 22px; }
.sector-setup-buckets .bucket-tickers { color: #2563eb; }
.sector-setup-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin: 8px 0; }
.sector-setup-table th { text-align: left; padding: 6px 9px; color: #6b7280; font-weight: 500;
    border-bottom: 1px solid #e5e7eb; background: #fff; }
.sector-setup-table td { padding: 6px 9px; border-bottom: 1px solid #f3f4f6; color: #111827; }
.sector-setup-table tr:hover td { background: #fff; }
.sector-setup-footer { margin: 8px 0 0; font-size: 0.78rem; color: #6b7280; }
.sector-setup-footer a { color: #2563eb; text-decoration: none; }
"""


def render_sector_setup_slack(summary: dict | None) -> str:
    """Slack mrkdwn block — returns "" when nothing to surface."""
    if not summary or not summary.get("buckets"):
        return ""
    regime = summary.get("regime", "?")
    advice = summary.get("regime_advice", "")
    lines = [f"📊 *Sector Setup This Week* — regime: `{regime}`", advice]
    for bucket, rows in summary["buckets"].items():
        emoji, hint = SLACK_BUCKET_HINTS.get(bucket, ("", ""))
        tickers = " ".join(f"`{r.get('ticker')}`" for r in rows)
        label = bucket
        lines.append(f"{emoji} {label}: {tickers} — {hint}")
    return "\n".join(lines)
