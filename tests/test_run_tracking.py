from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from backend.adapters import NoticeData, NoticeSummary
from backend.config import settings
from backend.db import get_db, init_db
from backend.service import BEIJING_TZ, run_crawl, try_create_run
from backend.stabilization_migration import migrate_stabilization_schema


class RunTrackingTests(unittest.TestCase):
    def test_list_retry_snapshot_heartbeat_and_stop_reason(self) -> None:
        original = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                migrate_stabilization_schema()

                class FlakyListAdapter:
                    def __init__(self):
                        self.list_attempts = 0

                    def set_request_interval(self, value):
                        self.interval = value

                    def list_notices(self, max_pages, max_notices, exclude_external_ids=None):
                        self.list_attempts += 1
                        if self.list_attempts == 1:
                            raise RuntimeError("temporary list failure")
                        published = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
                        return [NoticeSummary("tracked-1", "储能采购招标公告", "https://example.com/tracked-1", published, "招标公告")]

                    def fetch_notice(self, url, external_id=None, detail_id=None):
                        published = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
                        return NoticeData(str(external_id), "储能采购招标公告", url, "本项目采购储能设备。", published_at=published, notice_type="招标公告")

                    def close(self):
                        return None

                with get_db() as connection:
                    job_id = connection.execute("SELECT id FROM crawl_jobs ORDER BY id LIMIT 1").fetchone()["id"]
                    connection.execute("UPDATE crawl_jobs SET max_notices=1,lookback_days=1,retry_json=? WHERE id=?", ('{"max_attempts":2}', job_id))
                run_id = try_create_run(job_id)
                adapter = FlakyListAdapter()
                with patch("backend.service.make_adapter", return_value=adapter), patch("backend.service.time.sleep", return_value=None):
                    run_crawl(job_id, run_id)
                with get_db() as connection:
                    run = connection.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
                    retries = connection.execute("SELECT COUNT(*) FROM system_logs WHERE crawl_run_id=? AND event_type='crawl.retry'", (run_id,)).fetchone()[0]
                self.assertEqual(adapter.list_attempts, 2)
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["stop_reason"], "target_reached")
                self.assertIsNotNone(run["heartbeat_at"])
                self.assertEqual(json.loads(run["config_snapshot_json"])["max_notices"], 1)
                self.assertEqual(retries, 1)
            finally:
                settings.data_dir = original


if __name__ == "__main__":
    unittest.main()
