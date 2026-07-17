"""Tests for agents.utils.trader_mirror — monthly You-vs-System scorecard.

Spec: docs/specs/trader-mirror.md §3. No network — bars are synthetic and the
fetcher is injected everywhere.
"""

import datetime
import json
import os
import tempfile
import unittest

from agents.trading import rules
from agents.utils.trader_mirror import (
    INITIAL_STOP_BASE_PCT,
    build_mirror_summary,
    classify_trade,
    extract_trade_events,
    month_window,
    normalize_bars,
    render_mirror_html,
    render_mirror_slack,
    replay_system_exit,
    run_trader_mirror,
    score_trade,
    wilder_atr_pct_series,
)


def make_bars(start="2026-05-01", ohlc=None):
    """Build weekday bars from a list of (open, high, low, close)."""
    d = datetime.date.fromisoformat(start)
    bars = []
    for o, h, l, c in ohlc:
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        bars.append({"date": d.isoformat(), "open": float(o), "high": float(h),
                     "low": float(l), "close": float(c)})
        d += datetime.timedelta(days=1)
    return bars


def flat_bars(n, price=100.0, spread=1.0):
    return [(price, price + spread, price - spread, price)] * n


class MonthWindowTests(unittest.TestCase):
    def test_previous_month(self):
        label, start, end = month_window(datetime.date(2026, 7, 4))
        self.assertEqual(label, "2026-06")
        self.assertEqual(start, "2026-06-01")
        self.assertEqual(end, "2026-06-30")

    def test_january_wraps_to_december(self):
        label, start, end = month_window(datetime.date(2026, 1, 3))
        self.assertEqual(label, "2025-12")
        self.assertEqual(start, "2025-12-01")
        self.assertEqual(end, "2025-12-31")


class ReplayParityTests(unittest.TestCase):
    """Synthetic bars where the rules exit is known — the replay must match an
    independent drive of rules.apply_position_rules over the same series."""

    def _rally_then_break(self):
        # 20 warmup bars at 100 (ATR ~2%), then a rally to 131 and a crash bar.
        seq = flat_bars(20)
        rally = [103, 106, 110, 115, 120, 126, 131]
        seq += [(c - 1, c + 1, c - 2, c) for c in rally]
        seq += [(126, 127, 110, 112)]          # crash bar — breaches the trail
        return make_bars(ohlc=seq)

    def test_exit_at_close_after_stop_signal(self):
        bars = self._rally_then_break()
        entry_idx = 20
        entry_date = bars[entry_idx]["date"]
        out = replay_system_exit(bars, entry_date, 100.0, ticker="TST")
        self.assertIsNotNone(out)
        self.assertEqual(out["exit_kind"], "stop")
        # System exit fills at the CLOSE of the signal day (no look-ahead).
        self.assertEqual(out["exit_date"], bars[-1]["date"])
        self.assertEqual(out["exit_price"], 112.0)

        # Independent engine drive: same bars, same ATR series, same init.
        atr = wilder_atr_pct_series(bars)
        atr_entry = atr[entry_idx] or 0.0
        state = {
            "entry_price": 100.0,
            "stop_price": round(100.0 * (1 - (INITIAL_STOP_BASE_PCT + 0.5 * atr_entry) / 100.0), 2),
            "highest_price_seen": 100.0, "peak_gain_pct": 0.0,
            "breakeven_activated": False,
            "target1": rules.compute_targets(100.0, atr_entry)[0], "target1_hit": False,
            "target2": rules.compute_targets(100.0, atr_entry)[1], "target2_hit": False,
        }
        for i in range(entry_idx + 1, len(bars) - 1):
            a = atr[i] if atr[i] is not None else atr_entry
            rules.apply_position_rules("TST", state, bars[i]["close"], bars[i]["high"], a)
        # The stop the crash bar breached is the engine's stop entering the day.
        self.assertEqual(out["stop_at_signal"], round(state["stop_price"], 2))
        self.assertLessEqual(bars[-1]["low"], out["stop_at_signal"])

    def test_still_long_at_horizon_marks_open(self):
        # Gentle rise, lows never near the stop → engine still long at the end.
        seq = flat_bars(20)
        seq += [(100 + i * 0.5, 100.6 + i * 0.5, 99.8 + i * 0.5, 100.2 + i * 0.5)
                for i in range(1, 8)]
        bars = make_bars(ohlc=seq)
        out = replay_system_exit(bars, bars[20]["date"], 100.0)
        self.assertEqual(out["exit_kind"], "open")
        self.assertEqual(out["exit_date"], bars[-1]["date"])
        self.assertEqual(out["exit_price"], round(bars[-1]["close"], 2))

    def test_entry_bar_missing_returns_none(self):
        bars = make_bars(ohlc=flat_bars(5))
        self.assertIsNone(replay_system_exit(bars, "2030-01-01", 100.0))
        self.assertIsNone(replay_system_exit([], "2026-05-01", 100.0))


