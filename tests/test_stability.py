from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.adapters import NoticeData
from backend.config import settings
from backend.db import get_db, init_db, now_iso
from backend.service import create_attachment, ingest_notice_data, refresh_notice_analysis, try_create_run
from backend.stabilization_migration import migrate_stabilization_schema
from backend.storage import save_raw_html


class StabilityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_data_dir = settings.data_dir
        self.temp = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.temp.name)
        init_db()
        migrate_stabilization_schema()

    def tearDown(self) -> None:
        settings.data_dir = self.original_data_dir
        self.temp.cleanup()

    def test_group_refresh_preserves_other_group_evidence(self) -> None:
        with get_db() as connection:
            first = connection.execute("SELECT id FROM keyword_groups ORDER BY id LIMIT 1").fetchone()["id"]
            timestamp = now_iso()
            second = connection.execute(
                "INSERT INTO keyword_groups(name,include_any_json,created_at,updated_at) VALUES(?,?,?,?)",
                ("服务器", '["服务器"]', timestamp, timestamp),
            ).lastrowid
            notice_id = ingest_notice_data(
                connection, 1,
                NoticeData("stable-1", "储能服务器采购招标公告", "https://example.com/stable-1", "采购储能服务器设备。"),
                keyword_group_id=first, download_attachments=False,
            )
            refresh_notice_analysis(connection, notice_id, "储能服务器采购招标公告", "采购储能服务器设备。", second)
            groups = {row[0] for row in connection.execute("SELECT DISTINCT keyword_group_id FROM keyword_hits WHERE notice_id=? AND is_negative=0", (notice_id,))}
            self.assertEqual(groups, {first, second})

    def test_binding_is_recorded_for_task_run(self) -> None:
        with get_db() as connection:
            job = connection.execute("SELECT id,keyword_group_id FROM crawl_jobs ORDER BY id LIMIT 1").fetchone()
        run_id = try_create_run(job["id"])
        with get_db() as connection:
            notice_id = ingest_notice_data(
                connection, 1,
                NoticeData("stable-2", "储能采购招标公告", "https://example.com/stable-2", "本项目采购储能设备。"),
                keyword_group_id=job["keyword_group_id"], download_attachments=False,
                filter_unmatched=True, crawl_job_id=job["id"], crawl_run_id=run_id,
            )
            binding = connection.execute("SELECT * FROM notice_keyword_bindings WHERE notice_id=?", (notice_id,)).fetchone()
            self.assertEqual(binding["job_id"], job["id"])
            self.assertEqual(binding["last_run_id"], run_id)

    def test_existing_unmatched_notice_is_not_counted_as_success(self) -> None:
        with get_db() as connection:
            group = connection.execute("SELECT id FROM keyword_groups ORDER BY id LIMIT 1").fetchone()["id"]
            notice = NoticeData("stable-3", "储能采购招标公告", "https://example.com/stable-3", "本项目采购储能设备。")
            notice_id = ingest_notice_data(connection, 1, notice, keyword_group_id=group, download_attachments=False)
            connection.execute("UPDATE keyword_groups SET include_any_json=?,include_all_json=? WHERE id=?", ('["服务器"]', '[]', group))
            result = ingest_notice_data(connection, 1, notice, keyword_group_id=group, download_attachments=False, filter_unmatched=True)
            self.assertIsNone(result)
            self.assertIsNotNone(connection.execute("SELECT id FROM notices WHERE id=?", (notice_id,)).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM keyword_hits WHERE notice_id=? AND is_negative=0", (notice_id,)).fetchone()[0], 0)

    def test_attachment_identity_includes_parent_and_snapshots_are_versioned(self) -> None:
        with get_db() as connection:
            group = connection.execute("SELECT id FROM keyword_groups ORDER BY id LIMIT 1").fetchone()["id"]
            notice_id = ingest_notice_data(connection, 1, NoticeData("stable-4", "储能采购招标公告", "https://example.com/stable-4", "采购储能设备。"), keyword_group_id=group, download_attachments=False)
            parent1 = create_attachment(connection, notice_id, "一.zip", "https://example.com/1.zip")
            parent2 = create_attachment(connection, notice_id, "二.zip", "https://example.com/2.zip")
            child1 = create_attachment(connection, notice_id, "目录/清单.xlsx", None, parent_id=parent1)
            child2 = create_attachment(connection, notice_id, "目录/清单.xlsx", None, parent_id=parent2)
            self.assertNotEqual(child1, child2)
        first = save_raw_html(notice_id, "first")
        second = save_raw_html(notice_id, "second")
        self.assertNotEqual(first, second)
        self.assertTrue((settings.data_dir / first).exists())
        self.assertTrue((settings.data_dir / second).exists())


if __name__ == "__main__":
    unittest.main()
