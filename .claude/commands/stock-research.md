# Stock Research

Deep research on one or more tickers. Produces a structured conviction report and saves an HTML file to `data/`.

**Usage:** `/stock-research LITE BE CORZ`

Arguments (`$ARGUMENTS`) are space-separated tickers. If no arguments, ask the user which tickers to research.

---

## What to do

You are acting as a momentum trader's research analyst. The system context is:
- Trading rules: Minervini/Weinstein — Stage 2 only, no entries in RED/BLACKOUT market
- Quality Score dimensions: Market Cap, Rel Volume, EPS (max of Q/Q and Y/Y TTM), Inst Trans, Multi-screener, Stage, VCP, Dist from High
- "SNDK Pattern" = strong EPS Q/Q that our TTM-based screener likely missed (spin-offs, IPOs, character changes)
- Current positions and market state are in `data/positions.json` and `data/trading_state.json`

### Step 1 — Load context

Read `data/trading_state.json` to get current market state and sizing mode. Read `data/positions.json` to check if any of the tickers are already held.

### Step 1.5 — Chart vision (run for each ticker)

Fetch the Finviz daily chart image for each ticker at:
`https://finviz.com/chart.ashx?t={TICKER}&ty=c&ta=1&p=d`

Read this image with your vision capability and assess the chart against our setup criteria:

**Chart pattern checklist:**
- [ ] Price above 50 SMA and 200 SMA? (Stage 2 visual confirmation)
- [ ] 50 SMA trending up? Slope?
- [ ] Price relationship to 21 EMA and 10 EMA — near a pullback or extended?
- [ ] VCP visible? (tightening price range, declining volume on base)
- [ ] Volume pattern on recent up days vs down days — accumulation or distribution?
- [ ] Distance from 52-week high (rough visual estimate)?
- [ ] Any obvious risk: gap-fill risk, broken structure, climax run?
- [ ] Pattern match: which Minervini setup type best fits? (Stage 2 breakout / VCP / EMA pullback / none)

**Setup quality:** Rate as `A` (textbook, enter on next trigger), `B` (developing, needs more work), `C` (not ready/extended/broken).

Add a "Chart: A/B/C — [1-line pattern description]" line to the HTML per-ticker card.

Do NOT save chart images during research. The `data/chart_patterns/winners/` folder is only for confirmed trades that hit T1 or exited with a gain — it is written by the position monitor, not by research.

### Step 2 — Research each ticker in parallel

Run 3 searches per ticker simultaneously across all tickers. Research must be **forward-looking**, not just point-in-time.

For each ticker find:

1. **Earnings trend (last 4 quarters)** — EPS actual vs estimate each quarter, % beat/miss, trajectory. Is management sandbagging?
2. **Forward estimates** — Analyst consensus EPS for next 2-4 quarters. Annual FY estimates. Were estimates revised UP or DOWN in last 30-60 days? Management long-term targets?
3. **Revenue trajectory** — YoY growth rate last 4 quarters. Accelerating or decelerating? Forward revenue estimate next FY?
4. **Institutional cycle** — Fund count trending up/down? Name specific funds and their moves. Adoption phase or distribution?
5. **IPO / spin-off cycle** — When did it IPO? Lock-up expired? How many earnings reported as public company? Phase: Hot IPO / Lock-up / Orphan / Institutional adoption / Mature? **Actionable or not yet?**
6. **TAM + product cycle** — Market size now and in 3 years. Company's % of TAM. Early S-curve or late? What product cycle is driving this?
7. **Short interest + next catalyst** — Short % of float, trending. Next earnings date. Any other near-term catalyst?
8. **BEAR CASE — mandatory, never skip:** Debt load + interest expense as % of revenue. Customer concentration (top 1-3 customers % of revenue). Who is shorting this and what's their thesis (Kerrisdale, Muddy Waters, etc.)? Competitive threats — who builds the same thing? Insider selling at lock-up and since. What kills this thesis?
9. **SNDK pattern check** — Is TTM EPS distorted? What does Q/Q show that TTM hides?

Search queries per ticker:
- `"{TICKER} earnings EPS Q/Q analyst estimates forward 2026 2027 revised"`
- `"{TICKER} institutional ownership funds buying IPO cycle 2026"`
- `"{TICKER} TAM market size short interest next catalyst April 2026"`

### Step 3 — Score each ticker (9 dimensions)

| Dimension | HIGH (3) | MODERATE (2) | LOW (1) |
|-----------|----------|--------------|---------|
| EPS beat trend | Accelerating 3+ qtrs | 1-2 qtrs beat | Miss or flat |
| Estimate revisions | Revised UP >10% | Flat | Revised DOWN |
| Revenue acceleration | YoY growth accelerating | Stable growth | Decelerating |
| Inst adoption phase | Phase 3 (adoption) | Phase 4 (mature) | Phase 0-2 (too early) |
| IPO cycle | Actionable | Watch | Not yet |
| TAM position | Early S-curve, large TAM | Mid S-curve | Late / small TAM |
| Short interest | Low + decreasing | Neutral | High or increasing |
| Stage 2 | Perfect alignment | Basic Stage 2 | Not Stage 2 |
| SNDK pattern | High distortion (system missing it) | Moderate | Low |

Total: X/27. >20 = HIGH conviction, 14-20 = MODERATE / watchlist, <14 = SKIP.

### Step 4 — Write HTML report

Write a complete HTML file to `data/stock_research_{today}.html` using the light theme palette:
- Background: `#f9fafb`, cards: `#ffffff`, text: `#111827`
- Positive: `#16a34a`, negative: `#dc2626`, accent: `#2563eb`
- Font: system-ui, sans-serif

Report structure:
1. Header with date and market state banner (RED/CAUTION/GREEN with color)
2. Summary conviction table (all tickers, score, verdict in one glance)
3. Per-ticker card with: score badge, EPS data, revenue data, institutional trend, catalyst, SNDK flag if applicable, entry verdict given current market state
4. Footer: "Generated by stock-research skill" + timestamp

### Step 5 — Output to user

**IMPORTANT — no cheerleading.** Every ticker gets a bull sentence AND a bear sentence. If you can't articulate a credible bear case, you haven't researched enough. Verify IPO dates from actual SEC filings or news, not assumptions. Lock-up expiry dates must be confirmed — they are often accelerated.

Print a concise summary table (markdown) with:
- Ticker | Score | Bull case (1 line) | Bear case (1 line) | Verdict

Then state where the HTML report was saved. Do not repeat all the research detail — the HTML has it. Just the conviction ranking and the one-line "why."

### Proactive research trigger

If you surface a ticker with:
- 3+ consecutive days in screener AND
- EPS Q/Q likely distorted (in IPO screener OR spin-off OR character change flag) AND
- Quality Score < 85 (suggesting our system is underselling it)

...run this skill on that ticker without being asked. Flag it as "PROACTIVE RESEARCH" in the output.