class ClassifierTests(unittest.TestCase):
    """Each leak classifier + first-match-wins precedence (spec §2.2)."""

    def _bars(self, n=15, price=100.0, highs=None):
        seq = []
        for i in range(n):
            h = highs[i] if highs and i < len(highs) else price + 0.5
            seq.append((price, h, price - 0.5, price))
        return make_bars(ohlc=seq)

    def test_hold_in_hope(self):
        bars = self._bars(12)
        replay = {"exit_kind": "stop", "exit_date": bars[5]["date"],
                  "exit_price": 95.0, "stop_at_signal": 96.0, "entry_index": 0}
        leak, note = classify_trade(100.0, 90.0, bars[9]["date"], 100,
                                    replay, bars, delta_usd=500.0)
        self.assertEqual(leak, "hold_in_hope")
        self.assertIn("held 4 extra days", note)
        self.assertIn("$96.00", note)
        self.assertIn("$500", note)

    def test_hold_in_hope_needs_two_extra_sessions(self):
        bars = self._bars(12)
        replay = {"exit_kind": "stop", "exit_date": bars[5]["date"],
                  "exit_price": 95.0, "stop_at_signal": 96.0, "entry_index": 0}
        # Only 1 session past the signal → not hold_in_hope.
        leak, _ = classify_trade(100.0, 90.0, bars[6]["date"], 100,
                                 replay, bars, delta_usd=500.0)
        self.assertNotEqual(leak, "hold_in_hope")

    def test_round_trip(self):
        # Peaked +20% during the hold, exited +2%.
        highs = [100.5, 105, 112, 120, 118, 110, 105, 103]
        bars = self._bars(10, highs=highs)
        replay = {"exit_kind": "stop", "exit_date": bars[6]["date"],
                  "exit_price": 103.0, "stop_at_signal": 104.0, "entry_index": 0}
        # 1 session after signal (fails hold_in_hope), exit above stop anyway.
        leak, note = classify_trade(100.0, 102.0, bars[7]["date"], 50,
                                    replay, bars, delta_usd=50.0)
        self.assertEqual(leak, "round_trip")
        self.assertIn("peaked +20.0%", note)
        self.assertIn("exited +2.0%", note)
        # gave back (120 - 102) * 50 = $900 from the peak
        self.assertIn("$900", note)

    def test_early_exit(self):
        # User sells day 4 at 105; closes run to 112 within 10 sessions;
        # system still long at the horizon.
        seq = [(100, 100.5, 99.5, 100)] * 4 + [(105, 105.5, 104.5, 105)]
        seq += [(c, c + 0.5, c - 0.5, c) for c in (107, 109, 112, 111, 110)]
        bars = make_bars(ohlc=seq)
        replay = {"exit_kind": "open", "exit_date": bars[-1]["date"],
                  "exit_price": 110.0, "stop_at_signal": 98.0, "entry_index": 0}
        leak, note = classify_trade(100.0, 105.0, bars[4]["date"], 100,
                                    replay, bars, delta_usd=500.0)
        self.assertEqual(leak, "early_exit")
        self.assertIn("system exit was " + bars[-1]["date"], note)
        self.assertIn("$110.00", note)

    def test_early_exit_requires_5pct_upside(self):
        # Price only reaches +3% after the sale → disciplined, not early_exit.
        seq = [(100, 100.5, 99.5, 100)] * 4 + [(105, 105.5, 104.5, 105)]
        seq += [(c, c + 0.5, c - 0.5, c) for c in (106, 107, 108, 107, 106)]
        bars = make_bars(ohlc=seq)
        replay = {"exit_kind": "open", "exit_date": bars[-1]["date"],
                  "exit_price": 106.0, "stop_at_signal": 98.0, "entry_index": 0}
        leak, _ = classify_trade(100.0, 105.0, bars[4]["date"], 100,
                                 replay, bars, delta_usd=100.0)
        self.assertEqual(leak, "disciplined")

    def test_disciplined_beat_the_system(self):
        bars = self._bars(8)
        replay = {"exit_kind": "stop", "exit_date": bars[5]["date"],
                  "exit_price": 103.0, "stop_at_signal": 102.0, "entry_index": 0}
        leak, note = classify_trade(100.0, 106.0, bars[5]["date"], 100,
                                    replay, bars, delta_usd=-300.0)
        self.assertEqual(leak, "disciplined")
        self.assertIn("beat the system by $300", note)

    def test_precedence_hold_in_hope_beats_round_trip(self):
        # Trade qualifies for BOTH: peak +20% & exit +2% (round_trip) AND held
        # 3 sessions past a stop signal below which it exited (hold_in_hope).
        highs = [100.5, 110, 120, 118, 110, 105, 103, 102.5, 102.2]
        bars = self._bars(12, highs=highs)
        replay = {"exit_kind": "stop", "exit_date": bars[4]["date"],
                  "exit_price": 108.0, "stop_at_signal": 109.0, "entry_index": 0}
        leak, _ = classify_trade(100.0, 102.0, bars[8]["date"], 100,
                                 replay, bars, delta_usd=600.0)
        self.assertEqual(leak, "hold_in_hope")


