# Spec — Entry-Readiness Signals & Watchlist Lifecycle

**Status:** awaiting review
**Date:** 2026-04-23
**Context:** MU hit a textbook Stage 2 + VCP + pullback setup starting Apr 15 (Q=100, VCP conf 70→85, -3% to -5% from high) and was never called out as actionable. NVTS hit a Stage 2 + Power Move + institutional buying setup on Apr 16 (Q=103, Inst +11%, 3 screens) and also slipped through. The data was there both times; the system had no signal tuned to surface either setup class. MU additionally should have been auto-promoted `watching → focus` on Apr 15/20/21 and wasn't, due to a watchlist-lifecycle bug.

---

## Problem summary

1. **"SNDK pattern" signal is misnamed and mis-gated.** It's a narrow IPO/spin-off EPS-blind-spot detector but its label implies a general pattern. One of its six criteria (`ttm_distorted`) excludes *clean* TTM growers — which is backwards. It also only scans `filter_df` (post 10%-from-high gate), so NVTS-style deep-base breakouts are filtered out before scoring.
2. **No "Ready to Enter" callout exists.** Top picks are dumped into a generic Q-sorted list. The actionable VCP/pullback setups (MU-today type) are not pulled out.
3. **Watchlist lifecycle churns high-quality entries.** 14-day `age_out` flips `status="archived"` on tickers that have already been promoted to `priority="focus"`. The dedupe check on re-add ignores archived entries, so those tickers get re-added as fresh `watching` entries and re-promoted, stealing top-3 focus slots from legitimate candidates like MU. 13 duplicated tickers currently in `data/watchlist.json`.
4. **No `entry-ready` tier.** The lifecycle stops at `focus`. There's no automated bridge from "worth watching closely" to "meets actionable criteria now".

---

## Affected files

| File | Role |
|---|---|
| [agents/screener/finviz_agent.py](agents/screener/finviz_agent.py) | `send_slack_notification` (1106), `_update_watchlist` (1230), SNDK detection (1460-1524), SNDK Slack block (1194-1214) |
| [utils/research_stocks.py](utils/research_stocks.py) | SNDK-pattern references (15, 96, 123, 213, 236, 280, 316, 393, 423) |
| [data/watchlist.json](data/watchlist.json) | State — needs one-time dedupe after code fix |
| [tests/](tests/) | New unit tests for all new helpers |

---

## Changes

### Change 1 — Rename & fix "SNDK pattern" → "Hidden Growth"

**Purpose:** catch stocks with accumulating evidence (persistence + strong EPS + institutional buying + Stage 2), whether or not TTM is distorted. TTM distortion becomes an implicit signal for IPOs, not a hard gate.

