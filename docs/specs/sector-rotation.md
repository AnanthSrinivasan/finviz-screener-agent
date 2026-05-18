# Spec — Sector Rotation Tracker

**Status:** Approved 2026-05-08 · Pending implementation
**Loop:** spec → review → tasks → execute (per CLAUDE.md)

---

## 1. Problem

Index-level reads (SPY/QQQ) hide the dispersion that matters. On 2026-05-07 SPY closed -0.2% while XHB was -4.3% and SLV +2.0% — a 6-point intraday spread. Held positions in the wrong sector (REMX, AMAT, LSCC) bled while NVDA ran. We have no persistent layer that tells us where money is rotating into vs out of, no trend signal, and no anticipation flag. We need it for two reasons:

1. **Entry alignment** — soft-prefer Ready-to-Enter / Hidden Growth candidates whose sector is leading.
2. **Exit assist** — flag held positions whose sector RS is decaying for 2+ days, before the price stop fires.

## 2. Cycle context (motivation)

The market regime (May 2026) maps to **late June / early July 2020** in the post-COVID analog: V-bottom rip is over, dispersion rising, leadership narrowing, defensive bid emerging in precious metals. In this phase rotation is *fast* — 5d/20d windows dominate. A blow-off is the historical endgame (Aug-Sept 2020 = QQQ -10% correction after AAPL/TSLA-split blow-off). The tracker must surface dispersion changes near-real-time, not wait for monthly trends.

## 3. Goals & non-goals

**Goals**
- Persistent daily snapshot of sector/theme RS across a fixed ~33-ETF universe
- Trend signals (leadership change, decay, anticipation) computed off rolling 60d window
- 180d history retained for cycle-context overlays (regime classification only — not signals)
- 2x/week Slack roll-up (Mon + Thu post-close)
- New `rotation.html` dashboard tab — heatmap + sparklines + dispersion-over-time
- Sector-level alerts integrated into position monitor (2-day confirmation)
- Soft entry filter in paper executor

**Non-goals**
- Intraday updates (daily close is enough)
- Stock-level RS (already exists for individual tickers via Finviz)
- Hard entry blocks based on sector (only soft annotations)
- Predictive models / ML — pure rank-based deterministic signals

## 4. Universe (~33 ETFs, hand-curated)

Stored in `data/sector_etf_map.json`:

```json
{
  "sectors": {
    "XLK": {"name": "Technology",      "theme": "growth"},
    "XLF": {"name": "Financials",      "theme": "cyclical"},
    "XLE": {"name": "Energy",          "theme": "cyclical"},
    "XLV": {"name": "Healthcare",      "theme": "defensive"},
    "XLI": {"name": "Industrials",     "theme": "cyclical"},
    "XLY": {"name": "Cons Disc",       "theme": "cyclical"},
    "XLP": {"name": "Cons Staples",    "theme": "defensive"},
    "XLU": {"name": "Utilities",       "theme": "defensive"},
    "XLB": {"name": "Materials",       "theme": "cyclical"},
    "XLRE":{"name": "Real Estate",     "theme": "rate-sensitive"},
    "XLC": {"name": "Comm Services",   "theme": "growth"}
  },
  "thematics": {
    "SMH": {"name": "Semiconductors",  "theme": "growth-narrow"},
    "XBI": {"name": "Biotech",         "theme": "speculative"},
    "IBB": {"name": "Biotech LC",      "theme": "speculative"},
    "KRE": {"name": "Regional Banks",  "theme": "rate-sensitive"},
    "XOP": {"name": "Oil & Gas E&P",   "theme": "commodity"},
    "GDX": {"name": "Gold Miners",     "theme": "precious-metal"},
    "GLD": {"name": "Gold",            "theme": "precious-metal"},
    "SLV": {"name": "Silver",          "theme": "precious-metal"},
    "URA": {"name": "Uranium",         "theme": "energy-transition"},
    "LIT": {"name": "Lithium",         "theme": "energy-transition"},
    "ITB": {"name": "Homebuilders",    "theme": "rate-sensitive"},
    "XHB": {"name": "Homebuild Sup.",  "theme": "rate-sensitive"},
    "XRT": {"name": "Retail",          "theme": "cyclical"},
    "JETS":{"name": "Airlines",        "theme": "reopen"},
    "XME": {"name": "Metals & Mining", "theme": "commodity"},
    "REMX":{"name": "Rare Earth",      "theme": "energy-transition"},
    "COPX":{"name": "Copper Miners",   "theme": "commodity"},
    "TAN": {"name": "Solar",           "theme": "energy-transition"},
    "ICLN":{"name": "Clean Energy",    "theme": "energy-transition"},
    "ARKK":{"name": "Innovation",      "theme": "speculative"}
  },
  "benchmarks": {
    "SPY": {"name": "S&P 500"},
    "QQQ": {"name": "Nasdaq 100"},
    "IWM": {"name": "Russell 2000"},
    "DIA": {"name": "Dow"}
  }
}
```

Held-ticker → ETF mapping (`data/ticker_sector_map.json`) — hand-curated, with fall-through to Finviz `Sector` field via `agents/utils/sector_lookup.py` when missing:

