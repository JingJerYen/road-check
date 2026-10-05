"""地址轉座標。

兩個供應商，用 ROADCHECK_GEOCODER 選（google｜tgos）；沒設就看哪個有金鑰，Google 優先。

Google Geocoding API（預設）
  https://developers.google.com/maps/documentation/geocoding
  環境變數 GOOGLE_MAPS_API_KEY。個人就能申請，每月有免費額度；金鑰請在 Google Cloud 限制只能用 Geocoding API。
  已在開發環境確認端點可連、錯誤格式正確（{"status":"REQUEST_DENIED","error_message":...}）。

內政部 TGOS 全國門牌位置比對服務（備選）
  申請：https://www.tgos.tw（要有 www）→ 註冊 → 申請「全國門牌位置比對服務」→ APPID／APIKey。
  注意申請對象是政府機關、公營事業、法人與公司行號，沒有個人。
  環境變數 TGOS_APP_ID、TGOS_API_KEY；選填 TGOS_REFERER（金鑰綁定的網址）、TGOS_QUERYADDR_URL。
  服務是 ASP.NET Web Service，GET 帶參數，回 XML 包 JSON；oSRS=EPSG:4326 時 X 是經度、Y 是緯度。
  開發環境連不到 addr.tgos.tw，參數與回傳格式依文件寫，未實測。
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

TGOS_DEFAULT_URL = "https://addr.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr"
GOOGLE_DEFAULT_URL = "https://maps.googleapis.com/maps/api/geocode/json"
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


class Geocoder:
    """供應商共同介面。"""

    name = ""
    configured = False

    def query(self, address: str, max_results: int = 5) -> list[GeocodeResult]:
        raise NotImplementedError

    def geocode(self, address: str) -> GeocodeResult:
        """第一筆結果；沒有就丟 GeocodeError。"""
        results = self.query(address, max_results=1)
        if not results:
            raise GeocodeError(f"{self.name} 找不到這個地址：{address}")
        return results[0]


def _http_fetch(url: str, headers: dict, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise GeocodeError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}") from e
    except urllib.error.URLError as e:
        raise GeocodeError(f"network error reaching {urllib.parse.urlsplit(url).netloc}: {e.reason}") from e


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


class TgosGeocoder(Geocoder):
    name = "TGOS"

    def __init__(self, app_id: str | None = None, api_key: str | None = None, url: str | None = None,
                 referer: str | None = None, fetch: Fetch | None = None):
        self.app_id = app_id if app_id is not None else os.environ.get("TGOS_APP_ID", "")
        self.api_key = api_key if api_key is not None else os.environ.get("TGOS_API_KEY", "")
        self.url = url or os.environ.get("TGOS_QUERYADDR_URL", TGOS_DEFAULT_URL)
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


def normalize_address(address: str) -> str:
    """去空白、全形數字轉半形、「台北」→「臺北」（TGOS 用正體「臺」）。"""
    s = re.sub(r"\s+", "", address or "")
    s = s.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
    s = re.sub(r"^台北市", "臺北市", s)
    if s and not s.startswith(("臺北市", "新北市")) and not re.match(r"^[一-鿿]{2,3}[縣市]", s):
        s = "臺北市" + s          # 沒寫縣市就當台北市
    return s


class GoogleGeocoder(Geocoder):
    """Google Geocoding API。

    帶 region=tw、components=country:TW 讓結果限制在台灣，language=zh-TW 拿中文地址。
    精度只有 APPROXIMATE（例如只對到行政區或路）的結果會被丟掉，免得把整條路的中心當成停車位。
    """

    name = "Google"
    OK_LOCATION_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED", "GEOMETRIC_CENTER"}

    def __init__(self, api_key: str | None = None, url: str | None = None, fetch: Fetch | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY", "")
        self.url = url or os.environ.get("GOOGLE_GEOCODE_URL", GOOGLE_DEFAULT_URL)
        self._fetch = fetch or _http_fetch

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def query(self, address: str, max_results: int = 5) -> list[GeocodeResult]:
        if not self.configured:
            raise GeocodeError("沒有設定 GOOGLE_MAPS_API_KEY（Google Cloud Console → APIs → Geocoding API → 建立金鑰）")
        address = normalize_address(address)
        if not address:
            raise GeocodeError("地址是空的")
        params = {"address": address, "key": self.api_key, "language": "zh-TW", "region": "tw",
                  "components": "country:TW"}
        text = self._fetch(f"{self.url}?{urllib.parse.urlencode(params)}", {})
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise GeocodeError(f"Google 回應解析失敗：{e}") from e
        status = payload.get("status")
        if status == "ZERO_RESULTS":
            return []
        if status != "OK":
            raise GeocodeError(f"Google 查詢失敗：{status} {payload.get('error_message', '')}".strip())
        results = [r for r in (_google_result(item) for item in payload.get("results", [])) if r is not None]
        return results[:max_results]


def _google_result(item: dict) -> Optional[GeocodeResult]:
    geom = item.get("geometry") or {}
    loc = geom.get("location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    if geom.get("location_type") not in GoogleGeocoder.OK_LOCATION_TYPES:
        return None
    comps = {}
    for c in item.get("address_components", []):
        for t in c.get("types", []):
            comps.setdefault(t, c.get("long_name", ""))
    return GeocodeResult(
        lat=lat, lon=lon,
        full_address=item.get("formatted_address", ""),
        road=comps.get("route", ""),
        county=comps.get("administrative_area_level_1", ""),
        town=comps.get("administrative_area_level_2", "") or comps.get("administrative_area_level_3", ""),
    )


PROVIDERS = {"google": GoogleGeocoder, "tgos": TgosGeocoder}

_default: Geocoder | None = None


def make_geocoder(provider: str | None = None) -> Geocoder:
    """依 ROADCHECK_GEOCODER 或有設金鑰的那家建立；都沒有就回未設定的 Google（呼叫時會說缺哪個變數）。"""
    provider = (provider or os.environ.get("ROADCHECK_GEOCODER", "")).strip().lower()
    if provider:
        if provider not in PROVIDERS:
            raise GeocodeError(f"不認識的 ROADCHECK_GEOCODER={provider!r}，可用：{', '.join(PROVIDERS)}")
        return PROVIDERS[provider]()
    for cls in (GoogleGeocoder, TgosGeocoder):
        g = cls()
        if g.configured:
            return g
    return GoogleGeocoder()


def get_geocoder() -> Geocoder:
    global _default
    if _default is None:
        _default = make_geocoder()
    return _default


def set_geocoder(g: Geocoder | None) -> None:
    """測試或替換實作用。"""
    global _default
    _default = g
