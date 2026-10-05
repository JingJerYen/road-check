"""地址轉座標：內政部 TGOS「全國門牌地址定位服務」。

申請金鑰：https://www.tgos.tw （注意要有 www，光打 tgos.tw 沒有 DNS）→ 註冊 → 「TGOS MAP API」→ 新增應用程式 → 取得 APPID 與 APIKey。
環境變數：
  TGOS_APP_ID、TGOS_API_KEY   必填
  TGOS_REFERER               選填；TGOS 的金鑰綁定申請時填的網址，若回「權限不足」把那個網址設在這裡
  TGOS_QUERYADDR_URL         選填；預設 https://addr.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr

服務是 ASP.NET Web Service，用 GET 帶參數，回傳 XML 包著一段 JSON：
  <string xmlns="http://tempuri.org/">{"Info":[{"IsSuccess":"True",...}],"AddressList":[{"FULL_ADDR":...,"X":121.5,"Y":25.0}]}</string>
oSRS 給 EPSG:4326 時 X 是經度、Y 是緯度。

撰寫時此環境連不到 addr.tgos.tw，參數與回傳格式依官方文件與公開範例寫的，第一次有金鑰時請用
`roadcheck geocode "台北市西園路二段255號"` 確認。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .geo import LatLon, looks_like_twd97, twd97_to_wgs84

DEFAULT_URL = "https://addr.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr"
USER_AGENT = "roadcheck/0.2 (+https://github.com/JingJerYen/road-check)"


class GeocodeError(RuntimeError):
    pass


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    full_address: str          # TGOS 正規化後的完整地址
    road: str = ""             # 路名（含段），給路名比對用
    county: str = ""
    town: str = ""

    @property
    def location(self) -> LatLon:
        return (self.lat, self.lon)


Fetch = Callable[[str, dict], str]   # (url, headers) -> response text


def _http_fetch(url: str, headers: dict, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise GeocodeError(f"TGOS HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise GeocodeError(f"TGOS network error: {e.reason}") from e


def parse_queryaddr_response(text: str) -> list[dict]:
    """把 QueryAddr 的回應（XML 包 JSON，或直接 JSON）轉成 AddressList。失敗時丟 GeocodeError。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise GeocodeError(f"TGOS 回應不是 JSON：{text[:120]!r}")
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise GeocodeError(f"TGOS 回應解析失敗：{e}") from e
    info = (payload.get("Info") or [{}])[0] if isinstance(payload.get("Info"), list) else payload.get("Info") or {}
    ok = str(info.get("IsSuccess", "True")).lower() in ("true", "1", "")
    if not ok:
        msg = info.get("ErrorMsg") or info.get("ErrorMessage") or info.get("Message") or json.dumps(info, ensure_ascii=False)
        raise GeocodeError(f"TGOS 查詢失敗：{msg}")
    rows = payload.get("AddressList") or []
    return rows if isinstance(rows, list) else []


def _row_to_result(row: dict) -> Optional[GeocodeResult]:
    try:
        x, y = float(row.get("X")), float(row.get("Y"))
    except (TypeError, ValueError):
        return None
    if looks_like_twd97(x, y):              # 萬一 oSRS 沒生效
        lat, lon = twd97_to_wgs84(x, y)
    else:
        lat, lon = y, x
    if not (21.0 < lat < 26.5 and 118.0 < lon < 123.0):
        return None
    road = (row.get("ROAD") or "") + (row.get("SECTION") or "")
    return GeocodeResult(
        lat=lat, lon=lon,
        full_address=row.get("FULL_ADDR") or row.get("ADDRESS") or "",
        road=road, county=row.get("COUNTY") or "", town=row.get("TOWN") or "",
    )


class TgosGeocoder:
    def __init__(self, app_id: str | None = None, api_key: str | None = None, url: str | None = None,
                 referer: str | None = None, fetch: Fetch | None = None):
        self.app_id = app_id if app_id is not None else os.environ.get("TGOS_APP_ID", "")
        self.api_key = api_key if api_key is not None else os.environ.get("TGOS_API_KEY", "")
        self.url = url or os.environ.get("TGOS_QUERYADDR_URL", DEFAULT_URL)
        self.referer = referer if referer is not None else os.environ.get("TGOS_REFERER", "")
        self._fetch = fetch or _http_fetch

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.api_key)

    def query(self, address: str, max_results: int = 5) -> list[GeocodeResult]:
        if not self.configured:
            raise GeocodeError("沒有設定 TGOS_APP_ID / TGOS_API_KEY（到 https://www.tgos.tw 申請 TGOS MAP API 金鑰）")
        address = normalize_address(address)
        if not address:
            raise GeocodeError("地址是空的")
        params = {
            "oAPPId": self.app_id,
            "oAPIKey": self.api_key,
            "oAddress": address,
            "oSRS": "EPSG:4326",
            "oFuzzyType": "2",              # 模糊比對：找不到完全相同的門牌時給最接近的
            "oResultDataType": "JSON",
            "oFuzzyBuffer": "0",
            "oIsOnlyFullMatch": "false",
            "oIsLockCounty": "true",
            "oIsLockTown": "false",
            "oIsLockVillage": "false",
            "oIsLockRoadPIL": "false",
            "oIsSupportPast": "true",
            "oReturnMaxCount": str(max_results),
        }
        headers = {"Referer": self.referer} if self.referer else {}
        text = self._fetch(f"{self.url}?{urllib.parse.urlencode(params)}", headers)
        rows = parse_queryaddr_response(text)
        results = [r for r in (_row_to_result(row) for row in rows) if r is not None]
        return results[:max_results]

    def geocode(self, address: str) -> GeocodeResult:
        """第一筆結果；沒有就丟 GeocodeError。"""
        results = self.query(address, max_results=1)
        if not results:
            raise GeocodeError(f"TGOS 找不到這個地址：{address}")
        return results[0]


def normalize_address(address: str) -> str:
    """去空白、全形數字轉半形、「台北」→「臺北」（TGOS 用正體「臺」）。"""
    s = re.sub(r"\s+", "", address or "")
    s = s.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
    s = re.sub(r"^台北市", "臺北市", s)
    if s and not s.startswith(("臺北市", "新北市")) and not re.match(r"^[一-鿿]{2,3}[縣市]", s):
        s = "臺北市" + s          # 沒寫縣市就當台北市
    return s


_default: TgosGeocoder | None = None


def get_geocoder() -> TgosGeocoder:
    global _default
    if _default is None:
        _default = TgosGeocoder()
    return _default


def set_geocoder(g: TgosGeocoder | None) -> None:
    """測試或替換實作用。"""
    global _default
    _default = g
