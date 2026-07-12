import unittest

from utils.generators import generate_daily_cockpit as cp


class TestGateDecision(unittest.TestCase):
    def test_full_size_green(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "normal")
        self.assertEqual(g["action"], "FULL SIZE")
        self.assertEqual(g["color"], "green")
        self.assertEqual(g["cap"], 10)

    def test_half_size_caution(self):
        g = cp.gate_decision("CAUTION", "early-rotation", "normal")
        self.assertEqual(g["action"], "HALF SIZE")
        self.assertEqual(g["cap"], 7)

    def test_no_new_extended(self):
        g = cp.gate_decision("EXTENDED", "mid-rotation", "normal")
        self.assertEqual(g["action"], "NO NEW ENTRIES")
        self.assertEqual(g["color"], "red")
        self.assertEqual(g["cap"], 5)

    def test_regime_riskoff_overrides_full(self):
        g = cp.gate_decision("GREEN", "blow-off-risk", "normal")
        self.assertEqual(g["action"], "NO NEW ENTRIES")
        self.assertIn("blow-off-risk", g["detail"])

    def test_suspended_forces_paper(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "suspended")
        self.assertEqual(g["action"], "PAPER ONLY")

    def test_reduced_caps_full_to_half(self):
        g = cp.gate_decision("THRUST", "mid-rotation", "reduced")
        self.assertEqual(g["action"], "HALF SIZE")
        self.assertIn("reduced", g["detail"])

    def test_reduced_keeps_no_new_when_already_closed(self):
        g = cp.gate_decision("RED", "correlation_phase", "reduced")
        self.assertEqual(g["action"], "NO NEW ENTRIES")


class TestDisciplineLine(unittest.TestCase):
    def test_capital_preservation_when_gate_closed(self):
        g = cp.gate_decision("EXTENDED", "blow-off-risk", "reduced")
        line = cp.discipline_line({"current_sizing_mode": "reduced", "consecutive_losses": 2}, g)
        self.assertIn("Capital preservation", line)

    def test_winning_streak_warns_roundtrip(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "aggressive")
        line = cp.discipline_line({"consecutive_wins": 3}, g)
        self.assertIn("round-trip", line.lower())


class TestQualify(unittest.TestCase):
    def _row(self, **kw):
        base = {"Ticker": "AAA", "Quality Score": "85", "ATR%": "5", "Dist From High%": "-6",
                "Rel Volume": "1.0", "VCP": "80", "SMA20%": "3", "SMA50%": "8", "SMA200%": "20",
                "Company": "Co", "Sector": "Tech"}
        base.update(kw)
        return base

    def test_clean_setup_passes(self):
        out = cp.qualify_setups([self._row()], held=set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ticker"], "AAA")
        self.assertGreaterEqual(out[0]["stop_pct"], 8.0)  # -8% MAE floor

    def test_held_excluded(self):
        out = cp.qualify_setups([self._row()], held={"AAA"})
        self.assertEqual(out, [])

    def test_low_q_rejected(self):
        out = cp.qualify_setups([self._row(**{"Quality Score": "70"})], held=set())
        self.assertEqual(out, [])

    def test_extended_dist_rejected(self):
        out = cp.qualify_setups([self._row(**{"Dist From High%": "0.5"})], held=set())
        self.assertEqual(out, [])

    def test_not_stage2_perfect_rejected(self):
        # 50MA below 200MA (s200 < s50) => not perfect
        out = cp.qualify_setups([self._row(**{"SMA50%": "25", "SMA200%": "10"})], held=set())
        self.assertEqual(out, [])

    def test_peel_unsafe_rejected(self):
        # low-vol tier warn=3; s50/atr = 20/4 = 5 > 3 => extended
        out = cp.qualify_setups([self._row(**{"ATR%": "4", "SMA50%": "20"})], held=set())
        self.assertEqual(out, [])

    def test_top_n_cap(self):
        rows = [self._row(Ticker=f"T{i}", **{"Quality Score": str(80 + i)}) for i in range(6)]
        out = cp.qualify_setups(rows, held=set(), top_n=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["ticker"], "T5")  # highest Q first


class TestRecordStats(unittest.TestCase):
    def test_payoff_and_winrate(self):
        ts = {"total_wins": 16, "total_losses": 24, "current_sizing_mode": "reduced"}
        closed = [{"result_pct": 30}, {"result_pct": 20}, {"result_pct": -10}, {"result_pct": -5}]
        s = cp.record_stats(ts, closed)
        self.assertAlmostEqual(s["win_rate"], 40.0)
        self.assertAlmostEqual(s["avg_win"], 25.0)
        self.assertAlmostEqual(s["avg_loss"], -7.5)
        self.assertGreater(s["payoff"], 3.0)

    def test_empty_closed_safe(self):
        s = cp.record_stats({"total_wins": 0, "total_losses": 0}, [])
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["payoff"], 0.0)


