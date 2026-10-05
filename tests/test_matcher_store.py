import os
import tempfile
import unittest
from datetime import date

from roadcheck.matcher import is_relevant_period, match_all, match_one
from roadcheck.models import Event, Subscription
from roadcheck.store import Store


def ev(**kw):
    base = dict(source="s", source_id="1", kind="construction", title="t")
    base.update(kw)
    return Event(**base)


class MatcherTests(unittest.TestCase):
    def test_point_within_radius(self):
        sub = Subscription(id=1, name="p", kind="point", points=[(25.0418, 121.5440)], radius_m=150)
        m = match_one(sub, ev(lat=25.0415, lon=121.5438))
        self.assertIsNotNone(m)
        self.assertEqual(m.reason, "distance")
        self.assertLess(m.distance_m, 60)

    def test_point_outside_radius(self):
        sub = Subscription(id=1, name="p", kind="point", points=[(25.0418, 121.5440)], radius_m=50)
        self.assertIsNone(match_one(sub, ev(lat=25.0500, lon=121.5440)))

    def test_route_buffer(self):
        sub = Subscription(id=2, name="r", kind="route", points=[(25.0330, 121.5654), (25.0415, 121.5495)], radius_m=60)
        self.assertIsNotNone(match_one(sub, ev(lat=25.0372, lon=121.5575)))  # 線中點附近
        self.assertIsNone(match_one(sub, ev(lat=25.0450, lon=121.5700)))

    def test_road_name_fallback_when_no_coords(self):
        sub = Subscription(id=3, name="p", kind="point", points=[(25, 121)], roads=["忠孝東路四段"])
        m = match_one(sub, ev(address="忠孝東路4段 光復南路口至延吉街口"))
        self.assertIsNotNone(m)
        self.assertEqual(m.reason, "road")
        self.assertEqual(m.matched_roads, ["忠孝東路4段"])
        self.assertIsNone(match_one(sub, ev(address="民生東路三段")))

    def test_only_blocking(self):
        sub = Subscription(id=4, name="p", kind="point", points=[(25.0, 121.5)], radius_m=100, only_blocking=True)
        self.assertIsNone(match_one(sub, ev(lat=25.0, lon=121.5, blocks_traffic=False)))
        self.assertIsNotNone(match_one(sub, ev(lat=25.0, lon=121.5, blocks_traffic=True)))
        self.assertIsNotNone(match_one(sub, ev(lat=25.0, lon=121.5, blocks_traffic=None)))

    def test_period_window(self):
        today = date(2025, 10, 5)
        self.assertTrue(is_relevant_period(ev(start=date(2025, 10, 1), end=date(2025, 10, 5)), today, 2))
        self.assertFalse(is_relevant_period(ev(start=date(2025, 10, 1), end=date(2025, 10, 4)), today, 2))
        self.assertTrue(is_relevant_period(ev(start=date(2025, 10, 7), end=date(2025, 10, 9)), today, 2))
        self.assertFalse(is_relevant_period(ev(start=date(2025, 10, 8), end=date(2025, 10, 9)), today, 2))
        self.assertTrue(is_relevant_period(ev(), today, 2))

    def test_match_all_sorted(self):
        sub = Subscription(id=1, name="p", kind="point", points=[(25.0, 121.5)], radius_m=100)
        events = [ev(source_id="b", lat=25.0, lon=121.5, start=date(2025, 10, 7)),
                  ev(source_id="a", lat=25.0, lon=121.5, start=date(2025, 10, 5))]
        ms = match_all([sub], events, today=date(2025, 10, 5), horizon_days=3)
        self.assertEqual([m.event.source_id for m in ms], ["a", "b"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_subscription_roundtrip(self):
        sub = Subscription(name="家", kind="route", points=[(25.0, 121.5), (25.1, 121.6)], radius_m=40,
                           roads=["忠孝東路四段"], channel="line", channel_target="U123", only_blocking=True)
        self.store.add_subscription(sub)
        got = self.store.get_subscription(sub.id)
        self.assertEqual(got.points, sub.points)
        self.assertEqual(got.roads, ["忠孝東路4段"])
        self.assertEqual(got.channel_target, "U123")
        self.assertTrue(got.only_blocking)
        self.assertEqual(len(self.store.list_subscriptions(channel_target="U123")), 1)
        self.assertEqual(len(self.store.list_subscriptions(channel_target="nobody")), 0)
        self.assertTrue(self.store.delete_subscription(sub.id))
        self.assertIsNone(self.store.get_subscription(sub.id))

    def test_upsert_and_change_detection(self):
        e = ev(start=date(2025, 10, 5), end=date(2025, 10, 6), lat=25.0, lon=121.5)
        self.assertEqual(self.store.upsert_events([e]), (1, 0))
        self.assertEqual(self.store.upsert_events([e]), (0, 0))
        e2 = ev(start=date(2025, 10, 5), end=date(2025, 10, 9), lat=25.0, lon=121.5)
        self.assertEqual(self.store.upsert_events([e2]), (0, 1))
        stored = self.store.list_events()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].end, date(2025, 10, 9))

    def test_notification_dedupe_until_fingerprint_changes(self):
        sub = self.store.add_subscription(Subscription(name="p", kind="point", points=[(25, 121)]))
        e = ev(start=date(2025, 10, 5), end=date(2025, 10, 6))
        self.assertFalse(self.store.already_notified(sub.id, e))
        self.store.mark_notified(sub.id, e)
        self.assertTrue(self.store.already_notified(sub.id, e))
        e_changed = ev(start=date(2025, 10, 5), end=date(2025, 10, 8))
        self.assertFalse(self.store.already_notified(sub.id, e_changed))


