"""Tests for the book digest log persistence."""

import json
import os
import tempfile
import unittest

from agents.trading import book_table


class DigestLogPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_load_missing_returns_empty(self):
        os.unlink(self.path)
        log = book_table.load_digest_log(self.path)
        self.assertEqual(log["events_since_last"], [])
        self.assertEqual(log["last_book_post_ts"], "")

    def test_append_and_save_roundtrip(self):
        log = book_table.load_digest_log(self.path)
        book_table.append_digest_event(log, {"kind": "stop_hit", "ticker": "GLW", "message": "x"})
        book_table.append_digest_event(log, {"kind": "fade", "ticker": "AAOI", "message": "y"})
        book_table.save_digest_log(self.path, log)

        reloaded = book_table.load_digest_log(self.path)
        self.assertEqual(len(reloaded["events_since_last"]), 2)
        self.assertEqual(reloaded["events_since_last"][0]["ticker"], "GLW")

    def test_clear_resets_log_and_records_post_ts(self):
        log = book_table.load_digest_log(self.path)
        book_table.append_digest_event(log, {"kind": "fade", "ticker": "X", "message": "m"})
        book_table.clear_digest_log(log, "2026-05-09T14:30:00Z")
        self.assertEqual(log["events_since_last"], [])
        self.assertEqual(log["last_book_post_ts"], "2026-05-09T14:30:00Z")

    def test_corrupt_file_recovers_to_empty(self):
        with open(self.path, "w") as f:
            f.write("not valid json {{")
        log = book_table.load_digest_log(self.path)
        self.assertEqual(log["events_since_last"], [])


if __name__ == "__main__":
    unittest.main()
