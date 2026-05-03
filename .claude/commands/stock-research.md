# Stock Research

Deep research on one or more tickers. Produces an HTML report sized to the question being asked: **upside/risk** for held names, **entry conviction** for new candidates.

**Usage:** `/stock-research COHR` · `/stock-research LITE BE CORZ`

Arguments (`$ARGUMENTS`) are space-separated tickers.

**STOP — check arguments first.** If `$ARGUMENTS` is empty or blank, do NOT proceed. Reply with exactly: `Which ticker(s) do you want to research? (e.g. COHR, or LITE BE CORZ for multiple)` — then wait for the user's reply before doing anything else.

---

## Mandatory step 0 — Position + flag context

This step has burned us twice. **Do it first, get it right, do not skip.**

For each ticker, run a single Python block (cd into repo root) that:

```python
import json, glob, os
ticker = "TICKER"

# 1. Held? (positions.json has open_positions LIST, not dict)
p = json.load(open('data/positions.json'))
held = next((x for x in p.get('open_positions', []) if x.get('ticker') == ticker), None)

# 2. Hidden Growth flag? When did it fire and why?
hg = json.load(open('data/hidden_growth.json')) if os.path.exists('data/hidden_growth.json') else {}
hg_hit = next((c for c in hg.get('candidates', []) if c.get('ticker') == ticker), None)

# 3. Watchlist tier and source path
wl = json.load(open('data/watchlist.json')) if os.path.exists('data/watchlist.json') else {}
wl_entry = next((w for w in wl.get('entries', []) if w.get('ticker') == ticker), None)

# 4. Latest market state — read history file (rolling), not dated file
mh = json.load(open('data/market_monitor_history.json'))
state = mh.get('history', [{}])[-1].get('market_state') if isinstance(mh.get('history'), list) else mh.get('market_state')

# 5. Trading state
ts = json.load(open('data/trading_state.json'))
print(json.dumps({'held': held, 'hg_hit': hg_hit, 'wl': wl_entry, 'state': state,
                  'sizing': ts.get('current_sizing_mode'),
                  'streak': f"{ts.get('consecutive_wins')}W/{ts.get('consecutive_losses')}L"}, indent=2))
```

If any file shape doesn't match, **do not guess** — read the file and find the real shape before continuing. The schema bug is the #1 thing that has gone wrong here.

**Mode switch based on what you find:**

- **Held** → "potential & risk" mode. Skip the 9-dim entry score. The user already decided to enter; the question is *how high can it go and what would knock it down.*
- **Not held** → "entry conviction" mode. Score on 9 dims, give a verdict gated by market state and sizing mode.

State `RED` / `BLACKOUT` blocks new entries (Rule 6) but does NOT change the report on a held name — exit management is independent.

---

## Step 1 — Chart vision

Fetch `https://finviz.com/chart.ashx?t={TICKER}&ty=c&ta=1&p=d`. The URL redirects to `charts2.finviz.com` then to `charts2-node.finviz.com` — follow both redirects, then read the resulting PNG via the Read tool with the saved path from WebFetch's binary output.

Assess:
- Price vs SMA 20/50/200; slope of each
- 21 EMA pullback proximity (Qullamaggie entry)
- VCP base shape: contractions, volume drying
- Distance from 52-week high
- Recent volume — accumulation or distribution
- Risk: gap-fill, climax run, broken structure
- Setup grade: **A** (textbook), **B** (developing), **C** (broken/extended)

One sentence on the chart card: `Chart: A — [pattern in <12 words]`.

---

## Step 2 — Web research (parallel, 4 searches per ticker)

Run all 4 in a single tool batch:

1. **Earnings + estimates** — `"{TICKER} earnings EPS analyst estimates revised forward {YEAR} {YEAR+1}"`
2. **Analyst price targets** — `"{TICKER} price target high low median analyst {MONTH} {YEAR}"` (this is mandatory; the skill failed once by not pulling explicit targets)
3. **Insiders + ownership** — `"{TICKER} insider transactions Form 4 last 90 days institutional ownership 13D 13G {YEAR}"`
4. **Catalyst + bear** — `"{TICKER} TAM bear case short interest next catalyst earnings date {MONTH} {YEAR}"`

For each ticker, extract concrete numbers:

- **Earnings panel:** last 4 quarters EPS actual / estimate / beat-miss %, revenue YoY %, accelerating/decelerating
- **Forward estimates:** consensus EPS next 2 quarters and FY+1, # of upward revisions vs downward in last 30 days, management long-term targets
- **Analyst targets:** high / median / low / 12-month, # of buy/hold/sell ratings, recent target raises or cuts
- **Insider activity (last 90 days):** Form 4 buys/sells with $ amounts and price; named insiders; net buying or net selling
- **Institutional cycle:** named funds initiating/adding/trimming with sizes; phase tag (Early / Adoption / Mature / Distribution)
- **Catalyst calendar (next 90 days):** next earnings date, FDA / regulatory / product launch / index inclusion / lockup expiry
- **TAM + product cycle:** market size today and 3 years out, share, S-curve position
- **Bear case (mandatory specifics):** product/customer concentration, debt + interest as % of revenue, named short thesis (Kerrisdale / Muddy Waters / Hindenburg / etc.), competitive threats, what data point flips the thesis
- **SNDK check:** is TTM EPS distorted? What does Q/Q reveal that TTM hides?