**Location:** [finviz_agent.py:1460-1524](agents/screener/finviz_agent.py#L1460-L1524) (detection), [finviz_agent.py:1194-1214](agents/screener/finviz_agent.py#L1194-L1214) (Slack), [utils/research_stocks.py](utils/research_stocks.py) (all refs).

**New 6 criteria — need 4+ to flag:**
1. `persistence` — `appearances >= 3`
2. `eps_yy_strong` — `eps_yy > 50` *(NEW — rewards clean TTM)*
3. `eps_qq_strong` — `eps_qq > 50` OR (`eps_yy < 0` AND `eps_qq > 20`) *(keeps the 2nd clause to still catch IPO distortion naturally)*
4. `inst_buying` — `inst_trans >= 3`
5. `stage2_perfect` — stage==2 AND perfect alignment
6. `ipo_lifecycle` — 'IPO' tag in screeners

**Dropped:** `ttm_distorted` (was wrongly penalizing clean growers).

**Scope change:** scan over `summary_df` (all ATR-passing, pre-10%-gate) instead of `filter_df` (post-10%-gate). Rationale: Hidden Growth is a *research prompt*, not an actionable trade. 10% gate should not apply.

**Validation:**
- NVTS (Apr 16): persistence ✅ · eps_yy_strong(-24) ❌ · eps_qq_strong(eps_yy<0 & qq=32>20) ✅ · inst(+11) ✅ · stage2 ✅ · IPO ❌ = **4/6 → flagged.** Previously missed (10% excluded).
- SNDK (today): persistence ❌ · eps_yy_strong(-328) ❌ · eps_qq_strong(qq=618) ✅ · inst(77) ✅ · stage2 ✅ · IPO ✅ = **4/6 → flagged.** Same as before.
- MU (today): persistence ❌ · eps_yy_strong(410) ✅ · eps_qq_strong(756) ✅ · inst(-0.25) ❌ · stage2 ✅ · IPO ❌ = **3/6 → not flagged.** Correct — MU belongs to Ready-to-Enter tier, not Hidden Growth.

**Slack block changes:**
- Label: `:microscope: *Hidden Growth (4+/6 criteria):*` (was `SNDK pattern`)
- Top 3 by signal score (unchanged cap)
- Each ticker still shows `TTM X% / Q/Q Y%` tag and `⚠ distorted` flag when `eps_yy < -50 and eps_qq > 0`

---

### Change 2 — NEW "🎯 Ready to Enter" Slack block

**Purpose:** surface actionable Stage 2 + VCP + tight-pullback setups (the MU-type setup).

**Location:** new block in `send_slack_notification` ([finviz_agent.py:1106](agents/screener/finviz_agent.py#L1106)), placed at top of message above "Top Picks" and Hidden Growth.

**Criteria — all must pass:**
- Stage 2 perfect alignment
- VCP confidence ≥ 70
- Quality Score ≥ 80
- Dist from 52w high between **-1% and -10%** (tight base, not extended, not broken)
- ATR% ≤ 7%
- RVol ≤ 1.2 (dry-up or normal — not FOMO day)
- Not already in `data/positions.json` open positions

**Output:**
- Top **5** by Quality Score
- Each line: `` `TICKER` QXX · VCP X% · -X% · ATR X.X% · RVol X.Xx · /stock-research TICKER ``

**Validation on Apr 15 data:**
- MU: Stage 2 ✅ · VCP 70 ✅ · Q 100 ✅ · -3% ✅ · ATR 5.8% ✅ · RVol 0.95 ✅ · not in positions ✅ → **flagged 6 days before today.**

**Slack layout after both changes:**
```
📈 Finviz Daily Screener — <date>
🧠 Today's take: <AI summary>
🎯 Ready to Enter (top 5):           ← NEW
   `MU` Q100 · VCP 85% · -5% · ATR 5.5% · RVol 0.66x · /stock-research MU
   ...
🔥 Power Moves: ...
🔬 Hidden Growth (4+/6, research):  ← renamed
   `SNDK` (TTM -328% / Q/Q +618% ⚠ distorted) · /stock-research SNDK
   ...
📊 Top Picks (Q-sorted): ...
```

**Slack channel:** stay in `#daily-alerts`. No new channel. If louder pings wanted, user can add a Slack keyword notification on `"Ready to Enter"`.

---

### Change 3 — Watchlist lifecycle fixes + new `entry-ready` tier

**Purpose:** stop focus-tier churn, stop duplicate re-adds, add automated `focus → entry-ready` promotion that mirrors Ready-to-Enter criteria.

**Location:** [finviz_agent.py:1230-1367](agents/screener/finviz_agent.py#L1230-L1367) (`_update_watchlist`).

**3a. Reactivate archived tickers instead of re-adding (NEVER create duplicates).** [line 1267-1283](agents/screener/finviz_agent.py#L1267-L1283):

```python
# BEFORE — archived tickers fall through and get re-added as duplicates
existing_tickers = {e["ticker"] for e in existing if e.get("status") != "archived"}
# ... add new rows for anything not in existing_tickers ...

# AFTER — one row per ticker, always
existing_by_ticker = {e["ticker"]: e for e in existing}   # include all statuses

for ticker in screener_candidates:
    if ticker in existing_by_ticker:
        entry = existing_by_ticker[ticker]
        # Reactivate if aged-out (but not manual/stopped-out)
        if (entry.get("status") == "archived"
                and entry.get("archive_reason") == "age_out"
                and entry.get("source") == "screener_auto"):
            entry["status"] = "watching"
            entry["reactivated_date"] = today
            entry["archive_reason"] = None
            log.info("Watchlist: reactivated %s (previously aged out)", ticker)
        # Otherwise no-op — ticker is already tracked (watching/focus/entry-ready/manually-archived)
        continue
    # Brand new ticker → add fresh entry (existing add logic)
    ...
```

**Transitions (one row per ticker, always):**
| Current state | Ticker hits screener | Action |
|---|---|---|
| (not in watchlist) | yes | add fresh `watching` entry |
| `watching` | yes | no-op |
| `focus` | yes | no-op |
| `entry-ready` | yes | no-op |
| `archived` (age_out, screener_auto) | yes | reactivate → `watching`, clear `archive_reason`, set `reactivated_date` |
| `archived` (manual / stopped_out) | yes | no-op (user explicitly killed it) |

**Does this miss tickers?** No. Every screener hit either adds a new row or reactivates an existing row. Nothing is silently skipped except user-killed entries.

**Why no steady-state dedupe logic is needed:** with 3a-reactivate + 3b (below), no code path creates a duplicate row. Invariant: `len({e["ticker"] for e in watchlist}) == len(watchlist)`.

**3b. Don't age-out focus or entry-ready tickers.** [line 1257-1259](agents/screener/finviz_agent.py#L1257-L1259):
```python
# BEFORE
if (entry.get("source") == "screener_auto"
        and entry.get("status") == "watching"
        and entry.get("added", "9999") < cutoff):
# AFTER
if (entry.get("source") == "screener_auto"
        and entry.get("status") == "watching"
        and entry.get("priority") == "watching"   # NEW — protect focus & entry-ready
        and entry.get("added", "9999") < cutoff):
```

**3c. One-time migration to clean up existing duplicates (historical bug state).** 13 tickers are currently duplicated in `data/watchlist.json`: MRVL, VRT, GEV, VIK, CIEN, AMD, SQM, NVT, GLW, STX, LITE, MTSI, KEYS.

Standalone script `utils/dedupe_watchlist.py` (run once, then delete or keep for future one-offs). For each duplicated ticker:
- Keep the entry with highest priority (`entry-ready` > `focus` > `watching` > `archived`)
- Preserve earliest `added` date, earliest `focus_promoted_date`, latest `thesis`
- Drop the rest

After migration, `_update_watchlist` never needs dedupe again — 3a + 3b prevent duplicates by construction. Not part of the runtime path.

**3d. Raise focus-promotion cap from 3 → 5.** [line 1354](agents/screener/finviz_agent.py#L1354):
```python
for qs, t, entry in promote_candidates[:5]:   # was [:3]
```
Rationale: top-3 caused MU to be pushed out on Apr 15 (rank #4 by Q). 5 is still tight.

**3e. NEW — `entry-ready` tier promotion.** Runs after focus promotion. Criteria = Change 2 criteria exactly (Stage 2 perfect + VCP ≥70 + Q ≥80 + pullback -1% to -10% + ATR ≤7% + RVol ≤1.2 + not in positions). Promotes any `priority=focus` entry that meets all criteria → `priority=entry-ready`, sets `entry_ready_date = today`. No cap (narrow criteria self-limit).

Lifecycle becomes: `watching → focus → entry-ready → (manual) entered → (manual) closed`.

**3f. Return value.** `_update_watchlist` returns `(promoted_to_focus, promoted_to_entry_ready)`. Caller [line 1566-1568](agents/screener/finviz_agent.py#L1566-L1568) logs both.

---

## Tests (per CLAUDE.md: "every new pure function gets unit tests")

New tests in [tests/test_screener_signals.py](tests/test_screener_signals.py) (new file):

1. `test_hidden_growth_scoring` — feeds fixture rows, asserts criteria scoring math
2. `test_hidden_growth_mu_not_flagged` — MU data → 3/6, not flagged
3. `test_hidden_growth_nvts_apr16_flagged` — NVTS Apr 16 fixture → 4/6, flagged (incl. when 10%-excluded)
4. `test_hidden_growth_sndk_flagged` — SNDK today → 4/6, flagged
5. `test_ready_to_enter_mu_passes` — MU Apr 15 fixture → passes all 6 gates
6. `test_ready_to_enter_excludes_open_positions` — ticker in positions.json → excluded
7. `test_ready_to_enter_rejects_extended` — dist from high -0.5% → rejected (too extended)
8. `test_ready_to_enter_rejects_broken` — dist from high -15% → rejected (broken base)
9. `test_ready_to_enter_top_5_cap`
10. `test_reactivate_archived_age_out` — archived (age_out) + screener hit → single row reactivated to `watching`, no duplicate
11. `test_no_reactivate_manual_archive` — archived (manual / stopped_out) + screener hit → no-op
12. `test_no_duplicate_on_watching_rehit` — already watching, appears in screener again → no-op, no duplicate
13. `test_age_out_skips_focus` — focus-priority ticker past 14 days → not archived
14. `test_age_out_skips_entry_ready`
15. `test_promote_to_entry_ready_criteria` — mocks focus-tier entries, asserts which get promoted
16. `test_dedupe_migration_script` — runs `utils/dedupe_watchlist.py` on fixture with 13 dupes, asserts 1 row per ticker, highest priority preserved

---

## Open decisions (yes/no required before execute)

| # | Decision | Default | Note |
|---|---|---|---|
| 1 | Ready-to-Enter Q threshold | **≥80** | ≥70 gives 3-6 names; ≥80 gives 1-3 |
| 2 | Ready-to-Enter pullback window | **-1% to -10%** | widen to -12% if too few hits |
| 3 | Hidden Growth — include 10-pct excluded | **yes** | catches NVTS-Apr16 case |
| 4 | No new Slack channel | **yes** | use keyword notif instead |
| 5 | Age-out only applies to `priority=watching` | **yes** | protects focus & entry-ready |
| 6 | Dedupe keeps highest priority, merges fields | **yes** | one-time cleanup |
| 7 | Focus promotion cap 3 → 5 | **yes** | entry-ready cap: none |

---

## Execution plan (after approval)

1. Add `_dedupe_watchlist` helper + unit tests. Run cleanup — verify 13 dupes resolve, MU still present.
2. Fix age-out gate (3b) + unit tests.
3. Fix re-add dedupe (3a) + unit tests.
4. Raise focus cap 3→5 (3d).
5. Add `_promote_to_entry_ready` helper + tests.
6. Rewrite SNDK detection as Hidden Growth (Change 1) + tests. Rename all `utils/research_stocks.py` references.
7. Add Ready-to-Enter Slack block (Change 2) + tests.
8. Update `CLAUDE.md` and `SYSTEM_DOCS.md` (per memory rule).
9. Run `python -m unittest discover -s tests -t .` locally — must pass.
10. Commit each step as a separate commit.
11. Push. Trigger `daily-finviz.yml` manually via `gh workflow run`. Verify logs show Hidden Growth + Ready-to-Enter blocks firing, and MU promotes to `entry-ready`.
12. Report back with Slack screenshot and log excerpts.
