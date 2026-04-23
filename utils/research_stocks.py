#!/usr/bin/env python3
"""
Stock Research Utility — utils/research_stocks.py

Deep-researches tickers using Claude API + web_search. Produces a scored
conviction HTML report saved to data/stock_research_YYYY-MM-DD.html.

Usage:
    python utils/research_stocks.py LITE BE CORZ
    python utils/research_stocks.py --slack LITE CRWV   # also post to Slack

Proactive trigger (from daily screener):
    Called automatically by finviz_agent.py when a ticker qualifies as
    Hidden Growth (4+/6 criteria) — see finviz_agent._score_hidden_growth.
    Criteria: persistence (3+ days), strong TTM EPS, strong Q/Q EPS,
    institutional buying, Stage 2 perfect, IPO lifecycle.
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL  = "https://api.anthropic.com/v1/messages"
SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("research_stocks")

TODAY = datetime.date.today().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Market context loader
# ---------------------------------------------------------------------------
def load_market_context() -> dict:
    ctx = {"state": "UNKNOWN", "sizing": "normal", "held": []}
    try:
        if os.path.exists("data/trading_state.json"):
            with open("data/trading_state.json") as f:
                ts = json.load(f)
            ctx["state"]  = ts.get("market_state", "UNKNOWN")
            ctx["sizing"] = ts.get("sizing_mode", "normal")
    except Exception:
        pass
    try:
        if os.path.exists("data/positions.json"):
            with open("data/positions.json") as f:
                pos = json.load(f)
            ctx["held"] = [p["ticker"] for p in pos.get("open_positions", [])]
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# Claude API research call (web_search tool)
# ---------------------------------------------------------------------------
RESEARCH_PROMPT = """\
You are a senior research analyst for a momentum trader using Minervini + Weinstein rules.
Research {ticker} for a deep conviction report. Use web search extensively. Be specific and data-driven — no fluff, no disclaimers.

Market context: {market_state} market, sizing mode {sizing}. {held_note}

Answer ALL sections below. For each, cite actual numbers found in search results.

1. EARNINGS TREND (last 4 quarters):
   - For each of the last 4 quarters: EPS actual vs estimate, % beat/miss, revenue actual vs estimate.
   - Is EPS accelerating or decelerating quarter over quarter? Give the trajectory in numbers.
   - Does management consistently sandbag guidance? (i.e., guide low, print high)

2. FORWARD ESTIMATES (next 2-4 quarters + annual):
   - What are analyst consensus EPS estimates for the next 2-4 quarters?
   - Full-year EPS estimate for current FY and next FY?
   - Has the consensus estimate been revised UP or DOWN in the last 30-60 days? By how much?
   - What is management's own long-term EPS/revenue target (if provided)?

3. REVENUE TRAJECTORY:
   - Revenue growth rate last 4 quarters (YoY %). Is growth accelerating or decelerating?
   - Sequential (Q/Q) revenue growth trend?
   - Forward revenue estimates for next FY?

4. INSTITUTIONAL CYCLE:
   - Current institutional ownership %? Fund count — is it increasing or decreasing?
   - Name 2-3 specific major funds and their recent moves (new position / increased / decreased)?
   - Is this in institutional adoption phase (funds initiating) or distribution phase (funds exiting)?

5. IPO / SPIN-OFF CYCLE (critical for CRWV, CORZ, LUNR, SNDK-type — Hidden Growth names):
   - When did the stock IPO or spin off? How many months ago?
   - Has the lock-up period expired? (typically 90-180 days post-IPO)
   - How many standalone earnings reports have been filed as a public company?
   - Phase classification: Hot IPO (0-3mo) / Lock-up pressure (3-6mo) / Orphan (6-18mo) / Institutional adoption (12-36mo) / Mature (36mo+)
   - Is this stock actionable under Minervini rules yet, or is it still in a pre-actionable phase?

6. TAM + PRODUCT CYCLE (where is this in the S-curve?):
   - Total addressable market size now and in 3 years?
   - What % of TAM does this company currently capture?
   - Is the company riding an early S-curve (fast growth ahead) or late S-curve (growth slowing)?
   - Key product or technology cycle driving this — is it early innings or mature?

