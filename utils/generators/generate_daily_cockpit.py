#!/usr/bin/env python3
"""
Daily Cockpit — one decision-first morning pane (data/daily.html).

Spec: docs/specs/daily-cockpit.md. Replaces the daily chart "firehose" with a
single page that answers, in order: what headspace am I in (discipline banner) ·
can I trade today (gate) · what do I do with what I hold (book) · which names clear
every gate (qualified) · what am I stalking (radar: top-5 entry-ready + top-5 focus)
· where's money flowing (leadership: real 5d flow, then screenable bases) ·
am I improving (record).

Built around THIS user's documented leaks (round-tripping winners, hold-in-hope on
losers, over-trading weak tapes): every block binds an abstract principle to a live
number. Light theme only (memory/feedback_light_theme.md), plain-English
(memory/feedback_plain_english_dashboards.md).

Book pulls LIVE from SnapTrade (reusing position_monitor.fetch_positions +
generate_live_portfolio helpers), falling back to data/positions.json.

Pure render_*/decision fns are import-safe (no API keys) so they unit-test cleanly;
loaders + write_page do the IO.
"""

import csv
import datetime
import glob
import json
import logging
import os

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "daily.html")

# Realistic + stretch equity targets (memory/user_equity_curve_goal, user_discipline_password)
EC_REALISTIC = 150_000
EC_STRETCH = 200_000

# ---------------------------------------------------------------- formatters

def _money(v) -> str:
    v = float(v or 0)
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}"


def _pct(v) -> str:
    return f"{float(v or 0):+.1f}%"


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- pure decisions

# state -> base posture
_NO_NEW = {"RED", "DANGER", "BLACKOUT", "EXTENDED", "COOLING"}
_HALF = {"CAUTION", "STEADY-UPTREND"}
_FULL = {"GREEN", "THRUST", "TREND-FOLLOW"}
# ETF regimes that force risk-off regardless of breadth state
_REGIME_RISK_OFF = {"blow-off-risk", "late-rotation"}


def effective_cap(state: str) -> int:
    if state in {"GREEN", "THRUST", "TREND-FOLLOW"}:
        return 10
    if state in {"CAUTION", "STEADY-UPTREND"}:
        return 7
    return 5


def gate_decision(market_state: str, regime: str, sizing_mode: str) -> dict:
    """The day's risk verdict. Pure. Returns action/color/headline/detail/cap."""
    state = (market_state or "RED").upper()
    regime = (regime or "").lower()
    mode = (sizing_mode or "normal").lower()

    if state in _FULL:
        action, color = "FULL SIZE", "green"
    elif state in _HALF:
        action, color = "HALF SIZE", "amber"
    else:
        action, color = "NO NEW ENTRIES", "red"

    detail = []
    # ETF regime overlay can only tighten, never loosen
    if regime in _REGIME_RISK_OFF and action != "NO NEW ENTRIES":
        action, color = "NO NEW ENTRIES", "red"
        detail.append(f"ETF regime {regime} overrides — risk-off")
    elif regime in _REGIME_RISK_OFF:
        detail.append(f"ETF regime {regime}")

    # sizing-mode overlay (consecutive-loss discipline)
    if mode == "suspended":
        action, color = "PAPER ONLY", "red"
        detail.append("3+ losses — system suspended to paper")
    elif mode == "reduced" and action == "FULL SIZE":
        action, color = "HALF SIZE", "amber"
        detail.append("reduced mode (2 losses) caps size")
    elif mode == "reduced":
        detail.append("reduced mode — max 5% size")

    headline = {
        "FULL SIZE": "Green light — full size on qualified setups.",
        "HALF SIZE": "Half size only. Build the watchlist, be selective.",
        "NO NEW ENTRIES": "Gate closed. Manage the book — do not add risk.",
        "PAPER ONLY": "Paper only. Step away from live risk.",
    }[action]

    return {
        "state": state, "regime": regime, "action": action, "color": color,
        "headline": headline, "detail": " · ".join(detail), "cap": effective_cap(state),
    }


