"""Tests for agents.utils.sector_lookup — held-ticker → ETF resolver."""

import json
import os
import tempfile
import unittest
from unittest import mock


class SectorLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.map_path = os.path.join(self.tmpdir, "ticker_sector_map.json")
        with open(self.map_path, "w") as f:
            json.dump({"AAOI": "SMH", "REMX": "REMX"}, f)

        self.env_patch = mock.patch.dict(os.environ, {"DATA_DIR": self.tmpdir})
        self.env_patch.start()

        # Force re-import so DATA_DIR is picked up fresh
        import importlib
        import agents.utils.sector_lookup as sl
        importlib.reload(sl)
        self.sl = sl

    def tearDown(self):
        self.env_patch.stop()
        import importlib
        import agents.utils.sector_lookup as sl
        importlib.reload(sl)

    def test_explicit_map_hit(self):
        self.assertEqual(self.sl.lookup("AAOI"), "SMH")
        self.assertEqual(self.sl.lookup("aaoi"), "SMH")
        self.assertEqual(self.sl.lookup("REMX"), "REMX")

    def test_finviz_fallback(self):
        self.assertEqual(self.sl.lookup("UNKNOWN", finviz_sector="Technology"), "XLK")
        self.assertEqual(self.sl.lookup("UNKNOWN", finviz_sector="Healthcare"), "XLV")
        self.assertEqual(self.sl.lookup("UNKNOWN", finviz_sector="Basic Materials"), "XLB")

    def test_unmapped_returns_none(self):
        self.assertIsNone(self.sl.lookup("UNKNOWN"))
        self.assertIsNone(self.sl.lookup("UNKNOWN", finviz_sector=""))
        self.assertIsNone(self.sl.lookup("UNKNOWN", finviz_sector="Made Up Sector"))
        self.assertIsNone(self.sl.lookup(""))

    def test_explicit_beats_finviz(self):
        # AAOI is mapped to SMH; even if caller passes "Technology", we honor SMH
        self.assertEqual(self.sl.lookup("AAOI", finviz_sector="Technology"), "SMH")


if __name__ == "__main__":
    unittest.main()
