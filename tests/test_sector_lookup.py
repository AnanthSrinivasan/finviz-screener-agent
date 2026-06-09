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

    def test_industry_semiconductors_routes_to_smh(self):
        # Semis subsector should route to SMH, not XLK
        self.assertEqual(
            self.sl.lookup("AMAT", finviz_sector="Technology",
                           finviz_industry="Semiconductor Equipment & Materials"),
            "SMH",
        )
        self.assertEqual(
            self.sl.lookup("LRCX", finviz_sector="Technology",
                           finviz_industry="Semiconductors"),
            "SMH",
        )

    def test_industry_software_routes_to_igv(self):
        # Software subsectors should route to IGV, not XLK
        self.assertEqual(
            self.sl.lookup("SNOW", finviz_sector="Technology",
                           finviz_industry="Software - Application"),
            "IGV",
        )
        self.assertEqual(
            self.sl.lookup("PANW", finviz_sector="Technology",
                           finviz_industry="Software - Infrastructure"),
            "IGV",
        )

    def test_industry_biotech_routes_to_xbi(self):
        self.assertEqual(
            self.sl.lookup("MRNA", finviz_sector="Healthcare",
                           finviz_industry="Biotechnology"),
            "XBI",
        )
        self.assertEqual(
            self.sl.lookup("PFE", finviz_sector="Healthcare",
                           finviz_industry="Drug Manufacturers - General"),
            "XBI",
        )

    def test_industry_banks_routes_to_kbe(self):
        self.assertEqual(
            self.sl.lookup("WFC", finviz_sector="Financial",
                           finviz_industry="Banks - Diversified"),
            "KBE",
        )
        self.assertEqual(
            self.sl.lookup("RF", finviz_sector="Financial",
                           finviz_industry="Banks - Regional"),
            "KBE",
        )

    def test_industry_homebuilders_routes_to_xhb(self):
        self.assertEqual(
            self.sl.lookup("DHI", finviz_sector="Consumer Cyclical",
                           finviz_industry="Residential Construction"),
            "XHB",
        )

    def test_ticker_map_beats_industry(self):
        # AAOI is explicitly mapped to SMH; even with industry="Communication Equipment"
        # (which would otherwise fall through to sector XLK), the ticker map wins.
        self.assertEqual(
            self.sl.lookup("AAOI", finviz_sector="Technology",
                           finviz_industry="Communication Equipment"),
            "SMH",
        )

    def test_industry_beats_sector_fallback(self):
        # Industry routing must take precedence over the sector map.
        # Sector "Technology" alone → XLK; industry "Semiconductors" → SMH
        self.assertEqual(
            self.sl.lookup("UNKNOWN_SEMI", finviz_sector="Technology",
                           finviz_industry="Semiconductors"),
            "SMH",
        )

    def test_unknown_industry_falls_through_to_sector(self):
        # Industry that doesn't match any INDUSTRY_TO_ETF key falls through to sector map.
        self.assertEqual(
            self.sl.lookup("UNKNOWN", finviz_sector="Technology",
                           finviz_industry="Communication Equipment"),
            "XLK",
        )

    def test_credit_services_routes_to_arkf(self):
        # Fintech / consumer-finance industries → ARKF (DAVE/SoFi/AFRM class).
        self.assertEqual(
            self.sl.lookup("SOFI", finviz_sector="Financial",
                           finviz_industry="Credit Services"),
            "ARKF",
        )
        self.assertEqual(
            self.sl.lookup("AFRM", finviz_sector="Financial",
                           finviz_industry="Financial - Credit Services"),
            "ARKF",
        )


if __name__ == "__main__":
    unittest.main()
