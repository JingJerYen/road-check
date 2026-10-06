"""LINE Bot：使用者在 LINE 裡傳「位置」就能訂閱，不用裝 app。

指令（傳文字給 bot；回覆都會附快速按鈕）：
  傳送位置訊息             -> 以該點建立訂閱（預設半徑 100 公尺、未來 3 天），並立刻回覆目前那裡的狀況
  訂閱 25.0259,121.4927 [100m] [7天] [名稱]
                           -> 用座標訂閱；網站的「用 LINE 訂閱」按鈕就是送這個。直接貼「25.0259, 121.4927」也行
  查詢                     -> 現在所有訂閱附近的狀況
  列表                     -> 列出我的訂閱
  刪除 <編號>              -> 刪除訂閱
  半徑 <公尺> [編號]       -> 改半徑；不給編號就改最近一筆
  天數 <天> [編號]         -> 通知未來幾天內的事件（1–14，預設 3）
  路線 lat,lon;lat,lon;…   -> 建立路線訂閱
  路名 忠孝東路四段 …      -> 替最近一筆訂閱加上路名（給沒座標的公告比對用）
  地址 台北市西園路二段255號 -> 地址轉座標後訂閱（需 GOOGLE_MAPS_API_KEY）
  地圖                     -> 最近一筆訂閱在網頁上的連結（需 ROADCHECK_PUBLIC_URL）
  幫助                     -> 說明

回覆裡列出的事件會記為「已通知」，隔天早上的推播只送新的異動。

環境變數：
  LINE_CHANNEL_SECRET        驗證 X-Line-Signature
  LINE_CHANNEL_ACCESS_TOKEN  回覆／推播
  ROADCHECK_PUBLIC_URL       網站的公開網址，回覆與推播會附上地圖連結
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

from .dates import taipei_today
from .geo import parse_points
from .geocode import GeocodeError, Geocoder, get_geocoder
from .models import MAX_DAYS, MIN_DAYS, Subscription, extract_roads
from .notify import LineNotifier, Reply
from .service import brief_lines, current_matches, mark_seen, web_link
from .store import Store

log = logging.getLogger("roadcheck.linebot")

HELP = (
    "📍 傳送你的「位置」給我（按下面的「傳送位置」），就會訂閱那個停車點附近的施工、封路、集會與臨時佔用道路。"
    "每天早上有新異動才會通知你。\n\n"
    "其他指令：\n"
    "・查詢：現在訂閱的地方有什麼事\n"
    "・列表：看我的訂閱\n"
    "・天數 7：通知未來 7 天內的事件（1–14，預設 3）\n"
    "・半徑 200：範圍改成 200 公尺（預設 100）\n"
    "・刪除 3：刪除第 3 筆\n"
    "・路名 西園路二段：加上路名，沒座標的公告也能比對\n"
    "・地址 台北市西園路二段255號：用地址訂閱\n"
    "・路線 25.04,121.54;25.05,121.55：訂閱一條路線\n"
    "「天數」「半徑」後面可以加編號，例如「天數 7 2」改第 2 筆。"
)

LOCATION_BUTTON = ("📍 傳送位置", None)
MENU_QUICK = [LOCATION_BUTTON, ("查詢", "查詢"), ("列表", "列表"), ("幫助", "幫助")]
SETTINGS_QUICK = [("未來 3 天", "天數 3"), ("未來 7 天", "天數 7"), ("半徑 50 m", "半徑 50"),
                  ("半徑 200 m", "半徑 200"), ("查詢", "查詢"), ("列表", "列表"), LOCATION_BUTTON]

_COORDS_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*[,，]\s*(-?\d{1,3}\.\d+)")
_RADIUS_RE = re.compile(r"(\d{2,4})\s*(?:m|M|公尺|米)(?![a-zA-Z])")
_DAYS_RE = re.compile(r"(\d{1,2})\s*[天日]")
# 目前只有台北市的資料
TAIPEI_BBOX = (24.95, 25.22, 121.44, 121.68)
MIN_RADIUS_M, MAX_RADIUS_M = 20.0, 2000.0


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature or "")


def parse_webhook(body: bytes, signature: str, secret: str):
    """驗章並解析。回傳 (HTTP 狀態碼, events)；不是 200 時 events 為空。"""
    if secret and not verify_signature(secret, body, signature):
        return 403, []
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, []
    events = payload.get("events", []) if isinstance(payload, dict) else []
    return 200, [e for e in events if isinstance(e, dict)]


def dispatch_events(events: list, handle: Callable[[dict], Optional[str]], reply: Callable[[str, str], None]) -> None:
    """逐一處理事件並回覆；一個失敗不影響其他。"""
    for ev in events:
        try:
            text = handle(ev)
            if text and ev.get("replyToken"):
                reply(ev["replyToken"], text)
        except Exception:  # noqa: BLE001
            log.exception("failed handling LINE event %s", ev.get("type"))


def in_taipei(lat: float, lon: float) -> bool:
    return TAIPEI_BBOX[0] <= lat <= TAIPEI_BBOX[1] and TAIPEI_BBOX[2] <= lon <= TAIPEI_BBOX[3]


class CommandHandler:
    """把 LINE 事件轉成對 Store 的操作，回傳要回覆的 Reply。與 HTTP 無關，方便測試。"""

    def __init__(self, store: Store, default_radius_m: float = 100.0, geocoder: Optional[Geocoder] = None,
                 public_url: Optional[str] = None, today: Optional[Callable[[], date]] = None):
        self.store = store
        self.default_radius_m = default_radius_m
        self._geocoder = geocoder
        self.public_url = public_url if public_url is not None else os.environ.get("ROADCHECK_PUBLIC_URL", "")
        self.today = today or taipei_today

    @property
    def geocoder(self) -> Geocoder:
        return self._geocoder or get_geocoder()

    # ---- events ----
    def handle_event(self, event: dict) -> Optional[Reply]:
        etype = event.get("type")
        user_id = (event.get("source") or {}).get("userId", "")
        if not user_id:
            return None
        if etype == "follow":
            return Reply("歡迎！\n" + HELP, MENU_QUICK)
        if etype != "message":
            return None
        msg = event.get("message") or {}
        if msg.get("type") == "location":
            return self.subscribe_point(user_id, msg)
        if msg.get("type") == "text":
            return self.handle_text(user_id, msg.get("text", ""))
        return Reply("我只看得懂文字和位置喔。\n傳送位置就能訂閱。", MENU_QUICK)

    def subscribe_point(self, user_id: str, msg: dict) -> Reply:
        name = msg.get("title") or msg.get("address") or "我的位置"
        return self._create(Subscription(
            name=name[:40], kind="point", points=[(float(msg["latitude"]), float(msg["longitude"]))],
            radius_m=self.default_radius_m, channel="line", channel_target=user_id,
        ))

    # ---- helpers ----
    def _create(self, sub: Subscription, extra: str = "") -> Reply:
        self.store.add_subscription(sub)
        where = "線兩側" if sub.kind == "route" else "半徑"
        lines = [f"✅ 已訂閱 #{sub.id}「{sub.name}」",
                 f"{where} {sub.radius_m:.0f} 公尺，通知未來 {sub.days} 天。"]
        if extra:
            lines.append(extra.replace("{id}", str(sub.id)))
        if sub.kind == "point" and not in_taipei(*sub.points[0]):
            lines.append("⚠️ 這個位置不在台北市，目前只有台北市的資料。")
        lines += ["", self._status_block(sub)]
        link = web_link(self.public_url, sub)
        if link:
            lines += ["", f"🗺 地圖：{link}"]
        lines += ["", "每天早上有新的異動才會通知你。下面的按鈕可以改天數和半徑。"]
        return Reply("\n".join(lines), SETTINGS_QUICK)

    def _status_block(self, sub: Subscription) -> str:
        """這個訂閱現在符合的事件；列出來的同時記為已通知。"""
        if not self.store.source_stats():
            return "資料還在下載，好了之後有異動會通知你。"
        ms = current_matches(self.store, [sub], today=self.today())
        mark_seen(self.store, ms)
        if not ms:
            return f"目前未來 {sub.days} 天，這裡沒有已知的施工或封路。"
        return f"目前未來 {sub.days} 天有 {len(ms)} 件：\n" + "\n".join(brief_lines(ms))

    def _target(self, user_id: str, args: list) -> tuple:
        """(訂閱, 錯誤訊息)：有編號就找那一筆，否則最近一筆。"""
        if args:
            sub = self._own(user_id, args[0])
            return sub, (None if sub else "找不到這個訂閱。傳「列表」看看你的訂閱。")
        sub = self._latest(user_id)
        return sub, (None if sub else "你還沒有訂閱。")

    def _no_subs(self) -> Reply:
        return Reply("你還沒有訂閱。按「傳送位置」選你的停車位置就能訂閱。", MENU_QUICK)

    # ---- text commands ----
    def handle_text(self, user_id: str, text: str) -> Reply:
        text = text.strip()
        parts = text.split()
        if not parts:
            return Reply(HELP, MENU_QUICK)
        cmd, args = parts[0], parts[1:]
        if cmd.startswith("地址") and cmd != "地址":       # 「地址台北市…」沒空格也接受
            cmd, args = "地址", [cmd[2:], *args]
        if _COORDS_RE.match(text):                         # 直接貼座標
            cmd, args = "訂閱", parts

        if cmd in ("幫助", "help", "說明", "?", "？"):
            return Reply(HELP, MENU_QUICK)
        if cmd in ("訂閱", "subscribe"):
            return self._cmd_subscribe(user_id, " ".join(args))
        if cmd in ("查詢", "現在", "check", "狀況"):
            return self._cmd_check(user_id)
        if cmd in ("列表", "list", "清單"):
            return self._cmd_list(user_id)
        if cmd in ("地圖", "網頁", "map"):
            return self._cmd_map(user_id, args)
        if cmd in ("刪除", "delete", "del") and args:
            sub = self._own(user_id, args[0])
            if sub is None:
                return Reply("找不到這個編號。傳「列表」看看你的訂閱。", MENU_QUICK)
            self.store.delete_subscription(sub.id)
            return Reply(f"🗑 已刪除 #{sub.id}「{sub.name}」", MENU_QUICK)
        if cmd in ("半徑", "radius"):
            return self._cmd_radius(user_id, args)
        if cmd in ("天數", "days"):
            return self._cmd_days(user_id, args)
        if cmd in ("路線", "route") and args:
            try:
                pts = parse_points(" ".join(args))
                sub = Subscription(name=f"路線 {len(pts)} 點", kind="route", points=pts, radius_m=50.0,
                                   channel="line", channel_target=user_id)
            except (ValueError, IndexError):
                return Reply("路線格式：路線 25.04,121.54;25.05,121.55（至少兩點）", MENU_QUICK)
            return self._create(sub)
        if cmd in ("地址", "address", "addr"):
            return self._cmd_address(user_id, "".join(args))
        if cmd in ("路名", "roads") and args:
            sub = self._latest(user_id)
            if sub is None:
                return self._no_subs()
            sub.roads = sorted(set(sub.roads) | set(args))
            sub.__post_init__()
            self.store.update_subscription(sub)
            return Reply(f"✅ #{sub.id} 路名：{'、'.join(sub.roads)}", SETTINGS_QUICK)
        return Reply("看不懂這個指令。\n\n" + HELP, MENU_QUICK)

    def _cmd_subscribe(self, user_id: str, rest: str) -> Reply:
        m = _COORDS_RE.search(rest)
        if not m:
            return Reply("格式：訂閱 25.025954,121.492734 100m 7天\n或直接按「傳送位置」。", MENU_QUICK)
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (21.0 < lat < 26.5 and 118.0 < lon < 123.0):
            return Reply("這個座標不在台灣。格式是「緯度,經度」，例如 25.025954,121.492734。", MENU_QUICK)
        rest = rest[:m.start()] + rest[m.end():]
        radius = self.default_radius_m
        rm = _RADIUS_RE.search(rest)
        if rm:
            radius = max(MIN_RADIUS_M, min(float(rm.group(1)), MAX_RADIUS_M))
            rest = rest[:rm.start()] + rest[rm.end():]
        days = 3
        dm = _DAYS_RE.search(rest)
        if dm:
            days = int(dm.group(1))
            rest = rest[:dm.start()] + rest[dm.end():]
        name = " ".join(rest.split())[:40] or "地圖上選的位置"
        return self._create(Subscription(name=name, kind="point", points=[(lat, lon)], radius_m=radius,
                                         days=days, channel="line", channel_target=user_id))

    def _cmd_check(self, user_id: str) -> Reply:
        subs = self.store.list_subscriptions(channel_target=user_id)
        if not subs:
            return self._no_subs()
        blocks = []
        for s in subs:
            block = f"#{s.id}「{s.name}」\n{self._status_block(s)}"
            link = web_link(self.public_url, s)
            if link:
                block += f"\n🗺 {link}"
            blocks.append(block)
        return Reply("\n\n".join(blocks), SETTINGS_QUICK)

    def _cmd_list(self, user_id: str) -> Reply:
        subs = self.store.list_subscriptions(channel_target=user_id)
        if not subs:
            return self._no_subs()
        lines = [f"#{s.id} {s.name}（{'路線' if s.kind == 'route' else '位置'}，{s.radius_m:.0f}m，{s.days} 天"
                 + (f"，路名：{'、'.join(s.roads)}" if s.roads else "") + "）" for s in subs]
        return Reply("你的訂閱：\n" + "\n".join(lines) + "\n\n要刪除就傳「刪除 編號」。", MENU_QUICK)

    def _cmd_map(self, user_id: str, args: list) -> Reply:
        if not self.public_url:
            return Reply("這個 bot 沒有設定網頁網址。", MENU_QUICK)
        sub, err = self._target(user_id, args)
        if err:
            return Reply(f"🗺 {self.public_url.rstrip('/')}/", MENU_QUICK)
        return Reply(f"🗺 #{sub.id}「{sub.name}」：{web_link(self.public_url, sub) or self.public_url}", MENU_QUICK)

    def _cmd_radius(self, user_id: str, args: list) -> Reply:
        if not args:
            return Reply("請在「半徑」後面接公尺數，例如「半徑 200」。", SETTINGS_QUICK)
        try:
            r = float(args[0].rstrip("mM公尺米"))
        except ValueError:
            return Reply("半徑要是數字，例如「半徑 200」。", SETTINGS_QUICK)
        sub, err = self._target(user_id, args[1:])
        if err:
            return Reply(err, MENU_QUICK)
        sub.radius_m = max(MIN_RADIUS_M, min(r, MAX_RADIUS_M))
        self.store.update_subscription(sub)
        return Reply(f"✅ #{sub.id}「{sub.name}」半徑改為 {sub.radius_m:.0f} 公尺\n\n{self._status_block(sub)}",
                     SETTINGS_QUICK)

    def _cmd_days(self, user_id: str, args: list) -> Reply:
        if not args:
            return Reply(f"請在「天數」後面接數字，例如「天數 7」（{MIN_DAYS}–{MAX_DAYS} 天）。", SETTINGS_QUICK)
        try:
            n = int(args[0].rstrip("天日"))
        except ValueError:
            return Reply(f"天數要是數字，例如「天數 7」（{MIN_DAYS}–{MAX_DAYS} 天）。", SETTINGS_QUICK)
        sub, err = self._target(user_id, args[1:])
        if err:
            return Reply(err, MENU_QUICK)
        sub.days = n
        sub.__post_init__()           # 超出範圍會被夾到 1–14
        self.store.update_subscription(sub)
        note = "" if sub.days == n else f"（只能設 {MIN_DAYS}–{MAX_DAYS} 天）"
        return Reply(f"✅ #{sub.id}「{sub.name}」改為通知未來 {sub.days} 天{note}\n\n{self._status_block(sub)}",
                     SETTINGS_QUICK)

    def _cmd_address(self, user_id: str, address: str) -> Reply:
        if not address:
            return Reply("請在「地址」後面接門牌，例如「地址 台北市西園路二段255號」。", MENU_QUICK)
        try:
            g = self.geocoder.geocode(address)
        except GeocodeError as e:
            log.warning("geocode failed for %r: %s", address, e)
            return Reply(f"找不到這個地址的座標（{e}）。你也可以直接傳「位置」給我。", MENU_QUICK)
        roads = sorted(extract_roads(g.road or g.full_address or address))
        sub = Subscription(name=(g.full_address or address)[:40], kind="point", points=[g.location],
                           radius_m=self.default_radius_m, roads=roads, channel="line", channel_target=user_id)
        extra = f"座標 {g.lat:.5f}, {g.lon:.5f}" + (f"，路名 {'、'.join(roads)}" if roads else "")
        return self._create(sub, extra=extra + "\n位置不對的話傳「刪除 {id}」再重傳位置。")

    def _own(self, user_id: str, id_text: str) -> Optional[Subscription]:
        try:
            sid = int(id_text.lstrip("#"))
        except ValueError:
            return None
        sub = self.store.get_subscription(sid)
        return sub if sub and sub.channel_target == user_id else None

    def _latest(self, user_id: str) -> Optional[Subscription]:
        subs = self.store.list_subscriptions(channel_target=user_id)
        return subs[-1] if subs else None


def make_handler(handler: CommandHandler, reply: Callable[[str, str], None], secret: str):
    """單獨跑 LINE webhook（roadcheck serve-line）用；roadcheck serve 會把 webhook 掛在網站上。"""

    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"roadcheck line webhook ok")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            code, events = parse_webhook(self.rfile.read(length), self.headers.get("X-Line-Signature", ""), secret)
            # LINE 要求盡快回 200，先回再處理
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            dispatch_events(events, handler.handle_event, reply)

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
