"""Weekly Review §2 — Week-Ahead Shortlist (REPLACES the old Top 5).

The heart of the forward-looking rebuild. Instead of ranking by appearance
frequency (rear-view — shows last week's already-extended winners), this sources
from the FORWARD funnel and emits full trade-plan cards:

    Setup · Trigger · Stop · Size · Invalidation

Funnel sources (deduped by ticker):
  1. emerging candidates   — select_emerging_candidates() (the one good
                              forward idea in the old weekly), source='emerging'
  2. entry-ready watchlist  — watchlist.json rows priority='entry-ready',
                              status active, source='entry-ready'
  3. new / reacquired RS    — rs_leaders.json active/reacquired & recent,
     leaders                  source='rs-leader'

Each candidate is enriched with CURRENT metrics from the latest daily screener
CSV (Q / ATR% / SMA20% / SMA50% / dist-from-high / RS / Stage), gated for
Stage 2 + peel-safe, then turned into a deterministic trade-plan card. An
optional AI pass writes terse Setup/Invalidation prose per name.

Stop default −8% — the MAE-derived floor (2024-25 winners' MAE median −4.8%,
mean −10.5%; −8% respects normal winner drawdown without cutting the median
winner). Widened to 2×ATR% for volatile names. See data/mae_analysis.json.

Pure fns + html/slack renderers. Light theme only. Plain English.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# MAE-derived default stop floor (see data/mae_analysis.json / Feature D task 1).
DEFAULT_STOP_PCT = -8.0

# Regime → base position size for new entries. Mirrors the executor's market
# gate / effective_max_positions policy (alpaca_executor.py).
_SIZE_BY_STATE = {
    "GREEN": "Full", "THRUST": "Full", "TREND-FOLLOW": "Full",
    "CAUTION": "Half", "COOLING": "Half", "STEADY-UPTREND": "Half",
    "EXTENDED": "No new entries", "RED": "No new entries",
    "DANGER": "No new entries", "BLACKOUT": "No new entries",
}

# Lower source-rank = higher priority on ties (Q is the primary sort key).
_SOURCE_RANK = {"entry-ready": 0, "emerging": 1, "rs-leader": 2}
_SOURCE_LABEL = {
    "entry-ready": "entry-ready",
    "emerging": "emerging",
    "rs-leader": "RS leader",
}


def _f(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_stage2(stage_label) -> bool:
    if not isinstance(stage_label, str):
        return False
    s = stage_label.lower()
    return ("uptrend" in s or "stage 2" in s or "stage2" in s
            or s.startswith("2"))


def _parse_stage(raw) -> dict:
    """Normalize the screener Stage value into {stage, perfect, label}.

    The daily CSV `Stage` column is a serialized compute_stage() dict
    (e.g. "{'stage': 2, 'perfect': True, ...}"), but daily_quality and tests
    use plain word labels ("Stage 2 (Uptrend)"). Handle both."""
    d = None
    if isinstance(raw, dict):
        d = raw
    elif isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            import ast
            d = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            d = None
    if isinstance(d, dict):
        try:
            st = int(d.get("stage"))
        except (TypeError, ValueError):
            st = None
        perfect = bool(d.get("perfect"))
        if st is not None:
            label = f"Stage {st}{' perfect' if perfect else ''}"
        else:
            label = str(raw)
        return {"stage": st, "perfect": perfect, "label": label}
    s = str(raw)
    return {"stage": 2 if _is_stage2(s) else None, "perfect": False, "label": s}


# ----------------------------
# Funnel selection
# ----------------------------

def _gather_source_tickers(emerging_df, watchlist: dict, rs_leaders: dict,
                           today: str, recent_days: int) -> dict:
    """Collect {ticker: source} from the three forward funnels.

    First source to claim a ticker wins (entry-ready > emerging > rs-leader by
    iteration order below)."""
    out: dict[str, str] = {}

    # 1. entry-ready watchlist (highest-priority setup tier)
    for row in (watchlist or {}).get("watchlist", []) or []:
        if row.get("priority") != "entry-ready":
            continue
        if str(row.get("status", "")).lower() in ("archived", "closed", "removed"):
            continue
        t = str(row.get("ticker", "")).strip().upper()
        if t:
            out.setdefault(t, "entry-ready")

    # 2. emerging candidates
    if emerging_df is not None and len(emerging_df) > 0:
        for t in emerging_df["Ticker"].tolist():
            t = str(t).strip().upper()
            if t:
                out.setdefault(t, "emerging")

    # 3. RS leaders — active/reacquired and recently active
    cutoff = _date_minus(today, recent_days)
    for t, meta in (rs_leaders or {}).items():
        status = str(meta.get("current_status", "")).lower()
        if status not in ("active", "reacquired"):
            continue
        last = str(meta.get("last_active_date", "") or "")[:10]
        if last and last < cutoff:
            continue
        tk = str(t).strip().upper()
        if tk:
            out.setdefault(tk, "rs-leader")

    return out


def _date_minus(iso_date: str, days: int) -> str:
    import datetime
    try:
        d = datetime.date.fromisoformat(iso_date[:10])
    except (ValueError, TypeError):
        d = datetime.date.today()
    return (d - datetime.timedelta(days=days)).isoformat()


def select_shortlist_candidates(emerging_df, watchlist: dict, rs_leaders: dict,
                                held: set, daily_lookup: dict, today: str,
                                peel_warn_fn=None, max_n: int = 8,
                                recent_days: int = 7) -> list:
    """Build the ranked shortlist candidate list from the forward funnel.

    daily_lookup: {TICKER: dict-of-current-metrics} from the latest screener
                  CSV (keys: Quality Score, ATR%, SMA20%, SMA50%,
                  Dist From High%, RS Rating, Stage, Sector, Company).
    held:         set of currently-held tickers (excluded — new entries only).
    peel_warn_fn: callable(ticker, atr_pct) -> warn multiple. Defaults to the
                  screener's _peel_warn_for. Injected for testing.

    Returns up to max_n candidate dicts ranked by Quality Score (desc), gated
    for Stage 2 + peel-safe. Names absent from today's screener are skipped —
    a trustworthy plan needs current metrics.
    """
    if peel_warn_fn is None:
        from agents.screener.finviz_agent import _peel_warn_for as peel_warn_fn

    held = {str(h).strip().upper() for h in (held or set())}
    sources = _gather_source_tickers(emerging_df, watchlist, rs_leaders,
                                     today, recent_days)

    candidates = []
    for ticker, source in sources.items():
        if ticker in held:
            continue
        row = daily_lookup.get(ticker)
        if not row:
            continue  # no current metrics → no trustworthy plan

        atr = _f(row.get("ATR%"))
        sma50 = _f(row.get("SMA50%"))
        sma20 = _f(row.get("SMA20%"))
        q = _f(row.get("Quality Score"))
        dist52 = _f(row.get("Dist From High%"))
        rs = _f(row.get("RS Rating"))
        parsed_stage = _parse_stage(row.get("Stage", "—"))
        stage = parsed_stage["label"]

        if parsed_stage["stage"] != 2:
            continue
        if atr <= 0 or atr > 12:
            continue
        warn = _f(peel_warn_fn(ticker, atr), default=0.0)
        peel_mult = (sma50 / atr) if atr > 0 else 0.0
        if warn > 0 and peel_mult > warn:
            continue  # extended past peel-warn — chasing risk

        candidates.append({
            "ticker": ticker,
            "source": source,
            "source_label": _SOURCE_LABEL.get(source, source),
            "company": row.get("Company", ""),
            "sector": row.get("Sector", ""),
            "q": q,
            "atr_pct": round(atr, 1),
            "sma20_pct": round(sma20, 1),
            "sma50_pct": round(sma50, 1),
            "dist52": round(dist52, 1),
            "rs": int(rs) if rs else 0,
            "stage": stage,
            "stage_perfect": parsed_stage["perfect"],
            "peel_mult": round(peel_mult, 1),
            "peel_warn": round(warn, 1),
        })

    candidates.sort(key=lambda c: (-c["q"], _SOURCE_RANK.get(c["source"], 9)))
    return candidates[:max_n]


# ----------------------------
# Trade-plan card builder
# ----------------------------

def _size_for(market_state: str, atr_pct: float) -> str:
    base = _SIZE_BY_STATE.get((market_state or "").upper(), "Half")
    if base == "No new entries":
        return base
    if atr_pct > 7:
        return {"Full": "Half (high vol)", "Half": "¼ (high vol)"}.get(base, base)
    return base


def _trigger_for(sma20_pct: float, atr_pct: float) -> str:
    if sma20_pct < -1.0:
        return "Reclaim & hold above the 21 EMA (SMA20) on RVol ≥ 1.2"
    if sma20_pct <= 3.0:
        return "Holding the 21 EMA — add on a push to a new high on volume"
    return ("Extended above the 21 EMA — wait for a pullback to the 21 EMA. "
            "Chasing here is the round-trip risk")


def _stop_for(atr_pct: float, default_stop_pct: float) -> dict:
    pct = -max(abs(default_stop_pct), round(2 * atr_pct, 1))
    return {
        "pct": pct,
        "text": (f"{pct:.0f}% (−8% MAE floor / 2×ATR, whichever is wider) · "
                 "structural: any close below the 50 SMA"),
    }


def build_trade_plan_card(cand: dict, market_state: str,
                          default_stop_pct: float = DEFAULT_STOP_PCT) -> dict:
    """Turn an enriched candidate into a deterministic trade-plan card.

    Card fields: setup / trigger / stop / size / invalidation. The setup line
    is a deterministic baseline; an optional AI pass can overwrite `setup` and
    `invalidation` with terser prose (enrich_shortlist_notes_ai)."""
    atr = cand["atr_pct"]
    size = _size_for(market_state, atr)
    trigger = _trigger_for(cand["sma20_pct"], atr)
    stop = _stop_for(atr, default_stop_pct)

    setup = (
        f"{cand['source_label'].capitalize()} · {cand['stage']} · "
        f"{cand['sector'] or 'n/a'} · Q{int(cand['q'])} RS{cand['rs']} · "
        f"ATR {atr:.1f}% · {cand['dist52']:+.0f}% from 52w high"
    )

    invalidation = (
        "Two daily closes below the 21 EMA, or any close below the 50 SMA — "
        "thesis broken, exit."
    )

    return {
        **cand,
        "setup": setup,
        "trigger": trigger,
        "stop": stop["text"],
        "stop_pct": stop["pct"],
        "size": size,
        "invalidation": invalidation,
    }


def build_shortlist_cards(candidates: list, market_state: str,
                          default_stop_pct: float = DEFAULT_STOP_PCT) -> list:
    return [build_trade_plan_card(c, market_state, default_stop_pct)
            for c in candidates]


# ----------------------------
# Optional AI enrichment (terse Setup / Invalidation prose)
# ----------------------------

def build_ai_notes_prompt(cards: list, market_state: str) -> str:
    """Build a single batched prompt asking for one-line setup + invalidation
    per ticker. Factored out so the prompt is unit-testable without network."""
    lines = [
        f"Market regime: {market_state}. For each momentum-stock setup below, "
        "write ONE terse line for SETUP (what the chart/fundamental story is) "
        "and ONE for INVALIDATION (the price action that kills the thesis). "
        "Max ~15 words each. No fluff, no disclaimers. Format exactly:\n"
        "TICKER | setup: ... | invalidation: ...\n",
    ]
    for c in cards:
        lines.append(
            f"{c['ticker']} ({c['sector']}) Q{int(c['q'])} RS{c['rs']} "
            f"{c['stage']} ATR{c['atr_pct']:.1f}% dist {c['dist52']:+.0f}% "
            f"SMA20 {c['sma20_pct']:+.0f}% source={c['source_label']}"
        )
    return "\n".join(lines)


def _parse_ai_notes(text: str) -> dict:
    """Parse 'TICKER | setup: ... | invalidation: ...' lines into a dict."""
    out: dict[str, dict] = {}
    for raw in (text or "").splitlines():
        if "|" not in raw or "setup:" not in raw.lower():
            continue
        parts = [p.strip() for p in raw.split("|")]
        ticker = parts[0].strip().upper()
        note = {}
        for p in parts[1:]:
            low = p.lower()
            if low.startswith("setup:"):
                note["setup"] = p.split(":", 1)[1].strip()
            elif low.startswith("invalidation:"):
                note["invalidation"] = p.split(":", 1)[1].strip()
        if ticker and note:
            out[ticker] = note
    return out


def enrich_shortlist_notes_ai(cards: list, market_state: str,
                              api_key: str = "", post_fn=None) -> None:
    """Overwrite setup/invalidation with terse AI prose. Non-fatal — leaves the
    deterministic card untouched on any failure / missing key. Mutates cards."""
    if not cards or not api_key:
        return
    prompt = build_ai_notes_prompt(cards, market_state)
    try:
        if post_fn is None:
            import requests
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 700,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60,
            )
            if not resp.ok:
                log.warning("Shortlist AI notes HTTP %s", resp.status_code)
                return
            text = resp.json()["content"][0]["text"]
        else:
            text = post_fn(prompt)
        notes = _parse_ai_notes(text)
        for c in cards:
            n = notes.get(c["ticker"])
            if n:
                if n.get("setup"):
                    c["setup_note"] = n["setup"]
                if n.get("invalidation"):
                    c["invalidation"] = n["invalidation"]
    except Exception as e:  # never block the weekly
        log.warning("Shortlist AI enrichment failed (non-fatal): %s", e)


# ----------------------------
# HTML render (light theme)
# ----------------------------

SHORTLIST_CSS = """
.wsl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px,1fr));
            gap: 14px; margin-bottom: 28px; }
