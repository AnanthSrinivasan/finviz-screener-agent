"""
Unit tests for the Signal Scorecard (docs/specs/signal-scorecard.md §3).

Fires log (finviz_agent §2.1): idempotent same-day rewrite, 400-day trim,
non-fatal on IO error. Scoring (finviz_weekly_agent §2.2): forward-return math
on synthetic bars, younger-than-horizon skip, scored-once cache, flag rule,
excess vs SPY. Render functions are pure — no network.
"""

import json
import os
import tempfile
import unittest

from agents.screener.finviz_agent import (
    SIGNAL_BLOCKS,
    build_signal_fire_records,
    record_signal_fires,
)
from agents.screener.finviz_weekly_agent import (
    SCORECARD_STATUS_OK,
    SCORECARD_STATUS_REVIEW,
    aggregate_scorecard,
    load_signal_fires,
    render_signal_scorecard_html,
    render_signal_scorecard_slack,
    score_fire_outcome,
    score_pending_fires,
)


def make_bars(start_day: int, closes: list, highs: list = None, lows: list = None):
    """Synthetic ascending daily bars starting 2026-01-<start_day>."""
    bars = []
    for i, c in enumerate(closes):
        day = start_day + i
        bars.append({
            "t": f"2026-01-{day:02d}T05:00:00Z",
            "c": c,
            "h": (highs[i] if highs else c),
            "l": (lows[i] if lows else c),
        })
    return bars


def make_fire(date="2026-01-01", block="ready_to_enter", ticker="AAA", **extra):
    fire = {"date": date, "block": block, "ticker": ticker,
            "price": 100.0, "q": 85, "atr_pct": 3.0, "rank_in_block": 1}
    fire.update(extra)
    return fire


class TestBuildFireRecords(unittest.TestCase):
    def test_rank_and_meta(self):
        recs = build_signal_fire_records(
            "2026-01-05",
            {"ready_to_enter": ["AAA", "BBB"]},
            {"AAA": {"price": "1,712.00", "q": 84, "atr_pct": 3.2}},
        )
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["ticker"], "AAA")
        self.assertEqual(recs[0]["rank_in_block"], 1)
        self.assertEqual(recs[0]["price"], 1712.0)
        self.assertEqual(recs[1]["rank_in_block"], 2)
        # Missing meta never drops a fire — fields go null
        self.assertIsNone(recs[1]["price"])

    def test_dedup_and_blank_skip(self):
        recs = build_signal_fire_records(
            "2026-01-05", {"big_movers": ["aaa", "AAA", "", None]}, {})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["ticker"], "AAA")

    def test_all_spec_blocks_enumerated(self):
        self.assertEqual(len(SIGNAL_BLOCKS), 13)
        self.assertIn("ready_to_enter", SIGNAL_BLOCKS)
        self.assertIn("big_movers", SIGNAL_BLOCKS)


