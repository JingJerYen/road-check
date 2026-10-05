"""roadcheck 命令列。

  roadcheck subscribe point --name 家 --lat 25.04 --lon 121.54 --radius 100 [--line-user Uxxx]
  roadcheck subscribe route --name 通勤 --points "25.04,121.54;25.05,121.55" [--polyline <encoded>]
  roadcheck list
  roadcheck fetch [--source NAME] [--from-file PATH] [--dump]
      來源：taipei_today_construction（data.taipei 今日施工，有座標）
            taipei_planned_work（dig.taipei 預定施工路段，路名）
            taipei_ext_restriction（dig.taipei 使用道路集會／臨時使用道路，路名）
  roadcheck run [--dry-run] [--horizon-days 2]      # fetch + match + notify，排程每天跑
  roadcheck demo                                     # 用樣本資料跑一遍，不需要網路
  roadcheck serve-line [--port 8000]                 # LINE webhook
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .geo import decode_polyline, parse_points
from .matcher import match_all
from .models import Subscription
from .notify import ConsoleNotifier, format_digest, get_notifier, group_by_subscription
from .sources import ALL_SOURCES, SourceError
from .store import DEFAULT_DB, Store

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def cmd_subscribe(args, store: Store) -> int:
    if args.kind == "point":
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
        roads=args.roads or [], channel=channel, channel_target=args.line_user or "",
        only_blocking=args.only_blocking,
    )
    store.add_subscription(sub)
    print(f"added subscription #{sub.id} {sub.name} ({sub.kind}, {len(pts)} pts, {sub.radius_m:.0f} m, {channel})")
    return 0


def cmd_list(args, store: Store) -> int:
    subs = store.list_subscriptions()
    if not subs:
        print("(no subscriptions)")
    for s in subs:
        roads = f" roads={','.join(s.roads)}" if s.roads else ""
        print(f"#{s.id:<3} {s.kind:<5} {s.radius_m:>5.0f}m {s.channel:<7} {s.name}{roads}")
    n = len(store.list_events())
    print(f"{n} events in store ({store.path})")
    return 0


def _fetch_into_store(store: Store, source_names: list[str], from_file: str | None, dump: bool) -> int:
    failures = 0
    for name in source_names:
        src = ALL_SOURCES[name]()
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
    return 1 if _fetch_into_store(store, names, args.from_file, args.dump) else 0


def _notify(store: Store, subs, events, today, horizon_days, dry_run) -> int:
    matches = match_all(subs, events, today=today, horizon_days=horizon_days)
    fresh = [m for m in matches if not store.already_notified(m.subscription.id, m.event)]
    print(f"{len(matches)} matches, {len(fresh)} not yet notified")
    sent = 0
    for sub_id, group in group_by_subscription(fresh).items():
        sub = group[0].subscription
        text = format_digest(sub.name, group)
        try:
            notifier = get_notifier(sub.channel, dry_run=dry_run)
            notifier.send(sub.channel_target, text)
        except Exception as e:  # noqa: BLE001
            print(f"[{sub.channel}] send failed for #{sub_id}: {e}", file=sys.stderr)
            continue
        if not dry_run:
            for m in group:
                store.mark_notified(sub_id, m.event)
        sent += 1
    print(f"sent {sent} digest(s){' (dry-run, nothing recorded)' if dry_run else ''}")
    return 0


def cmd_run(args, store: Store) -> int:
    if not args.skip_fetch:
        _fetch_into_store(store, list(ALL_SOURCES), None, False)
    subs = store.list_subscriptions()
    if not subs:
        print("no subscriptions; nothing to do")
        return 0
    return _notify(store, subs, store.list_events(), date.today(), args.horizon_days, args.dry_run)


def cmd_demo(args, store: Store) -> int:
    """用 tests/fixtures 的樣本跑一遍：建兩筆訂閱、載入樣本、印出通知。"""
    from .sources import TaipeiExtRestriction, TaipeiPlannedWork, TaipeiTodayConstruction

    today = date.today()
    cons = TaipeiTodayConstruction().from_file(FIXTURE_DIR / "taipei_today_construction.sample.json")
    rest = TaipeiExtRestriction().from_file(FIXTURE_DIR / "taipei_ext_restriction.sample.html")
    plan = TaipeiPlannedWork().from_file(FIXTURE_DIR / "taipei_planned_work.real.html")
    # 樣本日期是固定的，demo 時把每個來源各自平移到今天附近
    for group in (cons, rest, plan):
        base = min((e.start for e in group if e.start), default=None)
        if base is None:
            continue
        shift = today - base
        for e in group:
            if e.start:
                e.start += shift
            if e.end:
                e.end += shift
    subs = [
        Subscription(id=1, name="停車：忠孝復興", kind="point", points=[(25.0418, 121.5440)], radius_m=150,
                     roads=["忠孝東路四段"]),
        Subscription(id=2, name="通勤：信義→民生", kind="route",
                     points=[(25.0330, 121.5654), (25.0415, 121.5495), (25.0520, 121.5440), (25.0580, 121.5440)],
                     radius_m=60, roads=["民生東路三段", "忠孝西路"]),
    ]
    print(f"demo: {len(cons)} construction + {len(plan)} planned-work + {len(rest)} restriction sample events,"
          " dates shifted to today")
    matches = match_all(subs, cons + rest + plan, today=today, horizon_days=args.horizon_days)
    for sub_id, group in group_by_subscription(matches).items():
        ConsoleNotifier().send(group[0].subscription.name, format_digest(group[0].subscription.name, group))
    if not matches:
        print("(no matches)")
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
    s.add_argument("--points", help='"lat,lon;lat,lon;..."')
    s.add_argument("--polyline", help="Google encoded polyline")
    s.add_argument("--radius", type=float, default=None, help="公尺（點預設 100，路線預設 50）")
    s.add_argument("--roads", nargs="*", help="路名，給沒座標的封路資料比對")
    s.add_argument("--line-user", help="LINE userId；有給就用 LINE 推播")
    s.add_argument("--only-blocking", action="store_true", help="只通知影響交通的事件")
    s.set_defaults(func=cmd_subscribe)

    sp.add_parser("list", help="列出訂閱與事件數").set_defaults(func=cmd_list)

    f = sp.add_parser("fetch", help="抓資料存進資料庫")
    f.add_argument("--source", choices=list(ALL_SOURCES))
    f.add_argument("--from-file", help="讀本地檔而非網路")
    f.add_argument("--dump", action="store_true", help="印出原始回應前 4000 字（除錯用）")
    f.set_defaults(func=cmd_fetch)

    r = sp.add_parser("run", help="fetch + 比對 + 通知（排程用）")
    r.add_argument("--dry-run", action="store_true", help="只印出，不真的推播也不記錄")
    r.add_argument("--skip-fetch", action="store_true")
    r.add_argument("--horizon-days", type=int, default=2, help="往後看幾天（預設 2）")
    r.set_defaults(func=cmd_run)

    d = sp.add_parser("demo", help="用樣本資料離線跑一遍")
    d.add_argument("--horizon-days", type=int, default=3)
    d.set_defaults(func=cmd_demo)

    l = sp.add_parser("serve-line", help="啟動 LINE webhook")
    l.add_argument("--host", default="0.0.0.0")
    l.add_argument("--port", type=int, default=8000)
    l.set_defaults(func=cmd_serve_line)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "subscribe" and args.radius is None:
        args.radius = 100.0 if args.kind == "point" else 50.0
    if args.cmd == "subscribe" and args.kind == "point" and (args.lat is None or args.lon is None):
        print("point needs --lat and --lon", file=sys.stderr)
        return 2
    store = Store(args.db)
    try:
        return args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
