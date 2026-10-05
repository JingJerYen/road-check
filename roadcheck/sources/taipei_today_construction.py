"""臺北市今日施工資訊（工務局道路挖掘管理中心，經 data.taipei 發布）。

資料集頁面：https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a
實際檔案：  https://tpnco.blob.core.windows.net/blobfs/Todaywork.json（每 10 分鐘更新）

已接真資料驗證（2026-10）。檔案是 GeoJSON FeatureCollection，每個 feature 的 properties：
  Ac_no 核備文號 | sno 序號（同一文號可多筆）| AppMode 通報類別代碼 | X/Y TWD97 座標（公尺）
  AppTime 通報時間 | App_Name 施工單位 | C_Name 行政區（不含「區」字）| Addr 施工位置
  Cb_Da/Ce_Da 核准施工起迄日（民國 yyy/mm/dd）| Co_Ti 施工時段（文字）
  Tc_Na 廠商 | Tc_Ma/Tc_Tl 監工 | Tc_Ma3/Tc_Tl3 現場人員 | NPurp 挖掘目的 | DType 逾時原因
  DLen 挖掘長度 | IsStay 是否長期工區（是／否）| IsBlock 是否影響交通（是／否）| PlanB 替代方案
  WItem 施工項目 | Positions_type MultiPolygon／MultiLineString | Positions 施工範圍座標（TWD97）
內容只有「今天在施工」的案件，沒有未來排程；Cb_Da 不會晚於今天。

同一個核備文號（Ac_no）會有很多筆 sno（一段路一筆，多的有 89 筆），parse() 會合併成一個 Event：
範圍聯集、日期取最寬、任一筆影響交通就算影響交通。市府放在裡面的測試資料（「全市APP測試」，
多邊形蓋住整個台北市）會被丟掉。

parse() 也接受 data.taipei resourceAquire 的 {"result":{"results":[...]}} 與單純 list（舊格式，
X/Y 為經緯度、Positions 為文字），方便離線測試與日後換來源。URL 可用 ROADCHECK_TAIPEI_TODAY_URL 覆寫。
"""
from __future__ import annotations

import os
from typing import Any

from ..dates import parse_bool, parse_date
from ..geo import LatLon, centroid, haversine_m, looks_like_twd97, twd97_to_wgs84
from ..models import Event
from .base import Source, http_get_json

DEFAULT_URL = "https://tpnco.blob.core.windows.net/blobfs/Todaywork.json"
DATASET_PAGE = "https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a"

# 資料集頁面「備註」欄的對照表
# 一個施工範圍的對角線超過這個距離就當成資料錯誤（測試資料的多邊形有 140 公里寬）
MAX_SHAPE_SPAN_M = 20_000

APP_MODE = {
    "0": "施工通報",
    "3": "銑鋪通報",
    "4": "搶修通報",
    "5": "道路維護通報",
    "6": "人手孔施工通報",
    "B": "建案公設復舊",
}


