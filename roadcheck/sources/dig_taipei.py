"""dig.taipei（臺北市道路挖掘管理中心）共用工具。

dig.taipei 的公開查詢頁都是 ASP.NET WebForms：
- 第一次 GET 拿到 ``__VIEWSTATE`` 等隱藏欄位與 ``ASP.NET_SessionId`` cookie；
- 之後所有操作（查詢、切換模式、GridView 翻頁、匯出）都是 POST 回同一個 URL，
  帶上前一頁的隱藏欄位，翻頁用 ``__EVENTTARGET=GridView1`` + ``__EVENTARGUMENT=Page$N``。
- 資料表是 <table id="GridView1">，第一列是「巢狀」的分頁 <table>，第二列才是表頭。

這裡提供：
- :class:`WebFormsSession`：帶 cookie 的 GET / postback，並處理隱藏欄位。
- :func:`parse_tables` / :func:`parse_grids`：支援巢狀表格的 HTML 表格解析，
  並把儲存格內 <a> 的 ``key=`` / ``caseid=`` 抓出來當穩定 ID。
- :func:`map_headers` / :func:`split_period`：表頭關鍵字對應與期間欄解析。
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Iterator, Optional

from ..dates import parse_date
from .base import USER_AGENT, SourceError

# ---------------------------------------------------------------------------
# WebForms session
# ---------------------------------------------------------------------------

_HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.I)
_PAGE_LINK_RE = re.compile(r"Page\$(\d+)")
_CURRENT_PAGE_RE = re.compile(r"<td><span>(\d+)</span></td>")


def hidden_fields(page: str) -> dict[str, str]:
    """抓出 ``__VIEWSTATE`` 之類以 ``__`` 開頭的隱藏欄位（HTML entity 已還原）。"""
    out: dict[str, str] = {}
    for name, value in _HIDDEN_RE.findall(page):
        if name.startswith("__"):
            out[htmlmod.unescape(name)] = htmlmod.unescape(value)
    return out


class WebFormsSession:
    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def _open(self, req: urllib.request.Request) -> bytes:
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise SourceError(f"HTTP {e.code} fetching {req.full_url}") from e
        except urllib.error.URLError as e:
            raise SourceError(f"network error fetching {req.full_url}: {e.reason}") from e

    def get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self._open(req).decode("utf-8", errors="replace")

    def postback(
        self,
        url: str,
        page: str,
        fields: Optional[dict[str, str]] = None,
        event_target: str = "",
        event_argument: str = "",
    ) -> str:
        """以 ``page``（上一頁 HTML）的隱藏欄位為基底，加上 ``fields`` 後 POST。"""
        form = hidden_fields(page)
        form["__EVENTTARGET"] = event_target
        form["__EVENTARGUMENT"] = event_argument
        form.update(fields or {})
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
            },
        )
        return self._open(req).decode("utf-8", errors="replace")

    def iter_pages(
        self,
        url: str,
        first_page: str,
        grid_id: str,
        fields: Optional[dict[str, str]] = None,
        max_pages: int = 200,
    ) -> Iterator[str]:
        """從第一頁開始，沿著 GridView 的 ``Page$N`` 連結翻到最後一頁，逐頁 yield HTML。"""
        page = first_page
        yield page
        n = 1
        while n < max_pages:
            n += 1
            if f"Page${n}" not in page:
                break
            page = self.postback(url, page, fields, event_target=grid_id, event_argument=f"Page${n}")
            current = _CURRENT_PAGE_RE.findall(page)
            if current and int(current[-1]) != n:   # 伺服器沒有真的翻頁，避免無窮迴圈
                break
            yield page


def roc_date(d: date) -> str:
    """西元 date -> dig.taipei 查詢欄位用的民國 yyyMMdd（例如 1151005）。"""
    return f"{d.year - 1911:03d}{d.month:02d}{d.day:02d}"


# ---------------------------------------------------------------------------
# HTML tables（支援巢狀）
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"(?:caseid|key)=(\d+)", re.I)


@dataclass
class Grid:
    rows: list[list[str]] = field(default_factory=list)
    refs: dict[tuple[int, int], str] = field(default_factory=dict)   # (row, col) -> key / caseid
    attrs: dict[str, str] = field(default_factory=dict)              # <table> 的屬性

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")


class _Frame:
    __slots__ = ("grid", "cells", "buf", "ref")

    def __init__(self, attrs):
        self.grid = Grid(attrs={k: (v or "") for k, v in attrs})
        self.cells: Optional[list[str]] = None
        self.buf: Optional[list[str]] = None
        self.ref: Optional[str] = None


class _TableParser(HTMLParser):
    """把頁面所有 <table> 抓成 Grid（巢狀表格各自獨立，內層先輸出）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.grids: list[Grid] = []
        self._stack: list[_Frame] = []

    @property
    def _top(self) -> Optional[_Frame]:
        return self._stack[-1] if self._stack else None

    def handle_starttag(self, tag, attrs):
        top = self._top
        if tag == "table":
            self._stack.append(_Frame(attrs))
            return
        if top is None:
            return
        if tag == "tr":
            top.cells = []
        elif tag in ("td", "th") and top.cells is not None:
            top.buf = []
            top.ref = None
        elif tag == "br" and top.buf is not None:
            top.buf.append(" ")
        elif tag == "a" and top.buf is not None and top.ref is None:
            for _, v in attrs:
                m = _REF_RE.search(v or "")
                if m:
                    top.ref = m.group(1)
                    break

    def handle_endtag(self, tag):
        top = self._top
        if top is None:
            return
        if tag in ("td", "th") and top.buf is not None and top.cells is not None:
            if top.ref:
                top.grid.refs[(len(top.grid.rows), len(top.cells))] = top.ref
            top.cells.append(" ".join("".join(top.buf).split()))
            top.buf = None
            top.ref = None
        elif tag == "tr" and top.cells is not None:
            if any(top.cells):
                top.grid.rows.append(top.cells)
            else:
                # 空列（例如只包了巢狀分頁表）不算資料，但 refs 的列號要對得上
                pass
            top.cells = None
        elif tag == "table":
            frame = self._stack.pop()
            self.grids.append(frame.grid)

    def handle_data(self, data):
        top = self._top
        if top is not None and top.buf is not None:
            top.buf.append(data)

    def close(self):
        super().close()
        while self._stack:              # 沒閉合的 <table>
            self.grids.append(self._stack.pop().grid)


