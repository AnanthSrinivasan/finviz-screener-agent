# Phase 1 Spec — RS Leader Signal (stock-level, persistent tracker)

**Status:** approved 2026-05-06, ready to execute
**Goal:** catch DOCN-class setups at the entry zone (Apr 2-8 type), persist them across pullbacks (Apr 10-15 type), and re-alert when they reacquire the setup (Apr 21 type).

## Why

DOCN was visible in our screener 6+ times from Apr 2 to May 4 but never triggered any signal. Root cause:
- Single-screener appearance (52w High only) → never hit persistence threshold
- VCP confidence stayed low (15) → blocked Ready-to-Enter (needs ≥70)
- Already at +3% to +4% above 52w high by the time Q≥80 → blocked Fresh Breakout dist gate

The miss is a **system design gap**, not a model intelligence gap. We need a signal that triggers on stock-level relative strength, not multi-screener persistence.

The system also throws candidates away during pullbacks (DOCN dropped out Apr 8-15) and has no concept of "reacquired setup." A persistent tracker fixes that.

## Why NO market_state gate

The user's correct objection: when leaders fly, our broad-breadth-driven `market_state` lags (calls RED while QLD rips). Gating the signal on `market_state ∈ {RED, COOLING, CAUTION, DANGER}` would have skipped DOCN on Apr 8 (THRUST) and during the GREEN run that followed. **A Stage 2 perfect, peel-safe stock with rising MA stack and Q ≥ 75 IS the relative strength signal — independent of how we've classified the regime.** We log `trigger_state` for analytics only.

## Part A — Daily detection predicate