If a search returns nothing useful for a panel, say so explicitly in the report — do not fabricate.

---

## Step 3a — For NEW candidates: 9-dimension score

| Dimension | HIGH (3) | MOD (2) | LOW (1) |
|-----------|----------|---------|---------|
| EPS beat trend | Accelerating 3+ qtrs | 1-2 qtrs beat | Miss or flat |
| Estimate revisions | Revised UP >10% | Flat | Revised DOWN |
| Revenue acceleration | YoY accelerating | Stable | Decelerating |
| Inst adoption phase | Phase 3 (adoption) | Phase 4 (mature) | Phase 0-2 / 5 (distrib) |
| IPO cycle | Actionable | Watch | Not yet |
| TAM position | Early S-curve, large | Mid S-curve | Late / small |
| Short interest | Low + decreasing | Neutral | High or rising |
| Stage 2 | Perfect alignment | Basic Stage 2 | Not Stage 2 |
| SNDK distortion | High (system underselling) | Moderate | Low |

Total /27. >20 = HIGH · 14–20 = MODERATE/watchlist · <14 = SKIP.

Verdict gates: state RED/BLACKOUT → WATCH only. Sizing `suspended` → paper only. Sizing `reduced` → max 5% of book.

---

## Step 3b — For HELD names: upside / risk scenario tables

**Skip the 9-dim score.** Replace with two tables driven by analyst targets + chart structure.

**Upside table:**

| Target | Price | Gain from current | Trigger |
|--------|-------|-------------------|---------|
| T1 (rules engine) | entry × 1.20 | +X% | Sell half + tighten trail |
| Median analyst 12mo | $X | +X% | Sell-side base case |
| T2 (rules engine) | entry × 1.40 | +X% | Trail tight |
| High analyst 12mo | $X | +X% | Bull case validated |
| Bull-case fundamentals | $X | +X% | Specific multiple × forward EPS scenario |

**Downside table:**

| Risk level | Price | Drop from current | Action |
|------------|-------|-------------------|--------|
| Current stop | $X | -X% | Auto-exit |
| 21 EMA | $X | -X% | Trim watch |
| 50 SMA | $X | -X% | Stage 2 in jeopardy |
| 200 SMA | $X | -X% | Stage 4 — out |
| Bear-case multiple compression | $X | -X% | Thesis broken |

**Horizon:** how many quarters does the bull thesis need? What's the next 1-2 earnings prints supposed to show?

**What flips this** (3-5 specific watch items):
- e.g. "next quarter rev growth <15%" / "named short publishes" / "insider sells >$X" / "key customer ABC announces in-house alternative"

---

## Step 4 — HTML report

Write to `data/stock_research/stock_research_{YYYY-MM-DD}.html`. Light theme:

- bg `#f9fafb` · cards `#ffffff` · text `#111827`
- positive `#16a34a` · negative `#dc2626` · accent `#2563eb`
- system-ui font

Structure:

1. Header — date, # tickers, market state banner (RED/CAUTION/GREEN/THRUST/COOLING/DANGER/BLACKOUT), sizing mode
2. Summary table — Ticker · Held? · Score (or n/a) · Bull · Bear · Verdict
3. Per-ticker card:
   - Position context box (if held: shares/entry/current/peak/stop/T1/T2/breakeven flag)
   - Hidden Growth box (if flagged: date, criteria, signal_score)
   - Chart grade + 1-line description
   - **Held names:** upside table + downside table + horizon + "what flips this"
   - **New names:** 9-dim score table + earnings panel + analyst panel + insider panel + catalyst calendar + bear case
4. Footer — generated by skill, timestamp

Do NOT save chart images during research. The `data/chart_patterns/winners/` folder belongs to the position monitor.

---

## Step 5 — Console output

Print a markdown table:

`Ticker | Held | Bull (≤12 words) | Bear (≤12 words) | Verdict / Action`

Then state the HTML path. Don't reprint the report content. Verdict for held names is exit-management oriented (e.g. "Hold to T1 $X · trim if breaks $Y"), not "BUY".

**Cheerleading is banned.** Every ticker gets a real bear sentence with specifics (a number, a name, a date). Generic platitudes ("competition exists", "valuation rich") are not a bear case — they're filler. If you cannot name what kills the thesis, search again.

---

## Failure modes to avoid (from past runs)

1. **Wrong schema** — `positions.json` has `open_positions` (list), not `positions` (dict). Iterate, don't subscript by ticker.
2. **Stale market state** — read `market_monitor_history.json` (rolling, fresh) NOT the latest dated file (may be a day stale).
3. **No analyst targets** — if you don't quote high/median/low/12mo, you have not done the research.
4. **No insider data** — Form 4 lookups in last 90 days are mandatory for any name up >50% YoY.
5. **Generic bear case** — must be specific (number, name, date). "Single-product risk" alone is not enough.
6. **Score theater on held names** — you already own it; the score is moot. Use the upside/downside tables instead.

---

## Proactive trigger

If a ticker hits all three:
- 3+ consecutive days in daily screener
- EPS Q/Q likely distorted (IPO screener / spin-off / character-change flag) — i.e. SNDK pattern
- Quality Score < 85 (system underselling)

…run this skill unprompted. Tag the report `PROACTIVE RESEARCH`.
