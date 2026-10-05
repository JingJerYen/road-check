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
