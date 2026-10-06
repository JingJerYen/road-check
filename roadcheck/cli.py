"""roadcheck 命令列。

  roadcheck subscribe point --name 家 --lat 25.04 --lon 121.54 --radius 100 [--line-user Uxxx]
  roadcheck subscribe point --name 停車 --address "台北市西園路二段255號"   # 地址轉座標（Google）
  roadcheck geocode "台北市西園路二段255號"            # 只查座標
  roadcheck subscribe route --name 通勤 --points "25.04,121.54;25.05,121.55" [--polyline <encoded>] [--days 7]
  roadcheck days 1 7                                  # 把 #1 改成通知未來 7 天
  roadcheck list
  roadcheck fetch [--source NAME] [--from-file PATH] [--dump]
  roadcheck run [--dry-run] [--horizon-days N]      # fetch + match + notify，排程每天跑；N 會覆寫所有訂閱的天數
  roadcheck demo                                     # 用樣本資料跑一遍，不需要網路
  roadcheck serve-line [--port 8000]                 # LINE webhook
  roadcheck web [--port 8080]                        # 網頁：地圖上放圖釘查詢
  roadcheck serve [--port 8080] [--notify-at 07:00]  # 網頁 + LINE webhook + 每天推播（正式上線用這個）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .dates import taipei_today
from .geo import decode_polyline, parse_points
from .geocode import GeocodeError, get_geocoder
from .matcher import match_all
from .models import MAX_DAYS, MIN_DAYS, Subscription, extract_roads
from .notify import ConsoleNotifier, format_digest, get_notifier, group_by_subscription
from .sources import ALL_SOURCES, SourceError
from .service import run_notifications
from .store import DEFAULT_DB, Store

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def cmd_subscribe(args, store: Store) -> int:
    roads = list(args.roads or [])
    if args.kind == "point":
        if args.address:
            try:
                g = get_geocoder().geocode(args.address)
            except GeocodeError as e:
                print(f"地址轉座標失敗：{e}", file=sys.stderr)
                return 1
            pts = [g.location]
            if not roads:
                roads = sorted(extract_roads(g.road or g.full_address or args.address))
            print(f"{get_geocoder().name}：{g.full_address or args.address} -> {g.lat:.5f}, {g.lon:.5f}"
                  + (f"（路名 {'、'.join(roads)}）" if roads else ""))
        else:
            pts = [(args.lat, args.lon)]
    else:
        if args.polyline:
            pts = decode_polyline(args.polyline)
        elif args.points:
            pts = parse_points(args.points)
        else:
            print("route needs --points or --polyline", file=sys.stderr)
            return 2
    channel = "line" if args.line_user else "console"
    sub = Subscription(
        name=args.name, kind=args.kind, points=pts, radius_m=args.radius,
        roads=roads, channel=channel, channel_target=args.line_user or "",
        only_blocking=args.only_blocking, days=args.days,
    )
    store.add_subscription(sub)
    print(f"added subscription #{sub.id} {sub.name} ({sub.kind}, {len(pts)} pts, {sub.radius_m:.0f} m, "
          f"未來 {sub.days} 天, {channel})")
    return 0


def cmd_geocode(args, store: Store) -> int:
    try:
        results = get_geocoder().query(args.address, max_results=args.limit)
    except GeocodeError as e:
        print(f"地址轉座標失敗：{e}", file=sys.stderr)
        return 1
    if not results:
        print("找不到結果", file=sys.stderr)
        return 1
    for g in results:
        roads = "、".join(sorted(extract_roads(g.road or g.full_address)))
        print(f"{g.lat:.6f},{g.lon:.6f}  {g.full_address}" + (f"  [{roads}]" if roads else ""))
    return 0


def cmd_list(args, store: Store) -> int:
    subs = store.list_subscriptions()
    if not subs:
        print("(no subscriptions)")
    for s in subs:
        roads = f" roads={','.join(s.roads)}" if s.roads else ""
        print(f"#{s.id:<3} {s.kind:<5} {s.radius_m:>5.0f}m {s.days:>2}天 {s.channel:<7} {s.name}{roads}")
    n = len(store.list_events())
    print(f"{n} events in store ({store.path})")
    return 0


def cmd_days(args, store: Store) -> int:
    sub = store.get_subscription(args.id)
    if sub is None:
        print(f"no subscription #{args.id}", file=sys.stderr)
        return 1
    if not MIN_DAYS <= args.n <= MAX_DAYS:
        print(f"天數要在 {MIN_DAYS}–{MAX_DAYS} 之間", file=sys.stderr)
        return 2
    sub.days = args.n
    store.update_subscription(sub)
    print(f"#{sub.id} {sub.name}：通知未來 {sub.days} 天")
    return 0


def ext_fetch_days(store: Store, override: int | None = None) -> int | None:
    """dig.taipei 要往後抓幾天：覆寫值 > 所有訂閱裡最大的天數（與 ROADCHECK_EXT_DAYS 取大）。

    回 None 表示交給來源自己的預設（沒有訂閱時）。
    """
    if override is not None:
        return override
    need = max((s.days for s in store.list_subscriptions()), default=None)
    env = os.environ.get("ROADCHECK_EXT_DAYS")
    if env:
        need = max(need or 0, int(env))
    return need


def _make_source(name: str, ext_days: int | None):
    cls = ALL_SOURCES[name]
    if ext_days is not None and name == "taipei_ext_restriction":
        return cls(days=ext_days)
    return cls()


def _fetch_into_store(store: Store, source_names: list[str], from_file: str | None, dump: bool,
                      ext_days: int | None = None) -> int:
    failures = 0
    for name in source_names:
        src = _make_source(name, ext_days)
        try:
            if from_file:
                if dump:
                    print(Path(from_file).read_text(encoding="utf-8", errors="replace")[:4000])
                events = src.from_file(from_file)
            else:
                raw = src.fetch_raw()
                if dump:
                    print(str(raw)[:4000])
                events = src.parse(raw)
        except SourceError as e:
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            failures += 1
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] parse failed: {e!r}", file=sys.stderr)
            failures += 1
            continue
        added, changed = store.upsert_events(events)
        with_coords = sum(1 for e in events if e.location)
        print(f"[{name}] {len(events)} events ({with_coords} with coordinates): +{added} new, {changed} changed")
    return failures


def cmd_fetch(args, store: Store) -> int:
    names = [args.source] if args.source else list(ALL_SOURCES)
    if args.from_file and not args.source:
        print("--from-file needs --source", file=sys.stderr)
        return 2
    return 1 if _fetch_into_store(store, names, args.from_file, args.dump, ext_fetch_days(store)) else 0


def cmd_run(args, store: Store) -> int:
    if not args.skip_fetch:
        _fetch_into_store(store, list(ALL_SOURCES), None, False, ext_fetch_days(store, args.horizon_days))
    stats = run_notifications(store, get_notifier, today=taipei_today(), horizon_days=args.horizon_days,
                              dry_run=args.dry_run, public_url=os.environ.get("ROADCHECK_PUBLIC_URL", ""))
    return 1 if stats["failed"] else 0


def cmd_demo(args, store: Store) -> int:
    """用 tests/fixtures 的樣本跑一遍：建兩筆訂閱、載入樣本、印出通知。"""
    from .sources import TaipeiTodayConstruction, TaipeiExtRestriction

    today = date.today()
    cons = TaipeiTodayConstruction().from_file(FIXTURE_DIR / "taipei_today_construction.real.json")
    rest = TaipeiExtRestriction().from_file(FIXTURE_DIR / "taipei_ext_restriction.real.json")
    # 樣本日期是固定的（2026-10）；不在「今天起 horizon 天內」的事件平移成從今天開始，長度不變，
    # 這樣不管哪天跑 demo 都看得到通知
    window_end = today + timedelta(days=args.horizon_days)
    shifted = 0
    for e in cons + rest:
        if e.start and e.end and (e.end < today or e.start > window_end):
            delta = today - e.start
            e.start += delta
            e.end += delta
            shifted += 1
    # 樣本裡有凱達格蘭大道集會、松高路臨時使用道路、市民大道五段施工，訂閱就放在那附近
    subs = [
        Subscription(id=1, name="停車：松高路", kind="point", points=[(25.0391, 121.5647)], radius_m=100,
                     roads=["南京東路二段"]),
        Subscription(id=2, name="通勤：中山南路→市民大道", kind="route",
                     points=[(25.0380, 121.5175), (25.0399, 121.5168), (25.0450, 121.5300), (25.0447, 121.5750)],
                     radius_m=60, roads=["凱達格蘭大道"]),
    ]
    print(f"demo: {len(cons)} construction + {len(rest)} restriction sample events ({shifted} moved to start today)")
    matches = match_all(subs, cons + rest, today=today, horizon_days=args.horizon_days)
    for sub_id, group in group_by_subscription(matches).items():
        ConsoleNotifier().send(group[0].subscription.name,
                               format_digest(group[0].subscription.name, group, days=args.horizon_days))
    if not matches:
        print("(no matches)")
    return 0


def cmd_web(args, store: Store) -> int:
    from .web import serve as serve_web
    store.close()               # 網站每個請求自己開連線
    serve_web(args.db, host=args.host, port=args.port, fetch_days=args.fetch_days,
              refresh_hours=args.refresh_hours, auto_refresh=not args.no_auto_refresh)
    return 0


def cmd_serve(args, store: Store) -> int:
    from .web import serve as serve_web
    store.close()
    serve_web(args.db, host=args.host, port=args.port, fetch_days=args.fetch_days,
              refresh_hours=args.refresh_hours, auto_refresh=not args.no_auto_refresh,
              line=True, notify_at=args.notify_at or None, public_url=args.public_url or "")
    return 0


def cmd_serve_line(args, store: Store) -> int:
    from .linebot import serve
    serve(store, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roadcheck", description="台北市通勤路線／停車位置施工封路預警")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路徑（預設 {DEFAULT_DB}，或環境變數 ROADCHECK_DB）")
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("subscribe", help="新增訂閱")
    s.add_argument("kind", choices=["point", "route"])
    s.add_argument("--name", required=True)
    s.add_argument("--lat", type=float)
    s.add_argument("--lon", type=float)
    s.add_argument("--address", help="門牌地址，轉成座標（需 GOOGLE_MAPS_API_KEY；或 TGOS 金鑰）")
    s.add_argument("--points", help='"lat,lon;lat,lon;..."')
    s.add_argument("--polyline", help="Google encoded polyline")
    s.add_argument("--radius", type=float, default=None, help="公尺（點預設 100，路線預設 50）")
    s.add_argument("--roads", nargs="*", help="路名，給沒座標的封路資料比對")
    s.add_argument("--line-user", help="LINE userId；有給就用 LINE 推播")
    s.add_argument("--only-blocking", action="store_true", help="只通知影響交通的事件")
    s.add_argument("--days", type=int, default=3, help=f"通知今天起未來幾天內的事件（{MIN_DAYS}–{MAX_DAYS}，預設 3）")
    s.set_defaults(func=cmd_subscribe)

    sp.add_parser("list", help="列出訂閱與事件數").set_defaults(func=cmd_list)

    dd = sp.add_parser("days", help="更改訂閱的通知天數")
    dd.add_argument("id", type=int)
    dd.add_argument("n", type=int, help=f"{MIN_DAYS}–{MAX_DAYS}")
    dd.set_defaults(func=cmd_days)

    g = sp.add_parser("geocode", help="地址轉座標（Google，或 ROADCHECK_GEOCODER=tgos）")
    g.add_argument("address")
    g.add_argument("--limit", type=int, default=5)
    g.set_defaults(func=cmd_geocode)

    f = sp.add_parser("fetch", help="抓資料存進資料庫")
    f.add_argument("--source", choices=list(ALL_SOURCES))
    f.add_argument("--from-file", help="讀本地檔而非網路")
    f.add_argument("--dump", action="store_true", help="印出原始回應前 4000 字（除錯用）")
    f.set_defaults(func=cmd_fetch)

    r = sp.add_parser("run", help="fetch + 比對 + 通知（排程用）")
    r.add_argument("--dry-run", action="store_true", help="只印出，不真的推播也不記錄")
    r.add_argument("--skip-fetch", action="store_true")
    r.add_argument("--horizon-days", type=int, default=None,
                   help="臨時覆寫所有訂閱的天數；不給就用每個訂閱自己的設定")
    r.set_defaults(func=cmd_run)

    d = sp.add_parser("demo", help="用樣本資料離線跑一遍")
    d.add_argument("--horizon-days", type=int, default=3)
    d.set_defaults(func=cmd_demo)

    w = sp.add_parser("web", help="啟動網頁：在地圖上放圖釘查詢")
    w.add_argument("--host", default="127.0.0.1", help="預設只給本機；手機要連就用 0.0.0.0")
    w.add_argument("--port", type=int, default=8080)
    w.add_argument("--fetch-days", type=int, default=7, help=f"外部管制路段往後抓幾天（{MIN_DAYS}–{MAX_DAYS}，預設 7）")
    w.add_argument("--refresh-hours", type=float, default=6.0, help="資料超過幾小時就在背景重抓（預設 6）")
    w.add_argument("--no-auto-refresh", action="store_true", help="不要自動抓資料（只用資料庫裡現有的）")
    w.set_defaults(func=cmd_web)

    sv = sp.add_parser("serve", help="正式上線：網頁 + LINE webhook（/line/webhook）+ 每天定時推播")
    sv.add_argument("--host", default="127.0.0.1", help="放在 Cloudflare Tunnel／ngrok 後面用預設即可")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--fetch-days", type=int, default=7, help=f"外部管制路段往後抓幾天（{MIN_DAYS}–{MAX_DAYS}）")
    sv.add_argument("--refresh-hours", type=float, default=6.0)
    sv.add_argument("--no-auto-refresh", action="store_true")
    sv.add_argument("--notify-at", default="07:00", help="台灣時間每天幾點推播（HH:MM，空字串表示不推播）")
    sv.add_argument("--public-url", default="", help="網站公開網址，推播會附地圖連結（或設 ROADCHECK_PUBLIC_URL）")
    sv.set_defaults(func=cmd_serve)

    l = sp.add_parser("serve-line", help="啟動 LINE webhook")
    l.add_argument("--host", default="0.0.0.0")
    l.add_argument("--port", type=int, default=8000)
    l.set_defaults(func=cmd_serve_line)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "subscribe" and not MIN_DAYS <= args.days <= MAX_DAYS:
        print(f"--days 要在 {MIN_DAYS}–{MAX_DAYS} 之間", file=sys.stderr)
        return 2
    if args.cmd == "subscribe" and args.radius is None:
        args.radius = 100.0 if args.kind == "point" else 50.0
    if args.cmd == "subscribe" and args.kind == "point" and not args.address and (args.lat is None or args.lon is None):
        print("point needs --lat and --lon, or --address", file=sys.stderr)
        return 2
    store = Store(args.db)
    try:
        return args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