def parse_grids(html: str) -> list[Grid]:
    p = _TableParser()
    p.feed(html)
    p.close()
    return p.grids


def parse_tables(html: str) -> list[list[list[str]]]:
    """相容舊介面：只回傳 rows。"""
    return [g.rows for g in parse_grids(html)]


# ---------------------------------------------------------------------------
# 表頭對應與期間解析
# ---------------------------------------------------------------------------

# 表頭關鍵字 -> 內部欄位。順序有意義：先比對較專一的字。
HEADER_MAP: list[tuple[tuple[str, ...], str]] = [
    (("管制期間", "期間", "起迄", "日期"), "period"),           # 挖掘管制日期 / 使用道路日期 / 預定施工日期
    (("管制起", "開始", "起日"), "start"),
    (("管制迄", "結束", "迄日", "終止"), "end"),
    (("管制原因", "原因", "類別", "類型", "活動名稱", "事由"), "reason"),
    (("管制路段", "路段", "地點", "位置"), "road"),
    (("申請單位", "施工單位", "單位", "主辦"), "agency"),
    (("案號", "編號", "文號"), "id"),
    (("時段", "時間"), "time"),
]


def map_headers(header: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header):
        for keys, fld in HEADER_MAP:
            if any(k in cell for k in keys) and fld not in mapping.values():
                mapping[idx] = fld
                break
    return mapping


def find_grid(grids: list[Grid], required: str = "road") -> Optional[tuple[Grid, int, dict[int, str]]]:
    """找出第一個有「路段」表頭的表格，回傳 (grid, 表頭列索引, 欄位對應)。

    dig.taipei 的 GridView 第一列是分頁列，所以表頭不一定在第 0 列，前幾列都找。
    """
    for g in grids:
        for hi, row in enumerate(g.rows[:3]):
            mapping = map_headers(row)
            if required in mapping.values() and len(mapping) >= 2:
                return g, hi, mapping
    return None


_DATE_TOKEN = re.compile(r"\d{2,4}[/.\-年]\d{1,2}[/.\-月]\d{1,2}|\b\d{7,8}\b")
_TIME_TOKEN = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?")


def split_period(text: str) -> tuple[Optional[date], Optional[date], str]:
    """期間欄 -> (start, end, time_window)。

    支援：
      115/09/08-115/10/11
      115/10/05 00:00:00-115/10/06 23:59:00   -> time_window "00:00-23:59"
      114/10/07~114/10/07、2025/10/06 至 2025/10/06、1141007～1141009
    """
    if not text:
        return None, None, ""
    dates = [d for d in (parse_date(t) for t in _DATE_TOKEN.findall(text)) if d]
    times = _TIME_TOKEN.findall(text)
    window = ""
    if len(times) >= 2:
        (h1, m1), (h2, m2) = times[0], times[-1]
        window = f"{int(h1):02d}:{m1}-{int(h2):02d}:{m2}"
    if len(dates) >= 2:
        return dates[0], dates[-1], window
    if len(dates) == 1:
        return dates[0], dates[0], window
    return None, None, window


def short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]
