"""簡易網站：在地圖上放圖釘、選未來幾天，查詢會影響那裡的施工與封路。

  roadcheck web [--port 8080] [--host 127.0.0.1] [--fetch-days 7] [--refresh-hours 6]

純標準庫 http.server。地圖用 Leaflet（放在 static/leaflet，不靠 CDN）＋ OpenStreetMap 圖磚。

API：
  GET  /api/status                         資料更新時間、筆數、是否更新中
  GET  /api/check?lat=&lon=&radius=&days=&roads=a,b
                                           範圍內的事件（matches）與附近的事件（nearby，畫在地圖上當參考）
  POST /api/refresh                        立刻在背景重新抓資料

資料放在 SQLite（跟 roadcheck run 共用同一個資料庫）。伺服器啟動時資料太舊或沒有資料就在背景抓，
之後每 refresh_hours 小時再抓一次。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from ..matcher import distance_m, is_relevant_period
from ..models import KIND_LABEL, MAX_DAYS, MIN_DAYS, Event, Subscription, normalize_road
from ..sources import ALL_SOURCES, SourceError
from ..store import Store

log = logging.getLogger("roadcheck.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SOURCE_LABEL = {
    "taipei_today_construction": "今日施工",
    "taipei_ext_restriction": "外部管制路段",
}
MIN_RADIUS_M, MAX_RADIUS_M = 10.0, 2000.0
NEARBY_MIN_M = 500.0          # 「附近」至少看這麼遠，給使用者參考
NEARBY_LIMIT = 60

Fetcher = Callable[[str, int], list]   # (db_path, ext_days) -> [(source, n_events | None, error | None)]


def fetch_all(db_path: str, ext_days: int) -> list:
    """抓所有來源存進資料庫。每個來源獨立，一個失敗不影響另一個。"""
    store = Store(db_path)
    results = []
    try:
        for name, cls in ALL_SOURCES.items():
            src = cls(days=ext_days) if name == "taipei_ext_restriction" else cls()
            try:
                events = src.fetch()
            except SourceError as e:
                log.warning("%s fetch failed: %s", name, e)
                results.append((name, None, str(e)))
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("%s parse failed", name)
                results.append((name, None, repr(e)))
                continue
            store.upsert_events(events)
            log.info("%s: %d events", name, len(events))
            results.append((name, len(events), None))
    finally:
        store.close()
    return results


class WebApp:
    """不含 HTTP 的邏輯，方便測試。"""

    def __init__(self, db_path: str, fetch_days: int = 7, refresh_hours: float = 6.0,
                 fetcher: Optional[Fetcher] = None, today: Optional[Callable[[], date]] = None):
        self.db_path = db_path
        self.fetch_days = max(MIN_DAYS, min(int(fetch_days), MAX_DAYS))
        self.refresh_hours = refresh_hours
        self.fetcher = fetcher or fetch_all
        self.today = today or date.today
        self._lock = threading.Lock()
        self._refreshing = False
        self._last_error: Optional[str] = None
        self._last_attempt: Optional[float] = None
        self._cache_key = None
        self._cache_events: list[Event] = []
        Store(db_path).close()          # 建表／遷移

    # ---- data ----
    def _events(self) -> list[Event]:
        store = Store(self.db_path)
        try:
            key = tuple((s["source"], s["events"], s["updated_at"]) for s in store.source_stats())
            with self._lock:
                if key == self._cache_key:
                    return self._cache_events
            events = store.list_current_events()
        finally:
            store.close()
        with self._lock:
            self._cache_key, self._cache_events = key, events
        return events

    def status(self) -> dict:
        store = Store(self.db_path)
        try:
            stats = store.source_stats()
        finally:
            store.close()
        with self._lock:
            refreshing, err = self._refreshing, self._last_error
        return {
            "today": self.today().isoformat(),
            "fetch_days": self.fetch_days,
            "min_days": MIN_DAYS,
            "max_days": MAX_DAYS,
            "refreshing": refreshing,
            "last_error": err,
            "sources": [dict(s, label=SOURCE_LABEL.get(s["source"], s["source"])) for s in stats],
        }

    def is_stale(self) -> bool:
        stats = self.status()["sources"]
        if len(stats) < len(ALL_SOURCES):
            return True
        oldest = min(_parse_ts(s["updated_at"]) for s in stats)
        return datetime.now(timezone.utc) - oldest > timedelta(hours=self.refresh_hours)

    def refresh(self, wait: bool = False) -> bool:
        """在背景重新抓資料；已經在抓就不重複。回傳這次有沒有啟動。"""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            self._last_attempt = time.time()

        def work():
            err = None
            try:
                results = self.fetcher(self.db_path, self.fetch_days)
                errors = [f"{SOURCE_LABEL.get(n, n)}：{e}" for n, _, e in results if e]
                err = "；".join(errors) or None
            except Exception as e:  # noqa: BLE001
                log.exception("refresh failed")
                err = repr(e)
            finally:
                with self._lock:
                    self._refreshing = False
                    self._last_error = err

        if wait:
            work()
        else:
            threading.Thread(target=work, name="roadcheck-refresh", daemon=True).start()
        return True

    def auto_refresh_loop(self, stop: threading.Event, check_every_s: float = 300.0) -> None:
        """資料太舊就抓；抓失敗至少隔 check_every_s 才重試。"""
        while not stop.is_set():
            try:
                recent = self._last_attempt and time.time() - self._last_attempt < check_every_s
                if not recent and self.is_stale():
                    self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("auto refresh check failed")
            stop.wait(check_every_s)

    # ---- query ----
    def check(self, lat: float, lon: float, radius_m: float = 100.0, days: int = 3,
              roads: Optional[list] = None) -> dict:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("lat/lon out of range")
        radius_m = max(MIN_RADIUS_M, min(float(radius_m), MAX_RADIUS_M))
        days = max(MIN_DAYS, min(int(days), self.fetch_days))
        sub = Subscription(name="web", kind="point", points=[(lat, lon)], radius_m=radius_m,
                           roads=[r for r in (roads or []) if r.strip()], days=days)
        today = self.today()
        nearby_m = max(radius_m * 4, NEARBY_MIN_M)
        matches, nearby = [], []
        for ev in self._events():
            if not is_relevant_period(ev, today, days):
                continue
            d = distance_m(sub, ev)
            if d is not None:
                if d <= radius_m:
                    matches.append(_event_json(ev, today, d, "distance"))
                elif d <= nearby_m:
                    nearby.append(_event_json(ev, today, d, "nearby"))
            elif sub.roads:
                hits = sorted(set(sub.roads) & ev.roads)
                if hits:
                    matches.append(_event_json(ev, today, None, "road", hits))
        matches.sort(key=lambda m: (not m["blocks_traffic"], m["reason"] != "distance",
                                    m["distance_m"] if m["distance_m"] is not None else 1e9, m["start"] or ""))
        nearby.sort(key=lambda m: m["distance_m"])
        return {
            "query": {"lat": lat, "lon": lon, "radius_m": radius_m, "days": days, "roads": sub.roads,
                      "today": today.isoformat(), "until": (today + timedelta(days=days)).isoformat()},
            "matches": matches,
            "nearby": nearby[:NEARBY_LIMIT],
            "nearby_radius_m": nearby_m,
            "status": self.status(),
        }


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _round_shape(shape) -> list:
    return [[round(p[0], 6), round(p[1], 6)] for p in shape]


def _event_json(ev: Event, today: date, dist: Optional[float], reason: str, roads: Optional[list] = None) -> dict:
    extra = ev.extra or {}
    if ev.start and ev.start > today:
        timing = f"{(ev.start - today).days} 天後開始"
    elif ev.end and ev.end == today:
        timing = "今天結束"
    else:
        timing = "進行中"
    return {
        "key": ev.key,
        "source": ev.source,
        "source_label": SOURCE_LABEL.get(ev.source, ev.source),
        "kind": ev.kind,
        "kind_label": KIND_LABEL.get(ev.kind, ev.kind),
        "category": extra.get("mode_label") or extra.get("app_mode") or "",
        "title": ev.title,
        "address": ev.address[:400],
        "start": ev.start.isoformat() if ev.start else None,
        "end": ev.end.isoformat() if ev.end else None,
        "timing": timing,
        "time_window": ev.time_window,
        "blocks_traffic": bool(ev.blocks_traffic),
        "agency": ev.agency,
        "purpose": ev.purpose,
        "plan_b": extra.get("plan_b") or "",
        "url": ev.url,
        "bulletin_url": extra.get("bulletin_url") or "",
        "reason": reason,
        "distance_m": round(dist, 1) if dist is not None else None,
        "matched_roads": roads or [],
        "lat": ev.lat,
        "lon": ev.lon,
        "shapes": [_round_shape(s) for s in ev.shapes],
    }


def _parse_roads(text: str) -> list:
    out = []
    for part in text.replace("，", ",").replace("、", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part:
            out.append(normalize_road(part))
    return out


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "roadcheck-web"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _static(self, rel: str) -> None:
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in target.parents or not target.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "text/javascript"):
                ctype += "; charset=utf-8"
            cache = "public, max-age=86400" if "/leaflet/" in str(target) else "no-cache"
            self._send(200, target.read_bytes(), ctype, cache)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    self._static("index.html")
                elif path.startswith("/static/"):
                    self._static(path[len("/static/"):])
                elif path == "/api/status":
                    self._json(200, app.status())
                elif path == "/api/check":
                    q = parse_qs(url.query)
                    try:
                        lat, lon = float(q["lat"][0]), float(q["lon"][0])
                        radius = float(q.get("radius", ["100"])[0])
                        days = int(q.get("days", ["3"])[0])
                    except (KeyError, ValueError, IndexError):
                        self._json(400, {"error": "需要 lat、lon（數字），可選 radius、days、roads"})
                        return
                    roads = _parse_roads(q.get("roads", [""])[0])
                    try:
                        self._json(200, app.check(lat, lon, radius, days, roads))
                    except ValueError as e:
                        self._json(400, {"error": str(e)})
                else:
                    self._json(404, {"error": "not found"})
            except BrokenPipeError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("GET %s failed", self.path)
                self._json(500, {"error": "server error"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/api/refresh":
                started = app.refresh()
                self._json(202 if started else 200, {"started": started, "status": app.status()})
            else:
                self._json(404, {"error": "not found"})

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080, fetch_days: int = 7,
          refresh_hours: float = 6.0, auto_refresh: bool = True) -> None:
    app = WebApp(db_path, fetch_days=fetch_days, refresh_hours=refresh_hours)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    stop = threading.Event()
    if auto_refresh:
        threading.Thread(target=app.auto_refresh_loop, args=(stop,), name="roadcheck-auto", daemon=True).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "") else host
    print(f"roadcheck web：打開瀏覽器到 http://{shown}:{server.server_address[1]}/  （Ctrl+C 結束）")
    if auto_refresh and app.is_stale():
        print("資料不存在或太舊，背景下載中（施工幾秒，外部管制路段約 5–10 分鐘），網頁會顯示進度。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
