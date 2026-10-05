import unittest

from roadcheck.geo import decode_polyline, haversine_m, parse_points, point_to_polyline_m, point_to_segment_m

TAIPEI_101 = (25.0339, 121.5645)
TAIPEI_MAIN = (25.0478, 121.5170)


class GeoTests(unittest.TestCase):
    def test_haversine_known_distance(self):
        # 台北101 到 台北車站 約 5 公里
        d = haversine_m(TAIPEI_101, TAIPEI_MAIN)
        self.assertTrue(4800 < d < 5200, d)

    def test_haversine_zero(self):
        self.assertAlmostEqual(haversine_m(TAIPEI_101, TAIPEI_101), 0.0)

    def test_point_to_segment_perpendicular(self):
        a, b = (25.0400, 121.5400), (25.0400, 121.5500)   # 東西向線段
        p = (25.0409, 121.5450)                             # 北邊約 100 m
        d = point_to_segment_m(p, a, b)
        self.assertTrue(95 < d < 105, d)

    def test_point_to_segment_beyond_endpoint(self):
        a, b = (25.0400, 121.5400), (25.0400, 121.5500)
        p = (25.0400, 121.5600)   # 線段延長線上，距 b 約 1 km
        d = point_to_segment_m(p, a, b)
        self.assertAlmostEqual(d, haversine_m(p, b), delta=5)

    def test_point_to_polyline_min_over_segments(self):
        line = [(25.0400, 121.5400), (25.0400, 121.5500), (25.0500, 121.5500)]
        p = (25.0450, 121.5505)
        self.assertTrue(point_to_polyline_m(p, line) < 60)

    def test_decode_polyline_google_example(self):
        pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(len(pts), 3)
        self.assertAlmostEqual(pts[0][0], 38.5, places=5)
        self.assertAlmostEqual(pts[0][1], -120.2, places=5)
        self.assertAlmostEqual(pts[2][0], 43.252, places=5)

    def test_parse_points(self):
        self.assertEqual(parse_points("25.04,121.54; 25.05 , 121.55"), [(25.04, 121.54), (25.05, 121.55)])


if __name__ == "__main__":
    unittest.main()


class Twd97Tests(unittest.TestCase):
    # 參考值由 pyproj（EPSG:3826 -> EPSG:4326）算出，容許 1e-6 度（約 0.1 公尺）
    REFERENCE = [
        ((301838.0, 2771000.0), (25.0462617, 121.5137407)),     # 台北車站附近
        ((309050.617, 2770535.957), (25.0418080, 121.5851987)), # 信義區福德街
        ((296797.833, 2764527.319), (24.9879903, 121.4635720)), # 資料集西南角
        ((312240.542, 2781577.375), (25.1413593, 121.6173098)), # 資料集東北角
        ((250000.0, 2500000.0), (22.6000807, 121.0)),           # 中央經線上
    ]

    def test_against_pyproj_reference(self):
        from roadcheck.geo import twd97_to_wgs84

        for (x, y), (lat, lon) in self.REFERENCE:
            got = twd97_to_wgs84(x, y)
            self.assertAlmostEqual(got[0], lat, places=6, msg=f"lat of {x},{y}")
            self.assertAlmostEqual(got[1], lon, places=6, msg=f"lon of {x},{y}")

    def test_looks_like_twd97(self):
        from roadcheck.geo import looks_like_twd97

        self.assertTrue(looks_like_twd97(303165.5, 2772301.5))
        self.assertFalse(looks_like_twd97(121.54, 25.04))


class ShapeTests(unittest.TestCase):
    RING = [(25.0, 121.5), (25.0, 121.6), (25.1, 121.6), (25.1, 121.5), (25.0, 121.5)]

    def test_point_in_ring(self):
        from roadcheck.geo import point_in_ring

        self.assertTrue(point_in_ring((25.05, 121.55), self.RING))
        self.assertFalse(point_in_ring((25.2, 121.55), self.RING))
        self.assertFalse(point_in_ring((25.05, 121.7), self.RING))

    def test_point_to_shape(self):
        from roadcheck.geo import point_to_shape_m

        self.assertEqual(point_to_shape_m((25.05, 121.55), self.RING), 0.0)
        d = point_to_shape_m((25.15, 121.55), self.RING)       # 多邊形北邊 0.05 度 ≈ 5.5 km
        self.assertAlmostEqual(d, 5560, delta=60)
        line = [(25.0, 121.5), (25.0, 121.6)]                   # 開放折線：裡面／外面沒差
        self.assertAlmostEqual(point_to_shape_m((25.01, 121.55), line), 1110, delta=20)

    def test_polyline_to_shape(self):
        from roadcheck.geo import polyline_to_shape_m

        crossing = [(24.9, 121.55), (25.2, 121.55)]              # 穿過多邊形但頂點都在外面
        self.assertEqual(polyline_to_shape_m(crossing, self.RING), 0.0)
        touching = [(24.9, 121.5), (25.0, 121.5)]                # 端點剛好碰到角
        self.assertEqual(polyline_to_shape_m(touching, self.RING), 0.0)
        inside_vertex = [(24.9, 121.55), (25.05, 121.55)]
        self.assertEqual(polyline_to_shape_m(inside_vertex, self.RING), 0.0)
        far = [(25.3, 121.5), (25.3, 121.6)]
        self.assertAlmostEqual(polyline_to_shape_m(far, self.RING), 22240, delta=200)

    def test_centroid(self):
        from roadcheck.geo import centroid

        self.assertIsNone(centroid([]))
        self.assertEqual(centroid([(25.0, 121.0), (25.2, 121.4)]), (25.1, 121.2))
