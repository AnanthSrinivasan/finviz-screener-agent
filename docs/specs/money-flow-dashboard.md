# Money Flow Dashboard — theme baskets inside the ETF Rotation page

**Status:** APPROVED 2026-07-15 (user) — Appendix A draft baskets approved as starting universe; tune constituents in production via propose→approve
**Date:** 2026-07-15
**Reference:** [@stamatoudism tweet 2026-07-14](https://x.com/stamatoudism/status/2077108745184653386) — Ecosystem → Theme → Stock flow hierarchy with hand-curated baskets, synthetic base-100 theme indices, and a stock-level rollup score.

## 1. Problem

The 45-ETF rotation tracker sees flow only at ETF altitude. Three concrete failures on the 2026-07-13 snapshot:

1. **Sub-theme spread is invisible.** Within cybersecurity, Stamatoudis's themes span global rank #3 (Vulnerability Mgmt) to #47 (Hardware Security). Our system renders all of it as one row: `HACK rank 3, bucket NEUTRAL`.
2. **Themes without an ETF don't exist for us.** His #1 ecosystem is Memory & Storage — SMH lumps NAND/memory with GPUs and analog, so that flow never surfaces as a group.
3. **Stock→sector routing is one-to-one and sometimes wrong.** CRWD/PANW carry Finviz industry "Software - Infrastructure" → `INDUSTRY_TO_ETF` routes them to IGV, which is currently BROKEN. The strongest theme's leaders inherit a broken-sector label. A stock can belong to several themes; our map allows exactly one.

## 2. Approved decisions (from review 2026-07-15)

- **Curation:** Claude proposes theme baskets, user approves. Ongoing adds follow the same propose→approve loop.
- **Scope:** Themes run **beside** the 45-ETF universe. No existing gate (Stage Transition, Rotation Catalyst, executor sizing) changes.
- **Size:** Start ~10–15 growth-focused themes (Appendix A draft).
- **Output:** **Revamp `etf_rotation.html` in place** — no new page. Slack additions go into the existing Mon/Thu sector-rotation post.

## 3. Data model

### 3.1 `data/theme_map.json` (NEW — hand-approved, versioned in git)

```json
{
  "version": "2026-07-15",
  "ecosystems": {
    "E-CYBER":  {"name": "Cybersecurity"},
    "E-AIHW":   {"name": "AI Hardware & Infrastructure"}
  },
  "themes": {
    "T-CYBEND": {
      "name": "Endpoint & Platform Security",
      "ecosystem": "E-CYBER",
      "sibling_etf": "HACK",
      "tickers": ["CRWD", "PANW", "FTNT", "S"]
    }
  }
}
```

- Many-to-many by construction: a ticker may appear in any number of themes.
- `sibling_etf` optional — enables the divergence flag (§4.3). Themes like Memory & Storage may have none.
- Edits only via user approval. The screener may *suggest* candidates (log-only), never auto-add.

### 3.2 `data/theme_rotation_history.json` (NEW)

Same shape as `sector_rotation_history.json` rows (`{date, theme, rs_score, rank, ret_1d}`), rolling 180d, appended idempotently per day. Powers `rank_delta_5d` / decay for themes exactly like ETFs.

### 3.3 `data/etf_rotation.json` (EXTENDED — additive, existing keys untouched)

```json
{
  "date": "...", "regime": "...", "etfs": [...],
  "themes": [{"theme_id": "T-CYBEND", "name": "...", "ecosystem": "E-CYBER",
              "sibling_etf": "HACK", "rs_score": 96, "rank": 2,
              "rank_delta_5d": -3, "divergence": 4,
              "index": {"base_date": "...", "level": 174.8, "spark": [100, ...]},
              "tickers": ["CRWD", ...]}],
  "stock_flow": [{"ticker": "CRWD", "themes": ["T-CYBEND", "T-CYBVULN"],
                  "flow_score": 1.84, "q_score": 84, "watchlist_tier": "focus",
                  "held": false}],
  "money_line": {"in": [...], "out": [...], "text": "Money is IN: ..."}
}
```

Downstream consumers (weekly §4, cockpit leadership) keep reading `etfs` unchanged; they may adopt `themes`/`money_line` in a later spec.

## 4. Engine (`agents/sector_rotation.py` + new helper `agents/utils/theme_flow.py`)

### 4.1 Synthetic theme index

- For each theme: fetch daily bars for constituents (reuse `fetch_bars()`, `agents/sector_rotation.py:115` — extend the symbol list with the union of theme tickers, ~60–100 extra symbols, one batched pass).
- Daily theme return = **equal-weight mean of constituent daily returns** (missing/halted ticker → excluded that day, min 2 valid).
- Base-100 cumulative index from earliest common date in the bars window (210d fetch → ~180d index). Rendered as an inline-SVG sparkline (last 60 sessions) — no external libs, matches strict-CSP/dark-theme constraints of existing pages.

### 4.2 Theme RS — same universe as ETFs, directly comparable

- Compute `ret_20d_vs_spy` per theme from the synthetic index (reuse `_ret` / `compute_returns` logic).
- **Percentile-rank themes and ETFs together in one combined pool (45 + ~14 rows)** → one comparable 0–99 RS space. A theme RS 96 vs HACK RS 92 means something.
- Theme `rank` = rank **among themes only** (1..N, for the leaderboard); `rs_score` comes from the combined pool. `rank_delta_5d` via `theme_rotation_history.json` (reuse `annotate_with_history` pattern, `agents/sector_rotation.py:263`).

### 4.3 Divergence flag

`divergence = theme.rs_score − sibling_etf.rs_score` (None when no sibling). Rendered when `|divergence| ≥ 5`:
- `+` → "leaders running hotter than the ETF" (the CRWD-inside-NEUTRAL-HACK case)
- `−` → "ETF strength is the laggards/weightings, not the leaders"

### 4.4 Stock flow rollup

For each ticker in the union of baskets:

```
flow_score = Σ over member themes of max(0, theme_rs − 50) / 50
```

Simple and explainable: membership in one red-hot theme (RS 95) ≈ 0.9; a CRWD in three hot themes stacks. Sort desc, top 15 → leaderboard. Each row enriched from data we already have (no new network):
- `q_score` from latest `finviz_screeners_*.csv` (via the same resolver pattern as the executor)
- `watchlist_tier` from `data/watchlist.json`
- `held` from `data/positions.json` open positions (+ paper/live stops files, badge only)

### 4.5 The Money Line

One auto-written sentence from the combined ETF+theme pool, honoring the "name ALL leading groups" rule:
- **IN:** every group with `rs_score ≥ 70` in the top ~5 combined ranks *plus* big 5d climbers (`rank_delta_5d ≤ −5`), named with sub-themes in parens.
- **OUT:** groups with `rank_delta_5d ≥ +10 AND rs_score < 50` (reuse `signals()` thresholds, `agents/sector_rotation.py:928`).
- Example: `Money is IN: Memory & Storage, Cyber (Endpoint #2, Vuln-Mgmt #4), Biotech. LEAVING: Cloud software, Fintech.`

## 5. Page revamp — `render_etf_rotation_html` (`agents/sector_rotation.py:528`)

New section order (kept sections unchanged unless noted):

1. **Regime banner** — kept.
2. **💰 Money Line** — new, one sentence, large type, directly under the banner.
3. **🔥 Flow Map** — new. Theme tiles grouped by ecosystem. Per tile: theme name · combined RS · Δ5d arrow (`↑↑/↑/→/↓` at |Δ|≥5/≥2) · 60-session sparkline · divergence chip (`⚠ basket +4 vs HACK`) · ticker chips linking to Finviz. Ecosystem header shows mean RS of its themes.
4. **🏆 Stock Flow Leaderboard** — new. Top 15 by `flow_score`: rank · ticker · theme chips · flow · Q · tier/held badge.
5. **⭐ Sweet Spot** — kept as-is.
6. **RS Leaderboard (ETF)** — kept.
7. **Full ETF metrics table** — kept, now inside a collapsed `<details>`.

Style: existing page CSS extended, dark theme preserved; plain-English deltas (`up 3` / `down 2`) per the 2026-05-29 rule — **no HOT/FADING category badges on this page**.

## 6. Slack (existing Mon/Thu post, `format_slack` `agents/sector_rotation.py:942`)

Additions only — no new channel, no new webhook:
- Money Line inserted directly after the `Phase:` line (posts every day the job posts).
- New block `🏆 Stock flow: CRWD (Endpoint·Vuln-Mgmt·Zero-Tr, Q84, focus) · MU (Memory·AI-Infra) · ...` — top 5.
- Existing IN/OUT lists unchanged (they gain nothing from themes yet; combined-pool version can follow after observation).

## 7. Workflow / ops

- Same `sector-rotation.yml` cron (20:15 UTC weekdays). Added cost: one more batched bars fetch (~60–100 symbols) + pure computation.
- All theme steps are non-fatal: `theme_map.json` missing/invalid → page renders exactly as today (graceful fallthrough like `etf_rotation.json` consumers).
- No executor, monitor, or screener gate reads theme data in this spec. **Phase 2 (observation-gated, ≥4 weeks like ETF phase-1):** theme-level Rotation Catalyst input, cockpit Leadership block adoption, weekly §4 adoption, and fixing the CRWD→IGV routing via many-to-many lookup.

## 8. Tests (`tests/test_theme_flow.py`)

- Synthetic index math: equal-weight, missing-ticker day, min-2 rule, base-100.
- Combined-pool RS: theme + ETF ranked together, deterministic fixture.
- `flow_score`: multi-theme stacking, sub-50 themes contribute 0.
- Divergence: sign, None when no sibling.
- Money Line: includes ALL qualifying groups (regression for the name-all-groups rule), OUT thresholds.
- Render: import-safe, no network; page renders with and without `theme_map.json`.

## 9. Non-goals

- No trading-gate changes (phase 2).
- No auto-curation of baskets — proposals require user approval.
- No replacement of the ETF universe.
- No new Slack channel or new HTML page.

---

## Appendix A — DRAFT theme universe (14 themes, 6 ecosystems) — FOR USER APPROVAL

Baskets drafted from: names the system already tracks (positions, watchlist, screener recurrences, EP fires), Stamatoudis's visible baskets, and general knowledge. **Tickers are a starting proposal — edit freely; wrong constituents are the main quality risk.**

| Ecosystem | Theme | Sibling ETF | Draft tickers |
|---|---|---|---|
| Cybersecurity | Endpoint & Platform | HACK | CRWD, PANW, FTNT, S |
| Cybersecurity | Vulnerability & Exposure Mgmt | HACK | TENB, QLYS, RPD |
| Cybersecurity | Zero-Trust & Identity | HACK | OKTA, CYBR, ZS, NET |
| AI Hardware | Memory & Storage | — | MU, WDC, STX, SNDK |
| AI Hardware | Optics & Networking | SMH | AAOI, COHR, LITE, CIEN, ANET |
| AI Hardware | AI Compute & Interconnect | SMH | NVDA, AMD, AVGO, ALAB, MRVL |
| AI Hardware | Datacenter Power & Cooling | PAVE | VRT, MOD, PWR, ETN |
| Space & Defense | Space | UFO | RKLB, ASTS, LUNR, RDW |
| Space & Defense | Drones & Defense Tech | UFO | UMAC, ONDS, KTOS, AVAV |
| Fintech & Crypto | Consumer Fintech | ARKF | DAVE, SOFI, HOOD, TOST |
| Fintech & Crypto | Crypto Infrastructure | BLOK | COIN, MSTR, MARA, CLSK |
| Frontier | Quantum | QTUM | QBTS, IONQ, RGTI |
| Frontier | Nuclear & Uranium | NLR | SMR, OKLO, CCJ, LEU |
| Health Tech | AI Health & Diagnostics | ARKG | TEM, HIMS, GH, NTRA |

Curation loop after approval: when a new theme emerges (e.g. an EP cluster fires in an unmapped group), I propose an addition in chat with evidence; nothing enters `theme_map.json` without your yes.
