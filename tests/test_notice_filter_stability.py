import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NoticeFilterStabilityTests(unittest.TestCase):
    def test_background_refresh_preserves_focused_notice_filter(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn("function noticeFilterIsActive()", source)
        self.assertIn("#platform-filter, #mark-filter, #attachment-filter", source)
        self.assertIn("renderCurrentViewWithoutInterruptingFilters(false)", source)
        self.assertIn("renderCurrentViewWithoutInterruptingFilters(true)", source)

    def test_frontend_cache_key_is_bumped_for_filter_fix(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("app.js?v=20260911-1", source)


if __name__ == "__main__":
    unittest.main()