def _wl(ticker, priority="entry-ready", status="watching"):
    return {"ticker": ticker, "priority": priority, "status": status}


def _srow(ticker, s20="1.0", dist="-6", q="85", price="100"):
    return {"Ticker": ticker, "SMA20%": s20, "Dist From High%": dist,
            "Quality Score": q, "Price": price}


class TestRadarPick(unittest.TestCase):
    def test_ranked_by_proximity_then_q(self):
        wl = [_wl("FAR"), _wl("NEAR"), _wl("TIE_LO"), _wl("TIE_HI")]
        rows = [_srow("FAR", s20="5.0"), _srow("NEAR", s20="0.4"),
                _srow("TIE_LO", s20="2.0", q="80"), _srow("TIE_HI", s20="2.0", q="95")]
        radar = cp.radar_pick(wl, rows)
        order = [n["ticker"] for n in radar["entry-ready"]]
        self.assertEqual(order, ["NEAR", "TIE_HI", "TIE_LO", "FAR"])

    def test_top_5_cap_per_tier(self):
        wl = [_wl(f"T{i}") for i in range(7)]
        rows = [_srow(f"T{i}", s20=str(i)) for i in range(7)]
        radar = cp.radar_pick(wl, rows)
        self.assertEqual(len(radar["entry-ready"]), 5)

    def test_archived_excluded_and_watching_absent(self):
        wl = [_wl("ZOMBIE", status="archived"), _wl("OK"),
              _wl("WATCHER", priority="watching"), _wl("FOC", priority="focus")]
        rows = [_srow(t) for t in ("ZOMBIE", "OK", "WATCHER", "FOC")]
        radar = cp.radar_pick(wl, rows)
        self.assertEqual([n["ticker"] for n in radar["entry-ready"]], ["OK"])
        self.assertEqual([n["ticker"] for n in radar["focus"]], ["FOC"])
        self.assertNotIn("watching", radar)

    def test_missing_from_screener_ranks_last(self):
        wl = [_wl("GONE"), _wl("HERE")]
        rows = [_srow("HERE", s20="4.0")]
        radar = cp.radar_pick(wl, rows)
        self.assertEqual([n["ticker"] for n in radar["entry-ready"]], ["HERE", "GONE"])
        self.assertIn("no fresh screener data", radar["entry-ready"][1]["trigger"])


class TestRadarTrigger(unittest.TestCase):
    def test_at_ema(self):
        self.assertIn("buy the hold", cp.radar_trigger(1.2, -8, 100))
        self.assertIn("buy the hold", cp.radar_trigger(-1.5, -8, 100))

    def test_pivot_near_high(self):
        # 2% below the 52w high at $98 → pivot ≈ $100
        t = cp.radar_trigger(3.0, -2.0, 98.0)
        self.assertIn("pivot", t)
        self.assertIn("$100.00", t)

    def test_pullback_when_extended(self):
        # 4% above the 21 EMA at $104 → pullback level ≈ $100
        t = cp.radar_trigger(4.0, -8.0, 104.0)
        self.assertIn("pullback to", t)
        self.assertIn("$100.00", t)

    def test_below_ema_wait_for_reclaim(self):
        self.assertIn("reclaim", cp.radar_trigger(-3.0, -8.0, 100.0))


