"""Recent-events feed durability.

A single stray byte froze data/recent_events.json from 2026-04-27 to
2026-08-19: json.load raised, the outer handler swallowed it as a warning,
and every append after that was silently discarded — so the Live Portfolio
"Recent market events" panel missed four months of market-state transitions
including the 2026-08-18 TREND-FOLLOW -> RED flip.
"""
import json
import os
import shutil
import tempfile
import unittest


class AppendRecentEventTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self.path = os.path.join(self.dir, "recent_events.json")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev
        shutil.rmtree(self.dir, ignore_errors=True)

    def _append(self, title):
        from utils.events import _append_recent_event
        _append_recent_event(category="market_state", title=title, severity="high")

    def _titles(self):
        with open(self.path) as f:
            return [e["title"] for e in json.load(f)["events"]]

    def test_writes_to_fresh_file(self):
        self._append("first")
        self.assertEqual(self._titles(), ["first"])

    def test_corrupt_file_self_heals_and_salvages_history(self):
        good = {"updated": "x", "events": [{"title": "old", "category": "market_state"}]}
        with open(self.path, "w") as f:
            f.write(json.dumps(good) + "}")  # the exact real-world corruption
        with open(self.path) as f:
            self.assertRaises(ValueError, json.load, f)

        self._append("TREND-FOLLOW -> RED")

        titles = self._titles()
        self.assertIn("TREND-FOLLOW -> RED", titles)
        self.assertIn("old", titles, "prior events should survive the repair")

    def test_unsalvageable_file_resets_rather_than_dying(self):
        with open(self.path, "w") as f:
            f.write("!!!not json at all!!!")
        self._append("new event")
        self.assertEqual(self._titles(), ["new event"])

    def test_write_is_atomic_no_temp_left_behind(self):
        self._append("a")
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_respects_max_keep(self):
        from utils.events import _append_recent_event
        for i in range(8):
            _append_recent_event(category="market_state", title=f"e{i}", max_keep=5)
        self.assertEqual(self._titles(), ["e3", "e4", "e5", "e6", "e7"])


if __name__ == "__main__":
    unittest.main()
