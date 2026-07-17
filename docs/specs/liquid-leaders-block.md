# Spec: 🏛 Liquid Leaders Block

## Problem

The screener is blind to mega-cap ($100B+) institutional names doing clean 21 EMA bounces. These names (UNH, CVS, ELV, CI, LLY, ISRG, ABT, etc.) never trip any of the 7 existing Finviz screens because they're not making 52w highs, don't have accelerating Q/Q EPS, and aren't moving 10%/day. They're grinding recoveries or steady uptrends with 2-4% ATR — perfect for sizing $30-40k and riding the 21 EMA.

The XBI/IHF June 2026 move exposed this: system served volatile small-cap biotech names (ZVRA 5.3% ATR, BEAM 6.9% ATR) while missing the IHF heavyweights (UNH 2.3%, CVS 2.4%) that were textbook 21 EMA bounces and far better R:R for real capital.

## Solution

Add a **standalone Finviz screen** for mega-cap liquid leaders, independent of the existing 7-screen funnel. Own URL → own snapshot fetch → own filtering → slots into Slack + HTML gallery as a new block.

## Finviz Screen URL

```
https://finviz.com/screener.ashx?v=111&f=cap_mega,ind_stocksonly,sh_avgvol_o1000,ta_sma20_pa,ta_sma50_pa,ta_sma200_pa&ft=4
```