7. SHORT INTEREST + NEXT CATALYST:
   - Short interest as % of float? Trending up (more bearish bets) or down?
   - Are any credible short sellers (Kerrisdale, Muddy Waters, Hindenburg, etc.) publicly short? What is their thesis?
   - Next earnings date?
   - Any other near-term catalyst: investor day, product launch, contract, index inclusion?

8. BEAR CASE — be explicit, do not skip this:
   - What is the single biggest risk that could make this investment fail?
   - Debt load: total debt, interest rate, interest expense as % of revenue. Is it serviceable?
   - Customer concentration: top 1-3 customers as % of revenue. What happens if one leaves?
   - Competitive threats: who is building the same thing and could commoditize this?
   - Insider selling: what did insiders do at the lock-up, at peaks, recently?
   - What does the bear case say about valuation vs realistic earnings power?

9. HIDDEN GROWTH / TTM DISTORTION CHECK:
   - Is EPS Y/Y TTM distorted (negative or misleading) because of spin-off, IPO, or character change?
   - What does EPS Q/Q show that TTM hides?
   - Would a momentum screener using only TTM EPS undervalue this stock significantly?

10. VERDICT — balanced, not promotional:
    - Bull case in one sentence with specific numbers.
    - Bear case in one sentence with specific numbers.
    - IPO phase: is this stock in an actionable Minervini phase? Yes / No / Watch
    - Forward conviction on NEXT 2 earnings: BETTER / WORSE / UNCERTAIN — why?
    - Final rating: HIGH / MODERATE / LOW / SKIP with the ONE deciding factor (positive or negative).

