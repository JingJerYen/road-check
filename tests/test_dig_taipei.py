"""dig.taipei 共用工具與兩個 adapter，用 2026-10 抓回來的真實頁面（已去掉 __VIEWSTATE）測。"""
import unittest
from datetime import date
from pathlib import Path

from roadcheck.models import extract_roads
from roadcheck.sources import TaipeiExtRestriction, TaipeiPlannedWork
from roadcheck.sources.dig_taipei import (
    WebFormsSession,
    find_grid,
    hidden_fields,
    map_headers,
    parse_grids,
    parse_tables,
    roc_date,
    split_period,
)

FIX = Path(__file__).parent / "fixtures"
MODE0 = FIX / "taipei_ext_restriction.real_mode0.html"
MODE1 = FIX / "taipei_ext_restriction.real_mode1.html"
MODE2_LAST = FIX / "taipei_ext_restriction.real_mode2_lastpage.html"
PWORK = FIX / "taipei_planned_work.real.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TableParserTests(unittest.TestCase):
    def test_nested_pager_table_does_not_swallow_grid(self):
        grids = parse_grids(_read(MODE0))
        ids = [g.id for g in grids]
        self.assertIn("GridView1", ids)
        grid = next(g for g in grids if g.id == "GridView1")
        # 分頁列（巢狀表格）不算資料列；表頭 + 10 筆
        self.assertEqual(len(grid.rows), 11)
        self.assertEqual(grid.rows[0], ["挖掘管制日期", "管制原因", "施工單位", "管制路段"])
        # 內層分頁表自己是一張表
        self.assertTrue(any(r == ["1", "2", "3", "4"] for g in grids for r in g.rows))

    def test_refs_captured_from_anchor(self):
        grid = next(g for g in parse_grids(_read(MODE0)) if g.id == "GridView1")
        self.assertEqual(grid.refs[(1, 3)], "11500183")
        self.assertEqual(len(grid.refs), 10)
        pw = next(g for g in parse_grids(_read(PWORK)) if g.id == "GridView1")
        self.assertEqual(pw.refs[(1, 3)], "11500580")

    def test_parse_tables_compat(self):
        tables = parse_tables("<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2<br>3</td></tr></table>")
        self.assertEqual(tables, [[["a", "b"], ["1", "2 3"]]])

    def test_find_grid_skips_query_form_table(self):
        grid, hi, mapping = find_grid(parse_grids(_read(MODE1)))
        self.assertEqual(grid.id, "GridView2")
        self.assertEqual(hi, 0)
        self.assertEqual(mapping, {0: "period", 1: "road"})


class HeaderAndPeriodTests(unittest.TestCase):
    def test_real_headers_map(self):
        self.assertEqual(map_headers(["挖掘管制日期", "管制原因", "施工單位", "管制路段"]),
                         {0: "period", 1: "reason", 2: "agency", 3: "road"})
        self.assertEqual(map_headers(["使用道路日期", "使用道路路段"]), {0: "period", 1: "road"})
        self.assertEqual(map_headers(["預定施工日期", "施工類型", "施工單位", "施工路段"]),
                         {0: "period", 1: "reason", 2: "agency", 3: "road"})

    def test_split_period_formats(self):
        self.assertEqual(split_period("115/09/08-115/10/11"), (date(2026, 9, 8), date(2026, 10, 11), ""))
        self.assertEqual(split_period("115/10/05 23:00:00-115/10/06 06:00:00"),
                         (date(2026, 10, 5), date(2026, 10, 6), "23:00-06:00"))
        self.assertEqual(split_period("114/10/07~114/10/07"), (date(2025, 10, 7), date(2025, 10, 7), ""))
        self.assertEqual(split_period("2025/10/06 至 2025/10/06"), (date(2025, 10, 6), date(2025, 10, 6), ""))
        self.assertEqual(split_period("1141007～1141009"), (date(2025, 10, 7), date(2025, 10, 9), ""))
        self.assertEqual(split_period("2025-10-05 - 2025-10-07"), (date(2025, 10, 5), date(2025, 10, 7), ""))
        self.assertEqual(split_period(""), (None, None, ""))

    def test_roc_date(self):
        self.assertEqual(roc_date(date(2026, 10, 5)), "1151005")
        self.assertEqual(roc_date(date(2025, 1, 2)), "1140102")

    def test_hidden_fields(self):
        h = hidden_fields(_read(MODE0))
        self.assertEqual(h["__VIEWSTATE"], "STRIPPED")
        self.assertEqual(h["__VIEWSTATEGENERATOR"], "8F99F6DD")
        self.assertNotIn("GridView1$ctl03$THidEn_da", h)   # 只拿 __ 開頭


class FakeSession(WebFormsSession):
    """不打網路：postback 回傳預先排好的頁面。"""

    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.calls = []

    def postback(self, url, page, fields=None, event_target="", event_argument=""):
        self.calls.append((event_target, event_argument, dict(fields or {})))
        return self.pages[event_argument]


