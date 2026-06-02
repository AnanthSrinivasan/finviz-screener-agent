"""Tests for agents.utils.week_ahead_shortlist — Weekly Review §2."""

import unittest

import pandas as pd

from agents.utils.week_ahead_shortlist import (
    DEFAULT_STOP_PCT,
    build_ai_notes_prompt,
    build_shortlist_cards,
    build_trade_plan_card,
    enrich_shortlist_notes_ai,
    render_shortlist_html,
    render_shortlist_slack,
    select_shortlist_candidates,
    _parse_ai_notes,
    _parse_stage,
)


def _row(ticker, q=85, atr=4.0, sma20=1.0, sma50=12.0, dist=-5.0, rs=70,
         stage="Stage 2 (Uptrend)", sector="Technology", company="Co"):
    return {
        "Ticker": ticker, "Quality Score": q, "ATR%": atr, "SMA20%": sma20,
        "SMA50%": sma50, "Dist From High%": dist, "RS Rating": rs,
        "Stage": stage, "Sector": sector, "Company": company,
    }


def _peel_warn_ok(ticker, atr):
    return 10.0  # generous — peel-safe unless sma50/atr huge


class SelectCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.daily = {
            "AAA": _row("AAA", q=90),
            "BBB": _row("BBB", q=80),
            "CCC": _row("CCC", q=70),
            "EXT": _row("EXT", q=88, atr=2.0, sma50=60.0),  # peel_mult 30 → extended
            "ST1": _row("ST1", q=92, stage="Stage 1 (Basing)"),  # not stage 2
        }
        self.watchlist = {"watchlist": [
            {"ticker": "AAA", "priority": "entry-ready", "status": "active"},
            {"ticker": "ARCH", "priority": "entry-ready", "status": "archived"},
        ]}
        self.emerging = pd.DataFrame([{"Ticker": "BBB"}, {"Ticker": "CCC"}])
        self.rs = {"DDD": {"current_status": "active", "last_active_date": "2026-05-30"}}

    def test_dedupes_and_ranks_by_q(self):
        out = select_shortlist_candidates(
            self.emerging, self.watchlist, self.rs, held=set(),
            daily_lookup=self.daily, today="2026-06-01",
            peel_warn_fn=_peel_warn_ok)
        tickers = [c["ticker"] for c in out]
        # AAA(90) > BBB(80) > CCC(70); DDD has no daily row → dropped
        self.assertEqual(tickers, ["AAA", "BBB", "CCC"])
        self.assertEqual(out[0]["source"], "entry-ready")
        self.assertEqual(out[1]["source"], "emerging")

    def test_excludes_held(self):
        out = select_shortlist_candidates(
            self.emerging, self.watchlist, self.rs, held={"AAA"},
            daily_lookup=self.daily, today="2026-06-01",
            peel_warn_fn=_peel_warn_ok)
        self.assertNotIn("AAA", [c["ticker"] for c in out])

    def test_drops_archived_entry_ready(self):
        out = select_shortlist_candidates(
            pd.DataFrame(), {"watchlist": [
                {"ticker": "ARCH", "priority": "entry-ready", "status": "archived"}]},
            {}, held=set(), daily_lookup={"ARCH": _row("ARCH")},
            today="2026-06-01", peel_warn_fn=_peel_warn_ok)
        self.assertEqual(out, [])

    def test_rejects_non_stage2_and_extended(self):
        out = select_shortlist_candidates(
            pd.DataFrame([{"Ticker": "EXT"}, {"Ticker": "ST1"}]),
            {"watchlist": []}, {}, held=set(),
            daily_lookup=self.daily, today="2026-06-01",
            peel_warn_fn=lambda t, a: 5.0)  # tight warn → EXT extended
        self.assertEqual(out, [])

    def test_rs_leader_recency_gate(self):
        old_rs = {"DDD": {"current_status": "active", "last_active_date": "2026-04-01"}}
        out = select_shortlist_candidates(
            pd.DataFrame(), {"watchlist": []}, old_rs, held=set(),
            daily_lookup={"DDD": _row("DDD")}, today="2026-06-01",
            peel_warn_fn=_peel_warn_ok, recent_days=7)
        self.assertEqual(out, [])

    def test_skips_ticker_without_daily_row(self):
        out = select_shortlist_candidates(
            pd.DataFrame([{"Ticker": "ZZZ"}]), {"watchlist": []}, {},
            held=set(), daily_lookup={}, today="2026-06-01",
            peel_warn_fn=_peel_warn_ok)
        self.assertEqual(out, [])


