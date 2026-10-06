"""LINE 訂閱流程：快速回覆、座標訂閱、查詢、合併推播、網站上的 webhook 與每日排程。"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import ThreadingHTTPServer

from roadcheck.dates import TAIPEI_TZ
from roadcheck.linebot import CommandHandler
from roadcheck.models import Event, Subscription
from roadcheck.notify import LineNotifier, Reply, line_text_message
from roadcheck.service import run_notifications, web_link
from roadcheck.store import Store
from roadcheck.web import WebApp, make_handler

TODAY = date(2026, 10, 6)
PIN = (25.025954, 121.492734)
RING = [(25.0257, 121.4925), (25.0257, 121.4930), (25.0262, 121.4930), (25.0262, 121.4925), (25.0257, 121.4925)]
PUBLIC = "https://roadcheck.example"


def text_event(text, user="U1", token="r"):
    return {"type": "message", "replyToken": token, "source": {"type": "user", "userId": user},
            "message": {"type": "text", "text": text}}


def loc_event(user="U1", lat=PIN[0], lon=PIN[1], title="我的停車位"):
    return {"type": "message", "replyToken": "r", "source": {"type": "user", "userId": user},
            "message": {"type": "location", "title": title, "address": "台北市", "latitude": lat, "longitude": lon}}


def seed(store):
    store.upsert_events([
        Event(source="taipei_ext_restriction", source_id="EXTREST-1", kind="restriction",
              title="活動管制｜廣照宮遶境", start=date(2026, 10, 9), end=date(2026, 10, 11),
              blocks_traffic=True, shapes=[RING]),
        Event(source="taipei_ext_restriction", source_id="URGENT-2", kind="restriction",
              title="臨時使用道路：萬華區西園路二段292號", address="萬華區西園路二段292號",
              start=date(2026, 10, 12), end=date(2026, 10, 12)),
    ])


class FakeNotifier:
    sent = []
    fail_for = set()

    def __init__(self, channel):
        self.channel = channel

    def send_many(self, target, texts):
        if target in FakeNotifier.fail_for:
            raise RuntimeError("boom")
        FakeNotifier.sent.append((self.channel, target, list(texts)))


def fake_factory(channel, dry_run=False):
    return FakeNotifier(channel)


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        seed(self.store)
        FakeNotifier.sent = []
        FakeNotifier.fail_for = set()

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)


class ReplyPayloadTests(unittest.TestCase):
    def test_reply_is_a_string_with_buttons(self):
        r = Reply("hello", [("未來 7 天", "天數 7"), ("📍 傳送位置", None)])
        self.assertEqual(r, "hello")
        self.assertIn("ell", r)
        msg = line_text_message(r)
        items = msg["quickReply"]["items"]
        self.assertEqual(items[0]["action"], {"type": "message", "label": "未來 7 天", "text": "天數 7"})
        self.assertEqual(items[1]["action"], {"type": "location", "label": "📍 傳送位置"})
        self.assertNotIn("quickReply", line_text_message("plain"))

    def test_long_text_keeps_buttons_and_is_truncated(self):
        msg = line_text_message(Reply("x" * 6000, [("a", "b")]))
        self.assertLessEqual(len(msg["text"]), 5000)
        self.assertIn("quickReply", msg)

    def test_push_payload_has_several_messages_and_no_buttons(self):
        n = LineNotifier("token")
        calls = []
        n._post = lambda url, payload: calls.append((url, payload))
        n.send_many("U1", [Reply("a", [("x", "y")]), "b"])
        url, payload = calls[0]
        self.assertTrue(url.endswith("/push"))
        self.assertEqual(payload["to"], "U1")
        self.assertEqual([m["text"] for m in payload["messages"]], ["a", "b"])
        self.assertNotIn("quickReply", payload["messages"][0])
        with self.assertRaises(ValueError):
            n.send_many("U1", ["1"] * 6)
        n.reply("tok", Reply("r", [("x", "y")]))
        self.assertIn("quickReply", calls[-1][1]["messages"][0])


class CommandTests(TempStore):
    def handler(self, public_url=PUBLIC):
        return CommandHandler(self.store, public_url=public_url, today=lambda: TODAY)

    def test_location_subscribes_and_shows_current_events(self):
        reply = self.handler().handle_event(loc_event())
        self.assertIn("已訂閱 #1「我的停車位」", reply)
        self.assertIn("目前未來 3 天有 1 件", reply)
        self.assertIn("廣照宮遶境", reply)
        self.assertIn("就在範圍內，影響交通", reply)
        self.assertIn(f"🗺 地圖：{PUBLIC}/#25.025954,121.492734,3,100", reply)
        labels = [q[0] for q in reply.quick]
        self.assertIn("未來 7 天", labels)
        self.assertIn(("📍 傳送位置", None), reply.quick)
        # 回覆裡給看過了，隔天不再推
        sub = self.store.get_subscription(1)
        ev = self.store.list_current_events()[0]
        self.assertTrue(self.store.already_notified(sub.id, ev))

    def test_subscribe_by_coordinates_text(self):
        reply = self.handler().handle_event(text_event("訂閱 25.025954,121.492734 200m 7天 萬華區西園路二段255號"))
        sub = self.store.get_subscription(1)
        self.assertEqual(sub.points, [PIN])
        self.assertEqual(sub.radius_m, 200)
        self.assertEqual(sub.days, 7)
        self.assertEqual(sub.name, "萬華區西園路二段255號")
        self.assertIn("目前未來 7 天有 1 件", reply)    # 292 號那件沒有座標也沒有路名 → 不算

    def test_pasted_coordinates_and_validation(self):
        h = self.handler()
        self.assertIn("已訂閱", h.handle_event(text_event("25.025954, 121.492734")))
        self.assertEqual(self.store.get_subscription(1).name, "地圖上選的位置")
        self.assertIn("不在台灣", h.handle_event(text_event("訂閱 35.6,139.7")))
        self.assertIn("格式", h.handle_event(text_event("訂閱 哪裡")))
        far = h.handle_event(text_event("訂閱 22.62,120.30"))          # 高雄
        self.assertIn("不在台北市", far)

    def test_check_list_map_and_settings(self):
        h = self.handler()
        self.assertIn("還沒有訂閱", h.handle_event(text_event("查詢")))
        h.handle_event(loc_event())
        h.handle_event(text_event("訂閱 25.05,121.55 公司"))
        check = h.handle_event(text_event("查詢"))
        self.assertIn("#1「我的停車位」", check)
        self.assertIn("#2「公司」", check)
        self.assertIn("沒有已知的施工或封路", check)
        self.assertIn("2 公司", h.handle_event(text_event("列表")).replace("#", ""))
        self.assertIn(f"{PUBLIC}/#25.050000,121.550000,3,100", h.handle_event(text_event("地圖")))
        self.assertIn("#1「我的停車位」", h.handle_event(text_event("地圖 1")))
        r = h.handle_event(text_event("半徑 300 1"))
        self.assertIn("#1「我的停車位」半徑改為 300 公尺", r)
        self.assertEqual(self.store.get_subscription(2).radius_m, 100)
        r = h.handle_event(text_event("天數 7 2"))
        self.assertIn("改為通知未來 7 天", r)
        self.assertIn("目前未來 7 天", r)
        self.assertIn("沒有設定網頁網址", self.handler(public_url="").handle_event(text_event("地圖")))

    def test_non_text_message(self):
        ev = {"type": "message", "replyToken": "r", "source": {"userId": "U1"}, "message": {"type": "sticker"}}
        self.assertIn("傳送位置", self.handler().handle_event(ev))


class RunNotificationsTests(TempStore):
    def add(self, name, target, lat=PIN[0], lon=PIN[1], days=7, channel="line", roads=()):
        return self.store.add_subscription(Subscription(name=name, kind="point", points=[(lat, lon)], days=days,
                                                        channel=channel, channel_target=target, roads=list(roads)))

    def test_one_push_per_user_with_one_message_per_subscription(self):
        self.add("停車", "U1", roads=["西園路二段"])
        self.add("老家", "U1", lat=PIN[0] + 0.0001)
        self.add("別人", "U2")
        self.add("很遠", "U3", lat=25.1, lon=121.6)
        stats = run_notifications(self.store, fake_factory, today=TODAY, public_url=PUBLIC, out=lambda m: None)
        self.assertEqual(stats["pushes"], 2)
        by_target = {t: texts for _, t, texts in FakeNotifier.sent}
        self.assertEqual(set(by_target), {"U1", "U2"})
        self.assertEqual(len(by_target["U1"]), 2)
        self.assertIn("「停車」未來 7 天附近有 2 件", by_target["U1"][0])
        self.assertIn(f"🗺 地圖：{PUBLIC}/#", by_target["U1"][0])
        # 第二次跑不會重送
        FakeNotifier.sent = []
        stats = run_notifications(self.store, fake_factory, today=TODAY, out=lambda m: None)
        self.assertEqual((stats["fresh"], stats["pushes"]), (0, 0))

    def test_failed_push_is_retried_next_time(self):
        self.add("停車", "U1")
        FakeNotifier.fail_for = {"U1"}
        stats = run_notifications(self.store, fake_factory, today=TODAY, out=lambda m: None)
        self.assertEqual(stats["failed"], 1)
        FakeNotifier.fail_for = set()
        stats = run_notifications(self.store, fake_factory, today=TODAY, out=lambda m: None)
        self.assertEqual(stats["pushes"], 1)

    def test_more_than_five_subscriptions_fit_in_one_push(self):
        for i in range(7):
            self.add(f"點{i}", "U1", lat=PIN[0] + i * 0.00001)
        run_notifications(self.store, fake_factory, today=TODAY, out=lambda m: None)
        (_, _, texts), = FakeNotifier.sent
        self.assertEqual(len(texts), 5)
        self.assertIn("「點6」", texts[-1])

    def test_web_link(self):
        sub = Subscription(name="x", kind="point", points=[PIN], radius_m=150, days=7)
        self.assertEqual(web_link(PUBLIC + "/", sub), f"{PUBLIC}/#25.025954,121.492734,7,150")
        self.assertEqual(web_link("", sub), "")


def sign(secret, body):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class WebLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        store = Store(self.tmp.name)
        seed(store)
        store.upsert_events([Event(source="taipei_today_construction", source_id="C1", kind="construction",
                                   title="c", lat=25.1, lon=121.6, start=TODAY, end=TODAY)])
        store.close()
        self.replies = []
        self.clock = [datetime(2026, 10, 6, 6, 59, tzinfo=TAIPEI_TZ)]
        FakeNotifier.sent = []
        self.app = WebApp(self.tmp.name, fetcher=lambda db, d: [], today=lambda: TODAY, now=lambda: self.clock[0],
                          line_secret="s3cret", line_basic_id="abc1234", public_url=PUBLIC + "/",
                          notify_at="07:00", line_reply=lambda tok, text: self.replies.append((tok, text)),
                          notifier_factory=fake_factory)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_status_exposes_line_settings(self):
        st = self.app.status()
        self.assertEqual(st["line"]["basic_id"], "@abc1234")
        self.assertEqual(st["line"]["add_friend_url"], "https://line.me/R/ti/p/@abc1234")
        self.assertEqual(st["notify_at"], "07:00")
        plain = WebApp(self.tmp.name, today=lambda: TODAY)
        self.assertFalse(plain.status()["line"]["enabled"])
        self.assertEqual(plain.line_webhook(b"{}", "")[0], 404)

    def test_webhook_signature_and_dispatch(self):
        body = json.dumps({"events": [loc_event(user="U7")]}).encode()
        self.assertEqual(self.app.line_webhook(body, "bad")[0], 403)
        code, events = self.app.line_webhook(body, sign("s3cret", body))
        self.assertEqual(code, 200)
        self.app.handle_line_events(events)
        (tok, text), = self.replies
        self.assertIn("已訂閱", text)
        self.assertIn(f"{PUBLIC}/#25.025954", text)

    def test_daily_schedule(self):
        store = Store(self.tmp.name)
        store.add_subscription(Subscription(name="停車", kind="point", points=[PIN], days=7,
                                            channel="line", channel_target="U1"))
        store.close()
        self.assertFalse(self.app.notify_due())                       # 06:59
        self.clock[0] = datetime(2026, 10, 6, 7, 0, tzinfo=TAIPEI_TZ)
        self.assertTrue(self.app.notify_due())
        stats = self.app.run_daily_notify()
        self.assertEqual(stats["pushes"], 1)
        self.assertEqual(FakeNotifier.sent[0][1], "U1")
        self.assertFalse(self.app.notify_due())                       # 今天送過了
        self.clock[0] = datetime(2026, 10, 7, 7, 5, tzinfo=TAIPEI_TZ)
        self.assertTrue(self.app.notify_due())                        # 隔天
        no_schedule = WebApp(self.tmp.name, notify_at=None)
        self.assertFalse(no_schedule.notify_due())

    def test_http_webhook_route(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            body = json.dumps({"events": [text_event("幫助", user="U8", token="tok8")]}).encode()

            def post(sig):
                req = urllib.request.Request(base + "/line/webhook", data=body, method="POST",
                                             headers={"X-Line-Signature": sig, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            self.assertEqual(post("bad"), 403)
            self.assertEqual(post(sign("s3cret", body)), 200)
            for _ in range(50):
                if self.replies:
                    break
                time.sleep(0.05)
            self.assertEqual(self.replies[0][0], "tok8")
            self.assertIn("傳送你的「位置」", self.replies[0][1])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