if __name__ == "__main__":
    unittest.main()


class ShapeMatchTests(unittest.TestCase):
    RING = [(25.040, 121.560), (25.040, 121.562), (25.042, 121.562), (25.042, 121.560), (25.040, 121.560)]

    def test_point_inside_polygon_is_zero_distance(self):
        sub = Subscription(id=1, name="p", kind="point", points=[(25.041, 121.561)], radius_m=10)
        # 代表點放很遠，證明用的是 shapes 不是 lat/lon
        m = match_one(sub, ev(lat=25.1, lon=121.7, shapes=[self.RING]))
        self.assertIsNotNone(m)
        self.assertEqual(m.distance_m, 0.0)

    def test_point_outside_polygon_uses_boundary_distance(self):
        sub = Subscription(id=1, name="p", kind="point", points=[(25.0425, 121.561)], radius_m=100)
        m = match_one(sub, ev(shapes=[self.RING]))          # 北邊 0.0005 度 ≈ 55 m
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m.distance_m, 55, delta=5)
        sub_far = Subscription(id=2, name="p", kind="point", points=[(25.045, 121.561)], radius_m=100)
        self.assertIsNone(match_one(sub_far, ev(shapes=[self.RING])))

    def test_route_crossing_polygon(self):
        sub = Subscription(id=3, name="r", kind="route", points=[(25.030, 121.561), (25.050, 121.561)], radius_m=20)
        m = match_one(sub, ev(shapes=[self.RING]))
        self.assertIsNotNone(m)
        self.assertLess(m.distance_m, 1.0)

    def test_open_line_shape(self):
        line = [(25.040, 121.560), (25.040, 121.570)]
        sub = Subscription(id=4, name="p", kind="point", points=[(25.0403, 121.565)], radius_m=50)
        m = match_one(sub, ev(shapes=[line]))
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m.distance_m, 33, delta=5)


class ShapeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_shapes_roundtrip(self):
        store = Store(self.tmp.name)
        ring = [(25.0, 121.5), (25.0, 121.6), (25.1, 121.6), (25.0, 121.5)]
        e = ev(lat=25.03, lon=121.55, shapes=[ring, [(25.2, 121.5), (25.3, 121.5)]])
        store.upsert_events([e])
        got = store.list_events()[0]
        self.assertEqual(got.shapes, e.shapes)
        self.assertEqual(got.fingerprint(), e.fingerprint())
        # 更新也會寫 shapes
        e2 = ev(lat=25.03, lon=121.55, shapes=[ring])
        store.upsert_events([e2])
        self.assertEqual(len(store.list_events()[0].shapes), 1)
        store.close()

    def test_migrates_old_database_without_shapes_column(self):
        import sqlite3

        conn = sqlite3.connect(self.tmp.name)
        conn.executescript("""
            CREATE TABLE events (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, lat REAL, lon REAL, address TEXT, start TEXT, end TEXT, time_window TEXT,
                blocks_traffic INTEGER, agency TEXT, purpose TEXT, url TEXT, extra TEXT,
                fingerprint TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            INSERT INTO events VALUES ('s:old','s','old','construction','t',25.0,121.5,'','2025-10-05','2025-10-06','',
                NULL,'','','','{}','abc','2025-10-05T00:00:00+00:00','2025-10-05T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()
        store = Store(self.tmp.name)
        old = store.list_events()
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0].shapes, [])
        store.upsert_events([ev(source_id="new", shapes=[[(25.0, 121.5), (25.1, 121.5)]])])
        self.assertEqual(len(store.list_events()), 2)
        store.close()
