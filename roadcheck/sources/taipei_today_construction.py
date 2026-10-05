"""臺北市今日施工資訊（工務局，data.taipei）。

資料集頁面：https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a
欄位（官方說明）：
  Ac_no 核准文號 | sno 序號 | AppMode 通報類別 | X 經度 | Y 緯度 | AppTime 通報時間
  App_Name 施工單位 | C_Name 行政區 | Addr 施工地點 | Cb_Da 核准施工起日 | Ce_Da 核准施工迄日
  Co_Ti 施工時段 | NPurp 挖掘目的 | DType 延期原因 | DLen 挖掘長度
  IsStay 是否長期工區 | IsBlock 是否影響交通 | PlanB 替代方案 | WItem 施工項目 | Positions 施工位置

資料來源（2026-10 查證）：data.taipei 頁面上這個資料集是「系統介接」，「下載」按鈕直接指向
  https://tpnco.blob.core.windows.net/blobfs/Todaywork.json
而不是 data.taipei 的 datastore；頁面上的 rid（afb20478-f915-4ffa-a8e8-f78738b2e732）用
``/api/v1/dataset/{rid}?scope=resourceAquire`` 查只會得到空陣列，舊的預設 rid
875ea014-… 更是另一個資料集（資料集目錄）。所以預設直接抓 blob JSON；若日後搬回 datastore，
把 URL 改成 resourceAquire 形式即可，``fetch_raw()`` 會自動翻頁。可用環境變數
ROADCHECK_TAIPEI_TODAY_URL 整個覆寫 URL。

注意：撰寫時的開發環境網路政策擋住 tpnco.blob.core.windows.net，blob JSON 的實際欄位尚未驗證；
``_records()`` 同時接受 list 與 {"result":{"results":[…]}} 兩種形狀，欄位名不分大小寫。
"""
from __future__ import annotations

import os
from typing import Any

from ..dates import parse_bool, parse_date
from ..models import Event
from .base import Source, http_get_json

# data.taipei 頁面「下載」連結指向的檔案（系統介接，不在 datastore）
DEFAULT_URL = "https://tpnco.blob.core.windows.net/blobfs/Todaywork.json"
# 頁面上的資源 id，只用來組 datastore 形式的 URL；目前 datastore 沒有資料
DEFAULT_RID = "afb20478-f915-4ffa-a8e8-f78738b2e732"
DATASTORE_URL = (
    "https://data.taipei/api/v1/dataset/" + DEFAULT_RID + "?scope=resourceAquire&limit=1000&offset=0"
)
DATASET_PAGE = "https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a"


def _records(raw: Any) -> list[dict]:
    """data.taipei 有兩種回傳形狀：
    - /api/v1/dataset/{rid}?scope=resourceAquire -> {"result": {"results": [...], "count": N}}
    - resource.download -> 直接是 list
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "result" in raw and isinstance(raw["result"], dict):
            return list(raw["result"].get("results", []))
        for key in ("results", "data", "records"):
            if isinstance(raw.get(key), list):
                return raw[key]
    raise ValueError("unrecognised data.taipei payload shape")


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
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f


class TaipeiTodayConstruction(Source):
    name = "taipei_today_construction"
    kind = "construction"

    def __init__(self, url: str | None = None, page_size: int = 1000):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_TODAY_URL", DEFAULT_URL)
        self.page_size = page_size

    def fetch_raw(self):
        # resourceAquire 介面一次最多 1000 筆，有 offset 就翻頁
        if "scope=resourceAquire" not in self.url:
            return http_get_json(self.url)
        all_rows: list[dict] = []
        offset = 0
        while True:
            url = self.url.replace("offset=0", f"offset={offset}")
            payload = http_get_json(url)
            rows = _records(payload)
            all_rows.extend(rows)
            count = payload.get("result", {}).get("count") if isinstance(payload, dict) else None
            if not rows or count is None or len(all_rows) >= int(count):
                break
            offset += self.page_size
        return all_rows

    def parse(self, raw) -> list[Event]:
        events: list[Event] = []
        for rec in _records(raw):
            ac_no = str(_get(rec, "Ac_no", "AC_NO", "ac_no"))
            sno = str(_get(rec, "sno", default="0"))
            if not ac_no:
                continue
            lon = _float(_get(rec, "X", default=None))
            lat = _float(_get(rec, "Y", default=None))
            # 偶爾 X/Y 會對調，用台北的範圍判斷修正
            if lat is not None and lon is not None and lat > 100 and lon < 90:
                lat, lon = lon, lat
            if lat is not None and not (21.0 < lat < 26.5):
                lat = lon = None
            addr = str(_get(rec, "Addr", "Positions"))
            positions = str(_get(rec, "Positions"))
            district = str(_get(rec, "C_Name"))
            purpose = str(_get(rec, "NPurp"))
            agency = str(_get(rec, "App_Name"))
            title = f"{district}{addr}" if district and not addr.startswith(district) else addr
            if purpose:
                title = f"{title}（{purpose}）"
            events.append(
                Event(
                    source=self.name,
                    source_id=f"{ac_no}-{sno}" if sno not in ("", "0") else ac_no,
                    kind="construction",
                    title=title or ac_no,
                    lat=lat,
                    lon=lon,
                    address=f"{addr} {positions}".strip(),
                    start=parse_date(_get(rec, "Cb_Da", default=None)),
                    end=parse_date(_get(rec, "Ce_Da", default=None)),
                    time_window=str(_get(rec, "Co_Ti")),
                    blocks_traffic=parse_bool(_get(rec, "IsBlock", default=None)),
                    agency=agency,
                    purpose=purpose,
                    url=DATASET_PAGE,
                    extra={
                        "district": district,
                        "length_m": _get(rec, "DLen"),
                        "long_term": parse_bool(_get(rec, "IsStay", default=None)),
                        "delay_reason": _get(rec, "DType"),
                        "work_items": _get(rec, "WItem"),
                        "plan_b": _get(rec, "PlanB"),
                        "contact": _get(rec, "Tc_Ma"),
                        "contact_tel": _get(rec, "Tc_Tl"),
                    },
                )
            )
        return events