class ExtractTradeEventsTests(unittest.TestCase):
    EVENTS = [
        {"date": "2026-05-01T10:00:00Z", "action": "BUY", "shares": 100, "price": 50.0},
        {"date": "2026-05-15T10:00:00Z", "action": "SELL", "shares": 100, "price": 55.0},
        {"date": "2026-06-02T10:00:00Z", "action": "BUY", "shares": 40, "price": 60.0},
        {"date": "2026-06-20T10:00:00Z", "action": "SELL", "shares": 40, "price": 58.0},
    ]

    def test_picks_episode_matching_close_date(self):
        ep = extract_trade_events(self.EVENTS, "2026-06-20")
        self.assertEqual(len(ep), 2)
        self.assertEqual(ep[0]["date"][:10], "2026-06-02")

    def test_first_episode_by_close_date(self):
        ep = extract_trade_events(self.EVENTS, "2026-05-16")
        self.assertEqual(ep[1]["price"], 55.0)

    def test_no_match_returns_none(self):
        self.assertIsNone(extract_trade_events(self.EVENTS, "2026-08-01"))
        self.assertIsNone(extract_trade_events([], "2026-06-20"))
        # BUY-only history (no SELL fills) → None
        self.assertIsNone(extract_trade_events(
            [{"date": "2026-06-01T10:00:00Z", "action": "BUY", "shares": 10, "price": 1.0}],
            "2026-06-20"))


class PartialSellDeltaTests(unittest.TestCase):
    def test_fifo_delta_with_two_sells(self):
        # Rising bars, stop never hit → system exits at the horizon close.
        seq = flat_bars(20)
        seq += [(100 + i, 100.6 + i, 99.8 + i, 100.2 + i) for i in range(1, 11)]
        bars = make_bars(ohlc=seq)
        entry_date = bars[20]["date"]
        sell1_date = bars[24]["date"]
        sell2_date = bars[26]["date"]
        pos = {"ticker": "PSL", "entry_price": 100.0, "entry_date": entry_date,
               "close_date": sell2_date}
        events = [
            {"date": entry_date + "T14:00:00Z", "action": "BUY", "shares": 100, "price": 100.0},
            {"date": sell1_date + "T14:00:00Z", "action": "SELL", "shares": 50, "price": 110.0},
            {"date": sell2_date + "T14:00:00Z", "action": "SELL", "shares": 50, "price": 105.0},
        ]
        out = score_trade(pos, events, lambda t, s, e: bars)
        self.assertEqual(out["status"], "scored")
        sys_exit = round(bars[-1]["close"], 2)
        self.assertEqual(out["system_exit_price"], sys_exit)
        # delta = sys_exit × total_sold − actual proceeds (pnl_walk semantics)
        expected = sys_exit * 100 - (50 * 110.0 + 50 * 105.0)
        self.assertAlmostEqual(out["delta_usd"], round(expected, 2), places=2)
        # weighted-average actual exit
        self.assertAlmostEqual(out["actual_exit_price"], 107.5, places=2)
        self.assertEqual(out["actual_exit_date"], sell2_date)


class MonthWindowSelectionTests(unittest.TestCase):
    def test_only_trades_closed_in_window_are_scored(self):
        closed = [
            {"ticker": "MAY", "entry_price": 10, "entry_date": "2026-05-01",
             "close_date": "2026-05-20"},
            {"ticker": "JUN", "entry_price": 10, "entry_date": "2026-06-01",
             "close_date": "2026-06-10"},
        ]
        summary = build_mirror_summary(closed, {}, "2026-06-01", "2026-06-30",
                                       lambda t, s, e: [], month_label="2026-06")
        tickers = [t["ticker"] for t in summary["trades"]]
        self.assertEqual(tickers, ["JUN"])


