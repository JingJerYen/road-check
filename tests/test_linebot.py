import os
import tempfile
import unittest

from roadcheck.linebot import CommandHandler, verify_signature
from roadcheck.store import Store


def loc_event(user="U1", lat=25.0418, lon=121.5440, title="公司停車場"):
    return {"type": "message", "replyToken": "r", "source": {"type": "user", "userId": user},
            "message": {"type": "location", "title": title, "address": "台北市", "latitude": lat, "longitude": lon}}


def text_event(text, user="U1"):
    return {"type": "message", "replyToken": "r", "source": {"type": "user", "userId": user},
            "message": {"type": "text", "text": text}}


class LineBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.h = CommandHandler(self.store)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_location_creates_point_subscription(self):
        reply = self.h.handle_event(loc_event())
        self.assertIn("已訂閱", reply)
        subs = self.store.list_subscriptions(channel_target="U1")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].kind, "point")
        self.assertEqual(subs[0].channel, "line")
        self.assertEqual(subs[0].name, "公司停車場")

    def test_list_radius_roads_delete(self):
        self.h.handle_event(loc_event())
        self.assertIn("#1", self.h.handle_event(text_event("列表")))
        self.assertIn("200", self.h.handle_event(text_event("半徑 200")))
        self.assertEqual(self.store.get_subscription(1).radius_m, 200)
        self.h.handle_event(text_event("路名 忠孝東路四段 復興南路"))
        self.assertEqual(self.store.get_subscription(1).roads, ["復興南路", "忠孝東路4段"])
        self.assertIn("已刪除", self.h.handle_event(text_event("刪除 1")))
        self.assertIsNone(self.store.get_subscription(1))

    def test_cannot_delete_other_users_subscription(self):
        self.h.handle_event(loc_event(user="U1"))
        self.assertIn("找不到", self.h.handle_event(text_event("刪除 1", user="U2")))
        self.assertIsNotNone(self.store.get_subscription(1))

    def test_route_command(self):
        reply = self.h.handle_event(text_event("路線 25.04,121.54;25.05,121.55"))
        self.assertIn("路線", reply)
        self.assertEqual(self.store.get_subscription(1).kind, "route")
        self.assertIn("格式", self.h.handle_event(text_event("路線 abc")))

    def test_follow_and_unknown(self):
        self.assertIn("歡迎", self.h.handle_event({"type": "follow", "source": {"userId": "U9"}}))
        self.assertIn("看不懂", self.h.handle_event(text_event("哈囉")))

    def test_signature(self):
        import base64, hashlib, hmac
        body = b'{"events":[]}'
        sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_signature("secret", body, sig))
        self.assertFalse(verify_signature("secret", body, "bad"))


if __name__ == "__main__":
    unittest.main()


class LineBotAddressTests(unittest.TestCase):
    def setUp(self):
        from roadcheck.geocode import TgosGeocoder
        from tests.test_geocode import SAMPLE_XML, SAMPLE_EMPTY, FakeFetch

        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.ok = TgosGeocoder(app_id="a", api_key="b", fetch=FakeFetch(SAMPLE_XML))
        self.empty = TgosGeocoder(app_id="a", api_key="b", fetch=FakeFetch(SAMPLE_EMPTY))

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_address_command_subscribes(self):
        h = CommandHandler(self.store, geocoder=self.ok)
        reply = h.handle_event(text_event("地址 台北市西園路二段255號"))
        self.assertIn("已訂閱", reply)
        self.assertIn("25.02730, 121.49390", reply)
        sub = self.store.list_subscriptions(channel_target="U1")[0]
        self.assertEqual(sub.points, [(25.0273, 121.4939)])
        self.assertEqual(sub.roads, ["西園路2段"])
        self.assertTrue(sub.name.endswith("西園路二段255號"))

    def test_address_without_space_and_not_found(self):
        h = CommandHandler(self.store, geocoder=self.empty)
        reply = h.handle_event(text_event("地址台北市不存在路1號"))
        self.assertIn("找不到", reply)
        self.assertEqual(self.store.list_subscriptions(channel_target="U1"), [])
        self.assertIn("請在「地址」後面", h.handle_event(text_event("地址")))
