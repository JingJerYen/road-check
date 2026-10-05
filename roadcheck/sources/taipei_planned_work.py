"""臺北市道路挖掘管理中心「預定施工路段」（新工處道路／人行道更新工程的未來排程）。

來源頁面：https://dig.taipei/Tpdig/PWorkData.aspx
和外部管制路段一樣是 ASP.NET WebForms 的 GridView，但：
- 沒有查詢條件，一次列出所有未來／進行中的工程（2026-10 時約 320 筆、32 頁）。
- 「匯出Excel」(ButExcel) 可以一次拿到整張表（內容其實是 HTML <table>），
  所以 fetch 先試匯出，失敗或解析不到資料才退回逐頁翻。
- 表頭：預定施工日期｜施工類型｜施工單位｜施工路段；日期格式 115/10/19-115/11/23（民國）。
- 路段儲存格的 <a> 有 ``ShowPWorkData.aspx?caseid=NNN``，caseid 是穩定 ID。
- 沒有座標，靠路名比對；「施工路段」通常是工程名稱，路名抽取大多抽得到。

這份是「預告」性質（相對於 data.taipei「今日施工」是當天核備案件）。
環境變數：ROADCHECK_TAIPEI_PWORK_URL 覆寫 URL。
"""
from __future__ import annotations

import logging
import os

from ..models import Event
from .base import Source
from .dig_taipei import Grid, WebFormsSession, find_grid, parse_grids, short_hash, split_period

log = logging.getLogger(__name__)

DEFAULT_URL = "https://dig.taipei/Tpdig/PWorkData.aspx"
EXPORT_BUTTON = {"ButExcel": "匯出Excel"}
GRID_ID = "GridView1"
ERROR_MARK = "系統執行發生錯誤"


class TaipeiPlannedWork(Source):
    name = "taipei_planned_work"
    kind = "construction"

    def __init__(self, url: str | None = None, max_pages: int = 200):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_PWORK_URL", DEFAULT_URL)
        self.max_pages = max_pages

    def fetch_raw(self) -> list[str]:
        """回傳一到多頁 HTML：匯出成功就是一頁完整表格，否則是 GridView 逐頁。"""
        sess = WebFormsSession()
        landing = sess.get(self.url)
        try:
            exported = sess.postback(self.url, landing, EXPORT_BUTTON)
            if ERROR_MARK not in exported and self.parse(exported):
                return [exported]
            log.warning("%s: 匯出Excel 沒有資料或回錯誤，改用逐頁翻", self.name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: 匯出Excel 失敗（%r），改用逐頁翻", self.name, e)
        return list(sess.iter_pages(self.url, landing, GRID_ID, max_pages=self.max_pages))

    def load_bytes(self, data: bytes):
        return data.decode("utf-8", errors="replace")

    def parse(self, raw) -> list[Event]:
        pages = [raw] if isinstance(raw, str) else list(raw)
        events: list[Event] = []
        seen: set[str] = set()
        for html in pages:
            found = find_grid(parse_grids(html))
            if not found:
                continue
            grid, hi, mapping = found
            events.extend(self._rows_to_events(grid, hi, mapping, seen))
        return events

    def _rows_to_events(self, grid: Grid, header_idx: int, mapping: dict[int, str], seen: set[str]) -> list[Event]:
        header = grid.rows[header_idx]
        events: list[Event] = []
        for ri in range(header_idx + 1, len(grid.rows)):
            row = grid.rows[ri]
            if len(row) < 2:
                continue
            rec = {mapping[i]: row[i] for i in mapping if i < len(row)}
            road = rec.get("road", "")
            if not road:
                continue
            start, end, window = split_period(rec.get("period", ""))
            work_type = rec.get("reason", "")
            caseid = next((ref for (r, _c), ref in grid.refs.items() if r == ri), "")
            sid = caseid or f"{start.isoformat() if start else 'na'}-{short_hash(road, work_type)}"
            if sid in seen:
                continue
            seen.add(sid)
            events.append(
                Event(
                    source=self.name,
                    source_id=sid,
                    kind="construction",
                    title=f"{road}（{work_type}）" if work_type else road,
                    address=road,
                    start=start,
                    end=end,
                    time_window=window,
                    blocks_traffic=None,
                    agency=rec.get("agency", ""),
                    purpose=work_type,
                    url=f"https://dig.taipei/TpdigR.net/Map/ShowPWorkData.aspx?caseid={caseid}" if caseid else self.url,
                    extra={"caseid": caseid, "raw_row": dict(zip(header, row))},
                )
            )
        return events