class TestRenderRadar(unittest.TestCase):
    def test_gate_closed_greys_tables(self):
        g = cp.gate_decision("RED", "", "normal")
        radar = cp.radar_pick([_wl("AAA")], [_srow("AAA")])
        html = cp.render_radar(radar, g)
        self.assertIn("radar-closed", html)
        self.assertIn("WATCH-ONLY", html)

    def test_gate_open_no_grey(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "normal")
        radar = cp.radar_pick([_wl("AAA")], [_srow("AAA")])
        html = cp.render_radar(radar, g)
        self.assertNotIn("radar-closed", html)

    def test_empty_radar_safe(self):
        g = cp.gate_decision("GREEN", "mid-rotation", "normal")
        self.assertIn("Radar empty", cp.render_radar({"entry-ready": [], "focus": []}, g))


def _etf(ticker, rs, delta, bucket="NEUTRAL"):
    return {"ticker": ticker, "name": ticker, "bucket": bucket,
            "rs_score": rs, "rank_delta_5d": delta}


class TestLeadershipFlows(unittest.TestCase):
    def test_flow_vs_structure_separation(self):
        # XLP/XLRE-class: BASE bucket but RS ~22 and falling ~20 ranks —
        # must appear in neither "flowing in" nor "bases worth screening".
        rotation = {"regime": "mid-rotation", "etfs": [
            _etf("XLP", 22, 21, bucket="BASE"),
            _etf("XLRE", 24, 19, bucket="BASE"),
            _etf("SMH", 95, -4, bucket="EXTENDED"),
            _etf("KRE", 66, -6, bucket="BASE"),
            _etf("WEAKCLIMB", 30, -8),   # climbing but RS<50 → not "flowing in"
        ]}
        fl = cp.leadership_flows(rotation)
        in_tickers = [e["ticker"] for e in fl["flowing_in"]]
        self.assertEqual(in_tickers, ["KRE", "SMH"])   # most-negative delta first
        self.assertNotIn("WEAKCLIMB", in_tickers)
        base_tickers = [e["ticker"] for e in fl["bases"]]
        self.assertEqual(base_tickers, ["KRE"])        # weak bases dropped, not shown
        out_tickers = [e["ticker"] for e in fl["flowing_out"]]
        self.assertEqual(out_tickers, ["XLP", "XLRE"])  # worst delta first

    def test_spread_sanity_threshold(self):
        wide = {"etfs": [_etf("SMH", 95, 0), _etf("IGV", 88, 0), _etf("XLP", 20, 0)]}
        narrow = {"etfs": [_etf("A", 60, 0), _etf("B", 40, 0)]}
        self.assertTrue(cp.leadership_flows(wide)["spread_wide"])
        self.assertEqual([e["ticker"] for e in cp.leadership_flows(wide)["leaders"]],
                         ["SMH", "IGV"])
        self.assertFalse(cp.leadership_flows(narrow)["spread_wide"])

    def test_render_appends_spread_note_only_when_wide(self):
        wide = {"regime": "correlation_phase", "etfs": [
            _etf("SMH", 95, -2), _etf("XLP", 20, 5)]}
        html = cp.render_leadership(wide)
        self.assertIn("spread is wide", html)
        self.assertIn("SMH", html)
        narrow = {"regime": "correlation_phase", "etfs": [
            _etf("A", 60, -2), _etf("B", 40, 5)]}
        self.assertNotIn("spread is wide", cp.render_leadership(narrow))


class TestRenderSmoke(unittest.TestCase):
    def test_empty_book_and_qualified_render(self):
        g = cp.gate_decision("EXTENDED", "blow-off-risk", "reduced")
        self.assertIn("100% cash", cp.render_book([], {}))
        self.assertIn("patience", cp.render_qualified([], g).lower())

    def test_leadership_empty_safe(self):
        self.assertIn("unavailable", cp.render_leadership({}))


if __name__ == "__main__":
    unittest.main()
