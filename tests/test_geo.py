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
