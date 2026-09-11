import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NoticeFilterStabilityTests(unittest.TestCase):
    def test_background_refresh_preserves_focused_notice_filter(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn("function noticeFilterIsActive()", source)
        self.assertIn("#platform-filter, #mark-filter, #attachment-filter", source)
        self.assertIn("renderCurrentViewWithoutInterruptingFilters(false, preserveFilterDom)", source)
        self.assertIn("renderCurrentViewWithoutInterruptingFilters(true)", source)

    def test_frontend_cache_key_is_bumped_for_filter_fix(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("app.js?v=20260911-2", source)

    def test_count_badges_update_without_replacing_notice_filters(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn("function updateNoticeCountBadges", source)
        self.assertIn("updateNoticeCountBadges(appState.noticeCounts)", source)
        self.assertIn("if (appState.mark !== 'all') appState.tab = 'all'", source)
        self.assertNotIn('<button class="filter-more"><span>＋</span>更多筛选</button>', source)

    def test_static_identity_is_not_a_user_switcher(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("杨正月", source)
        self.assertIn("商务部", source)
        self.assertNotIn("林晓彤", source)


if __name__ == "__main__":
    unittest.main()