def discipline_line(ts: dict, gate: dict) -> str:
    """One sentence tying live state to the right headspace."""
    mode = (ts.get("current_sizing_mode") or "normal").lower()
    losses = int(ts.get("consecutive_losses") or 0)
    wins = int(ts.get("consecutive_wins") or 0)
    if gate["action"] in ("NO NEW ENTRIES", "PAPER ONLY"):
        return "Capital preservation mode — today, NOT trading is the winning trade. Cash is a position."
    if mode == "reduced" or losses >= 2:
        return f"{losses} losses deep — size down, demand A+ setups only. The next trade does not have to make it back."
    if wins >= 2 and gate["action"] == "FULL SIZE":
        return "On a winning streak in a green tape — press the winners, but let them run. Don't round-trip."
    return "Follow the process. Patience on entries, conviction on size, scale winners into strength."


def tier_peel_warn(atr: float) -> float:
    if atr <= 4:
        return 3.0
    if atr <= 7:
        return 5.0
    if atr <= 10:
        return 6.5
    return 8.5


def qualify_setups(rows: list, held: set, top_n: int = 3) -> list:
    """Ready-to-Enter gate over screener CSV rows (CLAUDE.md criteria).
    Pure: rows are dicts with the screener CSV columns. Returns trade-plan cards."""
    held = {t.upper() for t in (held or set())}
    out = []
    for r in rows:
        tk = (r.get("Ticker") or "").upper()
        if not tk or tk in held:
            continue
        q = _f(r.get("Quality Score"))
        atr = _f(r.get("ATR%"))
        dist = _f(r.get("Dist From High%"))
        rvol = _f(r.get("Rel Volume"))
        vcp = _f(r.get("VCP"))
        s20 = _f(r.get("SMA20%"))
        s50 = _f(r.get("SMA50%"))
        s200 = _f(r.get("SMA200%"))
        # Stage 2 perfect proxy from SMA distances: 50MA above 200MA, price above 50 & 20
        stage_perfect = (s200 > s50) and (s50 > 0) and (s20 > 0)
        peel_safe = atr > 0 and (s50 / atr) <= tier_peel_warn(atr)
        if not (stage_perfect and q >= 80 and atr <= 7 and -12 <= dist <= -1
                and rvol <= 1.2 and vcp >= 70 and peel_safe):
            continue
        price = _f(r.get("_price"))  # optional; CSV has no price col, left blank
        stop_pct = max(8.0, 2 * atr)  # -8% MAE floor, widened for volatile names
        out.append({
            "ticker": tk, "q": q, "atr": atr, "dist": dist, "rvol": rvol,
            "vcp": vcp, "s20": s20, "stop_pct": stop_pct,
            "company": r.get("Company", ""), "sector": r.get("Sector", ""),
            "price": price,
        })
    out.sort(key=lambda x: -x["q"])
    return out[:top_n]


def record_stats(ts: dict, closed: list) -> dict:
    """Win-rate, streak, avg win vs avg loss — the metric that proves the EC math."""
    tw = int(ts.get("total_wins") or 0)
    tl = int(ts.get("total_losses") or 0)
    total = tw + tl
    win_rate = (tw / total * 100) if total else 0.0
    wins = [_f(c.get("result_pct")) for c in (closed or []) if _f(c.get("result_pct")) > 0]
    losses = [_f(c.get("result_pct")) for c in (closed or []) if _f(c.get("result_pct")) < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else 0.0
    return {
        "total_wins": tw, "total_losses": tl, "win_rate": win_rate,
        "consecutive_wins": int(ts.get("consecutive_wins") or 0),
        "consecutive_losses": int(ts.get("consecutive_losses") or 0),
        "sizing_mode": ts.get("current_sizing_mode") or "normal",
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
    }


# ---------------------------------------------------------------- loaders

def _load_json(name, default=None):
    p = os.path.join(DATA_DIR, name)
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def load_latest_market() -> dict:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "market_monitor_2*.json")))
    if files:
        try:
            with open(files[-1]) as f:
                return json.load(f)
        except Exception:
            pass
    return _load_json("trading_state.json", {}) or {}