.wsl-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px;
            padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,.04);
            border-top: 3px solid #2563eb; }
.wsl-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.wsl-ticker { font-size: 1.15rem; font-weight: 700; color: #2563eb; text-decoration: none; }
.wsl-ticker:hover { color: #1d4ed8; }
.wsl-src { font-size: 0.62rem; background: #eff6ff; color: #2563eb; padding: 1px 6px;
           border-radius: 3px; font-weight: 600; }
.wsl-company { font-size: 0.68rem; color: #6b7280; margin-bottom: 10px; }
.wsl-row { font-size: 0.78rem; color: #374151; line-height: 1.5; margin-bottom: 5px; }
.wsl-row b { color: #111827; display: inline-block; min-width: 86px; }
.wsl-setup { color: #6b7280; }
.wsl-size-warn { color: #b45309; font-weight: 600; }
"""


def render_shortlist_html(cards: list) -> str:
    """Render §2 Week-Ahead Shortlist as light-theme trade-plan cards."""
    if not cards:
        return (
            "<h2>🎯 Week-Ahead Shortlist</h2>"
            "<p class='lb-note'>No qualifying setups in the forward funnel this "
            "week — nothing in the entry-ready / emerging / RS-leader lanes "
            "passed the Stage 2 + peel-safe gate. Cash is a position.</p>"
        )
    card_html = ""
    for c in cards:
        setup_line = c.get("setup_note") or c["setup"]
        size = c["size"]
        size_cls = "wsl-size-warn" if ("high vol" in size or size == "No new entries") else ""
        fv = f"https://finviz.com/quote.ashx?t={c['ticker']}"
        card_html += (
            "<div class='wsl-card'>"
            "<div class='wsl-head'>"
            f"<a href='{fv}' target='_blank' class='wsl-ticker'>{c['ticker']}</a>"
            f"<span class='wsl-src'>{c['source_label']}</span>"
            f"<span class='wsl-company'>{c['company']}</span>"
            "</div>"
            f"<div class='wsl-row wsl-setup'><b>Setup</b>{setup_line}</div>"
            f"<div class='wsl-row'><b>Trigger</b>{c['trigger']}</div>"
            f"<div class='wsl-row'><b>Stop</b>{c['stop']}</div>"
            f"<div class='wsl-row'><b>Size</b><span class='{size_cls}'>{size}</span></div>"
            f"<div class='wsl-row'><b>Invalidation</b>{c['invalidation']}</div>"
            "</div>"
        )
    return (
        "<h2>🎯 Week-Ahead Shortlist <span class='h2-sub'>— what to do next week</span></h2>"
        "<p class='lb-note'>Forward funnel (entry-ready · emerging · RS leaders), "
        "Stage 2 + peel-safe, ranked by Quality Score. Each is a full plan: "
        "trigger, stop, size, invalidation. Stop floor −8% (MAE-derived).</p>"
        "<div class='wsl-grid'>" + card_html + "</div>"
    )


# ----------------------------
# Slack render
# ----------------------------

def render_shortlist_slack(cards: list) -> str:
    """Render §2 as Slack mrkdwn. Empty string when no cards."""
    if not cards:
        return ("🎯 *Week-Ahead Shortlist*\n_No qualifying setups in the forward "
                "funnel this week — nothing passed Stage 2 + peel-safe. Cash is a "
                "position._")
    lines = ["🎯 *Week-Ahead Shortlist* — what to do next week"]
    for c in cards:
        setup_line = c.get("setup_note") or (
            f"{c['source_label']} · {c['stage']} · Q{int(c['q'])} RS{c['rs']} · "
            f"ATR {c['atr_pct']:.1f}% · {c['dist52']:+.0f}% from high"
        )
        lines.append(
            f"\n*{c['ticker']}* ({c['sector']})\n"
            f" • _{setup_line}_\n"
            f" • *Trigger:* {c['trigger']}\n"
            f" • *Stop:* {c['stop']}\n"
            f" • *Size:* {c['size']}  ·  *Invalidation:* {c['invalidation']}"
        )
    return "\n".join(lines)
