from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import get_db, init_db, now_iso
from backend.main import app
from backend.service import try_create_run


class RunAllJobsTests(unittest.TestCase):
    def test_runs_every_eligible_job_and_keeps_overrides_temporary(self) -> None:
        original = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    default = connection.execute("SELECT id,site_id,lookback_days,max_pages,max_notices FROM crawl_jobs LIMIT 1").fetchone()
                    second_id = connection.execute(
                        "INSERT INTO crawl_jobs(name,site_id,schedule_text,lookback_days,max_pages,max_notices,enabled,created_at) VALUES(?,?,'手动',2,6,80,1,?)",
                        ("第二个可运行任务", default["site_id"], now_iso()),
                    ).lastrowid
                    running_id = connection.execute(
                        "INSERT INTO crawl_jobs(name,site_id,schedule_text,enabled,created_at) VALUES(?,?,'手动',1,?)",
                        ("正在运行的任务", default["site_id"], now_iso()),
                    ).lastrowid
                    group_id = connection.execute(
                        "INSERT INTO keyword_groups(name,enabled,created_at,updated_at) VALUES(?,0,?,?)",
                        ("已停用测试规则", now_iso(), now_iso()),
                    ).lastrowid
                    connection.execute(
                        "INSERT INTO crawl_jobs(name,site_id,keyword_group_id,schedule_text,enabled,created_at) VALUES(?,?,?,'手动',1,?)",
                        ("规则停用的任务", default["site_id"], group_id, now_iso()),
                    )
                self.assertIsNotNone(try_create_run(int(running_id)))

                overrides = {"lookback_days": 7, "max_pages": 3, "max_notices": 20}
                with patch("backend.main.run_crawl") as crawler:
                    response = TestClient(app).post("/api/crawl-jobs/run-all", json=overrides)

                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual({item["job_id"] for item in result["started"]}, {int(default["id"]), int(second_id)})
                self.assertEqual({item["reason"] for item in result["skipped"]}, {"已有采集批次正在运行", "绑定的关键词组已停用"})
                self.assertEqual(crawler.call_count, 2)
                for call in crawler.call_args_list:
                    self.assertEqual(call.args[2], overrides)
                    self.assertTrue(call.args[3])
                with get_db() as connection:
                    saved = connection.execute(
                        "SELECT lookback_days,max_pages,max_notices FROM crawl_jobs WHERE id=?", (second_id,)
                    ).fetchone()
                self.assertEqual(tuple(saved), (2, 6, 80))
            finally:
                settings.data_dir = original

    def test_frontend_top_refresh_uses_batch_endpoint(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertIn("openConfig('run-all')", source)
        self.assertIn("/api/crawl-jobs/run-all", source)
        self.assertIn("使用各任务原配置", source)


if __name__ == "__main__":
    unittest.main()
