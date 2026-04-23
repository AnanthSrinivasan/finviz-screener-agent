"""Unit tests for the one-time watchlist dedupe migration."""

import unittest

from utils.dedupe_watchlist import dedupe


class TestDedupeWatchlist(unittest.TestCase):
    def test_no_duplicates_passthrough(self):
        rows = [
            {"ticker": "MU", "status": "watching", "priority": "watching", "added": "2026-04-09"},
            {"ticker": "AXTI", "status": "watching", "priority": "watching", "added": "2026-04-14"},
        ]
        deduped, touched = dedupe(rows)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(touched, [])

    def test_archived_plus_focus_keeps_focus(self):
        rows = [
            {
                "ticker": "MRVL",
                "status": "archived",
                "priority": "focus",
                "added": "2026-04-02",
                "focus_promoted_date": "2026-04-14",
                "archive_reason": "age_out",
            },
            {
                "ticker": "MRVL",
                "status": "watching",
                "priority": "focus",
                "added": "2026-04-18",
                "focus_promoted_date": "2026-04-20",
            },
        ]
        deduped, touched = dedupe(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(touched, ["MRVL"])
        winner = deduped[0]
        # Non-archived wins (higher rank), but earliest added/promoted dates merged in
        self.assertEqual(winner["status"], "watching")
        self.assertEqual(winner["priority"], "focus")
        self.assertEqual(winner["added"], "2026-04-02")
        self.assertEqual(winner["focus_promoted_date"], "2026-04-14")

    def test_entry_ready_beats_focus_beats_watching_beats_archived(self):
        rows = [
            {"ticker": "X", "status": "archived", "priority": "watching", "added": "2026-01-01"},
            {"ticker": "X", "status": "watching", "priority": "watching", "added": "2026-02-01"},
            {"ticker": "X", "status": "watching", "priority": "focus", "added": "2026-03-01"},
            {
                "ticker": "X",
                "status": "watching",
                "priority": "entry-ready",
                "added": "2026-04-01",
            },
        ]
        deduped, touched = dedupe(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(touched, ["X"])
        self.assertEqual(deduped[0]["priority"], "entry-ready")
        self.assertEqual(deduped[0]["added"], "2026-01-01")  # earliest

    def test_preserves_earliest_focus_promoted_date(self):
        rows = [
            {
                "ticker": "Y",
                "status": "watching",
                "priority": "focus",
                "added": "2026-04-02",
                "focus_promoted_date": "2026-04-20",
            },
            {
                "ticker": "Y",
                "status": "archived",
                "priority": "focus",
                "added": "2026-03-15",
                "focus_promoted_date": "2026-04-05",
                "archive_reason": "age_out",
            },
        ]
        deduped, _ = dedupe(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["focus_promoted_date"], "2026-04-05")
        self.assertEqual(deduped[0]["added"], "2026-03-15")

    def test_rows_without_ticker_skipped(self):
        rows = [
            {"status": "watching"},  # no ticker — dropped
            {"ticker": "Z", "status": "watching", "priority": "watching", "added": "2026-04-01"},
        ]
        deduped, _ = dedupe(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["ticker"], "Z")

    def test_returns_new_lists(self):
        """dedupe should not mutate input."""
        rows = [
            {"ticker": "A", "status": "watching", "priority": "focus", "added": "2026-04-02"},
            {"ticker": "A", "status": "archived", "priority": "watching", "added": "2026-03-01"},
        ]
        import copy

        rows_snapshot = copy.deepcopy(rows)
        deduped, _ = dedupe(rows)
        self.assertEqual(rows, rows_snapshot, "input list should not be mutated")
        self.assertEqual(len(deduped), 1)


if __name__ == "__main__":
    unittest.main()