class UnscoredTests(unittest.TestCase):
    def test_no_fills_is_unscored_never_guessed(self):
        closed = [{"ticker": "GHOST", "entry_price": 50, "entry_date": "2026-06-01",
                   "close_date": "2026-06-15"}]
        summary = build_mirror_summary(closed, {}, "2026-06-01", "2026-06-30",
                                       lambda t, s, e: self.fail("must not fetch bars"))
        t = summary["trades"][0]
        self.assertEqual(t["status"], "unscored")
        self.assertIn("no recoverable fills", t["note"])
        self.assertIsNone(t["leak"])
        self.assertEqual(summary["unscored_count"], 1)
        self.assertEqual(summary["scored_count"], 0)

    def test_no_bars_is_unscored(self):
        history = {"NOBAR": [
            {"date": "2026-06-01T14:00:00Z", "action": "BUY", "shares": 10, "price": 50.0},
            {"date": "2026-06-15T14:00:00Z", "action": "SELL", "shares": 10, "price": 48.0},
        ]}
        closed = [{"ticker": "NOBAR", "entry_price": 50, "entry_date": "2026-06-01",
                   "close_date": "2026-06-15"}]
        summary = build_mirror_summary(closed, history, "2026-06-01", "2026-06-30",
                                       lambda t, s, e: [])
        self.assertEqual(summary["trades"][0]["status"], "unscored")
        self.assertIn("no daily bars", summary["trades"][0]["note"])


class EndToEndHoldInHopeTests(unittest.TestCase):
    """Full pipeline: bars → replay → fills → classification → buckets."""

    def _fixture(self):
        seq = flat_bars(20)                       # warmup, ATR ~2%
        seq += flat_bars(2)                       # entry + 1 quiet session
        seq += [(99, 99, 93, 93.5)]               # breach — system exit @ 93.5
        seq += [(92, 92.5, 91, 92), (91, 91.5, 90, 91), (90, 90.5, 89, 90)]
        bars = make_bars(ohlc=seq)
        entry_date = bars[20]["date"]
        exit_date = bars[-1]["date"]              # 3 sessions past the signal
        pos = {"ticker": "HIH", "entry_price": 100.0, "entry_date": entry_date,
               "close_date": exit_date}
        history = {"HIH": [
            {"date": entry_date + "T14:00:00Z", "action": "BUY", "shares": 100, "price": 100.0},
            {"date": exit_date + "T14:00:00Z", "action": "SELL", "shares": 100, "price": 90.0},
        ]}
        return bars, pos, history, entry_date, exit_date

    def test_classified_and_bucketed(self):
        bars, pos, history, entry_date, exit_date = self._fixture()
        start = entry_date[:7] + "-01"
        summary = build_mirror_summary([pos], history, start, "2026-12-31",
                                       lambda t, s, e: bars, month_label="2026-06")
        t = summary["trades"][0]
        self.assertEqual(t["status"], "scored")
        self.assertEqual(t["leak"], "hold_in_hope")
        self.assertEqual(t["system_exit_price"], 93.5)
        self.assertAlmostEqual(t["delta_usd"], (93.5 - 90.0) * 100, places=2)
        bucket = summary["buckets"]["hold_in_hope"]
        self.assertEqual(bucket["count"], 1)
        self.assertEqual(bucket["tickers"], ["HIH"])
        self.assertAlmostEqual(summary["total_left_usd"], 350.0, places=2)