def load_screener_rows() -> list:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "finviz_screeners_2*.csv")))
    if not files:
        return []
    try:
        with open(files[-1]) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def load_watchlist() -> list:
    w = _load_json("watchlist.json", [])
    if isinstance(w, dict):
        for v in w.values():
            if isinstance(v, list):
                return v
        return []
    return w or []


# ---------------------------------------------------------------- block renderers (pure)

def render_banner(ts: dict, gate: dict, equity: float) -> str:
    line = discipline_line(ts, gate)
    mode = (ts.get("current_sizing_mode") or "normal").upper()
    w, l = int(ts.get("consecutive_wins") or 0), int(ts.get("consecutive_losses") or 0)
    streak = f"{w}W streak" if w else (f"{l}L streak" if l else "flat streak")
    goal_pct = (equity / EC_REALISTIC * 100) if equity > 0 else 0
    goal = (f"{_money(equity)} · {goal_pct:.0f}% of {_money(EC_REALISTIC)} goal"
            if equity > 0 else f"goal {_money(EC_REALISTIC)} (stretch {_money(EC_STRETCH)})")
    return (
        "<div class='banner'>"
        f"<div class='banner-line'>{line}</div>"
        f"<div class='banner-meta'>{mode} mode · {streak} · {goal}</div>"
        "</div>"
    )


def render_gate(gate: dict, market: dict) -> str:
    spy = _f(market.get("spy_close") or market.get("spy_price"))
    spy50 = _f(market.get("spy_sma50_pct"))
    asof = market.get("date", "")
    sub = f"SPY {spy:.0f} ({_pct(spy50)} vs 50MA)" if spy else ""
    detail = f"<div class='gate-detail'>{gate['detail']}</div>" if gate["detail"] else ""
    return (
        f"<div class='gate gate-{gate['color']}'>"
        f"<div class='gate-top'><span class='gate-state'>{gate['state']}</span>"
        f"<span class='gate-action'>{gate['action']}</span></div>"
        f"<div class='gate-headline'>{gate['headline']}</div>"
        f"{detail}"
        f"<div class='gate-foot'>Position cap {gate['cap']} · {sub} · as of {asof}</div>"
        "</div>"
    )


def _verdict(gain, atr, s20, stage):
    from utils.generators.generate_live_portfolio import verdict_for
    return verdict_for(gain, atr, s20, stage)


