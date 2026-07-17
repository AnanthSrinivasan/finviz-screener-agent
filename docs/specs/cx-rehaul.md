# CX Rehaul — one front door, one design system, one morning message

**Status:** APPROVED 2026-07-15 (user) — Dashboard merge approved WITH condition: the report repository stays accessible (see A.2/A.4)
**Date:** 2026-07-15
**Depends on:** soft deps on money-flow-dashboard (money line in brief) and cohort-health (cohort line in brief) — brief degrades gracefully without either
**Conflicts:** touches `utils/generators/*` (index, cockpit, shared CSS) and `premarket_alert.py`; no overlap with rules/executor agents

## 1. CX assessment (2026-07-15) — the honest verdict

The content is excellent; the packaging fights the user.

- **11 hero buttons** on index.html (Cockpit, Dashboard, Live Portfolio, Claude Portfolio, Watchlist, ETF Rotation, Weekly, Gallery, Perf 24-25, Perf 26, MAE) + two card grids of ~50 dated galleries. Eleven doors, no hierarchy — the Daily Cockpit was built to be THE pane, then got parked as button #1 of 11.
- **No navigation between pages.** Every page is a dead end; going Cockpit → Book → Flow means going back through index each time.
- **≥3 CSS systems** (cockpit, portfolio_common, etf_rotation/index each their own) — pages feel like different products.
- **7 Slack channels**, and the user's actual morning question — "what's the tape, what do I hold, what do I do today" — is answered nowhere in one place on the phone. CLAUDE.md itself admits "daily Slack is the same firehose" (overhaul explicitly deferred — this spec is that overhaul).
- Redundancy: `generate_dashboard.py` (old positions dashboard) vs `live_portfolio.html` overlap; two Performance pages + MAE are three buttons for one question ("how am I doing").

## 2. Design principles

1. **One front door:** the Cockpit is home. Everything else is a drill-down, one click away, never a dead end.
2. **Web = depth, Slack = decisions.** The phone gets one composed brief; channels stay as raw feeds for drill-down.
3. **One design system** so every page reads as one product.
4. **Ruthless reduction (user directive 2026-07-15: "ruthlessly cut down clutter and clankiness").** Every button, section, badge, legend, and stat card must justify its place on first read; when in doubt, cut or collapse. This is a license to REMOVE, not just reorganize: duplicate stat cards, decorative badges that don't change a decision, always-open sections that should be `<details>`, verbose legends (move to a tooltip/one-liner), and any element two pages both render. Data never becomes unreachable — it becomes one click deeper. Apply this pass to EVERY page touched during migration, not only the index.

## 3. Workstream A — Navigation + information architecture

### A.1 Shared nav bar — `utils/generators/nav.py` (NEW)

`render_nav(active: str) -> str`, one horizontal bar injected at the top of every generated page:

`☀️ Cockpit · 💰 Flow · 📓 Book · 🤖 Paper · 👀 Watchlist · 🖼 Charts · 📈 Record`

- Cockpit → `daily.html` · Flow → `etf_rotation.html` · Book → `live_portfolio.html` · Paper → `claude_portfolio.html` · Watchlist → `watchlist.html` · Charts → latest `finviz_chart_grid_*.html` · Record → new `record.html` (A.3)
- Adopted by every generator (`generate_daily_cockpit`, `generate_live_portfolio`, `generate_portfolio`, `generate_watchlist`, `sector_rotation.render_etf_rotation_html`, chart grid, weekly HTML). Mobile: horizontal scroll, 44px touch targets.

### A.2 index.html becomes a landing that gets out of the way

- Hero: **one primary button (☀️ Open Cockpit)** + the nav bar. Remove the other 10 hero buttons (nav replaces them).
- Below: latest weekly card + **📚 Report Archive** — the full repository of every dated report (weekly reviews, chart galleries, performance pages, MAE, retired dashboard snapshots) stays on the index, grouped by type in `<details>` sections. **User condition (2026-07-15): nothing loses its home — every historical report remains reachable from the index.** No files in `data/` are deleted.

### A.3 Merge the "how am I doing" pages — `record.html` (NEW, replaces 3 buttons)

Tabs (client-side, no framework): **Performance 2026 YTD · Performance 2024-25 · MAE/MFE · Trader Mirror archive** (when that spec ships). Existing artifacts embedded/linked, not rewritten.

### A.4 Retire redundant page (user decision required)

`generate_dashboard.py` output vs `live_portfolio.html`: confirm the old dashboard has no unique content (expandable transaction timeline already lives in live portfolio?). If unique bits exist, fold them into live portfolio, then remove the Dashboard button + generator call. **APPROVED by user 2026-07-15** — condition: existing generated dashboard HTML files stay in `data/` and remain linked from the index Report Archive; only the generator call + hero button are retired.

## 4. Workstream B — One design system

`utils/generators/theme.py` (NEW): `BASE_CSS` (palette, type scale, table/card/badge/chip styles, dark theme via `prefers-color-scheme` + explicit toggle class) + `page_shell(title, nav, body)`. Migration order: index → cockpit → etf_rotation → watchlist → portfolios (portfolio_common's `PORTFOLIO_CSS` becomes a thin extension of BASE_CSS, its page_shell delegates). Visual language follows the cockpit (the page the user likes most): dark, dense, decision-first. No functional changes during migration — same data, same sections, one skin.

## 5. Workstream C — The Morning Brief (the Slack fix)

One composed message to `#daily-alerts` at 13:30 UTC (extends `premarket-alert.yml` / `premarket_alert.py` — runs after its existing 13:00 job, or same run posting second message). **Composes only from files agents already wrote** — no new computation, every line non-fatal (missing source → line omitted):

```
☀️ Brief — Tue Jul 15
🚦 Gate: NO NEW ENTRIES · index CAUTION · cohort STRESS (34)     ← gate_decision + cohort block
💰 Money: IN Memory, Cyber (Endpoint, Vuln-Mgmt) · OUT Cloud sw  ← etf_rotation.json money_line
📓 Book: flat (0 positions) · paper 2 open, both green           ← positions/paper/live state files
🎯 Today: CRWD Q84 at pivot 415 · MU Q81 pullback 21EMA          ← cockpit qualify/radar data
📅 ER today/tomorrow: none held · TENB (watchlist) reports AMC   ← earnings parse
⚠️ Risk: regime late-rotation — fresh leaders only, half size    ← regime_action
→ Cockpit: {PAGES_BASE_URL}/daily.html
```

7 lines, hard cap. Channels are untouched (detail feeds). This message is the phone-first product.

## 6. Rollout order

A.1+A.2 (nav + slim index) → C (brief) → B (theme migration, page by page) → A.3 (record.html) → A.4 (retire dashboard, after user confirms). Each step independently shippable and revertible.

## 7. Tests

`tests/test_nav.py` (render, active state, all links resolve to generated files), `tests/test_morning_brief.py` (composition from fixture files, every-source-missing degradation, 7-line cap), theme migration smoke: every generator import-safe + emits nav + BASE_CSS marker.

## 8. Non-goals

New data or signals (other specs). Two-way Slack bot (future Think-Big item — needs a server, GitHub Actions can't listen). Evening wrap. Rewriting weekly report layout (already rebuilt 2026-06-02).
