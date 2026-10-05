import unittest
from datetime import date

from roadcheck.dates import parse_bool, parse_date
from roadcheck.models import Event, Subscription, extract_roads, normalize_road


class DateTests(unittest.TestCase):
    def test_formats(self):
        expected = date(2025, 10, 5)
        for s in ["2025/10/05", "2025-10-05", "20251005", "114/10/05", "114-10-05", "1141005",
                  "2025/10/05 08:00", "114.10.05", "2025年10月5日"]:
            self.assertEqual(parse_date(s), expected, s)

    def test_garbage(self):
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("N/A"))
        self.assertIsNone(parse_date("2025/13/40"))

    def test_bool(self):
        self.assertTrue(parse_bool("Y"))
        self.assertTrue(parse_bool("是"))
        self.assertFalse(parse_bool("N"))
        self.assertIsNone(parse_bool("maybe"))


class RoadTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_road("忠孝東路四段"), "忠孝東路4段")
        self.assertEqual(normalize_road("忠孝東路 4 段"), "忠孝東路4段")
        self.assertEqual(normalize_road("臺北路"), "台北路")

    def test_extract(self):
        roads = extract_roads("忠孝東路四段 復興南路口至敦化南路口")
        self.assertIn("忠孝東路4段", roads)
        self.assertIn("復興南路", roads)
        self.assertIn("敦化南路", roads)

    def test_extract_avenue_and_street(self):
        roads = extract_roads("凱達格蘭大道、延吉街")
        self.assertIn("凱達格蘭大道", roads)
        self.assertIn("延吉街", roads)


class ModelTests(unittest.TestCase):
    def test_fingerprint_changes_with_dates(self):
        a = Event(source="s", source_id="1", kind="construction", title="t", start=date(2025, 1, 1), end=date(2025, 1, 2))
        b = Event(source="s", source_id="1", kind="construction", title="t", start=date(2025, 1, 1), end=date(2025, 1, 5))
        self.assertNotEqual(a.fingerprint(), b.fingerprint())
        self.assertEqual(a.fingerprint(), Event.from_row(a.to_row()).fingerprint())

    def test_subscription_validation(self):
        with self.assertRaises(ValueError):
            Subscription(name="x", kind="point", points=[])
        with self.assertRaises(ValueError):
            Subscription(name="x", kind="route", points=[(1, 2)])
        s = Subscription(name="x", kind="point", points=[(25, 121)], roads=["忠孝東路四段", " "])
        self.assertEqual(s.roads, ["忠孝東路4段"])


if __name__ == "__main__":
    unittest.main()