"""


def research_ticker(ticker: str, market_ctx: dict) -> dict:
    """Call Claude API with web_search to research one ticker. Returns structured dict."""
    held_note = f"NOTE: {ticker} is already held in portfolio." if ticker in market_ctx.get("held", []) else ""

    prompt = RESEARCH_PROMPT.format(
        ticker=ticker,
        market_state=market_ctx.get("state", "UNKNOWN"),
        sizing=market_ctx.get("sizing", "normal"),
        held_note=held_note,
    )

    for attempt in range(3):
        try:
            resp = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1200,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            if resp.status_code in (429, 529):
                wait = 30 * (attempt + 1)
                log.warning(f"{ticker}: rate limited, retrying in {wait}s")
                time.sleep(wait)
                continue
            if not resp.ok:
                log.error(f"{ticker}: API error {resp.status_code}: {resp.text[:200]}")
                return {"ticker": ticker, "error": f"HTTP {resp.status_code}"}

            blocks = resp.json().get("content", [])
            text = " ".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text")).strip()
            log.info(f"{ticker}: research complete ({len(text)} chars)")
            return {"ticker": ticker, "raw": text, "error": None}

        except Exception as e:
            log.error(f"{ticker}: unexpected error — {e}")
            if attempt == 2:
                return {"ticker": ticker, "error": str(e)}
            time.sleep(5)

    return {"ticker": ticker, "error": "max retries exceeded"}


# ---------------------------------------------------------------------------
# Parse raw research text into structured fields
# ---------------------------------------------------------------------------
def parse_research(raw: str) -> dict:
    """
    Extract section answers from numbered research text.
    Returns dict with keys: eps, revenue, institutional, technical, catalyst, sndk_risk, verdict.
    """
    import re
    sections = {
        "eps":           "",
        "revenue":       "",
        "institutional": "",
        "technical":     "",
        "catalyst":      "",
        "sndk_risk":     "",
        "verdict":       "",
    }
    labels = [
        (r"1\.\s*EARNINGS Q/Q", "eps"),
        (r"2\.\s*REVENUE",      "revenue"),
        (r"3\.\s*INSTITUTIONAL","institutional"),
        (r"4\.\s*TECHNICAL",    "technical"),
        (r"5\.\s*CATALYST",     "catalyst"),
        (r"6\.\s*SNDK RISK",    "sndk_risk"),
        (r"7\.\s*VERDICT",      "verdict"),
    ]
    for i, (pattern, key) in enumerate(labels):
        next_pattern = labels[i + 1][0] if i + 1 < len(labels) else None
        if next_pattern:
            m = re.search(f"{pattern}[:\s]*(.*?)(?={next_pattern})", raw, re.DOTALL | re.IGNORECASE)
        else:
            m = re.search(f"{pattern}[:\s]*(.*?)$", raw, re.DOTALL | re.IGNORECASE)
        if m:
            sections[key] = m.group(1).strip()

    # Extract conviction from verdict
    verdict_upper = sections["verdict"].upper()
    if "HIGH" in verdict_upper:
        sections["conviction"] = "HIGH"
    elif "MODERATE" in verdict_upper:
        sections["conviction"] = "MODERATE"
    elif "LOW" in verdict_upper:
        sections["conviction"] = "LOW"
    else:
        sections["conviction"] = "SKIP"

    # Detect SNDK pattern
    sndk_upper = sections["sndk_risk"].upper()
    sections["is_sndk"] = any(w in sndk_upper for w in ["YES", "IPO", "SPIN-OFF", "SPINOFF", "CHARACTER CHANGE", "DISTORTED"])

    return sections


# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------
CONVICTION_COLORS = {
    "HIGH":     ("#16a34a", "#dcfce7"),
    "MODERATE": ("#d97706", "#fef9c3"),
    "LOW":      ("#9ca3af", "#f3f4f6"),
    "SKIP":     ("#dc2626", "#fee2e2"),
}

MARKET_COLORS = {
    "GREEN":    "#16a34a",
    "THRUST":   "#16a34a",
    "CAUTION":  "#d97706",
    "COOLING":  "#d97706",
    "RED":      "#dc2626",
    "DANGER":   "#dc2626",
    "BLACKOUT": "#6b7280",
    "UNKNOWN":  "#6b7280",
}


MARKET_ENTRY_NOTE = {
    "GREEN":    ("✅ GREEN — Full size entries allowed.", "#f0fdf4", "#16a34a"),
    "THRUST":   ("✅ THRUST — Full size entries allowed. Act fast.", "#f0fdf4", "#16a34a"),
    "CAUTION":  ("⚠️ CAUTION — Half size only. Build watchlist.", "#fffbeb", "#d97706"),
    "COOLING":  ("⚠️ COOLING — Tighten stops, no new entries.", "#fffbeb", "#d97706"),
    "RED":      ("🚫 RED — Rule 6: No new entries.", "#fef2f2", "#dc2626"),
    "DANGER":   ("🚫 DANGER — Raise stops immediately. No entries.", "#fef2f2", "#dc2626"),
    "BLACKOUT": ("🚫 BLACKOUT (Sep) — No new entries.", "#f3f4f6", "#6b7280"),
}

def _ticker_card(ticker: str, parsed: dict, is_held: bool, market_state: str = "UNKNOWN") -> str:
    conviction = parsed.get("conviction", "SKIP")
    fg, bg = CONVICTION_COLORS.get(conviction, ("#6b7280", "#f3f4f6"))
    sndk_badge = (
        '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:12px;'
        'font-size:11px;font-weight:600;margin-left:8px;">⚡ HIDDEN GROWTH</span>'
        if parsed.get("is_sndk") else ""
    )
    held_badge = (
        '<span style="background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:12px;'
        'font-size:11px;font-weight:600;margin-left:8px;">HELD</span>'
        if is_held else ""
    )

    def row(label: str, content: str) -> str:
        content_safe = content.replace("<", "&lt;").replace(">", "&gt;") if content else "—"
        return (
            f'<tr><td style="width:130px;font-weight:600;color:#6b7280;vertical-align:top;'
            f'padding:6px 12px 6px 0;font-size:13px;">{label}</td>'
            f'<td style="padding:6px 0;font-size:13px;color:#111827;line-height:1.5">{content_safe}</td></tr>'
        )

    return f"""
