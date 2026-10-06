"""LINE Bot webhook：使用者在 LINE 裡傳「位置」就能訂閱，不用裝 app。

純標準庫 http.server，MVP 夠用；正式上線可放在 nginx/Cloudflare Tunnel 後面。

指令（傳文字給 bot）：
  傳送位置訊息           -> 以該點建立訂閱（預設半徑 100 公尺）
  列表                    -> 列出我的訂閱
  刪除 <編號>             -> 刪除訂閱
  半徑 <公尺>             -> 更改最近一筆訂閱的半徑
  天數 <天> [編號]        -> 通知未來幾天內的事件（1–14，預設 3）；不給編號就改最近一筆
  路線 lat,lon;lat,lon;…  -> 建立路線訂閱
  路名 忠孝東路四段 復興南路 -> 替最近一筆訂閱加上路名（給沒座標的封路資料比對用）
  地址 台北市西園路二段255號 -> 地址轉座標後訂閱（需 GOOGLE_MAPS_API_KEY，或 TGOS 金鑰）
  幫助                    -> 說明

環境變數：
  LINE_CHANNEL_SECRET        驗證 X-Line-Signature
  LINE_CHANNEL_ACCESS_TOKEN  回覆／推播
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from .geo import parse_points
from .geocode import GeocodeError, Geocoder, get_geocoder
from .models import MAX_DAYS, MIN_DAYS, Subscription, extract_roads
from .notify import LineNotifier
from .store import Store

log = logging.getLogger("roadcheck.linebot")

HELP = (
    "📍 傳送你的「位置」給我，就會訂閱那個停車位置附近的施工與封路。\n"
    "其他指令：\n"
    "・列表：看我的訂閱\n"
    "・刪除 3：刪除第 3 筆\n"
    "・半徑 200：把最近一筆改成 200 公尺\n"
    "・天數 7：通知未來 7 天內的事件（1–14 天，預設 3）；「天數 7 2」改第 2 筆\n"
    "・路線 25.04,121.54;25.05,121.55：訂閱一條路線\n"
    "・路名 忠孝東路四段 復興南路：加上路名，沒座標的封路公告也能比對\n"
    "・地址 台北市西園路二段255號：用門牌地址訂閱\n"
    "・幫助：看這段說明"
)


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature or "")


class CommandHandler:
    """把 LINE 事件轉成對 Store 的操作，回傳要回覆的文字。與 HTTP 無關，方便測試。"""

    def __init__(self, store: Store, default_radius_m: float = 100.0, geocoder: Geocoder | None = None):
        self.store = store
        self.default_radius_m = default_radius_m
        self._geocoder = geocoder

    @property
    def geocoder(self) -> Geocoder:
        return self._geocoder or get_geocoder()

    def handle_event(self, event: dict) -> str | None:
        etype = event.get("type")
        user_id = (event.get("source") or {}).get("userId", "")
        if not user_id:
            return None
        if etype == "follow":
            return "歡迎！\n" + HELP
        if etype != "message":
            return None
        msg = event.get("message") or {}
        if msg.get("type") == "location":
            return self.subscribe_point(user_id, msg)
        if msg.get("type") == "text":
            return self.handle_text(user_id, msg.get("text", ""))
        return None

    def subscribe_point(self, user_id: str, msg: dict) -> str:
        name = msg.get("title") or msg.get("address") or "我的位置"
        sub = Subscription(
            name=name[:40], kind="point",
            points=[(float(msg["latitude"]), float(msg["longitude"]))],
            radius_m=self.default_radius_m, channel="line", channel_target=user_id,
        )
        self.store.add_subscription(sub)
        return (f"✅ 已訂閱 #{sub.id}「{sub.name}」\n半徑 {sub.radius_m:.0f} 公尺，通知未來 {sub.days} 天。"
                f"\n附近有施工或封路時會通知你。傳「半徑 200」可調整範圍，「天數 7」可改天數。")

    def handle_text(self, user_id: str, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return HELP
        cmd, args = parts[0], parts[1:]
        if cmd.startswith("地址") and cmd != "地址":       # 「地址台北市…」沒空格也接受
            cmd, args = "地址", [cmd[2:], *args]
        if cmd in ("幫助", "help", "說明", "?"):
            return HELP
        if cmd in ("列表", "list", "清單"):
            subs = self.store.list_subscriptions(channel_target=user_id)
            if not subs:
                return "你還沒有訂閱。傳送位置給我就能訂閱。"
            lines = [f"#{s.id} {s.name}（{'路線' if s.kind == 'route' else '位置'}，{s.radius_m:.0f}m，{s.days} 天"
                     + (f"，路名：{'、'.join(s.roads)}" if s.roads else "") + "）" for s in subs]
            return "你的訂閱：\n" + "\n".join(lines)
        if cmd in ("刪除", "delete", "del") and args:
            sub = self._own(user_id, args[0])
            if sub is None:
                return "找不到這個編號。傳「列表」看看你的訂閱。"
            self.store.delete_subscription(sub.id)
            return f"🗑 已刪除 #{sub.id}「{sub.name}」"
        if cmd in ("半徑", "radius") and args:
            sub = self._latest(user_id)
            if sub is None:
                return "你還沒有訂閱。"
            try:
                r = float(args[0].rstrip("mM公尺"))
            except ValueError:
                return "半徑要是數字，例如「半徑 200」。"
            sub.radius_m = max(20.0, min(r, 2000.0))
            self.store.update_subscription(sub)
            return f"✅ #{sub.id}「{sub.name}」半徑改為 {sub.radius_m:.0f} 公尺"
        if cmd in ("天數", "days"):
            if not args:
                return f"請在「天數」後面接數字，例如「天數 7」（{MIN_DAYS}–{MAX_DAYS} 天）。"
            try:
                n = int(args[0].rstrip("天日"))
            except ValueError:
                return f"天數要是數字，例如「天數 7」（{MIN_DAYS}–{MAX_DAYS} 天）。"
            sub = self._own(user_id, args[1]) if len(args) > 1 else self._latest(user_id)
            if sub is None:
                return "找不到這個訂閱。傳「列表」看看你的訂閱。" if len(args) > 1 else "你還沒有訂閱。"
            sub.days = n
            sub.__post_init__()           # 超出範圍會被夾到 1–14
            self.store.update_subscription(sub)
            note = "" if sub.days == n else f"（只能設 {MIN_DAYS}–{MAX_DAYS} 天）"
            return f"✅ #{sub.id}「{sub.name}」改為通知未來 {sub.days} 天{note}"
        if cmd in ("路線", "route") and args:
            try:
                pts = parse_points(" ".join(args))
                sub = Subscription(name=f"路線 {len(pts)} 點", kind="route", points=pts, radius_m=50.0,
                                   channel="line", channel_target=user_id)
            except (ValueError, IndexError):
                return "路線格式：路線 25.04,121.54;25.05,121.55（至少兩點）"
            self.store.add_subscription(sub)
            return f"✅ 已訂閱路線 #{sub.id}，線兩側 {sub.radius_m:.0f} 公尺，通知未來 {sub.days} 天。"
        if cmd in ("地址", "address", "addr"):
            address = "".join(args) if args else text.strip()[2:].strip()
            if not address:
                return "請在「地址」後面接門牌，例如「地址 台北市西園路二段255號」。"
            try:
                g = self.geocoder.geocode(address)
            except GeocodeError as e:
                log.warning("geocode failed for %r: %s", address, e)
                return f"找不到這個地址的座標（{e}）。你也可以直接傳「位置」給我。"
            roads = sorted(extract_roads(g.road or g.full_address or address))
            sub = Subscription(name=(g.full_address or address)[:40], kind="point", points=[g.location],
                               radius_m=self.default_radius_m, roads=roads, channel="line", channel_target=user_id)
            self.store.add_subscription(sub)
            return (f"✅ 已訂閱 #{sub.id}「{sub.name}」\n座標 {g.lat:.5f}, {g.lon:.5f}，半徑 {sub.radius_m:.0f} 公尺，"
                    f"通知未來 {sub.days} 天"
                    + (f"，路名 {'、'.join(roads)}" if roads else "") + "。\n位置不對的話傳「刪除 %d」再重傳位置。" % sub.id)
        if cmd in ("路名", "roads") and args:
            sub = self._latest(user_id)
            if sub is None:
                return "你還沒有訂閱。"
            sub.roads = sorted(set(sub.roads) | set(args))
            sub.__post_init__()
            self.store.update_subscription(sub)
            return f"✅ #{sub.id} 路名：{'、'.join(sub.roads)}"
        return "看不懂這個指令。\n" + HELP

    def _own(self, user_id: str, id_text: str) -> Subscription | None:
        try:
            sid = int(id_text.lstrip("#"))
        except ValueError:
            return None
        sub = self.store.get_subscription(sid)
        return sub if sub and sub.channel_target == user_id else None

    def _latest(self, user_id: str) -> Subscription | None:
        subs = self.store.list_subscriptions(channel_target=user_id)
        return subs[-1] if subs else None


def make_handler(handler: CommandHandler, reply: Callable[[str, str], None], secret: str):
    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"roadcheck line webhook ok")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if secret and not verify_signature(secret, body, self.headers.get("X-Line-Signature", "")):
                self.send_response(403)
                self.end_headers()
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            # LINE 要求盡快回 200，先回再處理
            self.send_response(200)
            self.end_headers()
            for ev in payload.get("events", []):
                try:
                    text = handler.handle_event(ev)
                    if text and ev.get("replyToken"):
                        reply(ev["replyToken"], text)
                except Exception:  # noqa: BLE001
                    log.exception("failed handling event %s", ev.get("type"))

    return WebhookHandler


def serve(store: Store, host: str = "0.0.0.0", port: int = 8000) -> None:
    secret = os.environ.get("LINE_CHANNEL_SECRET", "")
    if not secret:
        log.warning("LINE_CHANNEL_SECRET not set; signature check disabled (dev only)")
    notifier = LineNotifier()
    cmd = CommandHandler(store)
    server = HTTPServer((host, port), make_handler(cmd, notifier.reply, secret))
    log.info("LINE webhook listening on http://%s:%d/", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
