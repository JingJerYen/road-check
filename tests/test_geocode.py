import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from roadcheck import cli, geocode
from roadcheck.geocode import GeocodeError, TgosGeocoder, normalize_address, parse_queryaddr_response
from roadcheck.store import Store

# 依 TGOS 文件與公開範例組的回應：ASP.NET Web Service 把 JSON 字串包在 XML 裡
SAMPLE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\r\n'
    '<string xmlns="http://tempuri.org/">{ "Info": [ { "IsSuccess": "True", "InAddress": "臺北市西園路二段255號", '
    '"InSRS": "EPSG:4326", "InFuzzyType": "2", "InFuzzyBuffer": "0", "InIsOnlyFullMatch": "False", '
    '"InIsLockCounty": "True", "InIsLockTown": "False", "InIsLockVillage": "False", "InIsLockRoadPIL": "False", '
    '"InIsSupportPast": "True", "InReturnMaxCount": "1", "OutTotal": "1", "OutMatchType": "完全比對", '
    '"OutMatchCode": "0", "OutTraceInfo": "" } ], "AddressList": [ { "FULL_ADDR": "臺北市萬華區新忠里5鄰西園路二段255號", '
    '"COUNTY": "臺北市", "TOWN": "萬華區", "VILLAGE": "新忠里", "NEIGHBORHOOD": "5鄰", "ROAD": "西園路", '
    '"SECTION": "二段", "LANE": "", "ALLEY": "", "SUB_ALLEY": "", "TONG": "", "NUMBER": "255號", '
    '"X": 121.4939, "Y": 25.0273 } ] }</string>'
)
SAMPLE_EMPTY = '<string xmlns="http://tempuri.org/">{ "Info": [ { "IsSuccess": "True", "OutTotal": "0" } ], "AddressList": [ ] }</string>'
SAMPLE_FAIL = '<string xmlns="http://tempuri.org/">{ "Info": [ { "IsSuccess": "False", "ErrorMsg": "APIKey 驗證失敗" } ] }</string>'


class FakeFetch:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.text


class ParseTests(unittest.TestCase):
    def test_xml_wrapped_json(self):
        rows = parse_queryaddr_response(SAMPLE_XML)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["NUMBER"], "255號")

    def test_plain_json_also_ok(self):
        rows = parse_queryaddr_response('{"Info":[{"IsSuccess":"True"}],"AddressList":[{"X":121.5,"Y":25.0}]}')
        self.assertEqual(rows[0]["X"], 121.5)

    def test_failure_raises_with_message(self):
        with self.assertRaises(GeocodeError) as ctx:
            parse_queryaddr_response(SAMPLE_FAIL)
        self.assertIn("APIKey", str(ctx.exception))
        with self.assertRaises(GeocodeError):
            parse_queryaddr_response("<html>Service Unavailable</html>")

    def test_normalize_address(self):
        self.assertEqual(normalize_address("台北市 西園路二段２５５號"), "臺北市西園路二段255號")
        self.assertEqual(normalize_address("西園路二段255號"), "臺北市西園路二段255號")
        self.assertEqual(normalize_address("新北市板橋區文化路一段1號"), "新北市板橋區文化路一段1號")
        self.assertEqual(normalize_address("  "), "")


