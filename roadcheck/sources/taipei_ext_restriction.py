"""臺北市道路挖掘管理中心「外部管制路段」（dig.taipei）。

頁面：https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx
已接真資料驗證（2026-10）。這一頁有三種「管制來源」，各是一個 ASP.NET WebForms 的 GridView：

  RadQMode0 活動管制   (EXTREST) 遶境、路跑、大型活動、電影拍攝……也混有「里長要求」「議員要求」
                                「文資管制區域」這種長年的挖掘管理規定，對通勤沒意義，會被過濾掉。
  RadQMode1 使用道路集會 (RALLY)   集會遊行申請使用的路段。
  RadQMode2 臨時使用道路 (URGENT)  吊車、搬家、工程車等臨時佔用道路。

資料來自兩個地方，合併後才完整：
  1. 列表 HTML：POST 切換模式＋查詢日期區間，可以看到未來的案件，但沒有座標。
     要帶 cookie（ASP.NET_SessionId）與 __VIEWSTATE，翻頁用 __doPostBack('GridViewN','Page$N')。
     每一列的地圖連結 ShowExtRest.aspx?key=<CASEID> 就是案號。
  2. 地圖 API：/TpdigR.net/Map/caseMap3.ashx?cmode=<EXTREST|RALLY|URGENT>&caseid=&fno=&qds=&qde=
     回 JSON，每筆一個 TWD97 多邊形（XYSTRING）。EXTREST 會依 qds/qde（民國 yyyMMdd）篩選；
     RALLY／URGENT 不帶 caseid 只回今天的案件，帶 caseid 則回該案件（任何日期）。

fetch_raw() 回傳 {"fetched_on": ..., "listing": [...], "cases": [...]}，parse() 把兩邊依 (mode, caseid)
合併。環境變數：ROADCHECK_TAIPEI_EXT_URL（頁面）、ROADCHECK_EXT_DAYS（往後看幾天，預設 14）、
ROADCHECK_EXT_MODES（逗號分隔，預設三種全抓）、ROADCHECK_EXT_MAX_DAYS（活動管制超過幾天視為長期規定，預設 90）。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Optional

from ..dates import parse_date
from ..geo import LatLon, centroid, looks_like_twd97, twd97_to_wgs84
from ..models import Event
from .base import USER_AGENT, Source, SourceError

log = logging.getLogger(__name__)

DEFAULT_URL = "https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx"
CASEMAP_PATH = "../Map/caseMap3.ashx"

MODES = {
    # mode: (radio value, GridView id, 顯示名稱, 是否影響交通)
    "EXTREST": ("RadQMode0", "GridView1", "活動管制", True),
    "RALLY": ("RadQMode1", "GridView2", "使用道路集會", True),
    "URGENT": ("RadQMode2", "GridView2", "臨時使用道路", None),
}
# 活動管制裡這些「管制原因」是對管線單位的挖掘管理規定，不是封路
EXCLUDED_REASONS = {"里長要求", "議員要求", "文資管制區域", "配合管制"}

_KEY_RE = re.compile(r"ShowExtRest\.aspx\?key=(\d+)")
_PERIOD_RE = re.compile(
    r"(\d{2,4}/\d{1,2}/\d{1,2})(?:\s+(\d{1,2}:\d{2})(?::\d{2})?)?\s*[-~～至到]\s*"
    r"(\d{2,4}/\d{1,2}/\d{1,2})(?:\s+(\d{1,2}:\d{2})(?::\d{2})?)?"
)
_CURRENT_PAGE_RE = re.compile(r"<td><span>(\d+)</span></td>")


def roc_ymd(d: date) -> str:
    """date -> 民國 yyyMMdd（查詢表單用）。"""
    return f"{d.year - 1911:03d}{d.month:02d}{d.day:02d}"


# ---------------------------------------------------------------- HTML 列表


class _GridParser(HTMLParser):
    """把頁面上所有 <table> 抓成 rows；支援巢狀表格（GridView 的分頁列裡還有一個 table）。

    每個 cell 是 {"text": str, "key": CASEID or None}，key 來自 cell 內 <a onclick="window.open('...key=...')">。
    """

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[dict]]] = []
        self._stack: list[list[list[dict]]] = []       # 進行中的 table
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "key": None}
        elif tag == "a" and self._cell is not None:
            for name, value in attrs:
                if name in ("onclick", "href") and value:
                    m = _KEY_RE.search(unescape(value))
                    if m:
                        self._cell["key"] = m.group(1)
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join("".join(self._cell["text"]).split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if any(c["text"] for c in self._row):
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
            # 巢狀表格結束後，外層的 row/cell 狀態已經被內層覆蓋；GridView 的分頁列本來就不需要
            self._row = None
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)


def parse_listing(html: str, mode: str) -> list[dict]:
    """從列表 HTML 抓出資料列。依表頭文字對欄位，表頭不是第一列也沒關係（第一列是分頁）。"""
    rows_out: list[dict] = []
    for table in _tables(html):
        header_idx = next((i for i, r in enumerate(table) if any("路段" in c["text"] for c in r)), None)
        if header_idx is None:
            continue
        header = [c["text"] for c in table[header_idx]]
        col = {}
        for i, h in enumerate(header):
            if "日期" in h or "期間" in h:
                col["period"] = i
            elif "原因" in h:
                col["reason"] = i
            elif "單位" in h:
                col["agency"] = i
            elif "路段" in h:
                col["road"] = i
        if "road" not in col or "period" not in col:
            continue
        for r in table[header_idx + 1:]:
            if len(r) <= max(col.values()):
                continue
            road_cell = r[col["road"]]
            if not road_cell["text"]:
                continue
            rows_out.append({
                "mode": mode,
                "caseid": road_cell["key"] or next((c["key"] for c in r if c["key"]), None),
                "period": r[col["period"]]["text"],
                "reason": r[col["reason"]]["text"] if "reason" in col else "",
                "agency": r[col["agency"]]["text"] if "agency" in col else "",
                "road": road_cell["text"],
            })
        return rows_out
    return rows_out


def _tables(html: str) -> list[list[list[dict]]]:
    p = _GridParser()
    p.feed(html)
    return p.tables


def _form_fields(html: str) -> dict[str, str]:
    """頁面上所有 <input>（hidden／text；radio 只取 checked；submit 不要），重送時要整份帶回去。"""
    out: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html):
        name = re.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        typ = (re.search(r'type="([^"]*)"', tag) or [None, "text"])[1]
        val = re.search(r'value="([^"]*)"', tag)
        value = unescape(val.group(1)) if val else ""
        if typ == "submit":
            continue
        if typ == "radio" and "checked" not in tag:
            continue
        out[unescape(name.group(1))] = value
    return out


def split_period(text: str) -> tuple[Optional[date], Optional[date], str]:
    """「115/10/12 00:00:00-115/10/13 23:59:00」-> (start, end, "00:00-23:59" 或 "")。"""
    m = _PERIOD_RE.search(text or "")
    if not m:
        d = parse_date(text)
        return d, d, ""
    start, end = parse_date(m.group(1)), parse_date(m.group(3))
    tw = _time_window(m.group(2), m.group(4))
    return start, end, tw


def _time_window(t0: Optional[str], t1: Optional[str]) -> str:
    if not t0 or not t1:
        return ""
    if t0 == "00:00" and t1 in ("23:59", "24:00"):
        return "全日"
    return f"{t0}-{t1}"


def _dt_parts(s: str) -> tuple[Optional[date], Optional[str]]:
    """caseMap3 的 ST_DA/EN_DA：「115/10/05 09:00:00」或「115/10/05」。"""
    s = (s or "").strip()
    d = parse_date(s)
    m = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?$", s)
    return d, (m.group(1) if m else None)


def _ring(xystring: str) -> list[LatLon]:
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", xystring or "")]
    pts: list[LatLon] = []
    for i in range(0, len(nums) - 1, 2):
        x, y = nums[i], nums[i + 1]
        if looks_like_twd97(x, y):
            pts.append(twd97_to_wgs84(x, y))
        elif 21 < y < 27 and 119 < x < 123:
            pts.append((y, x))
    if len(pts) >= 3 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts if len(pts) >= 2 else []


# ---------------------------------------------------------------- 抓取


class _AspxSession:
    """帶 cookie 的 GET/POST；POST 會把上一頁的表單欄位整份帶回去再覆寫。"""

    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, url: str, data: Optional[bytes] = None) -> str:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Referer": self.url})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise SourceError(f"HTTP {e.code} fetching {url}") from e
        except urllib.error.URLError as e:
            raise SourceError(f"network error fetching {url}: {e.reason}") from e

    def get(self) -> str:
        return self._open(self.url)

    def post(self, prev_html: str, overrides: dict[str, str]) -> str:
        fields = _form_fields(prev_html)
        fields.setdefault("__EVENTTARGET", "")
        fields.setdefault("__EVENTARGUMENT", "")
        fields.update(overrides)
        return self._open(self.url, urllib.parse.urlencode(fields).encode())

    def get_json(self, url: str):
        text = self._open(url)
        try:
            return json.loads(text.lstrip("﻿"))
        except json.JSONDecodeError as e:
            raise SourceError(f"invalid JSON from {url}: {e}") from e


class TaipeiExtRestriction(Source):
    name = "taipei_ext_restriction"
    kind = "restriction"

    def __init__(self, url: str | None = None, days: int | None = None, modes: list[str] | None = None,
                 max_event_days: int | None = None, max_pages: int = 400, max_case_fetches: int | None = None):
        self.url = url or os.environ.get("ROADCHECK_TAIPEI_EXT_URL", DEFAULT_URL)
        self.days = days if days is not None else int(os.environ.get("ROADCHECK_EXT_DAYS", "7"))
        env_modes = os.environ.get("ROADCHECK_EXT_MODES")
        self.modes = modes or ([m.strip().upper() for m in env_modes.split(",") if m.strip()] if env_modes else list(MODES))
        self.max_event_days = (max_event_days if max_event_days is not None
                               else int(os.environ.get("ROADCHECK_EXT_MAX_DAYS", "90")))
        self.max_pages = max_pages
        # 每個模式最多替幾個案件單獨抓幾何（每次約 1 秒）；臨時使用道路一週可能有上千件
        self.max_case_fetches = (max_case_fetches if max_case_fetches is not None
                                 else int(os.environ.get("ROADCHECK_EXT_MAX_CASE_FETCHES", "150")))
        self.casemap_url = urllib.parse.urljoin(self.url, CASEMAP_PATH)

    # ---- fetch ----
    def fetch_raw(self):
        today = date.today()
        d0, d1 = roc_ymd(today), roc_ymd(today + timedelta(days=self.days))
        sess = _AspxSession(self.url)
        listing: list[dict] = []
        cases: list[dict] = []
        for mode in self.modes:
            if mode not in MODES:
                raise SourceError(f"unknown dig.taipei mode {mode!r}; choose from {list(MODES)}")
            try:
                rows = self._fetch_listing(sess, mode, d0, d1)
            except SourceError as e:
                # 列表掛了還是可以拿地圖 API 的「今日」案件
                log.warning("%s listing failed (%s); falling back to caseMap3 only", mode, e)
                rows = []
            listing.extend(rows)
            # 沒幾何的案件依開始日由近到遠補抓，額度用完時至少最近的有座標
            first_start: dict[str, date] = {}
            for r in rows:
                if r["caseid"]:
                    s, _, _ = split_period(r["period"])
                    first_start[r["caseid"]] = min(first_start.get(r["caseid"], date.max), s or date.max)
            wanted = sorted(first_start, key=lambda c: (first_start[c], c))
            cases.extend(self._fetch_cases(sess, mode, d0, d1, wanted))
        return {"fetched_on": today.isoformat(), "listing": listing, "cases": cases}

    def _fetch_listing(self, sess: _AspxSession, mode: str, d0: str, d1: str) -> list[dict]:
        radio, grid, _, _ = MODES[mode]
        page = sess.get()
        page = sess.post(page, {"__EVENTTARGET": radio, "RadQMode": radio, "TxtQDate0": d0, "TxtQDate1": d1})
        rows = parse_listing(page, mode)
        n = 1
        while n < self.max_pages and f"Page${n + 1}&#39;" in page:
            page = sess.post(page, {"__EVENTTARGET": grid, "__EVENTARGUMENT": f"Page${n + 1}",
                                    "RadQMode": radio, "TxtQDate0": d0, "TxtQDate1": d1})
            if _CURRENT_PAGE_RE.findall(page) != [str(n + 1)]:
                log.warning("%s: paging stopped at page %d (server returned something else)", mode, n + 1)
                break
            rows.extend(parse_listing(page, mode))
            n += 1
        log.info("%s listing: %d rows over %d page(s)", mode, len(rows), n)
        return rows

    def _casemap(self, sess: _AspxSession, mode: str, caseid: str, d0: str, d1: str) -> list[dict]:
        q = urllib.parse.urlencode({"cmode": mode, "caseid": caseid, "fno": "", "qds": d0, "qde": d1})
        data = sess.get_json(f"{self.casemap_url}?{q}")
        if not isinstance(data, list):
            raise SourceError(f"caseMap3 {mode} returned non-list")
        for rec in data:
            rec["mode"] = mode
        return data

    def _fetch_cases(self, sess: _AspxSession, mode: str, d0: str, d1: str, wanted: list[str]) -> list[dict]:
        """先抓整批（EXTREST 是整個區間，RALLY／URGENT 只有今天），列表裡還缺的再一件一件抓。"""
        cases = self._casemap(sess, mode, "", d0, d1)
        have = {str(c.get("CASEID")) for c in cases}
        # EXTREST 的 API 不理 caseid，整批就是區間內全部；再單抓只是重複下載
        missing = [] if mode == "EXTREST" else [c for c in wanted if c not in have]
        if len(missing) > self.max_case_fetches:
            log.warning("%s: %d cases without geometry, only fetching the %d that start soonest",
                        mode, len(missing), self.max_case_fetches)
            missing = missing[: self.max_case_fetches]
        for caseid in missing:
            try:
                cases.extend(self._casemap(sess, mode, caseid, d0, d1))
            except SourceError as e:
                log.warning("%s case %s: %s", mode, caseid, e)
        log.info("%s: %d polygon records for %d cases", mode, len(cases), len({str(c.get('CASEID')) for c in cases}))
        return cases

    # ---- parse ----
    def parse(self, raw) -> list[Event]:
        if not isinstance(raw, dict) or "listing" not in raw and "cases" not in raw:
            raise ValueError("expected {'listing': [...], 'cases': [...]}")
        groups: dict[tuple[str, str], dict] = {}

        def group(mode: str, caseid: str) -> dict:
            return groups.setdefault((mode, caseid), {"rows": [], "cases": []})

        for row in raw.get("listing", []):
            mode = row.get("mode", "EXTREST")
            caseid = row.get("caseid") or "h" + hashlib.sha1(
                f"{mode}|{row.get('period')}|{row.get('road')}".encode()).hexdigest()[:10]
            group(mode, caseid)["rows"].append(row)
        for rec in raw.get("cases", []):
            group(rec.get("mode", "EXTREST"), str(rec.get("CASEID", "")))["cases"].append(rec)

        events: list[Event] = []
        for (mode, caseid), g in groups.items():
            ev = self._build(mode, caseid, g["rows"], g["cases"])
            if ev is not None:
                events.append(ev)
        events.sort(key=lambda e: (e.start or date.max, e.source_id))
        return events

    def _build(self, mode: str, caseid: str, rows: list[dict], recs: list[dict]) -> Optional[Event]:
        _, _, label, blocks = MODES.get(mode, (None, None, mode, None))
        # 日期與時段：地圖 API 的欄位比較完整（有時間），沒有才用列表的期間字串
        starts, ends, windows = [], [], []
        for rec in recs:
            s, t0 = _dt_parts(rec.get("ST_DA", ""))
            e, t1 = _dt_parts(rec.get("EN_DA", ""))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
            windows.append(_time_window(t0, t1))
        for row in rows:
            s, e, tw = split_period(row.get("period", ""))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
            windows.append(tw)
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        time_window = next((w for w in windows if w), "")

        reason = next((r["reason"] for r in rows if r.get("reason")), "")
        cause = next((c["CAUSE"] for c in recs if c.get("CAUSE")), "")
        agency = next((r["agency"] for r in rows if r.get("agency")), "") or next(
            (c["AP_NAME"] for c in recs if c.get("AP_NAME")), "")
        if mode == "EXTREST":
            if reason in EXCLUDED_REASONS:
                return None
            if start and end and (end - start).days > self.max_event_days:
                return None   # 幾年期的挖掘管理規定，不是活動

        roads = _dedupe([r["road"] for r in rows if r.get("road")]) or _dedupe([c.get("DIGADD", "") for c in recs])
        address = "；".join(roads)
        shapes = [ring for ring in (_ring(c.get("XYSTRING", "")) for c in recs) if ring]
        loc = centroid(p for s in shapes for p in s)
        purpose = cause or reason
        head = f"{label}｜{purpose}" if purpose and purpose != label else label
        title = f"{head}：{address}"
        if len(title) > 90:
            title = title[:88] + "…"
        return Event(
            source=self.name,
            source_id=f"{mode}-{caseid}",
            kind="restriction",
            title=title,
            lat=loc[0] if loc else None,
            lon=loc[1] if loc else None,
            address=address,
            start=start,
            end=end,
            time_window=time_window,
            blocks_traffic=blocks,
            agency=agency,
            purpose=purpose,
            url=self.url,
            shapes=shapes,
            extra={
                "mode": mode,
                "mode_label": label,
                "caseid": caseid,
                "reason": reason,
                "cause": cause,
                "note": next((c["RNOTE"] for c in recs if c.get("RNOTE")), ""),
                "contact": next((c["CONTMAN"] for c in recs if c.get("CONTMAN")), ""),
                "contact_tel": next((c["CONTACT"] for c in recs if c.get("CONTACT")), ""),
                "bulletin_url": next((c["URL"] for c in recs if c.get("URL")), ""),
                "polygons": len(shapes),
                "listed": bool(rows),
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
