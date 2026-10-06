import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from roadcheck.models import Event
from roadcheck.store import Store
from roadcheck.web import WebApp, make_handler

TODAY = date(2026, 10, 6)
PIN = (25.025954, 121.492734)
# 圖釘周圍約 30 m 的小方塊
RING = [(25.0257, 121.4925), (25.0257, 121.4930), (25.0262, 121.4930), (25.0262, 121.4925), (25.0257, 121.4925)]


def ev(sid, start, end, source="taipei_ext_restriction", kind="restriction", **kw):
    return Event(source=source, source_id=sid, kind=kind, title=f"t{sid}", start=start, end=end, **kw)


def seed(store: Store):
    store.upsert_events([
        ev("in-ring", date(2026, 10, 9), date(2026, 10, 11), shapes=[RING], blocks_traffic=True,
           extra={"mode_label": "活動管制", "bulletin_url": "https://example.test/a.pdf"}),
        ev("day6", date(2026, 10, 12), date(2026, 10, 12), address="萬華區西園路二段292號"),     # 只有路名
        ev("near", TODAY, TODAY, lat=25.0275, lon=121.4927),       # 約 170 m，在「附近」
        ev("far", TODAY, TODAY, lat=25.05, lon=121.55),            # 很遠
        ev("past", date(2026, 10, 1), date(2026, 10, 5), shapes=[RING]),
    ])
    store.upsert_events([ev("cons", date(2026, 9, 1), date(2026, 12, 31), source="taipei_today_construction",
                            kind="construction", lat=PIN[0] + 0.0002, lon=PIN[1], blocks_traffic=False,
                            extra={"app_mode": "道路維護通報", "plan_b": "建議改道"})])


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        store = Store(self.tmp.name)
        seed(store)
        store.close()
        self.app = WebApp(self.tmp.name, fetch_days=7, fetcher=lambda db, days: [], today=lambda: TODAY)

    def tearDown(self):
        os.unlink(self.tmp.name)


class StoreCurrentBatchTests(TempDb):
    def test_only_latest_batch_per_source(self):
        store = Store(self.tmp.name)
        time.sleep(1.1)       # last_seen 精度是秒
        store.upsert_events([ev("new-only", TODAY, TODAY)])   # 新的一批外部管制只有一件
        current = {e.source_id for e in store.list_current_events()}
        self.assertEqual(current, {"new-only", "cons"})       # 舊批次的外部管制消失，施工沿用上一批
        stats = {s["source"]: s["events"] for s in store.source_stats()}
        self.assertEqual(stats, {"taipei_ext_restriction": 1, "taipei_today_construction": 1})
        store.close()


class CheckTests(TempDb):
    def keys(self, items):
        return [m["key"].split(":")[1] for m in items]

    def test_three_days(self):
        r = self.app.check(*PIN, radius_m=100, days=3)
        self.assertEqual(self.keys(r["matches"]), ["in-ring", "cons"])   # 影響交通的排前面
        first = r["matches"][0]
        self.assertEqual(first["distance_m"], 0.0)
        self.assertEqual(first["timing"], "3 天後開始")
        self.assertEqual(first["category"], "活動管制")
        self.assertEqual(first["bulletin_url"], "https://example.test/a.pdf")
        self.assertEqual(len(first["shapes"]), 1)
        self.assertEqual(r["matches"][1]["plan_b"], "建議改道")
        self.assertEqual(r["matches"][1]["timing"], "進行中")
        self.assertEqual(self.keys(r["nearby"]), ["near"])
        self.assertEqual(r["query"]["until"], "2026-10-09")

    def test_seven_days_with_road_name(self):
        r = self.app.check(*PIN, radius_m=100, days=7, roads=["西園路二段"])
        self.assertEqual(self.keys(r["matches"]), ["in-ring", "cons", "day6"])
        road = r["matches"][-1]
        self.assertEqual(road["reason"], "road")
        self.assertIsNone(road["distance_m"])
        self.assertEqual(road["matched_roads"], ["西園路2段"])
        self.assertNotIn("day6", self.keys(self.app.check(*PIN, radius_m=100, days=7)["matches"]))

    def test_days_and_radius_are_clamped(self):
        r = self.app.check(*PIN, radius_m=1, days=99)
        self.assertEqual(r["query"]["days"], 7)        # 不超過伺服器抓的天數
        self.assertEqual(r["query"]["radius_m"], 10.0)
        with self.assertRaises(ValueError):
            self.app.check(95, 121)

    def test_status_and_staleness(self):
        s = self.app.status()
        self.assertEqual(s["fetch_days"], 7)
        self.assertEqual({x["label"] for x in s["sources"]}, {"今日施工", "外部管制路段"})
        self.assertFalse(self.app.is_stale())
        old = WebApp(self.tmp.name, refresh_hours=0.0001, fetcher=lambda db, d: [], today=lambda: TODAY)
        time.sleep(0.5)
        self.assertTrue(old.is_stale())

    def test_refresh_runs_fetcher_and_records_errors(self):
        calls = []

        def fetcher(db, days):
            calls.append((db, days))
            return [("taipei_today_construction", 3, None), ("taipei_ext_restriction", None, "HTTP 500")]

        app = WebApp(self.tmp.name, fetch_days=5, fetcher=fetcher, today=lambda: TODAY)
        self.assertTrue(app.refresh(wait=True))
        self.assertEqual(calls, [(self.tmp.name, 5)])
        st = app.status()
        self.assertFalse(st["refreshing"])
        self.assertIn("外部管制路段：HTTP 500", st["last_error"])


class HttpTests(TempDb):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_pages_and_static(self):
        code, ctype, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("/static/leaflet/leaflet.js", body.decode())
        for p in ("/static/app.js", "/static/app.css", "/static/leaflet/leaflet.js", "/static/leaflet/leaflet.css",
                  "/static/leaflet/images/marker-icon.png"):
            self.assertEqual(self.get(p)[0], 200, p)

    def test_no_path_traversal(self):
        for p in ("/static/../__init__.py", "/static/%2e%2e/__init__.py", "/static/leaflet/../../__init__.py",
                  "/static/", "/nope"):
            self.assertEqual(self.get(p)[0], 404, p)

    def test_check_api(self):
        code, ctype, body = self.get(f"/api/check?lat={PIN[0]}&lon={PIN[1]}&radius=100&days=7&roads="
                                     + quote("西園路二段、寶興街"))
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body)
        self.assertEqual(data["query"]["roads"], ["寶興街", "西園路2段"])
        self.assertEqual(len(data["matches"]), 3)
        self.assertIn("status", data)

    def test_check_api_errors(self):
        self.assertEqual(self.get("/api/check?lat=abc&lon=1")[0], 400)
        self.assertEqual(self.get("/api/check?lon=121")[0], 400)
        self.assertEqual(self.get("/api/check?lat=95&lon=121")[0], 400)

    def test_status_and_refresh(self):
        code, _, body = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["fetch_days"], 7)
        code, _, body = self.get("/api/refresh", method="POST")
        self.assertIn(code, (200, 202))
        self.assertIn("started", json.loads(body))


if __name__ == "__main__":
    unittest.main()
