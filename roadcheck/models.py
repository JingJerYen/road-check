"""資料模型。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal, Optional

from .geo import LatLon

EventKind = Literal["construction", "restriction", "parking_ban"]
SubscriptionKind = Literal["point", "route"]

KIND_LABEL = {
    "construction": "道路施工",
    "restriction": "交通管制／封路",
    "parking_ban": "停車格禁停",
}

# 「忠孝東路四段」「復興南路」「市民大道」「八德路2段」
# 排除連接詞與行政區字樣，避免「口至敦化南路」「大安區忠孝東路」被整段吃進去
_ROAD_RE = re.compile(
    r"[^\s\d口至到與及和、，,．.。；;：:（）()【】\[\]「」<>＜＞~～\-區段巷弄號]{1,6}?(?:大道|路|街)(?:[一二三四五六七八九十\d]{1,2}段)?"
)
# 會被誤認成路名的常見詞（資料來源的分類名稱、泛稱）
_NOT_ROADS = {"使用道路", "臨時使用道路", "山區道路", "區域道路", "道路", "馬路", "相鄰道路", "鄰近道路"}
_CJK_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
            "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def normalize_road(name: str) -> str:
    """統一路名寫法：去空白、中文數字段轉阿拉伯數字（忠孝東路四段 -> 忠孝東路4段）。"""
    s = re.sub(r"\s+", "", name)
    s = s.replace("臺", "台")

    def repl(m: re.Match) -> str:
        n = m.group(1)
        return _CJK_NUM.get(n, n) + "段"

    return re.sub(r"([一二三四五六七八九十\d]{1,2})段", repl, s)


def extract_roads(text: str) -> set[str]:
    """從施工地點描述抓出路名（含段）。"""
    if not text:
        return set()
    found = {normalize_road(m.group(0)) for m in _ROAD_RE.finditer(text)}
    # 過濾掉太短或不像路名的碎片
    return {r for r in found if len(r) >= 3 and r not in _NOT_ROADS and not r.endswith("道路")}


@dataclass
class Event:
    source: str                       # 資料來源代號，例如 taipei_today_construction
    source_id: str                    # 來源內唯一鍵（核准文號等）
    kind: EventKind
    title: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: str = ""                 # 原始地點文字
    start: Optional[date] = None
    end: Optional[date] = None
    time_window: str = ""             # 例如 "22:00-06:00"
    blocks_traffic: Optional[bool] = None
    agency: str = ""                  # 施工／申請單位
    purpose: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)
    # 施工／管制範圍：每個 shape 是一串 (lat, lon)，首尾相同為多邊形，否則為折線。
    # lat/lon 是代表點（通常是 shapes 的重心），沒有 shapes 時就只用 lat/lon。
    shapes: list[list[LatLon]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def location(self) -> Optional[LatLon]:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)

    @property
    def roads(self) -> set[str]:
        """地點文字裡的路名；address 沒東西才退回用 title（title 可能含來源分類字樣）。"""
        return extract_roads(self.address or self.title)

    def fingerprint(self) -> str:
        """內容指紋。日期或範圍變了就會改變，用來判斷要不要重新通知。"""
        payload = {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "time_window": self.time_window,
            "blocks_traffic": self.blocks_traffic,
            "address": self.address,
            "lat": round(self.lat, 6) if self.lat is not None else None,
            "lon": round(self.lon, 6) if self.lon is not None else None,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def to_row(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat() if self.start else None
        d["end"] = self.end.isoformat() if self.end else None
        d["extra"] = json.dumps(self.extra, ensure_ascii=False)
        d["shapes"] = json.dumps([[list(p) for p in shape] for shape in self.shapes]) if self.shapes else None
        d["fingerprint"] = self.fingerprint()
        return d

    @classmethod
    def from_row(cls, row: dict) -> "Event":
        return cls(
            source=row["source"],
            source_id=row["source_id"],
            kind=row["kind"],
            title=row["title"],
            lat=row["lat"],
            lon=row["lon"],
            address=row["address"] or "",
            start=date.fromisoformat(row["start"]) if row["start"] else None,
            end=date.fromisoformat(row["end"]) if row["end"] else None,
            time_window=row["time_window"] or "",
            blocks_traffic=None if row["blocks_traffic"] is None else bool(row["blocks_traffic"]),
            agency=row["agency"] or "",
            purpose=row["purpose"] or "",
            url=row["url"] or "",
            extra=json.loads(row["extra"] or "{}"),
            shapes=[[tuple(p) for p in shape] for shape in json.loads(row.get("shapes") or "[]")],
        )


@dataclass
class Subscription:
    name: str
    kind: SubscriptionKind
    points: list[LatLon]              # point: 一個點；route: 折線
    radius_m: float = 100.0           # 點：半徑；路線：線兩側緩衝
    roads: list[str] = field(default_factory=list)   # 沒座標的事件用路名比對
    channel: str = "console"          # console | line
    channel_target: str = ""          # LINE userId 等
    only_blocking: bool = False       # 只通知「影響交通」的事件
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.roads = sorted({normalize_road(r) for r in self.roads if r.strip()})
        if self.kind == "point" and len(self.points) != 1:
            raise ValueError("point subscription needs exactly one point")
        if self.kind == "route" and len(self.points) < 2:
            raise ValueError("route subscription needs at least two points")


@dataclass
class Match:
    subscription: Subscription
    event: Event
    reason: str                       # distance | road
    distance_m: Optional[float] = None
    matched_roads: list[str] = field(default_factory=list)
