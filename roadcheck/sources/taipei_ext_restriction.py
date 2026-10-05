"""臺北市道路挖掘管理中心「外部管制路段」（活動管制／使用道路集會／臨時使用道路）。

來源頁面：https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx
這頁是 ASP.NET WebForms 的 HTML 表格而非 API，路段是文字描述沒有座標。

實際頁面行為（2026-10 以真資料驗證）：
- GET 就會列出「活動管制」(RadQMode0) 當天的資料；查詢條件是民國 yyyMMdd 的起迄日
  （TxtQDate0 / TxtQDate1），以 ButQuery POST 送出，篩選採「期間有重疊」。
- 另外兩種來源「使用道路集會」(RadQMode1)、「臨時使用道路」(RadQMode2) 要先 POST
  ``__EVENTTARGET=RadQModeN`` 切換，表格會變成 GridView2（只有日期＋路段兩欄）。
- 每頁 10 筆，翻頁是 ``__EVENTTARGET=GridView1``（或 GridView2）+ ``__EVENTARGUMENT=Page$N``，
  翻頁時要一併送查詢欄位，篩選才會保留。
- 「匯出Excel」在這頁會回「執行緒已經中止」錯誤，所以只能翻頁。
- 表頭：活動管制 = 挖掘管制日期｜管制原因｜施工單位｜管制路段；
        集會／臨時 = 使用道路日期｜使用道路路段（日期含時分秒）。
- 路段儲存格內的 <a> 有 ``ShowExtRest.aspx?key=NNN``，key 是案件 ID；集會／臨時同一個 key
  常重複出現多列（一列一個路段或一次申請多筆），所以 source_id = key[-hash]。

重要：「活動管制」(RadQMode0) 分頁的內容其實是**挖掘管制**（議員要求、里長要求、文資管制區域、
配合活動期間禁止其他單位挖掘），期間動輒數年，對通勤或停車沒有影響，所以預設不抓；
只抓「使用道路集會」與「臨時使用道路」這兩種真正占用道路的事件。

環境變數：
  ROADCHECK_TAIPEI_EXT_URL    覆寫 URL
  ROADCHECK_TAIPEI_EXT_DAYS   查詢窗口：今天起往後幾天（預設 3，應 >= run 的 --horizon-days）
  ROADCHECK_TAIPEI_EXT_MODES  要抓的分頁，逗號分隔的 0/1/2（預設 "1,2"）
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Iterable

from ..dates import parse_date
from ..models import Event
from .base import Source
from .dig_taipei import (
    HEADER_MAP,  # noqa: F401  (re-export, 舊程式有 import)
    Grid,
    WebFormsSession,
    find_grid,
    map_headers,  # noqa: F401
    parse_grids,
    parse_tables,  # noqa: F401
    roc_date,
    short_hash,
    split_period,
)

DEFAULT_URL = "https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx"
DEFAULT_DAYS = 3
DEFAULT_MODES = "1,2"

# RadQMode 值 -> (顯示名稱, GridView id)
MODES: dict[str, tuple[str, str]] = {
    "RadQMode0": ("活動管制", "GridView1"),
    "RadQMode1": ("使用道路集會", "GridView2"),
    "RadQMode2": ("臨時使用道路", "GridView2"),
}
QUERY_BUTTON = {"ButQuery": "查　詢"}


class TaipeiExtRestriction(Source):
    name = "taipei_ext_restriction"
    kind = "restriction"

    def __init__(self, url: str | None = None, days: int | None = None, modes: Iterable[str] | None = None,
                 max_pages: int = 200):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_EXT_URL", DEFAULT_URL)
        self.days = int(days if days is not None else os.environ.get("ROADCHECK_TAIPEI_EXT_DAYS", DEFAULT_DAYS))
        if modes is None:
            modes = os.environ.get("ROADCHECK_TAIPEI_EXT_MODES", DEFAULT_MODES).split(",")
        self.modes = [m if m.startswith("RadQMode") else f"RadQMode{m.strip()}" for m in modes if str(m).strip()]
        unknown = [m for m in self.modes if m not in MODES]
        if unknown:
            raise ValueError(f"unknown ext restriction mode(s): {unknown}; use 0, 1, 2")
        self.max_pages = max_pages

    # ---- fetch ----
    def query_fields(self, today: date | None = None) -> dict[str, str]:
        today = today or date.today()
        return {"TxtQDate0": roc_date(today), "TxtQDate1": roc_date(today + timedelta(days=self.days))}

    def fetch_raw(self) -> list[dict]:
        """回傳 [{"mode": 顯示名稱, "html": 一頁的 HTML}, ...]，三種來源、所有頁。"""
        sess = WebFormsSession()
        landing = sess.get(self.url)
        dates = self.query_fields()
        pages: list[dict] = []
        for mode in self.modes:
            label, grid_id = MODES[mode]
            fields = {**dates, "RadQMode": mode}
            page = landing
            if mode != "RadQMode0":
                page = sess.postback(self.url, page, fields, event_target=mode)
            page = sess.postback(self.url, page, {**fields, **QUERY_BUTTON})
            for html in sess.iter_pages(self.url, page, grid_id, fields, max_pages=self.max_pages):
                pages.append({"mode": label, "html": html})
        return pages

    def load_bytes(self, data: bytes):
        return data.decode("utf-8", errors="replace")

    # ---- parse ----
    def parse(self, raw) -> list[Event]:
        if isinstance(raw, str):
            raw = [{"mode": "", "html": raw}]
        elif isinstance(raw, dict):
            raw = [raw]
        events: list[Event] = []
        seen: dict[str, str] = {}      # source_id -> 內容指紋，用來處理同 key 多列
        for page in raw:
            html = page["html"] if isinstance(page, dict) else str(page)
            mode = page.get("mode", "") if isinstance(page, dict) else ""
            found = find_grid(parse_grids(html))
            if not found:
                continue
            grid, hi, mapping = found
            events.extend(self._rows_to_events(grid, hi, mapping, mode or _guess_mode(grid.rows[hi]), seen))
        return events

    def _rows_to_events(self, grid: Grid, header_idx: int, mapping: dict[int, str], mode: str,
                        seen: dict[str, str]) -> list[Event]:
        header = grid.rows[header_idx]
        events: list[Event] = []
        for ri in range(header_idx + 1, len(grid.rows)):
            row = grid.rows[ri]
            if len(row) < 2:                       # 分頁列或「今天沒有案件」提示列
                continue
            rec = {mapping[i]: row[i] for i in mapping if i < len(row)}
            road = rec.get("road", "")
            if not road:
                continue
            start = parse_date(rec.get("start"))
            end = parse_date(rec.get("end"))
            window = rec.get("time", "")
            if (start is None or end is None) and rec.get("period"):
                ps, pe, pw = split_period(rec["period"])
                start, end = start or ps, end or pe
                window = window or pw
            reason = rec.get("reason", "") or mode
            key = rec.get("id") or self._ref_for_row(grid, ri)
            content = short_hash(road, rec.get("period", ""), str(start), str(end))
            if key:
                sid = key
                if seen.get(sid) not in (None, content):      # 同 key 但內容不同 -> 另一筆
                    sid = f"{key}-{content}"
            else:
                sid = f"{start.isoformat() if start else 'na'}-{short_hash(road, reason)}"
            if seen.get(sid) == content:                       # 完全重複的列（集會／臨時很常見）
                continue
            seen[sid] = content
            events.append(
                Event(
                    source=self.name,
                    source_id=sid,
                    kind="restriction",
                    title=f"{reason}：{road}" if reason else road,
                    address=road,
                    start=start,
                    end=end,
                    time_window=window,
                    # 挖掘管制（活動管制分頁）不是封路；集會／臨時使用道路才真的占用路面
                    blocks_traffic=None if mode == "活動管制" else True,
                    agency=rec.get("agency", ""),
                    purpose=reason,
                    url=self.url,
                    extra={"mode": mode, "key": key, "raw_row": dict(zip(header, row))},
                )
            )
        return events

    @staticmethod
    def _ref_for_row(grid: Grid, ri: int) -> str:
        for (r, _c), ref in grid.refs.items():
            if r == ri:
                return ref
        return ""


def _guess_mode(header: list[str]) -> str:
    joined = "".join(header)
    if "挖掘管制" in joined or "管制原因" in joined:
        return "活動管制"
    if "使用道路" in joined:
        return "使用道路"
    return ""