def render_book(rows: list, account: dict) -> str:
    if not rows:
        return ("<div class='empty'>0 positions — 100% cash. "
                "Nothing to manage; wait for the gate to open.</div>")
    rows = sorted(rows, key=lambda r: -r.get("mv", 0))
    equity = _f(account.get("equity"))
    body = ""
    cut = scale = 0
    for r in rows:
        gain = r["gain"]
        v = _verdict(gain, r["atr"], r["s20"], r["stage"])
        if gain <= -5:
            cut += 1
        if gain >= 20:
            scale += 1
        heat = "neg" if gain < 0 else ("pos" if gain > 0 else "zero")
        pctbk = (r["mv"] / equity * 100) if equity > 0 else 0
        body += (
            "<tr>"
            f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={r['ticker']}' target='_blank'>{r['ticker']}</a></td>"
            f"<td class='mono'>${r['live']:.2f}</td>"
            f"<td class='mono heat-{heat}'>{_pct(gain)}</td>"
            f"<td class='mono heat-{heat}'>{_money(r['pl'])}</td>"
            f"<td class='mono'>{pctbk:.0f}%</td>"
            f"<td class='mono'>{r['atr']:.1f}</td>"
            f"<td class='mono'>{r['stage']}</td>"
            f"<td>{v}</td>"
            "</tr>"
        )
    flags = []
    if cut:
        flags.append(f"<span class='flag flag-cut'>🚨 {cut} CUT — held past stop, name the pattern</span>")
    if scale:
        flags.append(f"<span class='flag flag-scale'>💰 {scale} SCALE ½ — lock it, don't round-trip</span>")
    flagbar = f"<div class='flagbar'>{' '.join(flags)}</div>" if flags else ""
    return (
        f"{flagbar}"
        "<table class='tbl'><thead><tr><th>TKR</th><th>Now</th><th>P/L%</th>"
        "<th>$P/L</th><th>%Bk</th><th>ATR%</th><th>St</th><th>Verdict</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_qualified(cards: list, gate: dict) -> str:
    gate_open = gate["action"] in ("FULL SIZE", "HALF SIZE")
    if not cards:
        return ("<div class='empty win'>0 setups clear every gate today. "
                "Nothing meets the standard — that's patience, not a miss.</div>")
    note = "" if gate_open else ("<div class='watchonly'>⚠ Gate is closed — "
                                 "these are WATCH-ONLY, not buys today.</div>")
    size = "Full" if gate["action"] == "FULL SIZE" else ("Half" if gate["action"] == "HALF SIZE" else "No-new (watch)")
    body = ""
    for c in cards:
        body += (
            "<div class='card'>"
            f"<div class='card-h'><a href='https://finviz.com/quote.ashx?t={c['ticker']}' target='_blank'>{c['ticker']}</a>"
            f"<span class='card-q'>Q{c['q']:.0f}</span></div>"
            f"<div class='card-sub'>{c['company']} · {c['sector']}</div>"
            "<div class='card-plan'>"
            f"<span>Stop <b>-{c['stop_pct']:.0f}%</b></span>"
            f"<span>Size <b>{size}</b></span>"
            f"<span>ATR {c['atr']:.1f}%</span>"
            f"<span>dist {c['dist']:.0f}%</span>"
            f"<span>VCP {c['vcp']:.0f}</span>"
            "</div></div>"
        )
    return note + f"<div class='cards'>{body}</div>"


def radar_trigger(s20: float, dist: float, price: float) -> str:
    """One-line action per radar name. Branch order: at the EMA beats everything
    (that IS the entry); near the high the pivot is the trigger; extended above
    the EMA means wait for the pullback level; below it means wait for reclaim."""
    if abs(s20) <= 1.5:
        return "at 21 EMA — buy the hold"
    if dist >= -3:
        pivot = price / (1 + dist / 100) if price > 0 else 0
        return f"pivot ~${pivot:,.2f}" if pivot else "at pivot — buy the break of the 52w high"
    if s20 > 1.5:
        ema = price / (1 + s20 / 100) if price > 0 else 0
        return f"pullback to ~${ema:,.2f}" if ema else "extended — wait for the 21 EMA pullback"
    return "below 21 EMA — wait for reclaim"


def radar_pick(wl: list, screener_rows: list, top_n: int = 5) -> dict:
    """Top-N entry-ready + focus for the cockpit (spec: cockpit-radar-revamp §2).
    Pure. Archived rows excluded (zombie defense); watching tier deliberately
    absent — it lives in the gallery watchlist, not the morning pane. Ranked by
    (proximity-to-trigger = |SMA20%| asc, Q desc); names missing from today's
    screener rank last (proximity 99)."""
    idx = {}
    for r in screener_rows or []:
        tk = (r.get("Ticker") or "").upper()
        if tk:
            idx[tk] = r
    out = {"entry-ready": [], "focus": []}
    for it in wl or []:
        tier = it.get("priority")
        if tier not in out or it.get("status") == "archived":
            continue
        tk = (it.get("ticker") or "").upper()
        row = idx.get(tk)
        if row is None:
            out[tier].append({"ticker": tk, "q": 0.0, "s20": None, "dist": None,
                              "prox": 99.0, "trigger": "no fresh screener data — check chart"})
            continue
        s20 = _f(row.get("SMA20%"))
        out[tier].append({
            "ticker": tk, "q": _f(row.get("Quality Score")), "s20": s20,
            "dist": _f(row.get("Dist From High%")), "prox": abs(s20),
            "trigger": radar_trigger(s20, _f(row.get("Dist From High%")), _f(row.get("Price"))),
        })
    for tier in out:
        out[tier].sort(key=lambda x: (x["prox"], -x["q"]))
        out[tier] = out[tier][:top_n]
    return out


def render_radar(radar: dict, gate: dict) -> str:
    gate_open = gate["action"] in ("FULL SIZE", "HALF SIZE")
    note = "" if gate_open else ("<div class='watchonly'>⚠ Gate is closed — "
                                 "radar is WATCH-ONLY today, not a shopping list.</div>")
    if not (radar.get("entry-ready") or radar.get("focus")):
        return ("<div class='empty'>Radar empty — entry-ready and focus tiers have no "
                "active names. They populate from the next screener run.</div>")

    def table(names):
        if not names:
            return "<div class='empty'>None right now.</div>"
        body = ""
        for n in names:
            s20 = _pct(n["s20"]) if n["s20"] is not None else "—"
            dist = _pct(n["dist"]) if n["dist"] is not None else "—"
            q = f"{n['q']:.0f}" if n["q"] else "—"
            body += (
                "<tr>"
                f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={n['ticker']}' target='_blank'>{n['ticker']}</a></td>"
                f"<td class='mono'>{q}</td>"
                f"<td class='mono'>{s20}</td>"
                f"<td class='mono'>{dist}</td>"
                f"<td>{n['trigger']}</td>"
                "</tr>"
            )
        return ("<table class='tbl'><thead><tr><th>TKR</th><th>Q</th><th>21EMA</th>"
                "<th>vs High</th><th>Trigger</th></tr></thead>"
                f"<tbody>{body}</tbody></table>")

    grid = (
        f"<div class='radar-grid{'' if gate_open else ' radar-closed'}'>"
        f"<div><div class='radar-h'>🎯 Top 5 Entry-Ready</div>{table(radar.get('entry-ready'))}</div>"
        f"<div><div class='radar-h'>🔭 Top 5 Focus</div>{table(radar.get('focus'))}</div>"
        "</div>"
    )
    return note + grid


def leadership_flows(rotation: dict) -> dict:
    """Pure split of etf_rotation.json into real 5d FLOW vs screenable structure
    (spec: cockpit-radar-revamp §3 — structure ≠ flow; the old §5 sold the BASE
    bucket as 'where money is flowing' and listed RS-22 laggards as leadership)."""
    etfs = (rotation or {}).get("etfs") or []
    flowing_in = sorted(
        (e for e in etfs
         if _f(e.get("rank_delta_5d")) < 0 and _f(e.get("rs_score")) >= 50),
        key=lambda e: (_f(e.get("rank_delta_5d")), -_f(e.get("rs_score"))),
    )[:5]
    flowing_out = sorted(
        (e for e in etfs if _f(e.get("rank_delta_5d")) > 0),
        key=lambda e: -_f(e.get("rank_delta_5d")),
    )[:3]
    bases = [
        e for e in etfs
        if e.get("bucket") == "BASE"
        and (_f(e.get("rs_score")) >= 50 or _f(e.get("rank_delta_5d")) < 0)
    ]
    scores = [_f(e.get("rs_score")) for e in etfs]
    spread = (max(scores) - min(scores)) if scores else 0.0
    leaders = sorted(etfs, key=lambda e: -_f(e.get("rs_score")))[:2]
    return {"flowing_in": flowing_in, "flowing_out": flowing_out, "bases": bases,
            "spread": spread, "spread_wide": spread >= 60, "leaders": leaders}


def _delta_word(e: dict) -> str:
    d = int(_f(e.get("rank_delta_5d")))
    return f"up {-d}" if d < 0 else (f"down {d}" if d > 0 else "—")


def render_leadership(rotation: dict) -> str:
    """Flow first, structure second. Full metrics table lives in etf_rotation.html."""
    etfs = (rotation or {}).get("etfs") or []
    if not etfs:
        return "<div class='empty'>Sector rotation data unavailable.</div>"
    fl = leadership_flows(rotation)
    regime = rotation.get("regime", "")
    try:
        from agents.utils.etf_rotation_summary import REGIME_ADVICE
        advice = REGIME_ADVICE.get(regime, "")
    except Exception:
        advice = ""

    def line(e):
        return (f"<span class='lchip lchip-flow' title='{e.get('name', '')}'>"
                f"{e.get('ticker', '?')} · {_delta_word(e)} · RS {int(_f(e.get('rs_score')))}"
                f" · {e.get('bucket', '—')}</span>")

    body = ""
    if fl["flowing_in"]:
        chips = " ".join(line(e) for e in fl["flowing_in"])
        body += ("<div class='lead-group lead-in'><div class='lead-label'>💸 Money flowing IN (5d)"
                 " <span class='lead-meaning'>climbing the RS ranks with strength already ≥50 — screen these groups first</span></div>"
                 f"<div class='lead-chips'>{chips}</div></div>")
    else:
        body += ("<div class='lead-group lead-in'><div class='lead-label'>💸 Money flowing IN (5d)</div>"
                 "<div class='lead-meaning'>No group is both climbing and strong right now.</div></div>")
    if fl["flowing_out"]:
        out_line = " · ".join(f"{e.get('ticker', '?')} {_delta_word(e)}" for e in fl["flowing_out"])
        body += (f"<div class='lead-group lead-out'><div class='lead-label'>💨 Flowing OUT "
                 f"<span class='lead-meaning'>{out_line}</span></div></div>")
    if fl["bases"]:
        chips = " ".join(
            f"<span class='lchip lchip-base' title='{e.get('name', '')}'>"
            f"{e.get('ticker', '?')} · RS {int(_f(e.get('rs_score')))} · {_delta_word(e)}</span>"
            for e in fl["bases"]
        )
        body += ("<div class='lead-group lead-base'><div class='lead-label'>🎯 Bases worth screening"
                 " <span class='lead-meaning'>tight structure AND holding/gaining relative strength — weak bases are dropped</span></div>"
                 f"<div class='lead-chips'>{chips}</div></div>")

    spread_note = ""
    if fl["spread_wide"] and fl["leaders"]:
        tops = " and ".join(
            f"{e.get('ticker', '?')} (RS {int(_f(e.get('rs_score')))})" for e in fl["leaders"])
        spread_note = (f" …but the RS spread is wide ({fl['spread']:.0f} pts top to bottom) — "
                       f"leaders exist: {tops}.")
    head = (f"<div class='lead-regime'>Regime: <b>{regime}</b>. {advice}{spread_note}</div>"
            if regime else "")
    foot = "<div class='lead-foot'><a href='etf_rotation.html'>Full rotation dashboard →</a></div>"
    return body + head + foot


def render_record(stats: dict) -> str:
    payoff = stats["payoff"]
    payoff_ok = payoff >= 2.0
    math_note = ("✅ winners outrun losers — the math works"
                 if payoff_ok else "⚠ winners must be bigger — tighten cuts / let winners run")
    return (
        "<div class='stat-grid'>"
        f"<div class='stat'><div class='stat-l'>Win rate</div><div class='stat-v'>{stats['win_rate']:.0f}%</div>"
        f"<div class='stat-s'>{stats['total_wins']}W / {stats['total_losses']}L</div></div>"
        f"<div class='stat'><div class='stat-l'>Avg win vs loss</div>"
        f"<div class='stat-v'>{_pct(stats['avg_win'])} / {_pct(stats['avg_loss'])}</div>"
        f"<div class='stat-s'>payoff {payoff:.1f}× — {math_note}</div></div>"
        f"<div class='stat'><div class='stat-l'>Sizing mode</div><div class='stat-v'>{stats['sizing_mode'].upper()}</div>"
        f"<div class='stat-s'>{stats['consecutive_wins']}W / {stats['consecutive_losses']}L streak</div></div>"
        "</div>"
    )


# ---------------------------------------------------------------- page assembly

COCKPIT_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8f9fc;color:#111827;padding:28px;max-width:1100px;margin:0 auto}
h1{font-size:1.5rem;font-weight:800}
.sub{color:#6b7280;font-size:.8rem;margin-bottom:18px}
.block{margin:22px 0}
.block-h{font-size:.72rem;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.09em;margin-bottom:10px}
.banner{background:#111827;color:#f9fafb;border-radius:12px;padding:18px 20px}
.banner-line{font-size:1.02rem;font-weight:600}
.banner-meta{font-size:.78rem;color:#9ca3af;margin-top:6px}
.gate{border-radius:12px;padding:18px 20px;color:#fff}
.gate-green{background:#15803d}.gate-amber{background:#b45309}.gate-red{background:#b91c1c}
.gate-top{display:flex;justify-content:space-between;align-items:baseline}
.gate-state{font-size:.8rem;font-weight:700;opacity:.85;letter-spacing:.05em}
.gate-action{font-size:1.5rem;font-weight:800}
.gate-headline{font-size:.95rem;margin-top:6px}
.gate-detail{font-size:.8rem;opacity:.9;margin-top:6px}
.gate-foot{font-size:.74rem;opacity:.8;margin-top:10px}
.tbl{width:100%;border-collapse:collapse;font-size:.82rem;background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden}
.tbl th{text-align:left;padding:9px 11px;color:#6b7280;font-weight:500;border-bottom:1px solid #e5e7eb;text-transform:uppercase;font-size:.64rem;letter-spacing:.05em;background:#f9fafb}
.tbl td{padding:9px 11px;border-bottom:1px solid #f3f4f6}
.tbl tr:last-child td{border-bottom:none}
.bold{font-weight:700}.mono{font-variant-numeric:tabular-nums}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
.heat-pos{color:#15803d;font-weight:600}.heat-neg{color:#b91c1c;font-weight:600}.heat-zero{color:#6b7280}
.flagbar{margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap}
.flag{font-size:.78rem;font-weight:600;padding:6px 10px;border-radius:8px}
.flag-cut{background:#fee2e2;color:#991b1b}.flag-scale{background:#fef3c7;color:#92400e}
.empty{color:#6b7280;font-size:.86rem;padding:18px;text-align:center;background:#fff;border:1px dashed #e5e7eb;border-radius:10px}
.empty.win{color:#15803d;border-color:#bbf7d0;background:#f0fdf4}
.watchonly{font-size:.8rem;color:#92400e;background:#fef3c7;padding:8px 12px;border-radius:8px;margin-bottom:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px}
.card-h{display:flex;justify-content:space-between;font-weight:800;font-size:1.05rem}
.card-q{color:#15803d}
.card-sub{font-size:.74rem;color:#6b7280;margin:4px 0 10px}
.card-plan{display:flex;flex-wrap:wrap;gap:10px;font-size:.76rem;color:#374151}
.radar-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}
.radar-closed{opacity:.55}
.radar-h{font-size:.78rem;font-weight:700;color:#374151;margin-bottom:8px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.stat{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px}
.stat-l{font-size:.66rem;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em}
.stat-v{font-size:1.3rem;font-weight:800;margin-top:4px}
.stat-s{font-size:.72rem;color:#6b7280;margin-top:4px}
.lead-regime{font-size:.84rem;color:#374151;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;margin-bottom:12px;line-height:1.45}
.lead-group{background:#fff;border:1px solid #e5e7eb;border-left-width:4px;border-radius:10px;padding:12px 14px;margin-bottom:10px}
.lead-in{border-left-color:#2563eb}.lead-out{border-left-color:#dc2626}.lead-base{border-left-color:#16a34a}
.lead-label{font-size:.82rem;font-weight:700;color:#111827;margin-bottom:9px}
.lead-meaning{font-weight:400;color:#6b7280;font-size:.76rem}
.lead-chips{display:flex;flex-wrap:wrap;gap:7px}
.lchip{display:inline-block;border-radius:6px;padding:4px 10px;font-size:.78rem;font-weight:700}
.lchip-flow{background:#dbeafe;color:#1d4ed8}.lchip-base{background:#dcfce7;color:#15803d}
.lead-foot{font-size:.74rem;margin-top:6px}
.footer{margin-top:30px;font-size:.7rem;color:#9ca3af}
"""


def render_page(ctx: dict) -> str:
    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%a %d %b %Y · %H:%M UTC")
    gate = ctx["gate"]

    def block(title, html):
        return f"<div class='block'><div class='block-h'>{title}</div>{html}</div>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>Daily Cockpit</title><style>{COCKPIT_CSS}</style></head><body>
<h1>☀️ Daily Cockpit</h1>
<p class="sub">One pane, one decision · {updated}</p>
{render_banner(ctx['trading_state'], gate, ctx['equity'])}
{block("🚦 1 · The Gate — can I trade today?", render_gate(gate, ctx['market']))}
{block("📓 2 · The Book — what do I do with what I hold?", render_book(ctx['book_rows'], ctx['account']))}
{block("🎯 3 · Qualified Today — names that clear every gate", render_qualified(ctx['qualified'], gate))}
{block("🎯 4 · Radar — top 5 entry-ready · top 5 focus", render_radar(ctx['radar'], gate))}
{block("🗺 5 · Leadership — where money is flowing", render_leadership(ctx['rotation']))}
{block("📊 6 · The Record — am I improving?", render_record(ctx['record']))}
<div class="footer">Decision-first daily routine · book = live SnapTrade · gate = market state + ETF regime + sizing mode · spec: docs/specs/daily-cockpit.md</div>
</body></html>"""


# ---------------------------------------------------------------- orchestrator

def _live_book():
    """(rows, account) from SnapTrade; falls back to positions.json on failure
    OR when SnapTrade quietly returns no positions (missing creds / auth error
    logs an ERROR but returns [] — without this the cockpit renders a false
    '100% cash' book). A genuinely flat book is safe here: the monitor
    auto-closes gone positions, so positions.json empties out too."""
    try:
        from agents.trading.position_monitor import fetch_positions
        from utils.generators.generate_live_portfolio import build_row, _fetch_account_balances
        positions = fetch_positions() or []
        if positions:
            account = _fetch_account_balances() or {}
            return [build_row(p) for p in positions], account
        log.warning("SnapTrade returned no positions — falling back to positions.json")
    except Exception as e:
        log.warning("live book failed (%s) — falling back to positions.json", e)
    pj = _load_json("positions.json", {}) or {}
    rows = []
    for p in pj.get("open_positions", []):
        avg = _f(p.get("avg_cost") or p.get("entry_price"))
        live = _f(p.get("current_price") or p.get("highest_price_seen") or avg)
        shares = _f(p.get("shares"))
        gain = ((live - avg) / avg * 100) if avg else 0
        rows.append({
            "ticker": p.get("ticker"), "shares": shares, "avg": avg, "live": live,
            "gain": gain, "pl": (live - avg) * shares, "mv": live * shares,
            "atr": _f(p.get("atr_pct")), "s20": 0.0, "stage": "?",
        })
    return rows, {}


def build_context() -> dict:
    market = load_latest_market()
    ts = _load_json("trading_state.json", {}) or {}
    rotation = _load_json("etf_rotation.json", {}) or {}
    positions_file = _load_json("positions.json", {}) or {}

    book_rows, account = _live_book()
    held = {r.get("ticker", "").upper() for r in book_rows}

    market_state = market.get("market_state") or ts.get("market_state") or "RED"
    regime = rotation.get("regime", "")
    gate = gate_decision(market_state, regime, ts.get("current_sizing_mode"))

    screener_rows = load_screener_rows()
    qualified = qualify_setups(screener_rows, held)
    radar = radar_pick(load_watchlist(), screener_rows)

    equity = _f(account.get("equity"))
    record = record_stats(ts, positions_file.get("closed_positions", []))

    return {
        "market": market, "trading_state": ts, "rotation": rotation, "gate": gate,
        "book_rows": book_rows, "account": account, "equity": equity,
        "qualified": qualified, "radar": radar, "record": record,
    }


def write_page() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        html = render_page(build_context())
    except Exception as e:
        log.warning("cockpit render failed: %s", e)
        html = (f"<!DOCTYPE html><html><body style='font-family:sans-serif;padding:40px'>"
                f"<h1>☀️ Daily Cockpit</h1><p>Refresh failed: {e}</p></body></html>")
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("daily.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    write_page()