def _page(current: int, links: list[int]) -> str:
    cells = "".join(f"<td><span>{n}</span></td>" if n == current else f"<td><a href=\"javascript:__doPostBack('GridView1','Page${n}')\">{n}</a></td>" for n in links)
    return f"<table id='GridView1'><tr><td><table><tr>{cells}</tr></table></td></tr><tr><th>x</th></tr><tr><td>p{current}</td></tr></table>"


class PagingTests(unittest.TestCase):
    def test_iter_pages_follows_links_until_last(self):
        pages = {"Page$2": _page(2, [1, 2, 3]), "Page$3": _page(3, [1, 2, 3])}
        sess = FakeSession(pages)
        out = list(sess.iter_pages("u", _page(1, [1, 2, 3]), "GridView1", {"TxtQDate0": "1151005"}))
        self.assertEqual(len(out), 3)
        self.assertEqual([c[1] for c in sess.calls], ["Page$2", "Page$3"])
        self.assertTrue(all(c[0] == "GridView1" and c[2]["TxtQDate0"] == "1151005" for c in sess.calls))

    def test_iter_pages_stops_if_server_does_not_advance(self):
        pages = {"Page$2": _page(1, [1, 2, 3])}   # 伺服器還是回第 1 頁
        sess = FakeSession(pages)
        out = list(sess.iter_pages("u", _page(1, [1, 2, 3]), "GridView1"))
        self.assertEqual(len(out), 1)

    def test_iter_pages_respects_max_pages(self):
        pages = {f"Page${n}": _page(n, list(range(1, 12))) for n in range(2, 12)}
        sess = FakeSession(pages)
        out = list(sess.iter_pages("u", _page(1, list(range(1, 12))), "GridView1", max_pages=4))
        self.assertEqual(len(out), 4)


class ExtRestrictionRealTests(unittest.TestCase):
    def test_mode0_excavation_restrictions(self):
        evs = TaipeiExtRestriction().from_file(MODE0)
        self.assertEqual(len(evs), 10)
        e = evs[0]
        self.assertEqual(e.source_id, "11500183")                 # ShowExtRest.aspx?key=
        self.assertEqual((e.start, e.end), (date(2026, 9, 8), date(2026, 10, 11)))
        self.assertEqual(e.purpose, "配合管制")
        self.assertEqual(e.agency, "國慶籌備委員會")
        self.assertEqual(e.extra["mode"], "活動管制")
        self.assertIsNone(e.blocks_traffic)                        # 挖掘管制不是封路
        self.assertIn("中華路1段", e.roads)
        self.assertIn("博愛路", e.roads)
        self.assertEqual(evs[2].end, date(2031, 12, 31))           # 120/12/31

    def test_mode1_dedupes_repeated_rows_and_keeps_time_window(self):
        evs = TaipeiExtRestriction().from_file(MODE1)
        # 頁面有 10 列，但同 key 同路段同期間的重複列只算一筆
        self.assertEqual(len(evs), 3)
        e = evs[0]
        self.assertEqual(e.source_id, "10865115010415")
        self.assertEqual((e.start, e.end), (date(2026, 10, 12), date(2026, 10, 13)))
        self.assertEqual(e.time_window, "00:00-23:59")
        self.assertTrue(e.blocks_traffic)
        self.assertEqual({"中華路1段", "衡陽路", "秀山街", "寶慶路"}, e.roads)

    def test_mode_label_comes_from_fetch_raw_pages(self):
        src = TaipeiExtRestriction()
        evs = src.parse([{"mode": "臨時使用道路", "html": _read(MODE2_LAST)}])
        self.assertEqual(len(evs), 6)
        self.assertEqual(evs[0].purpose, "臨時使用道路")
        self.assertEqual(evs[0].title, "臨時使用道路：內湖區內湖路一段４３９號。")
        self.assertEqual(evs[0].time_window, "10:00-18:00")
        self.assertEqual(evs[0].roads, {"內湖路1段"})
        self.assertEqual(evs[0].url, src.url)

    def test_same_key_different_content_gets_distinct_id(self):
        html = """<table id="GridView2"><tr><th>使用道路日期</th><th>使用道路路段</th></tr>
        <tr><td>115/10/05 00:00:00-115/10/05 23:59:00</td><td><a href="x.aspx?key=1">甲路</a></td></tr>
        <tr><td>115/10/06 00:00:00-115/10/06 23:59:00</td><td><a href="x.aspx?key=1">乙路</a></td></tr>
        <tr><td>115/10/05 00:00:00-115/10/05 23:59:00</td><td><a href="x.aspx?key=1">甲路</a></td></tr></table>"""
        evs = TaipeiExtRestriction().parse(html)
        self.assertEqual([e.source_id for e in evs], ["1", "1-" + evs[1].source_id.split("-")[1]])
        self.assertNotEqual(evs[0].source_id, evs[1].source_id)

    def test_default_modes_skip_excavation_tab(self):
        src = TaipeiExtRestriction()
        self.assertEqual(src.modes, ["RadQMode1", "RadQMode2"])
        self.assertEqual(TaipeiExtRestriction(modes=["0", "2"]).modes, ["RadQMode0", "RadQMode2"])
        with self.assertRaises(ValueError):
            TaipeiExtRestriction(modes=["7"])

    def test_query_fields_are_roc_dates(self):
        src = TaipeiExtRestriction(days=3)
        self.assertEqual(src.query_fields(date(2026, 10, 5)), {"TxtQDate0": "1151005", "TxtQDate1": "1151008"})

    def test_fetch_raw_switches_mode_then_queries_then_pages(self):
        src = TaipeiExtRestriction(days=1, modes=["1"])
        landing = _page(1, [1])
        calls = []

        class S(WebFormsSession):
            def get(self, url):
                return landing

            def postback(self, url, page, fields=None, event_target="", event_argument=""):
                calls.append((event_target, event_argument, dict(fields or {})))
                return _page(1, [1])

        import roadcheck.sources.taipei_ext_restriction as mod
        orig = mod.WebFormsSession
        mod.WebFormsSession = S
        try:
            pages = src.fetch_raw()
        finally:
            mod.WebFormsSession = orig
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["mode"], "使用道路集會")
        self.assertEqual(calls[0][0], "RadQMode1")                      # 先切換分頁
        self.assertEqual(calls[1][2]["ButQuery"], "查　詢")             # 再查詢
        self.assertEqual(calls[1][2]["RadQMode"], "RadQMode1")
        self.assertIn("TxtQDate1", calls[1][2])