New pure function in `agents/screener/finviz_agent.py`, placed next to `_is_fresh_breakout` (around [finviz_agent.py:1603](../agents/screener/finviz_agent.py#L1603)).

```python
def _is_rs_leader_candidate(row, open_positions_tickers: set) -> bool:
    """
    Pure predicate for RS Leader candidate detection. Stock-level only — no
    market state dependency. Catches DOCN Apr 6 type (Q=84, dist -4.9%, peel-safe).
    """
```

| Filter | Threshold | DOCN Apr 6 |
|---|---|---|
| Stage | 2 perfect | ✅ |
| Q | ≥ 75 | 84 ✅ |
| Dist 52w high | [-10%, +2%] | -4.9% ✅ |
| MA stack | SMA20% > 0 AND SMA50% > 0 AND SMA200% > 0 | 10.4 / 29.0 / >0 ✅ |
| ATR% | ≤ 8 | 7.1 ✅ |
| Peel-safe | `SMA50%/ATR% ≤ peel_warn(ticker, atr)` | 4.1 ≤ 6.5 ✅ |
| RVol | ≤ 1.5 | 0.78 ✅ |
| Sector | NOT in {Utilities, Energy, REIT, Consumer Defensive, Basic Materials} | Tech ✅ |
| Not held (real or paper) | — | — |

`peel_warn` reuses existing `_peel_warn_for(ticker, atr)` from finviz_agent.py.

Dist band `[-10%, +2%]` covers both pullback (DOCN Apr 6 at -4.9%) AND right-at-highs-quiet (DOCN Apr 2 at -0.8% though Q=69 misses Q≥75 there — Apr 6 is the cleaner trigger).

## Part B — Persistent tracker

New state file `data/rs_leaders.json`. Schema:

```json
{
  "DOCN": {
    "first_triggered": "2026-04-06",
    "trigger_state": "RED",
    "trigger_q": 84,
    "trigger_dist": -4.9,
    "trigger_atr_mult": 4.1,
    "current_status": "active",
    "last_active_date": "2026-04-06",
    "pullback_started": null,
    "reacquired_dates": [],
    "days_tracked": 1
  }
}
```

Daily lifecycle (run after Part A evaluates each day):

| Today's predicate | Prior status | New status | Slack action |
|---|---|---|---|
| ✅ trigger | not in state | `active` | 🛡️ NEW RS Leader |
| ✅ trigger | `active` | `active` (no-op, increment days_tracked) | nothing |
| ❌ no trigger | `active` | `pulling_back` (set `pullback_started=today`) | 📉 RS Leader pulling back |
| ❌ no trigger | `pulling_back` (≤14 days) | `pulling_back` | nothing |
| ✅ trigger | `pulling_back` | `reacquired` (append to `reacquired_dates`) | 🛡️ RS Leader REACQUIRED — clean re-entry |
| ❌ no trigger | `pulling_back` (>14 days) | drop entry | nothing |
| any | `reacquired` for >1 day | promote back to `active` | nothing |

**DOCN trace under this design:**
- Apr 6: NEW RS Leader (Q=84, peel-safe at -5%) → 🚨 ALERT
- Apr 7-8: stays active (no-op)
- Apr 9-15: drops out of screener (pullback) → status `pulling_back`, daily silent
- Apr 21: reappears Q=79, peel-safe → 🚨 REACQUIRED ALERT (the second entry shot)

## Wire-up — 5 touch points

1. **Predicate** `_is_rs_leader_candidate` at [finviz_agent.py:1603](../agents/screener/finviz_agent.py#L1603) area
2. **Tracker** `data/rs_leaders.json` + lifecycle helper functions in `agents/screener/finviz_agent.py`:
   - `_load_rs_leaders_state()`
   - `_update_rs_leaders_state(triggered_today: list[dict], today: str) -> dict[str, str]` (returns per-ticker action: `new` / `reacquired` / `pulling_back` / `aged_out` / `noop`)
3. **Slack block** in `_format_message` after Fresh Breakout (around [finviz_agent.py:1308](../agents/screener/finviz_agent.py#L1308)):
   - `🛡️ NEW RS Leader` — top 5 by Q from `new` actions today
   - `🛡️ RS Leader REACQUIRED` — all reacquired today
   - `📉 RS Leader pulling back` — all that just transitioned to `pulling_back` today
   - Each line: ticker, Q, dist, ATR mult, RVol, `/stock-research <ticker>` prompt
4. **Watchlist auto-add** in `_update_watchlist` (around [finviz_agent.py:1957](../agents/screener/finviz_agent.py#L1957)) — at first trigger:
   - priority=`focus` (one tier above default `watching` — RS Leader earned more attention than typical screener-auto)
   - source=`rs_leader_auto`
   - On reacquired: re-promote to `focus` if previously aged out
5. **Chart gallery** section in HTML build — labeled `🛡️ Relative Strength Leaders` with status badges (NEW / REACQUIRED). Pulling-back names omitted from gallery (would be daily noise).

## Tests — `tests/test_screener_signals.py` additions

- `test_rs_leader_docn_apr6_trigger` — DOCN Apr 6 row triggers
- `test_rs_leader_held_ticker_skipped` — ticker in open positions returns False
- `test_rs_leader_peel_extended_skipped` — peel-warn extended ticker returns False
- `test_rs_leader_sector_blacklist` — utility/REIT/etc. returns False even if other criteria pass
- `test_rs_leader_dist_above_2pct_skipped` — dist > +2% returns False
- `test_rs_leader_dist_below_neg10_skipped` — dist < -10% returns False
- `test_rs_leader_state_new_to_active` — first-day trigger creates `active` entry
- `test_rs_leader_state_active_to_pulling_back` — drop in screener flips status
- `test_rs_leader_state_reacquired` — re-trigger after pullback
- `test_rs_leader_aged_out_after_14_days` — pullback >14 days drops entry
- `test_rs_leader_market_state_logged_not_gated` — trigger fires regardless of regime; trigger_state field captured for analytics

## Approved decisions

1. **Q threshold:** 75 (catches DOCN Apr 6 at Q=84; Apr 2-3 at Q=69 missed but Apr 6 is the cleaner setup)
2. **Pullback grace:** 14 days max before drop
3. **Watchlist tier:** `focus` with `/stock-research <ticker>` prompt in Slack alert

## Out of scope for Phase 1

- Numeric RS Rating (computed metric vs SPY) — covered in Phase 2 spec
- "RS Hardened" badge for tickers persistent across multiple weak sessions — stretch goal, deferred

## Execution checklist

- [ ] `_is_rs_leader_candidate` + helper state functions
- [ ] `data/rs_leaders.json` schema + lifecycle
- [ ] Slack block (3 sub-blocks: NEW / REACQUIRED / pulling back)
- [ ] Watchlist auto-add at `focus` tier
- [ ] Chart gallery section
- [ ] 11 unit tests
- [ ] Update CLAUDE.md and SYSTEM_DOCS.md
- [ ] Run full unittest suite locally
- [ ] Run `daily-finviz.yml` workflow on GH Actions, verify logs
- [ ] Commit + push
