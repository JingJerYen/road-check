"""臺北市道路挖掘管理中心「外部管制路段」（活動管制／使用道路集會／臨時使用道路）。

來源頁面：https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx
這頁是 HTML 表格而非 API，且路段是文字描述沒有座標。
解析策略：找出第一個表頭含「管制」或「路段」字樣的 <table>，依表頭關鍵字對應欄位，
所以欄位順序或名稱小幅變動時仍能運作。沒有座標的事件靠路名比對訂閱。

注意：撰寫時此環境無法連到 dig.taipei，表頭關鍵字對應是依照頁面公開描述寫的，
第一次接真資料時請用 `roadcheck fetch --source taipei_ext_restriction --dump` 看原始表格確認。
"""
from __future__ import annotations

import os
from html.parser import HTMLParser

from ..dates import parse_date
from ..models import Event
from .base import Source, http_get

DEFAULT_URL = "https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx"

# 表頭關鍵字 -> 內部欄位
HEADER_MAP = [
    (("管制期間", "期間", "起迄"), "period"),
    (("管制起", "開始", "起日"), "start"),
    (("管制迄", "結束", "迄日", "終止"), "end"),
    (("管制原因", "原因", "類別", "活動名稱", "事由"), "reason"),
    (("管制路段", "路段", "地點", "位置"), "road"),
    (("申請單位", "單位", "主辦"), "agency"),
    (("案號", "編號", "文號"), "id"),
    (("時段", "時間"), "time"),
]


class _TableParser(HTMLParser):
    """把頁面所有 <table> 抓成 list[list[list[str]]]（table -> rows -> cells）。"""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._cells: list[str] | None = None
        self._buf: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._buf = []
        elif tag == "br" and self._buf is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._buf is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._buf).split()))
            self._buf = None
        elif tag == "tr" and self._cells is not None and self._rows is not None:
            if any(self._cells):
                self._rows.append(self._cells)
            self._cells = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def parse_tables(html: str) -> list[list[list[str]]]:
    p = _TableParser()
    p.feed(html)
    return p.tables


def _map_headers(header: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header):
        for keys, field in HEADER_MAP:
            if any(k in cell for k in keys) and field not in mapping.values():
                mapping[idx] = field
                break
    return mapping


def _split_period(text: str) -> tuple:
    """「114/10/05~114/10/07」「2025-10-05 至 2025-10-07」-> (start, end)。"""
    for sep in ("~", "～", "至", "-", "到", "—", "–"):
        if sep in text and sep != "-":
            a, _, b = text.partition(sep)
            return parse_date(a), parse_date(b)
    # 只有 "-" 時要小心日期本身也用 "-"，用空白切
    parts = text.replace("-", " - ", 2).split()
    dates = [d for d in (parse_date(p) for p in parts) if d]
    if len(dates) >= 2:
        return dates[0], dates[-1]
    if len(dates) == 1:
        return dates[0], dates[0]
    return None, None


class TaipeiExtRestriction(Source):
    name = "taipei_ext_restriction"
    kind = "restriction"

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_EXT_URL", DEFAULT_URL)

    def fetch_raw(self):
        return http_get(self.url).decode("utf-8", errors="replace")

    def load_bytes(self, data: bytes):
        return data.decode("utf-8", errors="replace")

    def parse(self, raw: str) -> list[Event]:
        tables = parse_tables(raw)
        for table in tables:
            if len(table) < 2:
                continue
            mapping = _map_headers(table[0])
            if "road" not in mapping.values():
                continue
            return self._rows_to_events(table[0], table[1:], mapping)
        return []

    def _rows_to_events(self, header, rows, mapping) -> list[Event]:
        events: list[Event] = []
        for n, row in enumerate(rows, start=1):
            rec = {mapping[i]: row[i] for i in mapping if i < len(row)}
            road = rec.get("road", "")
            if not road:
                continue
            start = parse_date(rec.get("start"))
            end = parse_date(rec.get("end"))
            if (start is None or end is None) and rec.get("period"):
                ps, pe = _split_period(rec["period"])
                start = start or ps
                end = end or pe
            reason = rec.get("reason", "")
            sid = rec.get("id") or f"{start.isoformat() if start else 'na'}-{abs(hash(road + reason)) % 10**8}"
            events.append(
                Event(
                    source=self.name,
                    source_id=sid,
                    kind="restriction",
                    title=f"{reason}：{road}" if reason else road,
                    address=road,
                    start=start,
                    end=end,
                    time_window=rec.get("time", ""),
                    blocks_traffic=True,
                    agency=rec.get("agency", ""),
                    purpose=reason,
                    url=self.url,
                    extra={"raw_row": dict(zip(header, row))},
                )
            )
        return events
