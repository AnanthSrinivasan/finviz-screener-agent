# Spec — Actionable Regime Layer for Sector Rotation Tracker

**Status:** Approved 2026-05-08 · Pending implementation
**Loop:** spec → review → tasks → execute (per CLAUDE.md)
**Builds on:** [sector-rotation.md](sector-rotation.md) (shipped 2026-05-08, commit 74bd7c0)

---

## 1. Problem

The shipped sector rotation tracker classifies the universe into one of five regime tags (`correlation_phase` / `early-rotation` / `mid-rotation` / `late-rotation` / `blow-off-risk`) and surfaces it in Slack. **The tag is descriptive, not prescriptive.** It tells you *what* the market is doing but not *what to do about it*. In a volatile fast-rotation tape (2020 Apr–Sep analog) that gap is the whole problem — knowing the regime without an action map costs more attention than it saves.

Today's Slack output:
```
Phase: `mid-rotation` · Dispersion p50
```
That's it. No guidance. The user has to translate "mid-rotation" → "what does that mean for sizing / entries / trims" every single time, from memory.

## 2. Goals & non-goals

**Goals**
- Each regime tag maps to one concrete action line in the Slack roll-up (sizing, entry posture, trim/hold guidance).
- The action map is data-driven (table in code), versioned, and easy to tweak.
- Action line shows in **every** Slack post (Mon/Thu scheduled + manual `force_slack=true`), not just on regime transitions.

**Non-goals (Phase 1)**
- No mutation of paper executor sizing logic — purely informational.
- No mutation of position monitor stops — purely informational.
- No new regime classifications. Five existing tags only.
- No regime-transition alerts ("regime flipped to blow-off-risk") — that's Phase 2.

## 3. Action map (Phase 1)

| Regime | Sizing posture | Entry posture | Held positions |
|---|---|---|---|
| `correlation_phase` | Size down — beta tape | Trade SPY/QQQ if anything; no sector edge | Hold, no adds |
| `early-rotation` | Normal sizing | Build watchlist in emerging RS leaders; wait 5d confirm before chasing | Hold |
| `mid-rotation` | Full size in GREEN/THRUST · half in CAUTION | Press confirmed RS leaders | Add to leaders, hold others |
| `late-rotation` | Reduce new-entry size 50% | New entries only in fresh leaders (RS rising); skip extended names | Trim names ≥+25% from entry; no adds to leaders |
| `blow-off-risk` | No new entries | Skip all entries | Tighten stops; trim aggressively; cash is a position |

Stored as a Python dict in `agents/sector_rotation.py`:

```python
REGIME_ACTIONS = {
    "correlation_phase": {
        "headline": "Beta tape — no sector edge",
        "sizing":   "Size down. Trade SPY/QQQ if anything.",
        "entries":  "No new sector entries.",
        "held":     "Hold, no adds.",
    },
    "early-rotation": {
        "headline": "Leadership forming",
        "sizing":   "Normal size.",
        "entries":  "Build watchlist in emerging RS leaders. Wait 5d confirm before chasing.",
        "held":     "Hold.",
    },
    "mid-rotation": {
        "headline": "Best entry tape",
        "sizing":   "Full size in GREEN/THRUST · half in CAUTION.",
        "entries":  "Press confirmed RS leaders.",
        "held":     "Add to leaders, hold others.",
    },
    "late-rotation": {
        "headline": "Leadership narrowing",
        "sizing":   "Reduce new-entry size 50%.",
        "entries":  "New entries only in fresh RS-rising leaders. Skip extended names.",
        "held":     "Trim names ≥+25% from entry. No adds to leaders.",
    },
    "blow-off-risk": {
        "headline": "Risk-off",
        "sizing":   "No new entries.",
        "entries":  "Skip all entries.",
        "held":     "Tighten stops · trim aggressively · cash is a position.",
    },
}
```

## 4. Slack format change

Today (current):
```
*Sector Rotation — 2026-05-08*
Phase: `mid-rotation` · Dispersion p50
```

