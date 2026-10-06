"""每個訂閱各自的通知天數（Subscription.days）。"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date

from roadcheck import cli
from roadcheck.linebot import CommandHandler
from roadcheck.matcher import match_all
from roadcheck.models import Event, Subscription
from roadcheck.notify import format_digest
from roadcheck.store import Store

TODAY = date(2026, 10, 6)


def ev(sid, start, end, lat=25.0, lon=121.5):
    return Event(source="s", source_id=sid, kind="construction", title=f"t{sid}",
                 lat=lat, lon=lon, start=start, end=end)


def text_event(text, user="U1"):
    return {"type": "message", "replyToken": "r", "source": {"type": "user", "userId": user},
            "message": {"type": "text", "text": text}}


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)


class ModelTests(unittest.TestCase):
    def test_default_and_clamp(self):
        self.assertEqual(Subscription(name="x", kind="point", points=[(25, 121)]).days, 3)
        self.assertEqual(Subscription(name="x", kind="point", points=[(25, 121)], days=0).days, 1)
        self.assertEqual(Subscription(name="x", kind="point", points=[(25, 121)], days=99).days, 14)


class MatchTests(unittest.TestCase):
    EVENTS = [
        ev("today", date(2026, 10, 6), date(2026, 10, 6)),
        ev("in3", date(2026, 10, 9), date(2026, 10, 9)),      # 今天 +3
        ev("in5", date(2026, 10, 11), date(2026, 10, 12)),    # 今天 +5
        ev("in10", date(2026, 10, 16), date(2026, 10, 16)),   # 今天 +10
        ev("past", date(2026, 10, 1), date(2026, 10, 5)),
    ]

    def ids(self, matches, sub_id):
        return sorted(m.event.source_id for m in matches if m.subscription.id == sub_id)

    def test_each_subscription_uses_its_own_days(self):
        s3 = Subscription(id=1, name="3天", kind="point", points=[(25.0, 121.5)], days=3)
        s7 = Subscription(id=2, name="7天", kind="point", points=[(25.0, 121.5)], days=7)
        ms = match_all([s3, s7], self.EVENTS, today=TODAY)
        self.assertEqual(self.ids(ms, 1), ["in3", "today"])
        self.assertEqual(self.ids(ms, 2), ["in3", "in5", "today"])

    def test_override_applies_to_all(self):
        s3 = Subscription(id=1, name="3天", kind="point", points=[(25.0, 121.5)], days=3)
        ms = match_all([s3], self.EVENTS, today=TODAY, horizon_days=14)
        self.assertEqual(self.ids(ms, 1), ["in10", "in3", "in5", "today"])

    def test_digest_header_mentions_days(self):
        s7 = Subscription(id=2, name="停車", kind="point", points=[(25.0, 121.5)], days=7)
        ms = match_all([s7], self.EVENTS, today=TODAY)
        self.assertTrue(format_digest("停車", ms, days=7).startswith("🚧 「停車」未來 7 天附近有 3 件新異動"))
        self.assertTrue(format_digest("停車", ms).startswith("🚧 「停車」附近有 3 件新異動"))


class StoreTests(TempDb):
    def test_roundtrip_and_update(self):
        store = Store(self.tmp.name)
        sub = store.add_subscription(Subscription(name="x", kind="point", points=[(25, 121)], days=7))
        self.assertEqual(store.get_subscription(sub.id).days, 7)
        sub.days = 2
        store.update_subscription(sub)
        self.assertEqual(store.get_subscription(sub.id).days, 2)
        store.close()

    def test_old_database_gets_default_days(self):
        conn = sqlite3.connect(self.tmp.name)
        conn.executescript("""
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, points TEXT NOT NULL,
                radius_m REAL NOT NULL, roads TEXT NOT NULL DEFAULT '[]', channel TEXT NOT NULL DEFAULT 'console',
                channel_target TEXT NOT NULL DEFAULT '', only_blocking INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            INSERT INTO subscriptions (name, kind, points, radius_m, created_at)
                VALUES ('舊的', 'point', '[[25.0, 121.5]]', 100, '2026-10-01T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()
        store = Store(self.tmp.name)
        self.assertEqual(store.list_subscriptions()[0].days, 3)
        store.close()


class CliTests(TempDb):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", self.tmp.name, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_subscribe_with_days_list_and_change(self):
        code, out, _ = self.run_cli("subscribe", "point", "--name", "停車", "--lat", "25.026", "--lon", "121.4927",
                                    "--days", "7")
        self.assertEqual(code, 0)
        self.assertIn("未來 7 天", out)
        _, out, _ = self.run_cli("list")
        self.assertIn(" 7天 ", out)
        code, out, _ = self.run_cli("days", "1", "3")
        self.assertEqual(code, 0)
        self.assertIn("未來 3 天", out)
        store = Store(self.tmp.name)
        self.assertEqual(store.get_subscription(1).days, 3)
        store.close()

    def test_out_of_range_rejected(self):
        code, _, err = self.run_cli("subscribe", "point", "--name", "x", "--lat", "25", "--lon", "121.5", "--days", "30")
        self.assertEqual(code, 2)
        self.assertIn("1–14", err)
        self.run_cli("subscribe", "point", "--name", "x", "--lat", "25", "--lon", "121.5")
        code, _, err = self.run_cli("days", "1", "0")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("days", "99", "3")
        self.assertEqual(code, 1)

    def test_ext_fetch_days_follows_longest_subscription(self):
        saved = os.environ.pop("ROADCHECK_EXT_DAYS", None)
        try:
            store = Store(self.tmp.name)
            self.assertIsNone(cli.ext_fetch_days(store))                     # 沒訂閱：交給來源預設
            store.add_subscription(Subscription(name="a", kind="point", points=[(25, 121)], days=3))
            store.add_subscription(Subscription(name="b", kind="point", points=[(25, 121)], days=10))
            self.assertEqual(cli.ext_fetch_days(store), 10)
            self.assertEqual(cli.ext_fetch_days(store, override=2), 2)
            os.environ["ROADCHECK_EXT_DAYS"] = "12"
            self.assertEqual(cli.ext_fetch_days(store), 12)
            src = cli._make_source("taipei_ext_restriction", 10)
            self.assertEqual(src.days, 10)
            store.close()
        finally:
            os.environ.pop("ROADCHECK_EXT_DAYS", None)
            if saved is not None:
                os.environ["ROADCHECK_EXT_DAYS"] = saved


class LineTests(TempDb):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp.name)
        self.h = CommandHandler(self.store)
        for name in ("停車", "公司"):
            self.store.add_subscription(Subscription(name=name, kind="point", points=[(25, 121.5)],
                                                     channel="line", channel_target="U1"))

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_days_updates_latest(self):
        reply = self.h.handle_event(text_event("天數 7"))
        self.assertIn("#2「公司」改為通知未來 7 天", reply)
        self.assertEqual(self.store.get_subscription(2).days, 7)
        self.assertEqual(self.store.get_subscription(1).days, 3)

    def test_days_with_id_and_suffix(self):
        reply = self.h.handle_event(text_event("天數 5天 1"))
        self.assertIn("#1「停車」改為通知未來 5 天", reply)
        self.assertEqual(self.store.get_subscription(1).days, 5)

    def test_days_clamped_and_errors(self):
        reply = self.h.handle_event(text_event("天數 30"))
        self.assertIn("未來 14 天", reply)
        self.assertIn("只能設 1–14 天", reply)
        self.assertIn("要是數字", self.h.handle_event(text_event("天數 七")))
        self.assertIn("後面接數字", self.h.handle_event(text_event("天數")))
        self.assertIn("找不到", self.h.handle_event(text_event("天數 3 99")))
        self.assertIn("找不到", self.h.handle_event(text_event("天數 3 1", user="U2")))   # 別人的訂閱

    def test_list_shows_days(self):
        self.assertIn("3 天", self.h.handle_event(text_event("列表")))


if __name__ == "__main__":
    unittest.main()