Filters:
- `cap_mega` = Market Cap > $200B (Finviz's mega filter). We also add a second pass with `cap_largeover` ($10B+) filtered to >$100B in code to catch the $100B-$200B band (ELV, CI, HCA, ISRG class).
- `ind_stocksonly` = no ETFs/funds
- `sh_avgvol_o1000` = >1M avg shares (these are mega-caps, always liquid)
- `ta_sma20_pa` = price above SMA20
- `ta_sma50_pa` = price above SMA50
- `ta_sma200_pa` = price above SMA200

## ETF Rotation Gate (required)

The block **only fires when the parent ETF is rotating in**. A mega-cap bouncing off 21 EMA without sector tailwind is noise. With the sector moving, it's conviction.

**Resolution**: each mega-cap ticker → parent ETF via `agents/utils/sector_lookup.py` (existing: `resolve_etf(ticker, industry, sector)` → ETF symbol). Then check today's `data/sector_rotation_YYYY-MM-DD.json` for that ETF:

```python
def _etf_is_rotating_in(etf_ticker: str, sector_snapshot: dict) -> bool:
    """ETF qualifies if rank improving AND not in the bottom half."""
    etf_data = sector_snapshot.get(etf_ticker)
    if not etf_data:
        return False
    rank = etf_data.get("rank", 99)
    rank_delta_5d = etf_data.get("rank_delta_5d", 0)
    # Rotating in: rank improving (delta negative) AND currently top half
    # OR strongly rising from anywhere (delta <= -8)
    return (rank <= 15 and rank_delta_5d <= -3) or (rank_delta_5d <= -8)
```

Thresholds:
- **Top-half + climbing**: `rank ≤ 15 AND rank_delta_5d ≤ -3` (ETF in upper half of universe, improving). Catches XBI moving from rank 12→6.
- **Strongly rising from anywhere**: `rank_delta_5d ≤ -8` (massive rotation regardless of current rank). Catches the early signal when a beaten-down sector suddenly accelerates.

This means the block can be **empty on many days** — that's correct. It fires when there's a real sector rotation + mega-cap 21 EMA setup confluence. When it does fire, you size with conviction.

## Code-Level Filtering (post-snapshot)

Predicate: `_is_liquid_leader(row, sector_snapshot)`:

```python
def _is_liquid_leader(row, sector_snapshot: dict) -> bool:
    """Mega-cap institutional names at/near 21 EMA — size $30-40k.
    Only fires when parent ETF is rotating in."""
    mcap = row.get("Market Cap")  # parsed float in millions
    if mcap is None or mcap < 100_000:  # < $100B
        return False
    atr = row.get("ATR%")
    if atr is None or atr < 1.5 or atr > 4.5:
        return False
    sma20 = row.get("SMA20%")
    sma50 = row.get("SMA50%")
    sma200 = row.get("SMA200%")
    if any(v is None for v in (sma20, sma50, sma200)):
        return False
    # At or near 21 EMA: SMA20% in [-3%, +6%]
    if sma20 < -3 or sma20 > 6:
        return False
    # Above 50 SMA and 200 SMA
    if sma50 <= 0 or sma200 <= 0:
        return False
    # ETF rotation gate
    industry = row.get("Industry", "")
    sector = row.get("Sector", "")
    ticker = row.get("Ticker", "")
    etf = resolve_etf(ticker, industry, sector)
    if not etf or not _etf_is_rotating_in(etf, sector_snapshot):
        return False
    return True
```

Key differences from other blocks:
- **ETF rotation gate required** — no sector tailwind = no signal. This is what gives sizing conviction.
- **No Q score gate** — irrelevant for this class (EPS not accelerating)
- **No Stage 2 requirement** — just needs price above all MAs (which is Stage 2 by definition if 50>200, but we don't check perfect alignment)
- **No VCP/RVol/persistence** — these are institutional grinds, not breakout setups
- **No peel-warn gate** — a mega-cap 5× above 50 SMA with 2.3% ATR is NOT extended in the same way a small-cap is
- **ATR% 1.5–4.5** — the sweet spot for sizing $30-40k (tight stop, big notional)
- **SMA20% [-3%, +6%]** — captures both the active bounce (+1 to +6, already off the EMA) and the approach/test (-3 to 0, sitting right on it). Wider than the 21 EMA Pullback block's [-2%, +3%] because mega-caps have shallower pullbacks.

## Sorting

Sorted by **proximity to 21 EMA** (absolute value of SMA20%), ascending — closest to bounce = most actionable entry.

## Output

### Slack Block

Position: **after 🎯 Ready to Enter, before 🚀 Fresh Breakout** (high priority — these are full-conviction entries).

```
:classical_building: *Liquid Leaders* (mega-cap 21 EMA bounce — sector rotating in, size $30-40k):
• *UNH* — XBI ↗ rank 6 (up 8) · ATR 2.3% · S20 +3.9% · $427 · /stock-research UNH
• *CVS* — XBI ↗ rank 6 (up 8) · ATR 2.4% · S20 +3.9% · $104 · /stock-research CVS
• *ELV* — XBI ↗ rank 6 (up 8) · ATR 2.8% · S20 +1.2% · $512 · /stock-research ELV
```

Top 5 by proximity to 21 EMA. Each line shows the **parent ETF + rotation signal** so you immediately see WHY this name is actionable today.

### HTML Gallery

New `<details open>` section "🏛 Liquid Leaders" with chart cards. Each card gets a blue `MEGA` badge + ATR% + "Size: $30-40k" annotation.

### Daily Cockpit

Feed into §3 Qualified Today when gate is open — these names are always entry-eligible regardless of growth criteria since they're a different sizing lane.

## Integration Points

### File: `agents/screener/finviz_agent.py`

1. **New screen in `screener_urls` dict** — `"Liquid Leaders"` key with the Finviz URL above. This is an 8th screen but these names are treated separately (own pipeline, not merged into the growth scoring).

   Actually — **better approach**: DON'T add to `screener_urls`. That would merge these into `summary_df` and subject them to the $30M dollar-volume gate, Quality Score, Stage analysis etc. Instead:

   **Standalone fetch in `main()`** after the primary pipeline completes:
   - Own `fetch_liquid_leaders()` function: hits the Finviz mega-cap URL, parses tickers, fetches snapshots (reuses `fetch_snapshots_concurrent`), filters with `_is_liquid_leader()`.
   - Returns a list of dicts with: ticker, price, ATR%, SMA20%, SMA50%, SMA200%, market_cap.
   - Injected into `send_slack_notification()` as a separate arg.
   - Injected into `build_chart_gallery()` as a separate section.

2. **Exclusion**: skip any ticker already in `open_positions` (from positions.json).

3. **Dedup against summary_df**: if a mega-cap happens to also trip a growth screen (unlikely but possible), don't double-show it. The Liquid Leaders block takes priority for display (it's the correct framing for the name).

### Workflow

Runs inside `daily-finviz.yml` — no new workflow needed. The extra Finviz screen + ~10-20 snapshot fetches adds ~30-60s to the run.

### Watchlist

**No auto-add.** These are rotation plays — enter on the bounce, ride to the next leg, exit. They don't belong in the persistent growth watchlist. If the user wants to track one, manual add via workflow_dispatch.

## Test Plan

1. Unit test `_is_liquid_leader()` with UNH/CVS-like rows (pass) and small-cap/high-ATR rows (fail).
2. Unit test `fetch_liquid_leaders()` with mocked Finviz response.
3. Integration: `gh workflow run daily-finviz.yml` → verify Slack output includes the 🏛 block.
4. Verify UNH/CVS appear when their SMA20% is in [-3%, +6%] range.

## What This Does NOT Do

- Does not replace any existing block — complementary lane
- Does not auto-trade these (executor stays growth-focused for now)
- Does not add to watchlist (different sizing/management philosophy)
- Does not use AI commentary (unnecessary — the entry is pure: bounce off 21 EMA, stop below 50 SMA)

## Sizing Philosophy (annotated in output)

- Entry: 21 EMA bounce (SMA20% crossing from negative to positive, or holding 0-3%)
- Stop: below 50 SMA (SMA50% going negative) = ~5-8% risk on a 2-4% ATR name
- Size: $30-40k (vs $5-10k on a typical growth name)
- Target: next leg (+8-15%), or trail with 21 EMA
- The point: same $ P&L as a volatile small-cap trade, 1/3 the stress, 3× the hit rate