class TestRecordSignalFires(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "signal_fires.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_same_day_rewrite(self):
        r1 = [make_fire(ticker="AAA"), make_fire(ticker="BBB")]
        self.assertTrue(record_signal_fires(r1, "2026-01-01", path=self.path))
        r2 = [make_fire(ticker="CCC")]
        self.assertTrue(record_signal_fires(r2, "2026-01-01", path=self.path))
        with open(self.path) as f:
            kept = json.load(f)
        self.assertEqual([r["ticker"] for r in kept], ["CCC"])

    def test_rolling_trim(self):
        old = make_fire(date="2024-01-01", ticker="OLD")
        recent = make_fire(date="2026-06-01", ticker="NEW")
        with open(self.path, "w") as f:
            json.dump([old, recent], f)
        record_signal_fires([make_fire(date="2026-07-01", ticker="TODAY")],
                            "2026-07-01", path=self.path, rolling_days=400)
        with open(self.path) as f:
            kept = json.load(f)
        self.assertEqual({r["ticker"] for r in kept}, {"NEW", "TODAY"})

    def test_nonfatal_on_io_error(self):
        bad_path = os.path.join(self.tmp.name, "not_a_dir_file")
        with open(bad_path, "w") as f:
            f.write("x")
        # parent "directory" is a file → open() for write raises inside
        result = record_signal_fires(
            [make_fire()], "2026-01-01",
            path=os.path.join(bad_path, "fires.json"))
        self.assertFalse(result)  # returned, did not raise

    def test_load_tolerates_missing_and_garbage(self):
        self.assertEqual(load_signal_fires(os.path.join(self.tmp.name, "nope.json")), [])
        with open(self.path, "w") as f:
            f.write("{broken")
        self.assertEqual(load_signal_fires(self.path), [])


class TestScoreFireOutcome(unittest.TestCase):
    def test_forward_return_math(self):
        # Fire on day 1 close=100; 20 forward sessions. Day-5 close 110,
        # day-20 close 120, high touches 111 (hit10), low 95 (dd −5%).
        closes = [100.0] * 5 + [110.0] + [105.0] * 14 + [120.0]
        highs = [c + 1 for c in closes]
        highs[5] = 111.0
        lows = list(closes)
        lows[6] = 95.0
        bars = make_bars(1, closes, highs, lows)
        spy = make_bars(1, [500.0] * 20 + [510.0])  # +2% same window
        out = score_fire_outcome(make_fire(date="2026-01-01"), bars, spy, horizon=20)
        self.assertAlmostEqual(out["ret_5d"], 10.0, places=2)
        self.assertAlmostEqual(out["ret_20d"], 20.0, places=2)
        self.assertTrue(out["hit10_20d"])
        self.assertAlmostEqual(out["max_drawdown_20d"], -5.0, places=2)
        self.assertAlmostEqual(out["spy_ret_20d"], 2.0, places=2)
        self.assertAlmostEqual(out["excess_20d"], 18.0, places=2)

    def test_younger_than_horizon_skipped(self):
        bars = make_bars(1, [100.0] * 10)  # only 9 forward sessions
        self.assertIsNone(score_fire_outcome(make_fire(date="2026-01-01"),
                                             bars, None, horizon=20))

    def test_no_bars_skipped(self):
        self.assertIsNone(score_fire_outcome(make_fire(), [], None))
        self.assertIsNone(score_fire_outcome(make_fire(), None, None))

    def test_scored_cache_respected(self):
        cached = make_fire(ticker="AAA", scored={"ret_5d": 1.0})
        fresh = make_fire(ticker="BBB", date="2026-01-01")
        bars = make_bars(1, [100.0] * 21)
        n = score_pending_fires([cached, fresh], {"BBB": bars}, None, horizon=20)
        self.assertEqual(n, 1)
        self.assertEqual(cached["scored"], {"ret_5d": 1.0})  # untouched
        self.assertIn("scored", fresh)


class TestFlagRule(unittest.TestCase):
    def _fires(self, n, excess, hit10, start="2026-01-01"):
        """n scored fires spread weekly from `start` (spans n weeks)."""
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        out = []
        for i in range(n):
            out.append(make_fire(
                date=(d0 + dt.timedelta(weeks=i)).isoformat(),
                block="base_building", ticker=f"T{i}",
                scored={"ret_5d": -1.0, "ret_20d": excess, "hit10_20d": hit10,
                        "max_drawdown_20d": -5.0, "spy_ret_20d": 0.0,
                        "excess_20d": excess}))
        return out

    def test_flagged_when_all_thresholds_met(self):
        sc = aggregate_scorecard(self._fires(20, -2.0, False), "2026-07-01")
        blk = sc["blocks"]["base_building"]
        self.assertEqual(blk["status"], SCORECARD_STATUS_REVIEW)
        self.assertTrue(blk["newly_flagged"])

    def test_not_flagged_under_min_fires(self):
        sc = aggregate_scorecard(self._fires(19, -2.0, False), "2026-07-01")
        self.assertEqual(sc["blocks"]["base_building"]["status"], SCORECARD_STATUS_OK)

    def test_not_flagged_positive_excess(self):
        sc = aggregate_scorecard(self._fires(20, 2.0, False), "2026-07-01")
        self.assertEqual(sc["blocks"]["base_building"]["status"], SCORECARD_STATUS_OK)

    def test_not_flagged_good_hit10(self):
        sc = aggregate_scorecard(self._fires(20, -2.0, True), "2026-07-01")
        self.assertEqual(sc["blocks"]["base_building"]["status"], SCORECARD_STATUS_OK)

    def test_not_flagged_short_span(self):
        # 20 fires all in one week — weeks_span < 8
        fires = [make_fire(date="2026-06-01", block="base_building", ticker=f"T{i}",
                           scored={"ret_5d": -1.0, "ret_20d": -2.0,
                                   "hit10_20d": False, "max_drawdown_20d": -5.0,
                                   "spy_ret_20d": 0.0, "excess_20d": -2.0})
                 for i in range(20)]
        sc = aggregate_scorecard(fires, "2026-07-01")
        self.assertEqual(sc["blocks"]["base_building"]["status"], SCORECARD_STATUS_OK)

    def test_backfilled_excluded_from_lifetime(self):
        fires = self._fires(20, -2.0, False)
        for f in fires:
            f["backfilled"] = True
        sc = aggregate_scorecard(fires, "2026-07-01")
        blk = sc["blocks"]["base_building"]
        self.assertEqual(blk["lifetime"]["n_fires"], 0)
        self.assertEqual(blk["status"], SCORECARD_STATUS_OK)

    def test_newly_flagged_false_when_already_review(self):
        fires = self._fires(20, -2.0, False)
        prev = {"blocks": {"base_building": {"status": SCORECARD_STATUS_REVIEW}}}
        sc = aggregate_scorecard(fires, "2026-07-01", prev)
        self.assertFalse(sc["blocks"]["base_building"]["newly_flagged"])


class TestRender(unittest.TestCase):
    def _scorecard(self):
        fires = [make_fire(date="2026-05-01", block="ready_to_enter", ticker="AAA",
                           scored={"ret_5d": 3.0, "ret_20d": 8.0, "hit10_20d": True,
                                   "max_drawdown_20d": -2.0, "spy_ret_20d": 1.0,
                                   "excess_20d": 7.0}),
                 make_fire(date="2026-05-01", block="base_building", ticker="BBB",
                           scored={"ret_5d": -2.0, "ret_20d": -4.0, "hit10_20d": False,
                                   "max_drawdown_20d": -9.0, "spy_ret_20d": 1.0,
                                   "excess_20d": -5.0})]
        return aggregate_scorecard(fires, "2026-07-01")

    def test_html_renders_sorted_no_network(self):
        html = render_signal_scorecard_html(self._scorecard())
        self.assertIn("Signal Scorecard", html)
        # positive-excess block sorts above negative
        self.assertLess(html.index("ready_to_enter"), html.index("base_building"))
        self.assertIn("sc-under", html)  # negative-excess amber tint

    def test_html_empty_when_no_blocks(self):
        self.assertEqual(render_signal_scorecard_html({"blocks": {}}), "")

    def test_slack_best_worst(self):
        text = render_signal_scorecard_slack(self._scorecard())
        lines = text.split("\n")
        self.assertLessEqual(len(lines), 3)
        self.assertIn("ready_to_enter", lines[0])
        self.assertIn("base_building", lines[1])

    def test_slack_unrated_fallback(self):
        fires = [make_fire(date="2026-06-30")]  # no scored yet
        sc = aggregate_scorecard(fires, "2026-07-01")
        text = render_signal_scorecard_slack(sc)
        self.assertIn("none past the 20-session horizon", text)


if __name__ == "__main__":
    unittest.main()
