"""把事件對到訂閱。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from .geo import haversine_m, point_to_polyline_m
from .models import Event, Match, Subscription


def is_relevant_period(ev: Event, today: date, horizon_days: int) -> bool:
    """今天到 today+horizon 之間有重疊才算。沒有日期的事件一律保留。"""
    window_end = today + timedelta(days=horizon_days)
    if ev.end is not None and ev.end < today:
        return False
    if ev.start is not None and ev.start > window_end:
        return False
    return True


def match_one(sub: Subscription, ev: Event) -> Optional[Match]:
    if sub.only_blocking and ev.blocks_traffic is False:
        return None
    loc = ev.location
    if loc is not None:
        if sub.kind == "point":
            d = haversine_m(sub.points[0], loc)
        else:
            d = point_to_polyline_m(loc, sub.points)
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
