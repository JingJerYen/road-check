"""把事件對到訂閱。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from .geo import haversine_m, point_to_polyline_m, point_to_shape_m, polyline_to_shape_m
from .models import Event, Match, Subscription


def is_relevant_period(ev: Event, today: date, horizon_days: int) -> bool:
    """今天到 today+horizon 之間有重疊才算。沒有日期的事件一律保留。"""
    window_end = today + timedelta(days=horizon_days)
    if ev.end is not None and ev.end < today:
        return False
    if ev.start is not None and ev.start > window_end:
        return False
    return True


def distance_m(sub: Subscription, ev: Event) -> Optional[float]:
    """訂閱到事件的最短距離；事件沒有任何座標時回 None。

    有 shapes（施工多邊形、管制範圍）就對形狀算，在多邊形裡面是 0；否則對代表點算。
    """
    if ev.shapes:
        if sub.kind == "point":
            return min(point_to_shape_m(sub.points[0], s) for s in ev.shapes)
        return min(polyline_to_shape_m(sub.points, s) for s in ev.shapes)
    loc = ev.location
    if loc is None:
        return None
    if sub.kind == "point":
        return haversine_m(sub.points[0], loc)
    return point_to_polyline_m(loc, sub.points)


def match_one(sub: Subscription, ev: Event) -> Optional[Match]:
    if sub.only_blocking and ev.blocks_traffic is False:
        return None
    d = distance_m(sub, ev)
    if d is not None:
        if d <= sub.radius_m:
            return Match(sub, ev, reason="distance", distance_m=d)
        return None
    # 沒座標：路名比對
    if sub.roads:
        hits = sorted(set(sub.roads) & ev.roads)
        if hits:
            return Match(sub, ev, reason="road", matched_roads=hits)
    return None


def match_all(
    subs: Iterable[Subscription],
    events: Iterable[Event],
    today: Optional[date] = None,
    horizon_days: int = 2,
) -> list[Match]:
    today = today or date.today()
    evs = [e for e in events if is_relevant_period(e, today, horizon_days)]
    out: list[Match] = []
    for sub in subs:
        for ev in evs:
            m = match_one(sub, ev)
            if m:
                out.append(m)
    out.sort(key=lambda m: (m.subscription.id or 0, m.event.start or date.max, m.distance_m or 0))
    return out
