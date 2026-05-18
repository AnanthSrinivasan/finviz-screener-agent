# Research Log

Findings, reusable analysis scripts, and methodologies. Append-only — when a question recurs ("did we miss X?"), check here BEFORE re-doing the work.

## Index

| Date | Topic | Script | Doc |
|---|---|---|---|
| 2026-05-09 | Missed +30% bursts last 60 days — RKLB/DOCN class | [`utils/retro_missed_bursts.py`](../../utils/retro_missed_bursts.py) | [missed_bursts_2026-05-09.md](missed_bursts_2026-05-09.md) |

## Reusable analysis playbooks

### "Did we miss ticker X / class of names Y?"
1. Run `python3 utils/retro_missed_bursts.py` — covers last 60 days of screener CSVs, classifies which gate excluded each +30% burst.
2. Output: `data/retro_missed_bursts.json` (per-ticker entry close, max close, gain %, gate flags).
3. Inspect with: `python3 -c "import json,pandas as pd; df=pd.DataFrame(json.load(open('data/retro_missed_bursts.json'))); print(df[df.ticker=='X'])"`

### "What does our screener catch vs miss across the universe?"
Same script — bucket counts at the end show: dist>0 (extended), dist<-25 (deep base), ATR>8, Q<70, not-stage-perfect, vs the genuinely-fixable set (stage perfect + Q≥70 + ATR≤8 + in-base).

### "Does proposed gate change X save more bursts?"
Edit `classify_exclusion()` in the retro script — add a new pass condition. Re-run; compare `surfaced` vs `surfaced_wide` counts.

## Data caveats (don't trip on these next time)

- **Pre Mar-25 2026 CSVs lack columns**: ATR%, Dist From High%, Rel Volume, Quality Score were added later. Filter `df.dropna(subset=['dist','rvol','atr','q'])` to compare apples-to-apples.
- **Alpaca free tier**: must use `feed=iex` and `adjustment=split` — `feed=sip` returns 403 on recent data. Hyphen/dot tickers (BF-B, BRK.B) error with 400; filter them out.
- **Base Building feature was added late-Apr 2026** — retro showing `bb=true` on April-mid rows is what *would have* fired had the feature existed; for actual-shipped behavior on those dates, check git log against the screener date.
- **Distance from 52w high is misleading for HTF bases**. RKLB Apr 16 was -16.7% from 52w ATH but -11% from the relevant Jan/Feb swing high. Future analyses should compute swing-high-relative distance for HTF base candidates.
