# Spec — System Staleness + Routing Fixes (deferred from 2026-06-09 session)

**Context:** CTO health-check session 2026-06-09. The headline screener fix
(dollar-volume gate + Base/Near-High screen, commit `c406796`) shipped. This spec
captures the remaining issues found while diagnosing. Read `MEMORY.md` + CLAUDE.md
first. Each item below has a confirmed root cause with `file:line` anchors.

Priority order: **2 (executor date) → 6 (screener runtime) → 5 (routing) → 3 (paper growth panel)**.
Item 1 (AMZN ghost) was **FIXED this session** (commit `d28456d`). Item 4 is NOT a
bug (downgraded). See each section.

> **Session update 2026-06-09:** the screener fix shipped (`c406796`), the AMZN
> ghost fix shipped (`d28456d`), and verification revealed the user re-entered live
> (DAVE 30 @ 282.76, ZVRA 300 @ 13.52) — the dashboard now correctly reflects the
> live book. Remaining open: items 2, 3, 5, 6.

---

## 6. Screener runtime — universe doubled, run time ~2×  🟡 new (perf)

**Symptom:** After the 2026-06-09 screener fix (`c406796`), the daily run went from
~4–5 min to **9m41s**, and the universe grew **133 → 254 rows**.

**Root cause (NOT "screening everything" — still only the 7 Finviz screens):**
- Dominant: **lowering the share-vol floor 5× (1M → 200K)** on Growth/52WH/IPO
  admits the entire 200K–1M avg-share band that was previously excluded.
- The new Base/Near-High screen adds ~79 tagged names (much overlapping).
- Each admitted ticker costs a **snapshot fetch (network) + Claude commentary** —
  the slow part. The `$30M` dollar-vol gate runs **AFTER** the snapshot
  ([finviz_agent.py, post-enrichment block](agents/screener/finviz_agent.py)),
  so it cleans the output but does NOT save scrape time — we snapshot names we
  then drop.

**Proposed fix:** add a **cheap dollar-volume PRE-filter** using the screener
table's own `Volume × Price` (already scraped in `aggregate_and_save`, available
before snapshots) to drop obviously-illiquid names *before* the snapshot fetch.
Keeps the DAVE-class (high $-vol passes), cuts runtime back toward baseline. Keep
the existing post-snapshot gate as the precise final cut (uses true avg volume).
Watch the Actions timeout if the universe keeps growing.

---

## 1. AMZN ghost — FIXED 2026-06-09 (commit `d28456d`)  ✅

