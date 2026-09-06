# EU screener — EXPERIMENT

Isolated by design. Imports nothing from `agents/` or `utils/`, writes nothing
to `data/`, and no workflow calls it. Delete this folder and production is
unchanged.

## What it does

Applies the **technical half** of the Ready-to-Enter gate to any market:

| Gate | Threshold |
|---|---|
| Stage 2 | 50 MA above 200 MA, price not >10% under the 50 |
| Stage 2 perfect | also above the 20 MA (required unless dist ≤ −10%) |
| Distance from 52w high | −1% to −12% |
| ATR% | ≤ 7% |
| Relative volume | ≤ 1.2 |
| Peel-safe | ATR multiple from 50 MA ≤ tier warn (3.0 / 5.0 / 6.5 / 8.5) |

## What it deliberately does NOT do

**Quality Score** and **RS Rating** are absent. Q needs Finviz fundamental
fields that do not exist for EU listings; RS is a percentile *within a scored
universe*, which is meaningless on a small hand-fed list. So this ranks on
setup cleanliness only — least extended first — not on leadership.

## Usage

    python3 run.py export.csv          # passers only
    python3 run.py export.csv --all    # plus rejects with reasons

TradingView → Screener → market → columns Price, SMA20, SMA50, SMA200, ATR,
Relative Volume, 52-week High → Export. Header spellings vary; unmatched
columns cause a loud exit listing what was found, fixable in `ALIASES`.

## Tests

    python3 -m unittest test_criteria -v

10 tests, no network, no fixtures beyond real numbers. The anchor is
**Rheinmetall (XETR:RHM) on 2026-09-05** — read off the user's own chart. The
engine reproduces TradingView's `ATR% Multiple From MA = -1.37` and
`% Gain From MA = -5.57%` to two decimals, then rejects the name as Stage 4.

That fixture exists because on 2026-09-06 the model asserted European defense
showed "Stage 2 structure" from memory, with no data. RHM was 48% off its high,
below a declining 200 MA. The test encodes the correct answer so the claim
cannot be made again silently.

## Known limitation

The thresholds here are a **copy** of the production ones (CLAUDE.md,
2026-09-06) and can drift from them. That is the accepted cost of isolation.
If this graduates, delete them and import the real predicates instead.