class ParseStageTests(unittest.TestCase):
    def test_dict_repr_string_from_csv(self):
        raw = "{'stage': 2, 'badge': '🟢 Stage 2', 'perfect': True, 'sma20': 3.3}"
        out = _parse_stage(raw)
        self.assertEqual(out["stage"], 2)
        self.assertTrue(out["perfect"])
        self.assertEqual(out["label"], "Stage 2 perfect")

    def test_dict_object(self):
        out = _parse_stage({"stage": 3, "perfect": False})
        self.assertEqual(out["stage"], 3)
        self.assertEqual(out["label"], "Stage 3")

    def test_plain_word_label(self):
        out = _parse_stage("Stage 2 (Uptrend)")
        self.assertEqual(out["stage"], 2)
        self.assertEqual(out["label"], "Stage 2 (Uptrend)")

    def test_non_stage2_label(self):
        self.assertIsNone(_parse_stage("Stage 1 (Basing)")["stage"])

    def test_real_csv_stage_renders_clean_in_card(self):
        daily = {"CGNX": {
            "Ticker": "CGNX", "Quality Score": 97, "ATR%": 4.1, "SMA20%": 3.3,
            "SMA50%": 15.7, "Dist From High%": -8.0, "RS Rating": 60,
            "Stage": "{'stage': 2, 'perfect': True}", "Sector": "Technology",
            "Company": "Cognex",
        }}
        out = select_shortlist_candidates(
            pd.DataFrame([{"Ticker": "CGNX"}]), {"watchlist": []}, {},
            held=set(), daily_lookup=daily, today="2026-06-02",
            peel_warn_fn=_peel_warn_ok)
        self.assertEqual(out[0]["stage"], "Stage 2 perfect")
        self.assertTrue(out[0]["stage_perfect"])


class CardBuilderTests(unittest.TestCase):
    def _cand(self, **kw):
        base = {
            "ticker": "AAA", "source": "emerging", "source_label": "emerging",
            "company": "Co", "sector": "Technology", "q": 85.0, "atr_pct": 4.0,
            "sma20_pct": 1.0, "sma50_pct": 12.0, "dist52": -5.0, "rs": 70,
            "stage": "Stage 2 (Uptrend)", "peel_mult": 3.0, "peel_warn": 10.0,
        }
        base.update(kw)
        return base

    def test_stop_uses_mae_floor_for_low_atr(self):
        card = build_trade_plan_card(self._cand(atr_pct=3.0), "GREEN")
        # 2×ATR=6 < 8 floor → stop -8%
        self.assertEqual(card["stop_pct"], -8.0)
        self.assertIn("50 SMA", card["stop"])

    def test_stop_widens_for_high_atr(self):
        card = build_trade_plan_card(self._cand(atr_pct=6.0), "GREEN")
        # 2×ATR=12 > 8 → stop -12%
        self.assertEqual(card["stop_pct"], -12.0)

    def test_size_full_in_green(self):
        self.assertEqual(build_trade_plan_card(self._cand(atr_pct=4.0), "GREEN")["size"], "Full")

    def test_size_half_in_caution(self):
        self.assertEqual(build_trade_plan_card(self._cand(), "CAUTION")["size"], "Half")

    def test_size_blocked_in_extended(self):
        self.assertEqual(build_trade_plan_card(self._cand(), "EXTENDED")["size"],
                         "No new entries")

    def test_high_vol_downgrades_size(self):
        card = build_trade_plan_card(self._cand(atr_pct=9.0), "GREEN")
        self.assertEqual(card["size"], "Half (high vol)")

    def test_trigger_pullback_vs_extended(self):
        pb = build_trade_plan_card(self._cand(sma20_pct=-3.0), "GREEN")
        self.assertIn("Reclaim", pb["trigger"])
        ext = build_trade_plan_card(self._cand(sma20_pct=10.0), "GREEN")
        self.assertIn("pullback", ext["trigger"].lower())

    def test_default_stop_constant(self):
        self.assertEqual(DEFAULT_STOP_PCT, -8.0)