**Was:** [position_monitor.py:1599](agents/trading/position_monitor.py#L1599) `exit(0)`
when SnapTrade returned empty, *before* the Step 9 auto-close — so a position left
in positions.json when the user went fully flat lingered forever (AMZN shown owned
while the live book was empty).

**Fix shipped:** `fetch_positions()` records `LAST_SNAPTRADE_ACCOUNTS`; on a
*confirmed*-flat book (accounts reachable, 0 holdings) with lingering open
positions, `main()` now runs `sync_snaptrade_with_rules([], …)` to auto-close them,
persists, Slack-alerts, and regenerates the dashboards via `_regenerate_dashboards()`.
API-blip guard: only closes when accounts were actually reachable. Test:
`tests/test_position_monitor.py::test_fully_flat_closes_all_lingering_positions`.
(In practice AMZN cleared via the *normal* reconcile once the user re-entered DAVE/
ZVRA — the book became non-empty — but the flat-path fix prevents recurrence when
genuinely flat.)

### (historical) Original AMZN ghost analysis — for reference

**Symptom:** Dashboard / `data/positions.json` shows AMZN as an open position
(100 sh, entry 268.34, entry_date 2026-05-21). The user's **live SnapTrade book is
empty (0 positions) — they are flat.** Verified live: `fetch_positions()` → "1
account, 0 positions".

**Root cause:** [position_monitor.py:1574-1577](agents/trading/position_monitor.py#L1574)
```python
positions = fetch_positions()
if not positions and not has_trade_input:
    log.info("No open positions found — nothing to monitor.")
    exit(0)
```
When SnapTrade returns empty (user fully flat), `main()` exits **before Step 9
reconcile** ([position_monitor.py:1663](agents/trading/position_monitor.py#L1663)),
which is the auto-close path
([sync_snaptrade_with_rules, line ~970](agents/trading/position_monitor.py#L970))
that would move AMZN from `open_positions` → `closed_positions`. So **any position
left in positions.json when the user goes fully flat never closes — it becomes a
permanent ghost** and the dashboard reads it forever.

**Design tension (do NOT just delete the guard):** a transient SnapTrade API
failure ALSO returns empty. Removing the guard would auto-close the entire book on a
fake "flat" reading. The fix must distinguish *confirmed flat* from *API blip*.

**Proposed fix:**
- The guard fires only when `fetch_positions()` returns `[]`. Distinguish a
  *successful* empty pull (account fetch succeeded, 0 holdings) from an *errored*
  pull. `fetch_positions()` already logs "Found N account(s)" then "Total positions:
  0" on a genuine flat — thread a success flag out of it (e.g. return
  `(positions, ok)` or set a module flag) so the caller knows the empty is real.
- When the empty is *confirmed* AND `positions.json` still has `open_positions`,
  **run the reconcile/auto-close path** (Step 9) instead of `exit(0)`. Each
  auto-close already resolves a real exit price (SnapTrade SELL fill → live quote →
  peak fallback), so closes will be priced correctly.
- Optional belt-and-suspenders: require **2 consecutive** confirmed-empty pulls
  before closing the last names (guards a one-off blip that still "succeeds").

**Immediate hotfix (offer to user):** one-shot move AMZN from `open_positions` →
`closed_positions` in `positions.json` with `close_source="manual_flat_reconcile"`
and a real close price (live AMZN quote), so the dashboard is honest today. This is
data-only; the code fix above prevents recurrence.

**Tests:** add a `sync_snaptrade_with_rules` test for "empty SnapTrade + non-empty
positions.json → all open positions auto-closed." Add a `main()`-guard test for
confirmed-empty-runs-reconcile vs errored-empty-skips.

---

## 2. Alpaca executor — "no screener data for <today>"  🔴 confirmed

**Symptom (user-reported):** "Alpaca executor — no screener data for 2026-06-05",
run failed 2026-06-05 00:07 UTC.

**Root cause:** [alpaca_executor.py:600-603](agents/trading/alpaca_executor.py#L600)
loads `load_screener_csv(today)` where `today = datetime.date.today()` (UTC,
[line 559](agents/trading/alpaca_executor.py#L559)). `load_screener_csv`
([line 204](agents/trading/alpaca_executor.py#L204)) requires
`finviz_screeners_<today>.csv` exactly and returns `[]` if absent → hard
`SystemExit(1)`. The failing run fired at **00:07 UTC** (8:07pm ET prev day) —
*before* that day's screener (20:30 UTC) had produced a CSV. Normal daily runs work
because they're triggered by `workflow_run` right after the screener; off-cycle
triggers (manual, retry, late workflow_run) hit the gap.

**Proposed fix:** in `load_screener_csv`, fall back to the **most recent available**
`finviz_screeners_*.csv` with date ≤ today when today's file is absent. Log which
date it used. Add a staleness guard (e.g. refuse if the newest CSV is > N trading
days old) so it never trades on badly stale data. Cheap, contained.

**Tests:** `load_screener_csv` with (a) today present → today, (b) today absent,
yesterday present → yesterday, (c) all absent → `[]`.

---

## 5. DAVE → ARKF / fintech industry routing  🟡 confirmed

**Symptom:** DAVE (Dave Inc, fintech neobank) has no correct sector/ETF mapping.

**Findings:**
- `data/ticker_sector_map.json`: **DAVE not mapped.**
- `agents/utils/sector_lookup.py` `INDUSTRY_TO_ETF`: no fintech / "Credit Services"
  route. DAVE's Finviz industry ("Software - Application") matches
  [line 26](agents/utils/sector_lookup.py#L26) → **IGV (enterprise software)** —
  wrong. DAVE's real theme is **ARKF (Fintech)**, which IS in the ETF universe but
  DAVE never maps there.
- Consequence: Stage Transition / Rotation Catalyst blocks check IGV's tailwind for
  DAVE instead of fintech's. (Also note: ARKF is currently low-RS/BROKEN, so DAVE is
  a high-RS name in a weak sector = idiosyncratic leader — a *useful* signal the
  system can't currently surface.)

**Proposed fix:** add fintech/consumer-finance industries → ARKF in `INDUSTRY_TO_ETF`
("Credit Services", "Financial - Credit Services", select fintech app names), and
add an explicit `DAVE → ARKF` override in `ticker_sector_map.json` (same pattern as
the existing AAOI→SMH override). Verify with `sector_lookup.lookup()`.

---

## 3. Paper (Alpaca) portfolio — no performance / growth view  🟡 needs investigation + build

**Symptom (user):** "paper claude portfolio … not showing performance or how the
account grew."

**Findings so far:**
- Paper state files exist: `data/paper_stops.json`, `data/paper_trading_state.json`.
- Candidate generators: `utils/generators/generate_portfolio.py`,
  `generate_dashboard.py` (vs `generate_live_portfolio.py` = SnapTrade live).
- **No paper-portfolio link found in `index.html`**, and **no equity-curve history
  log exists** for the Alpaca paper account (nothing snapshots account equity over
  time).

**Proposed work:**
1. Identify which page the user means (likely `generate_portfolio.py` or the cockpit)
   and whether it renders Alpaca paper holdings + P&L at all.
2. Add a **paper equity-curve**: snapshot Alpaca `/account` equity daily into a new
   `data/paper_equity_history.json` (append `{date, equity, cash, positions_value}`)
   from `alpaca_monitor.py` (runs inside position-monitor.yml). Then render a growth
   panel (starting equity → current, % return, simple sparkline/line) on the paper
   portfolio page. Mirror `generate_live_portfolio.py` style (light theme).
3. Link it from `index.html`.

**Tests:** pure render fn for the equity panel + the history-append helper.

---

## 4. ETF rotation "stale 6/05" — ❌ NOT A BUG (downgraded)

Initially flagged: `data/etf_rotation.json` showed date 2026-06-05 while today was
06-09. **Root cause was a stale *local* working copy** — the file is committed back
by the daily `sector-rotation.yml` Actions run; `git pull` (via the commit hook)
refreshed it to 06-09. The production pipeline was never stale. **Lesson for the
agent:** `git pull --rebase origin main` BEFORE reading any `data/*` file for
analysis — Actions commits data constantly and the local copy lags. (This also
caused the agent to hand the user 4-day-stale sector RS leaders mid-session.)

---

## Verification rule reminder
After each code fix: `python -m unittest discover -s tests -t .` AND run the relevant
GH Actions workflow (`gh workflow run …` + `gh run watch`), verify logs — unit tests
alone are not sufficient (CLAUDE.md rules 3-4).