Phase 1 (approved):
```
*Sector Rotation — 2026-05-08*
Phase: `mid-rotation` · Dispersion p50
*Best entry tape*
  • Sizing:  Full size in GREEN/THRUST · half in CAUTION.
  • Entries: Press confirmed RS leaders.
  • Held:    Add to leaders, hold others.
```

The headline renders as its own bolded row beneath the phase line. The three action lines render as a compact bullet block beneath the headline. **Action block ships from day 1** — even on cold start when regime defaults to `correlation_phase` due to empty history. History fills in naturally over subsequent runs.

## 5. Implementation

**Single file change:** `agents/sector_rotation.py`

- Add `REGIME_ACTIONS` dict (constant near the top, after imports).
- Extend `format_slack(snapshot, sig)` to look up the action block by `snapshot["regime"]` and inject it after the phase line.
- Helper `regime_action(regime: str) -> dict | None` — returns the dict or None if regime is unknown (forward-compat for new tags).

**No changes to:**
- Snapshot data shape (`sector_rotation_YYYY-MM-DD.json` stays the same — the action map is a presentation concern, not persisted)
- History file
- Workflow yaml
- Tests in `test_etf_sector_rotation.py` (unchanged)

## 6. Tests (in same commit)

`tests/test_etf_sector_rotation.py` — extend with:
- `test_regime_action_lookup` — every regime tag in `REGIME_ACTIONS` is also returned by `classify_regime()`'s value space (no orphan tags)
- `test_format_slack_includes_action_headline` — synthetic snapshot with `regime="mid-rotation"` → output contains "Best entry tape"
- `test_format_slack_unknown_regime_no_crash` — synthetic snapshot with `regime="unknown"` → falls back gracefully

## 7. Phase 2 (deferred — out of scope for this spec)

After 4 weeks of observed regime tags + Slack posts, evaluate:
- Whether `blow-off-risk` and `late-rotation` tags fire correctly (no false positives in mid-rotation tape).
- If yes, wire to:
  - **Paper executor** (`alpaca_executor.py`): `blow-off-risk` blocks new entries; `late-rotation` reduces `size_mul` by 50% on top of existing market_state sizing.
  - **Position monitor** (`position_monitor.py`): `blow-off-risk` triggers a "TIGHTEN STOPS" Slack alert listing all held positions ≥+10% from entry.
- **Regime transition alerts**: separate Slack post on day a regime changes (`mid-rotation → late-rotation` etc.), keyed off prior day's snapshot.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Action lines turn into ignored boilerplate | Keep them short (3 lines max), action-verb led. If we see ourselves skimming past them, revisit. |
| Action map becomes stale as system evolves | It's a single dict in one file — easy to edit. Tag with date in commit history when the rules change. |
| Wrong regime → wrong action | Phase 1 is informational only. Phase 2 graduation is gated on 4 weeks of validation. |
| Slack volume bloat | Net +4 lines per post. Acceptable. Already gated to Mon/Thu (or manual force). |

## 9. Tasks (numbered, each one committable)

1. **REGIME_ACTIONS dict + helper** — add constant + `regime_action()` to `agents/sector_rotation.py`.
2. **Slack format extension** — update `format_slack()` to inject headline + 3-line action block.
3. **Tests** — three new test cases per §6.
4. **Manual workflow run** — `gh workflow run sector-rotation.yml -f force_slack=true` and verify Slack output formatting.
5. **Docs** — append the action map table + Phase 2 plan to CLAUDE.md "Sector Rotation Tracker" section. Update SYSTEM_DOCS.md §5b.

## 10. Resolved decisions (from review 2026-05-08)

- **Headline placement** — own bolded row beneath the phase line (not inline).
- **Late-rotation trim threshold** — ≥+25% (between T1 +20% and T2 +40%; sits above the first peel and below the +30% trail-floor tier).
- **Cold start** — ship action block from day 1; regime defaults to `correlation_phase` until enough history accumulates, which is fine.
