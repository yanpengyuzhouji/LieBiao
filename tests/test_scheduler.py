from datetime import datetime, timezone
import unittest

from backend.scheduler import is_due, parse_schedule


class SchedulerRuleTests(unittest.TestCase):
    def test_supported_rules(self) -> None:
        self.assertEqual(parse_schedule("每 30 分钟"), {"kind": "interval", "seconds": 1800})
        self.assertEqual(parse_schedule("每2小时"), {"kind": "interval", "seconds": 7200})
        self.assertEqual(parse_schedule("每天 08:30"), {"kind": "clock", "hour": 8, "minute": 30, "weekdays": False})
        self.assertEqual(parse_schedule("工作日 08:30"), {"kind": "clock", "hour": 8, "minute": 30, "weekdays": True})
        self.assertIsNone(parse_schedule("手动"))

    def test_interval_due_and_duplicate_guard_input(self) -> None:
        now = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(is_due("每30分钟", "2026-09-04T01:00:00+00:00", None, now))
        self.assertFalse(is_due("每30分钟", "2026-09-04T01:45:00+00:00", None, now))

    def test_enable_anchor_restarts_countdown(self) -> None:
        now = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)
        anchor = "2026-09-04T01:55:00+00:00"
        self.assertFalse(is_due("每30分钟", "2026-09-04T01:00:00+00:00", None, now, "Asia/Shanghai", anchor))
        self.assertTrue(is_due("每30分钟", "2026-09-04T01:00:00+00:00", None, now.replace(minute=25), "Asia/Shanghai", anchor))


if __name__ == "__main__":
    unittest.main()