def _records(raw: Any) -> list[dict]:
    """接受三種形狀：
    - GeoJSON {"type":"FeatureCollection","features":[{"properties":{...}}]}（目前的真資料）
    - data.taipei resourceAquire {"result":{"results":[...]}}
    - 直接是 list
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("features"), list):
            return [f.get("properties") or {} for f in raw["features"]]
        if "result" in raw and isinstance(raw["result"], dict):
            return list(raw["result"].get("results", []))
        for key in ("results", "data", "records"):
            if isinstance(raw.get(key), list):
                return raw[key]
    raise ValueError("unrecognised Todaywork payload shape")


def _get(rec: dict, *names: str, default=""):
    """欄位名大小寫在不同版本不一致，一律不分大小寫找。"""
    lower = {k.lower(): v for k, v in rec.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return default


def _float(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _to_latlon(x: float, y: float) -> LatLon | None:
    """X/Y 可能是 TWD97 公尺或經緯度（舊格式），也可能 X/Y 對調。"""
    if looks_like_twd97(x, y):
        return twd97_to_wgs84(x, y)
    lat, lon = y, x
    if lat > 90 and lon < 90:   # 對調
        lat, lon = lon, lat
    if 21.0 < lat < 26.5 and 119.0 < lon < 123.0:
        return lat, lon
    return None


def _shapes(positions: Any) -> list[list[LatLon]]:
    """Positions（MultiPolygon 或 MultiLineString 的巢狀座標）-> 多個 shape。

    遞迴找到最底層的「點的序列」（每個點是 [x, y]），每一串轉成一個 shape。
    """
    out: list[list[LatLon]] = []

    def walk(node):
        if not isinstance(node, list) or not node:
            return
        if all(isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[0], (int, float)) for p in node):
            pts = [_to_latlon(float(p[0]), float(p[1])) for p in node]
            pts = [p for p in pts if p is not None]
            if len(pts) >= 2:
                out.append(pts)
            return
        for child in node:
            walk(child)

    walk(positions)
    return [s for s in out if _span_m(s) <= MAX_SHAPE_SPAN_M]


def _span_m(shape: list[LatLon]) -> float:
    lats = [p[0] for p in shape]
    lons = [p[1] for p in shape]
    return haversine_m((min(lats), min(lons)), (max(lats), max(lons)))


def _is_test_record(rec: dict) -> bool:
    return any("測試" in str(_get(rec, k)) for k in ("Addr", "Tc_Na", "App_Name"))


class TaipeiTodayConstruction(Source):
    name = "taipei_today_construction"
    kind = "construction"

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_TODAY_URL", DEFAULT_URL)

    def fetch_raw(self):
        return http_get_json(self.url)

    def parse(self, raw) -> list[Event]:
        groups: dict[str, list[dict]] = {}
        for rec in _records(raw):
            ac_no = str(_get(rec, "Ac_no", "AC_NO"))
            if not ac_no or _is_test_record(rec):
                continue
            groups.setdefault(ac_no, []).append(rec)
        return [self._build(ac_no, recs) for ac_no, recs in groups.items()]

    def _build(self, ac_no: str, recs: list[dict]) -> Event:
        first = recs[0]
        shapes: list[list[LatLon]] = []
        locs: list[LatLon] = []
        positions_text: list[str] = []
        starts, ends, blocks, addrs, windows = [], [], [], [], []
        for rec in recs:
            x, y = _float(_get(rec, "X", default=None)), _float(_get(rec, "Y", default=None))
            loc = _to_latlon(x, y) if x is not None and y is not None else None
            positions = _get(rec, "Positions")
            if isinstance(positions, list):
                shapes.extend(_shapes(positions))
            elif isinstance(positions, str) and positions:
                positions_text.append(positions)
            if loc:
                locs.append(loc)
            s, e = parse_date(_get(rec, "Cb_Da", default=None)), parse_date(_get(rec, "Ce_Da", default=None))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
            blocks.append(parse_bool(_get(rec, "IsBlock", default=None)))
            addrs.append(str(_get(rec, "Addr")))
            windows.append(str(_get(rec, "Co_Ti")))
        loc = centroid(p for s in shapes for p in s) or centroid(locs)
        addr = "；".join(_dedupe(addrs))
        district = str(_get(first, "C_Name"))
        if district and not district.endswith("區"):
            district += "區"
        purpose = str(_get(first, "NPurp"))
        app_mode = str(_get(first, "AppMode"))
        title = addr if not district or addr.startswith(district) else f"{district}{addr}"
        if purpose:
            title = f"{title}（{purpose}）"
        if len(title) > 90:
            title = title[:88] + "…"
        blocks_traffic = True if any(b is True for b in blocks) else (False if any(b is False for b in blocks) else None)
        return Event(
            source=self.name,
            source_id=ac_no,
            kind="construction",
            title=title or ac_no,
            lat=loc[0] if loc else None,
            lon=loc[1] if loc else None,
            address=" ".join([addr] + _dedupe(positions_text)).strip(),
            start=min(starts) if starts else None,
            end=max(ends) if ends else None,
            time_window=next((w for w in windows if w), ""),
            blocks_traffic=blocks_traffic,
            agency=str(_get(first, "App_Name")),
            purpose=purpose,
            url=DATASET_PAGE,
            shapes=shapes,
            extra={
                "district": district,
                "app_mode": APP_MODE.get(app_mode, app_mode),
                "segments": len(recs),
                "length_m": _get(first, "DLen"),
                "long_term": parse_bool(_get(first, "IsStay", default=None)),
                "delay_reason": _get(first, "DType"),
                "work_items": _get(first, "WItem"),
                "plan_b": _get(first, "PlanB"),
                "contractor": _get(first, "Tc_Na"),
                "contact": _get(first, "Tc_Ma"),
                "contact_tel": _get(first, "Tc_Tl"),
                "reported_at": _get(first, "AppTime"),
            },
        )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        it = (it or "").strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
