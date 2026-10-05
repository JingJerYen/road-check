import unittest
from datetime import date
from pathlib import Path

from roadcheck.sources import TaipeiExtRestriction, TaipeiTodayConstruction
from roadcheck.sources.taipei_ext_restriction import parse_listing, roc_ymd, split_period

FIX = Path(__file__).parent / "fixtures"


class TodayConstructionRealTests(unittest.TestCase):
    """tests/fixtures/taipei_today_construction.real.json 是 2026-10 從 Todaywork.json 擷取的 4 筆（聯絡人已去識別）。"""

    def setUp(self):
        self.events = TaipeiTodayConstruction().from_file(FIX / "taipei_today_construction.real.json")
        self.by_id = {e.source_id: e for e in self.events}

    def test_geojson_shape_and_ids(self):
        self.assertEqual(len(self.events), 4)
        self.assertEqual(set(self.by_id), {"11501436-13", "11501436-1", "115002040-1", "115001828-6"})

    def test_twd97_converted_to_taipei_latlon(self):
        for e in self.events:
            self.assertIsNotNone(e.location, e.source_id)
            self.assertTrue(24.9 < e.lat < 25.3 and 121.4 < e.lon < 121.7, (e.source_id, e.location))
        e = self.by_id["115001828-6"]                       # 市民大道5段50號前
        self.assertAlmostEqual(e.lat, 25.0477, places=3)
        self.assertAlmostEqual(e.lon, 121.5647, places=3)

    def test_positions_become_shapes(self):
        poly = self.by_id["11501436-13"]                    # MultiPolygon
        self.assertEqual(len(poly.shapes), 1)
        self.assertEqual(poly.shapes[0][0], poly.shapes[0][-1])   # 封閉
        lines = self.by_id["115002040-1"]                   # MultiLineString
        self.assertEqual(len(lines.shapes), 7)
        self.assertTrue(all(len(s) == 2 for s in lines.shapes))

    def test_fields(self):
        e = self.by_id["115001828-6"]
        self.assertEqual((e.start, e.end), (date(2026, 7, 13), date(2026, 10, 20)))   # 115/07/13
        self.assertTrue(e.blocks_traffic)                   # IsBlock 是
        self.assertEqual(e.agency, "水利處")
        self.assertEqual(e.extra["district"], "信義區")      # C_Name 只有「信義」
        self.assertEqual(e.extra["app_mode"], "施工通報")    # AppMode 0
        self.assertTrue(e.title.startswith("信義區市民大道5段50號前"))
        self.assertIn("市民大道5段", e.roads)
        self.assertEqual(self.by_id["11501436-13"].extra["app_mode"], "道路維護通報")
        self.assertFalse(self.by_id["11501436-1"].blocks_traffic)
        self.assertEqual(e.extra["contact"], "王ＯＯ")


class TodayConstructionLegacyShapeTests(unittest.TestCase):
    """舊的 data.taipei resourceAquire 形狀（X/Y 是經緯度、Positions 是文字）也要能讀。"""

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
        self.assertEqual(e.shapes, [])
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
    """tests/fixtures/taipei_ext_restriction.real.json：2026-10 從 dig.taipei 列表 HTML 與 caseMap3.ashx 擷取並裁短。

    listing 10 列、cases 14 筆，涵蓋：
      - 活動管制裡該濾掉的：配合管制(11500183)、議員要求(11500208)、超過 90 天的府內機關申請(11500017)
      - 只有地圖 API 有的遶境案(11500281) 與今日集會案(10865115009910)
      - 列表＋幾何都有的凱達格蘭大道集會(10865115010880，列表 4 列重複)、松高路臨時使用道路(10967115382827)
      - 只有列表沒有幾何的臨時使用道路(10967115374444)
    """

    def setUp(self):
        self.src = TaipeiExtRestriction()
        self.events = self.src.from_file(FIX / "taipei_ext_restriction.real.json")
        self.by_id = {e.source_id: e for e in self.events}

    def test_merge_and_filter(self):
        self.assertEqual(
            set(self.by_id),
            {"RALLY-10865115009910", "EXTREST-11500281", "RALLY-10865115010880",
             "URGENT-10967115374444", "URGENT-10967115382827"},
        )
        self.assertNotIn("EXTREST-11500183", self.by_id)   # 配合管制
        self.assertNotIn("EXTREST-11500208", self.by_id)   # 議員要求
        self.assertNotIn("EXTREST-11500017", self.by_id)   # 346 天的規定

    def test_listing_rows_dedupe_into_one_event(self):
        e = self.by_id["RALLY-10865115010880"]
        self.assertEqual((e.start, e.end), (date(2026, 10, 19), date(2026, 10, 19)))
        self.assertEqual(e.time_window, "全日")               # 00:00-23:59
        self.assertEqual(len(e.shapes), 3)
        self.assertTrue(all(s[0] == s[-1] for s in e.shapes))
        self.assertTrue(e.extra["listed"])
        self.assertTrue(e.blocks_traffic)
        self.assertIn("凱達格蘭大道", e.roads)
        self.assertIn("中山南路", e.roads)
        self.assertAlmostEqual(e.lat, 25.0399, places=3)
        self.assertAlmostEqual(e.lon, 121.5168, places=3)

    def test_case_only_event_from_map_api(self):
        e = self.by_id["EXTREST-11500281"]                   # 遶境，列表裡沒有
        self.assertFalse(e.extra["listed"])
        self.assertEqual((e.start, e.end), (date(2026, 10, 9), date(2026, 10, 11)))
        self.assertEqual(e.purpose, "財團法人台北市廣照宮飛天大聖誕辰遶境")
        self.assertEqual(e.agency, "萬華區公所")
        self.assertTrue(e.title.startswith("活動管制｜財團法人台北市廣照宮飛天大聖誕辰遶境："))
        self.assertEqual(len(e.shapes), 3)
        self.assertIn("青年路", e.roads)
        self.assertEqual(e.extra["contact_tel"], "02-0000-0000")

    def test_listing_only_event_has_no_geometry(self):
        e = self.by_id["URGENT-10967115374444"]
        self.assertIsNone(e.location)
        self.assertEqual(e.shapes, [])
        self.assertEqual(e.time_window, "16:00-21:00")
        self.assertIsNone(e.blocks_traffic)
        self.assertEqual(e.roads, {"南京東路2段"})

    def test_urgent_with_geometry(self):
        e = self.by_id["URGENT-10967115382827"]
        self.assertEqual((e.start, e.end), (date(2026, 10, 19), date(2026, 10, 20)))
        self.assertEqual(e.time_window, "22:00-23:59")
        self.assertEqual(len(e.shapes), 2)
        self.assertEqual(e.roads, {"松高路"})

    def test_excluded_reason_can_be_relaxed(self):
        src = TaipeiExtRestriction(max_event_days=10_000)
        src_events = {e.source_id for e in src.from_file(FIX / "taipei_ext_restriction.real.json")}
        self.assertIn("EXTREST-11500017", src_events)        # 只放寬天數，原因過濾仍在
        self.assertNotIn("EXTREST-11500208", src_events)

    def test_sorted_by_start(self):
        starts = [e.start for e in self.events]
        self.assertEqual(starts, sorted(starts))