class PlannedWorkRealTests(unittest.TestCase):
    def setUp(self):
        self.src = TaipeiPlannedWork()
        self.events = self.src.from_file(PWORK)

    def test_export_table_parses(self):
        self.assertEqual(len(self.events), 12)
        e = self.events[0]
        self.assertEqual(e.source_id, "11500580")                 # ShowPWorkData.aspx?caseid=
        self.assertEqual(e.kind, "construction")
        self.assertEqual((e.start, e.end), (date(2026, 10, 19), date(2026, 11, 23)))
        self.assertEqual(e.purpose, "道路更新")
        self.assertEqual(e.agency, "新工處")
        self.assertEqual(e.title, "武昌街2段與漢中街口彩色鋪面更新工程（道路更新）")
        self.assertEqual(e.roads, {"武昌街2段", "漢中街"})
        self.assertIn("caseid=11500580", e.url)
        self.assertIsNone(e.location)

    def test_multi_page_input_dedupes_by_caseid(self):
        html = _read(PWORK)
        self.assertEqual(len(self.src.parse([html, html])), 12)

    def test_fetch_raw_prefers_export_and_falls_back_to_paging(self):
        export = _read(PWORK)
        import roadcheck.sources.taipei_planned_work as mod

        class Good(WebFormsSession):
            def get(self, url):
                return _page(1, [1, 2])

            def postback(self, url, page, fields=None, event_target="", event_argument=""):
                self.last = (fields, event_target, event_argument)
                return export

        class Broken(Good):
            def postback(self, url, page, fields=None, event_target="", event_argument=""):
                if fields and "ButExcel" in fields:
                    return "系統執行發生錯誤：BtnExcel_Click()"
                return _page(2, [1, 2])

        orig = mod.WebFormsSession
        try:
            mod.WebFormsSession = Good
            self.assertEqual(len(self.src.fetch_raw()), 1)
            mod.WebFormsSession = Broken
            with self.assertLogs(mod.log, level="WARNING"):
                self.assertEqual(len(self.src.fetch_raw()), 2)   # 退回逐頁：第 1、2 頁
        finally:
            mod.WebFormsSession = orig


class RoadExtractionOnRealTextTests(unittest.TestCase):
    def test_punctuation_and_city_prefix_are_not_part_of_road(self):
        self.assertEqual(extract_roads("使用道路集會：光復南路【光復南路292號（不含）至光復南路306號（不含）】東側。"),
                         {"光復南路"})
        self.assertEqual(extract_roads("台北市八德路二段232號。台北市八德路二段232號"), {"八德路2段"})
        self.assertEqual(extract_roads("臨時使用道路：信義區松壽路18、20號及中庭徒步區。信義區松智路17號"),
                         {"松壽路", "松智路"})
        self.assertEqual(extract_roads("松信路（自松隆路至永吉路）西側人行道"), {"松信路", "松隆路", "永吉路"})
        self.assertIn("市民大道3段", extract_roads("市民大道三段"))


if __name__ == "__main__":
    unittest.main()
