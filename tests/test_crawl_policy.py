from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.adapters import NoticeData, NoticeSummary
from backend.config import settings
from backend.db import get_db, init_db
from backend.service import (
    BEIJING_TZ,
    lookback_cutoff,
    notice_policy_rejection,
    parse_notice_datetime,
    run_crawl,
    try_create_run,
)


class CrawlPolicyUnitTests(unittest.TestCase):
    def test_lookback_uses_beijing_calendar_day_boundary(self) -> None:
        reference = datetime(2026, 9, 4, 15, 30, tzinfo=BEIJING_TZ)
        self.assertEqual(lookback_cutoff(0, reference), datetime(2026, 9, 4, 0, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(lookback_cutoff(1, reference), datetime(2026, 9, 3, 0, 0, tzinfo=BEIJING_TZ))

    def test_source_dates_are_normalized_and_strictly_checked(self) -> None:
        cutoff = datetime(2026, 9, 3, 0, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(parse_notice_datetime("2026年9月3日 08:30").strftime("%Y-%m-%d %H:%M"), "2026-09-03 08:30")
        self.assertIsNone(notice_policy_rejection("2026-09-03 00:00", "招标公告", cutoff, {"招标公告"}))
        self.assertIn("早于回溯边界", notice_policy_rejection("2026-09-02 23:59", "招标公告", cutoff, {"招标公告"}) or "")
        self.assertIn("缺少可解析", notice_policy_rejection(None, "招标公告", cutoff, {"招标公告"}) or "")
        self.assertIn("不在任务范围", notice_policy_rejection("2026-09-04", "采购公告", cutoff, {"招标公告"}) or "")


class CrawlPolicyIntegrationTests(unittest.TestCase):
    def test_run_crawl_enforces_policy_and_retry_before_ingest(self) -> None:
        original_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                now = datetime.now(BEIJING_TZ)
                recent = now.strftime("%Y-%m-%d %H:%M")
                old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")

                class FakeAdapter:
                    def __init__(self) -> None:
                        self.fetch_attempts: dict[str, int] = {}

                    def list_notices(self, max_pages, max_notices, exclude_external_ids=None):
                        del max_pages, max_notices, exclude_external_ids
                        return [
                            NoticeSummary("old", "过期储能公告", "https://example.com/old", old, "招标公告"),
                            NoticeSummary("recent", "近期储能公告", "https://example.com/recent", recent, "招标公告"),
                            NoticeSummary("retry", "重试后成功公告", "https://example.com/retry", recent, "招标公告"),
                            NoticeSummary("unknown", "无发布时间公告", "https://example.com/unknown", None, "招标公告"),
                            NoticeSummary("wrong-type", "类型不符公告", "https://example.com/wrong", recent, "采购公告"),
                        ]

                    def fetch_notice(self, url, external_id=None, detail_id=None):
                        del url, detail_id
                        key = str(external_id)
                        self.fetch_attempts[key] = self.fetch_attempts.get(key, 0) + 1
                        if key == "retry" and self.fetch_attempts[key] == 1:
                            raise RuntimeError("temporary failure")
                        publication = None if key == "unknown" else recent
                        return NoticeData(
                            external_id=key,
                            title=f"{key} 储能设备采购招标公告",
                            url=f"https://example.com/{key}",
                            body_text="本项目采购储能设备，采用公开招标方式。",
                            published_at=publication,
                            notice_type="招标公告",
                        )

                    def close(self) -> None:
                        return None

                with get_db() as connection:
                    job = connection.execute("SELECT id FROM crawl_jobs ORDER BY id LIMIT 1").fetchone()
                    self.assertIsNotNone(job)
                    job_id = int(job["id"])
                    connection.execute(
                        "UPDATE crawl_jobs SET lookback_days=1,categories_json=?,max_notices=10,interval_ms=200,retry_json=? WHERE id=?",
                        ('["招标公告"]', '{"max_attempts":2}', job_id),
                    )
                run_id = try_create_run(job_id)
                self.assertIsNotNone(run_id)
                adapter = FakeAdapter()
                with patch("backend.service.make_adapter", return_value=adapter), patch("backend.service.time.sleep", return_value=None):
                    run_crawl(job_id, int(run_id))

                with get_db() as connection:
                    run = connection.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
                    stored = connection.execute("SELECT external_id FROM notices ORDER BY external_id").fetchall()
                    policy_logs = connection.execute(
                        "SELECT COUNT(*) FROM system_logs WHERE crawl_run_id=? AND event_type='crawl.policy_filter'",
                        (run_id,),
                    ).fetchone()[0]
                    retry_logs = connection.execute(
                        "SELECT COUNT(*) FROM system_logs WHERE crawl_run_id=? AND event_type='crawl.retry'",
                        (run_id,),
                    ).fetchone()[0]

                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["discovered"], 5)
                self.assertEqual(run["detail_success"], 2)
                self.assertEqual(run["filtered_count"], 3)
                self.assertEqual(run["failed_count"], 0)
                self.assertEqual([row["external_id"] for row in stored], ["recent", "retry"])
                self.assertNotIn("old", adapter.fetch_attempts)
                self.assertNotIn("wrong-type", adapter.fetch_attempts)
                self.assertEqual(adapter.fetch_attempts["retry"], 2)
                self.assertEqual(policy_logs, 3)
                self.assertEqual(retry_logs, 1)
            finally:
                settings.data_dir = original_data_dir


if __name__ == "__main__":
    unittest.main()