class ExtRestrictionHelperTests(unittest.TestCase):
    def test_split_period(self):
        self.assertEqual(split_period("115/10/12 00:00:00-115/10/13 23:59:00"),
                         (date(2026, 10, 12), date(2026, 10, 13), "全日"))
        self.assertEqual(split_period("115/09/08-115/10/11"), (date(2026, 9, 8), date(2026, 10, 11), ""))
        self.assertEqual(split_period("115/10/11 09:00:00-115/10/11 14:00:00"),
                         (date(2026, 10, 11), date(2026, 10, 11), "09:00-14:00"))
        self.assertEqual(split_period("114/10/07~114/10/07"), (date(2025, 10, 7), date(2025, 10, 7), ""))
        self.assertEqual(split_period(""), (None, None, ""))

    def test_roc_ymd(self):
        self.assertEqual(roc_ymd(date(2026, 10, 5)), "1151005")

    def test_parse_listing_handles_nested_pager_table(self):
        html = """
        <html><body><form>
        <table class="nav"><tr><td>首頁</td></tr></table>
        <table id="GridView2">
          <tr><td colspan="2"><table><tr><td><span>1</span></td><td><a href="javascript:__doPostBack('GridView2','Page$2')">2</a></td></tr></table></td></tr>
          <tr><th>使用道路日期</th><th>使用道路路段</th></tr>
          <tr><td>115/10/19 00:00:00-115/10/19 23:59:00</td><td>
            <a onclick="window.open(&#39;../Map/ShowExtRest.aspx?key=10865115010880&amp;key2=x&#39;,&#39;_parent&#39;);" href="#">凱達格蘭大道【1號至2號】北側</a>
            <input type="hidden" name="GridView2$ctl03$THidEn_da" value="1151019235900" /></td></tr>
          <tr><td>115/10/20 09:00:00-115/10/20 12:00:00</td><td><a href="#">無連結的列</a></td></tr>
        </table></form></body></html>"""
        rows = parse_listing(html, "RALLY")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["caseid"], "10865115010880")
        self.assertEqual(rows[0]["road"], "凱達格蘭大道【1號至2號】北側")
        self.assertEqual(rows[0]["period"], "115/10/19 00:00:00-115/10/19 23:59:00")
        self.assertIsNone(rows[1]["caseid"])
        self.assertEqual(rows[1]["mode"], "RALLY")

    def test_parse_listing_four_column_grid(self):
        html = """<table id="GridView1">
          <tr><th>挖掘管制日期</th><th>管制原因</th><th>施工單位</th><th>管制路段</th></tr>
          <tr><td>115/09/08-115/10/11</td><td>配合管制</td><td>國慶籌備委員會</td><td>
            <a onclick="window.open(&#39;../Map/ShowExtRest.aspx?key=11500183&#39;)">中華路1段以東</a></td></tr>
        </table>"""
        rows = parse_listing(html, "EXTREST")
        self.assertEqual(rows, [{"mode": "EXTREST", "caseid": "11500183", "period": "115/09/08-115/10/11",
                                 "reason": "配合管制", "agency": "國慶籌備委員會", "road": "中華路1段以東"}])

    def test_rows_without_caseid_still_become_events(self):
        raw = {"listing": [{"mode": "URGENT", "caseid": None, "period": "115/10/20 09:00:00-115/10/20 12:00:00",
                            "reason": "", "agency": "", "road": "大安區忠孝東路四段231號"}], "cases": []}
        evs = TaipeiExtRestriction().parse(raw)
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0].source_id.startswith("URGENT-h"))
        self.assertEqual(evs[0].time_window, "09:00-12:00")
        self.assertIn("忠孝東路4段", evs[0].roads)


if __name__ == "__main__":
    unittest.main()