class RenderTests(unittest.TestCase):
    """Renderers are pure and import-safe — no network, no env."""

    def _summary(self, total=2340.0):
        return {
            "month": "2026-06",
            "window": {"start": "2026-06-01", "end": "2026-06-30"},
            "generated": "2026-07-04",
            "trades": [
                {"status": "scored", "ticker": "VIK", "entry_price": 80.0,
                 "entry_date": "2026-06-02", "actual_exit_price": 82.0,
                 "actual_exit_date": "2026-06-20", "system_exit_price": 95.0,
                 "system_exit_date": "2026-06-15", "system_exit_kind": "stop",
                 "delta_usd": 1800.0, "leak": "hold_in_hope",
                 "note": "stop said out at $90.00 on 2026-06-15"},
                {"status": "scored", "ticker": "Z", "entry_price": 50.0,
                 "entry_date": "2026-06-05", "actual_exit_price": 51.0,
                 "actual_exit_date": "2026-06-25", "system_exit_price": 56.4,
                 "system_exit_date": "2026-06-18", "system_exit_kind": "stop",
                 "delta_usd": 540.0, "leak": "round_trip",
                 "note": "peaked +18.0%, exited +2.0%"},
                {"status": "scored", "ticker": "OK", "entry_price": 20.0,
                 "entry_date": "2026-06-08", "actual_exit_price": 23.0,
                 "actual_exit_date": "2026-06-22", "system_exit_price": 22.9,
                 "system_exit_date": "2026-06-22", "system_exit_kind": "stop",
                 "delta_usd": -10.0, "leak": "disciplined",
                 "note": "beat the system by $10"},
                {"status": "unscored", "ticker": "NA", "entry_date": "2026-06-01",
                 "close_date": "2026-06-12", "leak": None, "delta_usd": 0.0,
                 "note": "no recoverable fills in position_history"},
            ],
            "buckets": {
                "hold_in_hope": {"count": 1, "delta_usd": 1800.0, "tickers": ["VIK"]},
                "round_trip": {"count": 1, "delta_usd": 540.0, "tickers": ["Z"]},
                "early_exit": {"count": 0, "delta_usd": 0.0, "tickers": []},
                "disciplined": {"count": 1, "delta_usd": -10.0, "tickers": ["OK"]},
            },
            "total_left_usd": total,
            "scored_count": 3, "unscored_count": 1, "disciplined_count": 1,
        }

    def test_slack_verdict_first_and_five_lines_max(self):
        text = render_mirror_slack(self._summary(), html_url="https://x/mirror.html")
        lines = text.split("\n")
        self.assertLessEqual(len(lines), 5)
        self.assertIn("Trader Mirror — June", lines[0])
        self.assertIn("you left $2,340 on the table.", lines[0])
        self.assertIn("hold-in-hope $1,800 (VIK)", lines[1])
        self.assertIn("round-trip $540 (Z)", lines[1])
        self.assertIn("early-exit $0", lines[1])
        self.assertIn("disciplined: 1 of 3 trades", lines[2])
        self.assertIn("unscored: 1", lines[2])

    def test_slack_neutral_month_stated_plainly(self):
        text = render_mirror_slack(self._summary(total=120.0))
        self.assertIn("a wash", text.split("\n")[0])
        self.assertNotIn("on the table", text)

    def test_slack_user_beat_the_system(self):
        text = render_mirror_slack(self._summary(total=-900.0))
        self.assertIn("you beat the system by $900.", text.split("\n")[0])

    def test_html_renders_trades_and_trend(self):
        html = render_mirror_html(self._summary(), prior_summaries=[])
        self.assertIn("Trader Mirror — 2026-06", html)
        self.assertIn("VIK", html)
        self.assertIn("hold-in-hope", html)
        self.assertIn("unscored", html)
        self.assertIn("3-month trend", html)
        self.assertIn("You left $2,340 on the table", html)

    def test_normalize_bars(self):
        raw = [{"t": "2026-06-01T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5},
               {"t": "2026-06-02T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": None}]
        out = normalize_bars(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], "2026-06-01")
        self.assertEqual(out[0]["close"], 1.5)


class RunTraderMirrorTests(unittest.TestCase):
    def test_writes_html_and_json_no_network(self):
        seq = flat_bars(20) + flat_bars(2) + [(99, 99, 93, 93.5)]
        seq += [(92, 92.5, 91, 92), (91, 91.5, 90, 91), (90, 90.5, 89, 90)]
        bars = make_bars(start="2026-05-01", ohlc=seq)
        entry_date, exit_date = bars[20]["date"], bars[-1]["date"]
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "positions.json"), "w") as f:
                json.dump({"open_positions": [], "closed_positions": [
                    {"ticker": "HIH", "entry_price": 100.0, "entry_date": entry_date,
                     "close_date": exit_date}]}, f)
            with open(os.path.join(td, "position_history.json"), "w") as f:
                json.dump({"updated": "x", "history": {"HIH": [
                    {"date": entry_date + "T14:00:00Z", "action": "BUY",
                     "shares": 100, "price": 100.0},
                    {"date": exit_date + "T14:00:00Z", "action": "SELL",
                     "shares": 100, "price": 90.0}]}}, f)
            # exit_date lands in June 2026 → run on the first Saturday of July.
            summary = run_trader_mirror(
                td, slack_webhook="", pages_base="",
                today=datetime.date(2026, 7, 4), bars_fetcher=lambda t, s, e: bars)
            self.assertEqual(summary["month"], "2026-06")
            self.assertEqual(summary["scored_count"], 1)
            self.assertTrue(os.path.exists(os.path.join(td, "trader_mirror_2026-06.html")))
            with open(os.path.join(td, "trader_mirror_2026-06.json")) as f:
                saved = json.load(f)
            self.assertEqual(saved["buckets"]["hold_in_hope"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
