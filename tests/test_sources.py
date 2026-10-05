import unittest
from datetime import date
from pathlib import Path

from roadcheck.sources import TaipeiExtRestriction, TaipeiTodayConstruction

FIX = Path(__file__).parent / "fixtures"


class TodayConstructionTests(unittest.TestCase):
    def setUp(self):
        self.events = TaipeiTodayConstruction().from_file(FIX / "taipei_today_construction.sample.json")

    def test_count_and_keys(self):
        self.assertEqual(len(self.events), 5)
        self.assertEqual(self.events[0].key, "taipei_today_construction:114A1234567-1")
        self.assertEqual(self.events[3].source_id, "114D5555555-2")

    def test_mixed_date_formats(self):
        self.assertEqual(self.events[0].start, date(2025, 10, 5))   # 114/10/05
        self.assertEqual(self.events[1].start, date(2025, 10, 6))   # 2025/10/06
        self.assertEqual(self.events[2].start, date(2025, 10, 5))   # 20251005
        self.assertEqual(self.events[3].end, date(2025, 10, 30))    # 1141030

    def test_coordinates_and_flags(self):
        e = self.events[0]
        self.assertAlmostEqual(e.lat, 25.0415)
        self.assertAlmostEqual(e.lon, 121.5438)
        self.assertTrue(e.blocks_traffic)
        self.assertEqual(e.time_window, "22:00-06:00")
        self.assertIsNone(self.events[4].location)   # 空座標

    def test_roads_extracted(self):
        self.assertIn("忠孝東路4段", self.events[0].roads)
        self.assertIn("復興南路", self.events[0].roads)

    def test_accepts_bare_list_payload(self):
        raw = [{"Ac_no": "X1", "X": "121.5", "Y": "25.0", "Addr": "測試路", "Cb_Da": "114/01/01", "Ce_Da": "114/01/02"}]
        evs = TaipeiTodayConstruction().parse(raw)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].source_id, "X1")

    def test_swapped_xy_is_corrected(self):
        raw = [{"Ac_no": "X2", "X": "25.0", "Y": "121.5", "Addr": "測試路"}]
        e = TaipeiTodayConstruction().parse(raw)[0]
        self.assertAlmostEqual(e.lat, 25.0)
        self.assertAlmostEqual(e.lon, 121.5)


class ExtRestrictionTests(unittest.TestCase):
    def setUp(self):
        self.events = TaipeiExtRestriction().from_file(FIX / "taipei_ext_restriction.sample.html")

    def test_skips_nav_table_and_parses_grid(self):
        self.assertEqual(len(self.events), 3)

    def test_fields(self):
        e = self.events[0]
        self.assertEqual(e.source_id, "114-EXT-001")
        self.assertEqual(e.kind, "restriction")
        self.assertEqual(e.purpose, "宗教遶境")
        self.assertEqual(e.start, date(2025, 10, 7))
        self.assertEqual(e.end, date(2025, 10, 7))
        self.assertEqual(e.time_window, "14:00-18:00")
        self.assertIsNone(e.location)
        self.assertIn("民生東路3段", e.roads)
        self.assertIn("龍江路", e.roads)

    def test_period_with_zhi(self):
        e = self.events[1]
        self.assertEqual((e.start, e.end), (date(2025, 10, 6), date(2025, 10, 6)))

    def test_multi_day(self):
        e = self.events[2]
        self.assertEqual((e.start, e.end), (date(2025, 10, 8), date(2025, 10, 9)))
        self.assertIn("凱達格蘭大道", e.roads)


if __name__ == "__main__":
    unittest.main()