class GeocoderTests(unittest.TestCase):
    def test_geocode_builds_request_and_parses(self):
        fetch = FakeFetch(SAMPLE_XML)
        g = TgosGeocoder(app_id="APP", api_key="KEY", referer="https://example.test", fetch=fetch)
        r = g.geocode("台北市西園路二段255號")
        self.assertAlmostEqual(r.lat, 25.0273)
        self.assertAlmostEqual(r.lon, 121.4939)
        self.assertEqual(r.road, "西園路二段")
        self.assertEqual(r.town, "萬華區")
        self.assertTrue(r.full_address.endswith("西園路二段255號"))
        url, headers = fetch.calls[0]
        self.assertIn("oAPPId=APP", url)
        self.assertIn("oAPIKey=KEY", url)
        self.assertIn("oSRS=EPSG%3A4326", url)
        self.assertIn("oAddress=%E8%87%BA%E5%8C%97%E5%B8%82", url)   # 臺北市（台→臺）
        self.assertEqual(headers, {"Referer": "https://example.test"})

    def test_twd97_result_is_converted(self):
        fetch = FakeFetch('{"Info":[{"IsSuccess":"True"}],"AddressList":[{"FULL_ADDR":"x","X":300000.0,"Y":2770000.0}]}')
        r = TgosGeocoder(app_id="a", api_key="b", fetch=fetch).geocode("x")
        self.assertTrue(24.9 < r.lat < 25.2 and 121.4 < r.lon < 121.6)

    def test_no_result(self):
        g = TgosGeocoder(app_id="a", api_key="b", fetch=FakeFetch(SAMPLE_EMPTY))
        with self.assertRaises(GeocodeError) as ctx:
            g.geocode("臺北市不存在路999號")
        self.assertIn("找不到", str(ctx.exception))

    def test_missing_key(self):
        g = TgosGeocoder(app_id="", api_key="", fetch=FakeFetch(SAMPLE_XML))
        self.assertFalse(g.configured)
        with self.assertRaises(GeocodeError) as ctx:
            g.geocode("x")
        self.assertIn("TGOS_APP_ID", str(ctx.exception))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()

    def tearDown(self):
        geocode.set_geocoder(None)
        os.unlink(self.tmp.name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", self.tmp.name, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_subscribe_point_by_address(self):
        geocode.set_geocoder(TgosGeocoder(app_id="a", api_key="b", fetch=FakeFetch(SAMPLE_XML)))
        code, out, _ = self.run_cli("subscribe", "point", "--name", "停車", "--address", "台北市西園路二段255號")
        self.assertEqual(code, 0, out)
        self.assertIn("25.02730, 121.49390", out)
        store = Store(self.tmp.name)
        sub = store.list_subscriptions()[0]
        store.close()
        self.assertEqual(sub.points, [(25.0273, 121.4939)])
        self.assertEqual(sub.roads, ["西園路2段"])          # 從 TGOS 的路名自動帶入
        self.assertEqual(sub.radius_m, 100.0)

    def test_subscribe_point_address_failure(self):
        geocode.set_geocoder(TgosGeocoder(app_id="", api_key="", fetch=FakeFetch(SAMPLE_XML)))
        code, _, err = self.run_cli("subscribe", "point", "--name", "停車", "--address", "x")
        self.assertEqual(code, 1)
        self.assertIn("TGOS_APP_ID", err)

    def test_point_needs_coords_or_address(self):
        code, _, err = self.run_cli("subscribe", "point", "--name", "停車")
        self.assertEqual(code, 2)
        self.assertIn("--address", err)

    def test_geocode_command(self):
        geocode.set_geocoder(TgosGeocoder(app_id="a", api_key="b", fetch=FakeFetch(SAMPLE_XML)))
        code, out, _ = self.run_cli("geocode", "西園路二段255號")
        self.assertEqual(code, 0)
        self.assertIn("25.027300,121.493900", out)
        self.assertIn("西園路2段", out)


if __name__ == "__main__":
    unittest.main()


# Google Geocoding API 的回應（結構依官方文件；錯誤格式已在開發環境實際打過確認）
GOOGLE_OK = '''{
  "results": [{
    "address_components": [
      {"long_name": "255號", "short_name": "255號", "types": ["street_number"]},
      {"long_name": "西園路二段", "short_name": "西園路二段", "types": ["route"]},
      {"long_name": "萬華區", "short_name": "萬華區", "types": ["administrative_area_level_2", "political"]},
      {"long_name": "台北市", "short_name": "台北市", "types": ["administrative_area_level_1", "political"]},
      {"long_name": "台灣", "short_name": "TW", "types": ["country", "political"]},
      {"long_name": "108", "short_name": "108", "types": ["postal_code"]}
    ],
    "formatted_address": "108台灣台北市萬華區西園路二段255號",
    "geometry": {"location": {"lat": 25.0272, "lng": 121.4938}, "location_type": "ROOFTOP",
                 "viewport": {"northeast": {"lat": 25.0285, "lng": 121.4951}, "southwest": {"lat": 25.0258, "lng": 121.4924}}},
    "place_id": "ChIJx", "plus_code": {"compound_code": "2FGV+VG 台北市萬華區"}, "types": ["street_address"]
  }],
  "status": "OK"
}'''
GOOGLE_APPROX = '''{"results": [{"address_components": [{"long_name": "萬華區", "types": ["administrative_area_level_2"]}],
  "formatted_address": "台灣台北市萬華區", "geometry": {"location": {"lat": 25.03, "lng": 121.5}, "location_type": "APPROXIMATE"},
  "types": ["administrative_area_level_2"]}], "status": "OK"}'''
GOOGLE_ZERO = '{"results": [], "status": "ZERO_RESULTS"}'
GOOGLE_DENIED = '{"error_message": "The provided API key is invalid. ", "results": [], "status": "REQUEST_DENIED"}'


class GoogleGeocoderTests(unittest.TestCase):
    def make(self, text, key="K"):
        from roadcheck.geocode import GoogleGeocoder

        self.fetch = FakeFetch(text)
        return GoogleGeocoder(api_key=key, fetch=self.fetch)

    def test_ok_result(self):
        r = self.make(GOOGLE_OK).geocode("台北市西園路二段255號")
        self.assertEqual((r.lat, r.lon), (25.0272, 121.4938))
        self.assertEqual(r.road, "西園路二段")
        self.assertEqual(r.town, "萬華區")
        self.assertEqual(r.county, "台北市")
        self.assertEqual(r.full_address, "108台灣台北市萬華區西園路二段255號")
        url, _ = self.fetch.calls[0]
        self.assertTrue(url.startswith("https://maps.googleapis.com/maps/api/geocode/json?"))
        self.assertIn("key=K", url)
        self.assertIn("region=tw", url)
        self.assertIn("components=country%3ATW", url)
        self.assertIn("language=zh-TW", url)

    def test_approximate_results_are_rejected(self):
        with self.assertRaises(GeocodeError) as ctx:
            self.make(GOOGLE_APPROX).geocode("台北市萬華區")
        self.assertIn("找不到", str(ctx.exception))

    def test_zero_results_and_denied(self):
        self.assertEqual(self.make(GOOGLE_ZERO).query("x"), [])
        with self.assertRaises(GeocodeError) as ctx:
            self.make(GOOGLE_DENIED).geocode("x")
        self.assertIn("REQUEST_DENIED", str(ctx.exception))
        self.assertIn("invalid", str(ctx.exception))

    def test_missing_key(self):
        with self.assertRaises(GeocodeError) as ctx:
            self.make(GOOGLE_OK, key="").geocode("x")
        self.assertIn("GOOGLE_MAPS_API_KEY", str(ctx.exception))


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in
                      ("ROADCHECK_GEOCODER", "GOOGLE_MAPS_API_KEY", "TGOS_APP_ID", "TGOS_API_KEY")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_to_google_and_falls_back_to_tgos_keys(self):
        from roadcheck.geocode import GoogleGeocoder, TgosGeocoder, make_geocoder

        self.assertIsInstance(make_geocoder(), GoogleGeocoder)        # 沒金鑰：Google（呼叫時會說缺金鑰）
        os.environ["TGOS_APP_ID"] = "a"
        os.environ["TGOS_API_KEY"] = "b"
        self.assertIsInstance(make_geocoder(), TgosGeocoder)          # 只有 TGOS 金鑰
        os.environ["GOOGLE_MAPS_API_KEY"] = "k"
        self.assertIsInstance(make_geocoder(), GoogleGeocoder)        # 兩個都有：Google 優先
        os.environ["ROADCHECK_GEOCODER"] = "tgos"
        self.assertIsInstance(make_geocoder(), TgosGeocoder)          # 明確指定
        os.environ["ROADCHECK_GEOCODER"] = "bing"
        with self.assertRaises(GeocodeError):
            make_geocoder()