class AiNotesTests(unittest.TestCase):
    def _cards(self):
        cands = [{
            "ticker": "AAA", "source": "emerging", "source_label": "emerging",
            "company": "Co", "sector": "Tech", "q": 85.0, "atr_pct": 4.0,
            "sma20_pct": 1.0, "sma50_pct": 12.0, "dist52": -5.0, "rs": 70,
            "stage": "Stage 2", "peel_mult": 3.0, "peel_warn": 10.0,
        }]
        return build_shortlist_cards(cands, "GREEN")

    def test_prompt_lists_tickers(self):
        p = build_ai_notes_prompt(self._cards(), "GREEN")
        self.assertIn("AAA", p)
        self.assertIn("invalidation", p.lower())

    def test_parse_notes(self):
        text = "AAA | setup: clean base breakout | invalidation: loses 21 EMA"
        out = _parse_ai_notes(text)
        self.assertEqual(out["AAA"]["setup"], "clean base breakout")
        self.assertEqual(out["AAA"]["invalidation"], "loses 21 EMA")

    def test_enrich_overwrites_via_post_fn(self):
        cards = self._cards()
        enrich_shortlist_notes_ai(
            cards, "GREEN", api_key="x",
            post_fn=lambda p: "AAA | setup: tight VCP | invalidation: below 50 SMA")
        self.assertEqual(cards[0]["setup_note"], "tight VCP")
        self.assertEqual(cards[0]["invalidation"], "below 50 SMA")

    def test_enrich_noop_without_key(self):
        cards = self._cards()
        before = cards[0]["invalidation"]
        enrich_shortlist_notes_ai(cards, "GREEN", api_key="", post_fn=lambda p: "x")
        self.assertEqual(cards[0]["invalidation"], before)
        self.assertNotIn("setup_note", cards[0])


class RenderTests(unittest.TestCase):
    def _cards(self):
        cands = [{
            "ticker": "AAA", "source": "entry-ready", "source_label": "entry-ready",
            "company": "Alpha Co", "sector": "Technology", "q": 85.0, "atr_pct": 4.0,
            "sma20_pct": 1.0, "sma50_pct": 12.0, "dist52": -5.0, "rs": 70,
            "stage": "Stage 2 (Uptrend)", "peel_mult": 3.0, "peel_warn": 10.0,
        }]
        return build_shortlist_cards(cands, "GREEN")

    def test_html_has_plan_fields(self):
        html = render_shortlist_html(self._cards())
        for label in ("Setup", "Trigger", "Stop", "Size", "Invalidation"):
            self.assertIn(label, html)
        self.assertIn("AAA", html)
        self.assertNotIn("#0f172a", html)  # light theme

    def test_html_empty_state(self):
        html = render_shortlist_html([])
        self.assertIn("Cash is a position", html)

    def test_slack_has_plan_fields(self):
        txt = render_shortlist_slack(self._cards())
        self.assertIn("Week-Ahead Shortlist", txt)
        self.assertIn("Trigger", txt)
        self.assertIn("Stop", txt)

    def test_slack_empty_state(self):
        self.assertIn("Cash is a position", render_shortlist_slack([]))


if __name__ == "__main__":
    unittest.main()