```json
{
  "AAOI": "SMH",
  "AMAT": "SMH",
  "LSCC": "SMH",
  "NVDA": "SMH",
  "CRWV": "XLK",
  "REMX": "REMX",
  "TNA": "IWM",
  "GLW": "XLK",
  "INDV": "XLV",
  "FIGS": "XLY"
}
```

## 5. Architecture

```
agents/
  sector_rotation.py            # NEW — daily snapshot + trend signals
  utils/
    sector_lookup.py            # NEW — ticker→ETF resolver (map + Finviz fallback)
agents/trading/rules.py         # extend: _is_sector_rotating_out() decay confirmation
data/
  sector_etf_map.json           # NEW — ETF universe definition
  ticker_sector_map.json        # NEW — held-ticker→ETF map
  sector_rotation_YYYY-MM-DD.json   # NEW — daily snapshot
  sector_rotation_history.json  # NEW — rolling 180d
finviz_weekly_agent.py          # extend: weekly roll-up section
alpaca_executor.py              # extend: soft sector filter on entry
position_monitor.py             # extend: emit SECTOR ROTATING OUT alert
.github/workflows/
  sector-rotation.yml           # NEW — daily 21:15 UTC
tests/
  test_sector_rotation.py       # NEW — RS calc, decay, leadership change
  test_sector_lookup.py         # NEW — fallback chain
```

## 6. Data shapes

**Daily snapshot** `data/sector_rotation_YYYY-MM-DD.json`:

```json
{
  "date": "2026-05-08",
  "universe_size": 33,
  "spy_ret_1d": -0.0019,
  "dispersion_1d_stdev": 0.018,
  "dispersion_percentile_180d": 0.84,
  "regime": "late-rotation",
  "etfs": [
    {
      "etf": "SMH",
      "name": "Semiconductors",
      "theme": "growth-narrow",
      "close": 245.12,
      "ret_1d": -0.0142,
      "ret_5d": -0.0289,
      "ret_20d": 0.0411,
      "ret_vs_spy_5d": -0.0210,
      "ret_vs_spy_20d": 0.0290,
      "rs_score": 78,
      "rank": 7,
      "rank_5d_ago": 4,
      "rank_delta_5d": -3,
      "is_20d_rs_high": false,
      "decay_streak_days": 0
    }
  ]
}
```

**Rolling history** `data/sector_rotation_history.json` — list of {date, etf, rs_score, rank} only (lean), 180d × 33 ETFs ≈ 6000 rows. Used for sparklines + regime classification.

## 7. Trend signals (computed in `sector_rotation.py`)

| Signal | Trigger | Action |
|---|---|---|
| **Leadership change (IN)** | `rank_delta_5d <= -10` AND `rs_score >= 70` | Slack green line: "money flowing INTO {etf}" |
| **Leadership decay** | `rank_delta_5d >= +10` (rank worsened) AND `rs_score < 50` | Slack red line: "money rotating OUT of {etf}" |
| **Anticipation (20d RS high)** | `ret_vs_spy_20d` is new max in last 20d AND `rs_score < 60` | Slack target line: "{etf} breaking out of laggard zone — early signal". **Wait-for-confirmation**: must hold 2 consecutive days. |
| **Sector rotating out (held positions)** | Held ticker's mapped ETF has `decay_streak_days >= 2` | Position monitor fires SECTOR ROTATING OUT — {etf} alert. Informational, no auto-exit. |

**Regime classification** (cycle-context overlay, not a trading signal):
- `correlation_phase` — universe stdev of `ret_5d` < p20 of 180d → "indiscriminate beta"
- `early-rotation` — stdev p20-p50, top RS concentrated in 2-3 themes
- `mid-rotation` — stdev p50-p80, leadership broadening or holding
- `late-rotation` — stdev > p80, narrowing leadership (where we are now)
- `blow-off-risk` — late-rotation + SPY at 20d high + breadth (from `market_monitor_history.json`) declining

## 8. Slack output (Mon + Thu, 21:15 UTC)

```
Sector Rotation — Thu 2026-05-08

Phase: late-rotation · Dispersion p84 (rising) · Analog: Jul 2020

IN (5d RS rising)
  - SLV   Silver           rank 3 (+12)   RS 92
  - GLD   Gold             rank 5 (+8)    RS 87
  - XLK   Technology       rank 9 (+4)    RS 71

OUT (5d RS falling)
  - REMX  Rare Earth       rank 28 (-15)  RS 22  · 2d decay
  - XHB   Homebuilders     rank 30 (-12)  RS 18  · 2d decay
  - URA   Uranium          rank 27 (-11)  RS 24  · 1d decay

Anticipation (confirmed 2d)
  - JETS  Airlines         RS 58 up from 32 over 5d

Held positions in OUT sectors
  - REMX (REMX), AMAT/AAOI/LSCC (SMH — RS 71 but pulling back hard)
```

## 9. `rotation.html` dashboard tab

