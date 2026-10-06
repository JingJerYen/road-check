"""訂閱的比對與推播流程，給命令列 `roadcheck run`、網站伺服器的每日排程、LINE 指令共用。"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import date
from typing import Callable, Iterable, Optional

from .matcher import match_all
from .models import Event, Match, Subscription
from .notify import format_digest, short_date
from .store import Store

log = logging.getLogger("roadcheck.service")

MAX_MESSAGES_PER_PUSH = 5      # LINE 一次 push 最多 5 則訊息，計費仍算 1 則


def web_link(public_url: Optional[str], sub: Subscription, days: Optional[int] = None) -> str:
    """訂閱位置在網頁上的連結（沒設 ROADCHECK_PUBLIC_URL 就回空字串）。"""
    if not public_url or sub.kind != "point":
        return ""
    lat, lon = sub.points[0]
    return f"{public_url.rstrip('/')}/#{lat:.6f},{lon:.6f},{days or sub.days},{sub.radius_m:.0f}"


def current_matches(store: Store, subs: Iterable[Subscription], today: Optional[date] = None,
                    horizon_days: Optional[int] = None, events: Optional[list] = None) -> list:
    """用每個來源最新一批資料比對。"""
    if events is None:
        events = store.list_current_events()
    return match_all(list(subs), events, today=today, horizon_days=horizon_days)


def brief_lines(matches: list, limit: int = 5) -> list:
    """一件一行的精簡清單，給 LINE 回覆用。"""
    ordered = sorted(matches, key=lambda m: (not m.event.blocks_traffic, m.reason != "distance",
                                             m.distance_m if m.distance_m is not None else 1e9,
                                             m.event.start or date.max))
    lines = []
    for m in ordered[:limit]:
        ev = m.event
        period = short_date(ev.start) if ev.start == ev.end else f"{short_date(ev.start)}–{short_date(ev.end)}"
        title = ev.title if len(ev.title) <= 38 else ev.title[:37] + "…"
        if m.reason == "road":
            where = "路名相同"
        elif m.distance_m is not None and m.distance_m < 1:
            where = "就在範圍內"
        else:
            where = f"約 {m.distance_m:.0f} 公尺"
        flags = "，影響交通" if ev.blocks_traffic else ""
        lines.append(f"• {period} {title}（{where}{flags}）")
    if len(ordered) > limit:
        lines.append(f"…還有 {len(ordered) - limit} 件")
    return lines


def mark_seen(store: Store, matches: Iterable[Match]) -> None:
    """已經在回覆裡給使用者看過的，就不要隔天再當「新異動」推一次。"""
    for m in matches:
        if m.subscription.id is not None:
            store.mark_notified(m.subscription.id, m.event)


def run_notifications(store: Store, get_notifier: Callable, today: Optional[date] = None,
                      horizon_days: Optional[int] = None, dry_run: bool = False,
                      public_url: Optional[str] = None, out: Callable[[str], None] = print) -> dict:
    """比對所有訂閱，把還沒通知過的新異動送出去。

    同一個人（同 channel＋target）的多個訂閱合併成一次推播（最多 5 則訊息），省 LINE 額度。
    送成功才記錄為已通知。回傳統計。
    """
    subs = store.list_subscriptions()
    if not subs:
        out("no subscriptions; nothing to do")
        return {"matches": 0, "fresh": 0, "pushes": 0, "failed": 0}
    matches = current_matches(store, subs, today=today, horizon_days=horizon_days)
    fresh = [m for m in matches if not store.already_notified(m.subscription.id, m.event)]
    out(f"{len(matches)} matches, {len(fresh)} not yet notified")

    by_sub: "OrderedDict[int, list]" = OrderedDict()
    for m in fresh:
        by_sub.setdefault(m.subscription.id, []).append(m)
    by_user: "OrderedDict[tuple, list]" = OrderedDict()
    for group in by_sub.values():
        sub = group[0].subscription
        by_user.setdefault((sub.channel, sub.channel_target), []).append(group)

    pushes = failed = 0
    for (channel, target), groups in by_user.items():
        texts = []
        for group in groups:
            sub = group[0].subscription
            days = horizon_days if horizon_days is not None else sub.days
            text = format_digest(sub.name, group, days=days)
            link = web_link(public_url, sub, days)
            if link:
                text += f"\n\n🗺 地圖：{link}"
            texts.append(text)
        if len(texts) > MAX_MESSAGES_PER_PUSH:          # 超過的併進最後一則
            keep = MAX_MESSAGES_PER_PUSH - 1
            texts = texts[:keep] + ["\n\n".join(texts[keep:])]
        try:
            notifier = get_notifier(channel, dry_run=dry_run)
            notifier.send_many(target, texts)
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.warning("[%s] send failed for %s: %s", channel, target or "(console)", e)
            out(f"[{channel}] send failed for {target or '(console)'}: {e}")
            continue
        pushes += 1
        if not dry_run:
            for group in groups:
                mark_seen(store, group)
    out(f"sent {pushes} push(es) for {len(by_sub)} subscription(s)"
        f"{' (dry-run, nothing recorded)' if dry_run else ''}")
    return {"matches": len(matches), "fresh": len(fresh), "pushes": pushes, "failed": failed}