<div style="background:#fff;border-radius:12px;border:1px solid #e5e7eb;margin-bottom:20px;overflow:hidden;">
  <div style="background:{bg};border-bottom:2px solid {fg};padding:14px 20px;display:flex;align-items:center;justify-content:space-between;">
    <div>
      <span style="font-size:22px;font-weight:700;color:{fg};">${ticker}</span>
      {sndk_badge}{held_badge}
    </div>
    <span style="background:{fg};color:#fff;padding:4px 14px;border-radius:20px;font-weight:700;font-size:13px;">{conviction}</span>
  </div>
  <div style="padding:16px 20px;">
    <table style="width:100%;border-collapse:collapse;">
      {row("Earnings Q/Q", parsed.get("eps",""))}
      {row("Revenue", parsed.get("revenue",""))}
      {row("Fwd Estimates", parsed.get("forward_estimates",""))}
      {row("Institutional", parsed.get("institutional",""))}
      {row("IPO Phase", parsed.get("ipo_phase",""))}
      {row("TAM / S-curve", parsed.get("tam",""))}
      {row("Technical", parsed.get("technical",""))}
      {row("Short / Catalyst", parsed.get("catalyst",""))}
      {row("Hidden Growth", parsed.get("sndk_risk",""))}
      {row("Verdict", parsed.get("verdict",""))}
    </table>
    {_market_entry_note(market_state)}
  </div>