- **Top:** dispersion-over-time chart (180d, line + filled p20/p80 band overlays). Shows where current regime sits.
- **Heatmap:** rows = ETFs (sorted by today's rank), columns = `1d / 5d / 20d / 60d` returns vs SPY. Light-theme palette: green/red gradient on white, #16a34a/#dc2626 anchors per memory rule.
- **Sparkline column:** last 20d of rank per ETF.
- **Click ETF row** → filter chart grid to held tickers in that sector.
- **Bottom:** regime classification banner with analog tag.

## 10. Workflow

`.github/workflows/sector-rotation.yml` — `cron: '15 21 * * 1-5'` (21:15 UTC weekdays, after `market_monitor.yml` at 21:00). Slack output gated to Mon/Thu via in-script day-of-week check. Other days still write the snapshot + update history but skip Slack.

## 11. Trading integration

**Position monitor** (`position_monitor.py`):
- Load latest sector_rotation snapshot at run start
- For each held position, look up sector via `sector_lookup.py`
- If `decay_streak_days >= 2` → fire one-time-per-day SECTOR ROTATING OUT alert (dedup via new `sector_rotating_out_alerted_date` field on position record)
- Does NOT mutate stop or status — informational

**Paper executor** (`alpaca_executor.py`):
- Before placing each BUY, look up candidate's sector
- If sector RS bottom quartile (rank > 75% of universe) → annotate Slack with "Sector lagging (RS {n})" line
- Does NOT block the buy — soft signal only

**Weekly agent** (`finviz_weekly_agent.py`):
- New section "Sector Rotation — Week in Review"
- 1w + 4w RS leaderboard, leadership transitions table, regime-change events
- Historical analog flag if dispersion crossed p80 in last 5d

## 12. Tests (in same commit as each task)

- `test_sector_rotation.py`
  - `test_rs_score_percentile_rank` — known returns → expected rank
  - `test_leadership_change_detection_in` — 10pt rank improvement triggers
  - `test_leadership_decay_2day_confirmation` — single-day decay does NOT fire alert; 2 consecutive days does
  - `test_anticipation_signal_requires_2day_hold` — 1-day breakout doesn't trigger; 2-day does
  - `test_dispersion_calculation` — stdev of returns matches manual
  - `test_regime_classification_late_rotation` — synthetic narrow leadership → "late-rotation"
- `test_sector_lookup.py`
  - `test_explicit_map_hit` — AAOI → SMH from map
  - `test_finviz_fallback` — unknown ticker falls through to Finviz Sector field
  - `test_unmapped_returns_none` — no map, no Finviz → None (skips signal)

## 13. Rollout

1. Build agent + tests, run locally → confirm snapshot generation with current data.
2. Run workflow manually via `gh workflow run sector-rotation.yml --ref main`.
3. Verify Slack output formatting in `#daily-alerts`.
4. Backfill 60d history one-time via `python -c "from agents.sector_rotation import backfill; backfill(days=60)"` (Alpaca historical bars, no extra cost).
5. Enable cron after 1 week of clean snapshots.
6. Add `rotation.html` to dashboard nav.
7. Wire position monitor + paper executor integration last (after we've seen the signal quality).

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Yfinance/Alpaca rate limits on universe pull | Alpaca `get_bars` accepts list — single batched call. ~33 symbols, no issue. |
| Sector ETF map drift (new themes emerge) | Hand-curated, easy to extend. Document in CLAUDE.md. |
| 2-day confirmation lags real rotations | Acceptable — false-positive cost > lag cost. Daily snapshots still visible in dashboard for human discretionary read. |
| Soft entry filter ignored | It's intentional — the human decides. Annotation, not gate. |
| `[skip ci]` data commits bloating repo | Same pattern as existing `market_monitor_*.json` files. Already handled by archive_data.py at 70d cutoff (extend rule for `sector_rotation_*.json`). |

## 15. Tasks (numbered, each one committable)

1. **Universe + sector map** — write `data/sector_etf_map.json`, `data/ticker_sector_map.json`, `agents/utils/sector_lookup.py` + tests.
2. **Snapshot agent core** — `agents/sector_rotation.py`: pull bars, compute returns + RS ranks + dispersion. Write daily JSON. Tests for RS calc.
3. **Trend layer** — leadership change, decay (2-day confirm), anticipation (2-day confirm), regime classification. Tests.
4. **History rollup** — append to `sector_rotation_history.json`, prune to 180d. Backfill helper.
5. **Workflow** — `.github/workflows/sector-rotation.yml` daily 21:15 UTC. Manual run + verify.
6. **Slack block** — Mon/Thu post-close formatting in `sector_rotation.py`.
7. **`rotation.html` dashboard** — extend `utils/generate_index.py` (or new generator). Heatmap + dispersion chart + sparklines.
8. **Position monitor integration** — sector decay alert, dedup field, sector_lookup wiring.
9. **Paper executor integration** — soft annotation on lagging-sector candidates.
10. **Weekly agent integration** — rotation section in Saturday roll-up.
11. **archive_data.py extension** — include `sector_rotation_*.json` in 70d archival rule.
12. **Docs** — update CLAUDE.md (Architecture table + Data Files + Trading Rules sections) and SYSTEM_DOCS.md.
