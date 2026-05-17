# ETF Rotation Dashboard

**Status:** Approved 2026-05-17 — execute
**Predecessor:** ad-hoc TAN-class scan + ETF-universe scan in chat session 2026-05-17

## Why

Reading sector rotation from the 35-ETF universe is faster than scanning 5,000 stocks. The existing daily Slack alert tells you the regime; this page makes the **ETF-level setup state** visible at a glance — which themes are in a base, which are pre-breakout, which are extended (wait for PB), which are broken.

## Curated universe — 28 ETFs

**Sectors (11):** XLK · XLF · XLE · XLV · XLI · XLY · XLP · XLU · XLB · XLRE · XLC

**Thematics (17):**
- Growth sub: SMH · IGV · HACK · ARKK
- Healthcare sub: XBI
- Financials sub: KRE
- Energy sub: XOP · FCG
- Materials sub: GDX · COPX · LIT · URA
- Building sub: ITB
- Clean energy: TAN · ICLN
- Space / Defense: UFO · ARKX · ITA

Dropped from prior map (low signal / redundant): XSD, IBB, XHB, PBW, FAN, URNM, ROKT, PHO, REMX, XME, GLD, SLV, BOTZ, JETS, XRT.

## Buckets

Same logic used in the ad-hoc scan:

| Bucket | Predicate |
|---|---|
| **BASE** | `s50_rising AND s200_rising AND mult50 < 3 AND range20 < 12% AND -10 < dist52 < -2` |
| **PRE-BREAKOUT** | `s50_rising AND s200_rising AND mult50 < 4 AND -10 ≤ dist52 ≤ 0` (and not BASE) |
| **EXTENDED** | `mult50 > 5 OR dist52 > -2` |
| **BROKEN** | `mult50 < -1 OR NOT s200_rising` |
| **NEUTRAL** | anything else |

`mult50` = `(close − sma50) × close / (sma50 × atr14)` (same formula as peel calibration, matches TradingView ATR% Multiple).

## Implementation

### 1. `data/sector_etf_map.json` — curated universe

Rewrite `thematics` block. Sectors block unchanged. Benchmarks unchanged.

### 2. `agents/sector_rotation.py` — extend

Already fetches Alpaca bars + computes RS scores for the universe. Add:
- Per-ETF setup-metric compute: ATR%, mult50, dist52, range20, ret20, ema21_dist, RVol, MA stack
- Bucket assignment per ETF
- Write `data/etf_rotation.json` (daily snapshot, list of `{ticker, name, bucket, metrics}`)

### 3. `agents/sector_rotation.py` — HTML render

New function `render_etf_rotation_html(snapshot, regime_data) -> str`. Writes `data/etf_rotation.html`.

Page layout (one screen, top-down):
1. **Regime banner** — pulls existing regime + REGIME_ACTIONS block
2. **🎯 Base / Pre-breakout** — top section, big cards, ranked by `(10 - mult50) + (15 - range20)`
3. **🚀 Extended (wait for PB)** — secondary, smaller cards
4. **❌ Broken** — collapsed `<details>` section
5. **Full metrics table** — all 28 ETFs, all metrics, sortable

Styling: light theme per repo convention (white cards, #111827 text, #16a34a/#dc2626 pos/neg, green border for BASE, amber for EXTENDED, red for BROKEN).

### 4. `utils/generate_index.py` — link new page

Add tile: `📊 ETF Rotation` pointing to `data/etf_rotation.html`. Tile shows today's date + count of BASE setups.

### 5. Tests

- `tests/test_etf_rotation_buckets.py` — bucket-assignment predicate on synthetic metrics (BASE / PRE-BREAKOUT / EXTENDED / BROKEN / NEUTRAL boundary cases)
- `tests/test_etf_rotation_render.py` — HTML render returns non-empty string, contains "BASE", "EXTENDED", "Broken", regime tag

## Schedule

Wires into `sector_rotation.py` (`sector-rotation.yml` workflow, daily 21:15 UTC Mon-Fri). Already-running cron — no new workflow file needed. Page refreshes daily. Slack stays unchanged (existing Mon/Thu post).

## Out of scope

- Drill-down per ETF (top 5 constituents) — deferred to v2
- Intraday refresh — daily-only for now
- Historical ETF setup-state tracking (rolling 90d) — deferred

## Success criteria

- `data/etf_rotation.html` written on next sector-rotation workflow run
- Linked from index, renders correctly in light theme
- All 28 ETFs bucketed; no `n/a` rows from data-feed gaps on common tickers
- All new tests green; full suite (`python -m unittest discover -s tests -t .`) green