</div>"""


def _market_entry_note(market_state: str) -> str:
    note, bg, color = MARKET_ENTRY_NOTE.get(
        market_state, ("Market state unknown — check trading_state.json.", "#f3f4f6", "#6b7280")
    )
    return (
        f'<div style="background:{bg};border-radius:8px;padding:12px 16px;margin-top:14px;'
        f'font-size:12px;font-weight:600;color:{color};">{note}</div>'
    )


def generate_html(results: list, market_ctx: dict) -> str:
    market_state = market_ctx.get("state", "UNKNOWN")
    market_color = MARKET_COLORS.get(market_state, "#6b7280")
    sizing       = market_ctx.get("sizing", "normal")
    held         = market_ctx.get("held", [])

    # Summary table rows
    summary_rows = []
    for r in results:
        ticker   = r["ticker"]
        parsed   = r.get("parsed", {})
        conv     = parsed.get("conviction", "SKIP")
        fg, bg   = CONVICTION_COLORS.get(conv, ("#6b7280", "#f3f4f6"))
        sndk     = "⚡ Yes" if parsed.get("is_sndk") else "—"
        verdict  = parsed.get("verdict", "")[:120] + ("…" if len(parsed.get("verdict","")) > 120 else "")
        verdict_safe = verdict.replace("<","&lt;").replace(">","&gt;")
        held_str = " (HELD)" if ticker in held else ""
        summary_rows.append(
            f'<tr>'
            f'<td style="font-weight:700;color:{fg};padding:8px 12px;">${ticker}{held_str}</td>'
            f'<td style="padding:8px 12px;"><span style="background:{bg};color:{fg};padding:2px 10px;border-radius:10px;font-weight:600;font-size:12px;">{conv}</span></td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#92400e;">{sndk}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#374151;">{verdict_safe}</td>'
            f'</tr>'
        )

    ticker_cards = ""
    for r in results:
        if r.get("error"):
            ticker_cards += f'<div style="background:#fee2e2;border-radius:8px;padding:16px;margin-bottom:16px;color:#dc2626;"><b>${r["ticker"]}</b> — Research failed: {r["error"]}</div>'
        else:
            ticker_cards += _ticker_card(r["ticker"], r.get("parsed", {}), r["ticker"] in held, market_state)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Research — {TODAY}</title>
<style>
  body {{ font-family:system-ui,sans-serif; background:#f9fafb; color:#111827; margin:0; padding:24px; }}
  h1 {{ font-size:22px; font-weight:700; color:#111827; margin:0 0 4px; }}
  .subtitle {{ font-size:14px; color:#6b7280; margin-bottom:24px; }}
  table.summary {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; border:1px solid #e5e7eb; margin-bottom:28px; }}
  table.summary th {{ background:#f3f4f6; color:#374151; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; padding:10px 12px; text-align:left; }}
  table.summary tr:not(:last-child) td {{ border-bottom:1px solid #f3f4f6; }}
</style>
</head>
<body>
<h1>Stock Research Report — {TODAY}</h1>
<div class="subtitle">
  Market: <b style="color:{market_color}">{market_state}</b> &nbsp;·&nbsp;
  Sizing: <b>{sizing}</b> &nbsp;·&nbsp;
  Generated: {datetime.datetime.now().strftime("%H:%M ET")}
</div>

<table class="summary">
  <thead>
    <tr>
      <th>Ticker</th><th>Conviction</th><th>Hidden Growth</th><th>Key Reason</th>
    </tr>
  </thead>
  <tbody>
    {"".join(summary_rows)}
  </tbody>
</table>

{ticker_cards}

<div style="font-size:11px;color:#9ca3af;margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;">
  Generated by utils/research_stocks.py · {TODAY} · Minervini/Weinstein framework
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Slack summary (optional)
# ---------------------------------------------------------------------------
def send_slack_summary(results: list, market_ctx: dict, html_path: str):
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack post.")
        return

    lines = []
    for r in results:
        parsed = r.get("parsed", {})
        conv   = parsed.get("conviction", "SKIP")
        emoji  = {"HIGH": "🟢", "MODERATE": "🟡", "LOW": "⚪", "SKIP": "🔴"}.get(conv, "⚪")
        sndk   = " ⚡HG" if parsed.get("is_sndk") else ""
        verdict_short = parsed.get("verdict", "")[:100]
        lines.append(f"{emoji} *${r['ticker']}* {conv}{sndk} — {verdict_short}")

    market_state = market_ctx.get("state", "UNKNOWN")
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📊 Stock Research — {TODAY}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"Market: *{market_state}* | Sizing: *{market_ctx.get('sizing','normal')}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ]
    }

    pages_base = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    if pages_base and html_path:
        fname = os.path.basename(html_path)
        payload["blocks"].append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{pages_base}/data/{fname}|Open full report>"}
        })

    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.ok:
            log.info("Slack summary sent.")
        else:
            log.error(f"Slack failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Slack error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(tickers: list, post_slack: bool = False) -> str:
    """
    Research tickers, generate HTML, return path to saved report.
    Can be imported and called from finviz_agent.py for proactive research.
    """
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — cannot run research.")
        return ""

    market_ctx = load_market_context()
    log.info(f"Market: {market_ctx['state']} | Sizing: {market_ctx['sizing']} | Held: {market_ctx['held']}")

    results = []
    # Sequential to avoid rate limits; each call uses up to 5 web searches
    for ticker in tickers:
        log.info(f"Researching {ticker}...")
        r = research_ticker(ticker.upper(), market_ctx)
        if r.get("raw"):
            r["parsed"] = parse_research(r["raw"])
        else:
            r["parsed"] = {}
        results.append(r)
        time.sleep(2)  # polite gap between API calls

    html = generate_html(results, market_ctx)
    os.makedirs("data", exist_ok=True)
    out_path = f"data/stock_research_{TODAY}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Report saved: {out_path}")

    # Console summary table
    print(f"\n{'='*70}")
    print(f"STOCK RESEARCH — {TODAY}  |  Market: {market_ctx['state']}  |  Sizing: {market_ctx['sizing']}")
    print(f"{'='*70}")
    print(f"{'Ticker':<8} {'Conviction':<12} {'SNDK':<6} Key reason")
    print(f"{'-'*70}")
    for r in results:
        parsed = r.get("parsed", {})
        conv   = parsed.get("conviction", "SKIP")
        sndk   = "⚡ YES" if parsed.get("is_sndk") else "—"
        v      = parsed.get("verdict", r.get("error", "error"))[:60]
        print(f"${r['ticker']:<7} {conv:<12} {sndk:<6} {v}")
    print(f"{'='*70}")
    print(f"HTML report: {out_path}\n")

    if post_slack:
        send_slack_summary(results, market_ctx, out_path)

    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research stocks using Claude API + web_search")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols to research")
    parser.add_argument("--slack", action="store_true", help="Post summary to Slack")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    run(args.tickers, post_slack=args.slack)
