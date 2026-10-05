"""經緯度幾何運算（純標準庫）。

台北市範圍很小，用等距圓柱投影轉成公尺平面座標就夠精確（誤差 < 0.1%）。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]  # (lat, lon)


def haversine_m(a: LatLon, b: LatLon) -> float:
    """兩點之間的大圓距離（公尺）。"""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _to_xy(p: LatLon, ref_lat: float) -> tuple[float, float]:
    """投影到以 ref_lat 為基準的平面（公尺）。"""
    k = math.cos(math.radians(ref_lat))
    x = math.radians(p[1]) * EARTH_RADIUS_M * k
    y = math.radians(p[0]) * EARTH_RADIUS_M
    return x, y


def point_to_segment_m(p: LatLon, a: LatLon, b: LatLon) -> float:
    """點 p 到線段 ab 的最短距離（公尺）。"""
    ref = p[0]
    px, py = _to_xy(p, ref)
    ax, ay = _to_xy(a, ref)
    bx, by = _to_xy(b, ref)
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def point_to_polyline_m(p: LatLon, line: Sequence[LatLon]) -> float:
    """點到折線的最短距離（公尺）。單點折線視為一個點。"""
    if not line:
        raise ValueError("polyline is empty")
    if len(line) == 1:
        return haversine_m(p, line[0])
    return min(point_to_segment_m(p, line[i], line[i + 1]) for i in range(len(line) - 1))


def decode_polyline(encoded: str, precision: int = 5) -> list[LatLon]:
    """解碼 Google Encoded Polyline（Directions API、OSRM 都用這格式）。"""
    factor = 10 ** precision
    coords: list[LatLon] = []
    index = lat = lon = 0
    while index < len(encoded):
        for which in ("lat", "lon"):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == "lat":
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def parse_points(text: str) -> list[LatLon]:
    """解析 "lat,lon;lat,lon;..." 字串。"""
    pts: list[LatLon] = []
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        lat_s, lon_s = chunk.split(",")
        pts.append((float(lat_s), float(lon_s)))
    return pts


def polyline_length_m(line: Iterable[LatLon]) -> float:
    pts = list(line)
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# ---- TWD97 (EPSG:3826, TM2 121°E, GRS80) -> WGS84 ----
# 台北市政府的資料（今日施工、道管中心案件）座標都是 TWD97 二度分帶，單位公尺。
_A = 6_378_137.0
_F = 1 / 298.257222101
_K0 = 0.9999
_LON0 = math.radians(121.0)
_FALSE_E = 250_000.0
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_N = _F / (2 - _F)


def looks_like_twd97(x: float, y: float) -> bool:
    """台北市的 TWD97 座標大約 x 29–32 萬、y 276–279 萬；用寬一點的範圍判斷。"""
    return 100_000 < x < 400_000 and 2_400_000 < y < 2_900_000


def twd97_to_wgs84(x: float, y: float) -> LatLon:
    """TWD97 TM2 (x 東距, y 北距) -> (lat, lon)。誤差 < 1 公分（Snyder 1987 公式）。"""
    # 子午線弧長 -> 底點緯度（footpoint latitude）
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
        + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
        + (1097 * e1 ** 4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * cos1 ** 2
    t1 = tan1 ** 2
    n1 = _A / math.sqrt(1 - _E2 * sin1 ** 2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1 ** 2) ** 1.5
    d = (x - _FALSE_E) / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * _EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * _EP2 - 3 * c1 ** 2) * d ** 6 / 720
    )
    lon = _LON0 + (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * _EP2 + 24 * t1 ** 2) * d ** 5 / 120
    ) / cos1
    return math.degrees(lat), math.degrees(lon)


# ---- 面／線形狀 ----
# shape = 一串 (lat, lon)。首尾相同就當封閉多邊形（在裡面距離為 0），否則當折線。


def is_closed_ring(shape: Sequence[LatLon]) -> bool:
    return len(shape) >= 4 and shape[0] == shape[-1]


def point_in_ring(p: LatLon, ring: Sequence[LatLon]) -> bool:
    """射線法。台北市範圍小，直接拿經緯度當平面算就夠。"""
    inside = False
    n = len(ring)
    for i in range(n - 1):
        (y1, x1), (y2, x2) = ring[i], ring[i + 1]
        if (y1 > p[0]) != (y2 > p[0]):
            x_cross = x1 + (p[0] - y1) * (x2 - x1) / (y2 - y1)
            if p[1] < x_cross:
                inside = not inside
    return inside


def point_to_shape_m(p: LatLon, shape: Sequence[LatLon]) -> float:
    """點到形狀的距離：封閉多邊形內為 0，否則到邊界／折線的最短距離。"""
    if is_closed_ring(shape) and point_in_ring(p, shape):
        return 0.0
    return point_to_polyline_m(p, shape)


def _orient(a: LatLon, b: LatLon, c: LatLon) -> float:
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


def segments_intersect(a: LatLon, b: LatLon, c: LatLon, d: LatLon) -> bool:
    """線段 ab 與 cd 是否相交（含端點碰到）。小範圍直接用經緯度當平面算。"""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0:
        return True

    def on_segment(p: LatLon, q: LatLon, r: LatLon) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])

    return (o1 == 0 and on_segment(a, c, b)) or (o2 == 0 and on_segment(a, d, b)) or \
           (o3 == 0 and on_segment(c, a, d)) or (o4 == 0 and on_segment(c, b, d))


def polylines_cross(line: Sequence[LatLon], other: Sequence[LatLon]) -> bool:
    return any(
        segments_intersect(line[i], line[i + 1], other[j], other[j + 1])
        for i in range(len(line) - 1)
        for j in range(len(other) - 1)
    )


def polyline_to_shape_m(line: Sequence[LatLon], shape: Sequence[LatLon]) -> float:
    """折線（通勤路線）到形狀的距離。

    路線穿過形狀的邊、或路線頂點落在多邊形內 -> 0；
    否則取「形狀頂點到路線」與「路線頂點到形狀」兩個方向的最小值。
    """
    if len(line) == 1:
        return point_to_shape_m(line[0], shape)
    if polylines_cross(line, shape):
        return 0.0
    best = min(point_to_shape_m(v, shape) for v in line)
    if best == 0.0:
        return 0.0
    return min(best, min(point_to_polyline_m(v, line) for v in shape))


def centroid(points: Iterable[LatLon]) -> LatLon | None:
    pts = list(points)
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
