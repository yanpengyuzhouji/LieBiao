import unittest

from backend.db import beijing_time


class BeijingTimeTests(unittest.TestCase):
    def test_utc_is_converted_to_beijing(self) -> None:
        self.assertEqual(beijing_time("2026-09-04T02:00:00+00:00"), "2026-09-04T10:00:00+08:00")

    def test_source_time_without_offset_is_treated_as_beijing(self) -> None:
        self.assertEqual(beijing_time("2026-09-04 10:00:00"), "2026-09-04T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
