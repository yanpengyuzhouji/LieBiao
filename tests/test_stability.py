from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.adapters import NoticeData
from backend.config import settings
from backend.db import get_db, init_db, now_iso
from backend.main import list_notices
from backend.service import attachment_tree_needs_reparse, create_attachment, find_site, ingest_notice_data, refresh_notice_analysis, try_create_run
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

    def test_manual_import_promotes_existing_crawl_notice(self) -> None:
        with get_db() as connection:
            notice = NoticeData("manual-1", "手工导入公告", "https://www.bidding.csg.cn/zbgg/manual-1.jhtml", "正文")
            notice_id = ingest_notice_data(connection, 1, notice, source_type="crawl", download_attachments=False)
            ingest_notice_data(connection, 1, notice, source_type="url_import", download_attachments=False)
            self.assertEqual(connection.execute("SELECT source_type FROM notices WHERE id=?", (notice_id,)).fetchone()["source_type"], "url_import")

    def test_cached_archive_retries_failed_or_legacy_doc_children(self) -> None:
        with get_db() as connection:
            notice_id = ingest_notice_data(connection, 1, NoticeData("cache-1", "缓存公告", "https://example.com/cache-1", "正文"), download_attachments=False)
            parent = create_attachment(connection, notice_id, "附件.zip", "https://example.com/a.zip", "extracted")
            connection.execute("UPDATE attachments SET parse_status='parsed' WHERE id=?", (parent,))
            child = create_attachment(connection, notice_id, "旧文件.doc", None, "stored", parent)
            connection.execute("UPDATE attachments SET parse_status='unsupported' WHERE id=?", (child,))
            self.assertTrue(attachment_tree_needs_reparse(connection, parent))
            connection.execute("UPDATE attachments SET parse_status='parsed' WHERE id=?", (child,))
            self.assertFalse(attachment_tree_needs_reparse(connection, parent))

    def test_site_matching_rejects_lookalike_and_unknown_hosts(self) -> None:
        with get_db() as connection:
            self.assertIsNotNone(find_site(connection, "https://www.bidding.csg.cn/zbgg/1.jhtml"))
            self.assertIsNone(find_site(connection, "https://www.bidding.csg.cn.example.com/zbgg/1.jhtml"))
            self.assertIsNone(find_site(connection, "https://example.com/notice"))

    def test_only_unmatched_notice_with_parse_issue_is_shown_in_recovery_filter(self) -> None:
        with get_db() as connection:
            parsed_id = ingest_notice_data(connection, 1, NoticeData("hidden-1", "完整未命中公告", "https://example.com/hidden-1", "普通正文"), source_type="crawl", download_attachments=False)
            issue_id = ingest_notice_data(connection, 1, NoticeData("hidden-2", "异常未命中公告", "https://example.com/hidden-2", "普通正文"), source_type="crawl", download_attachments=False)
            create_attachment(connection, issue_id, "待解析附件.doc", "https://example.com/pending.doc")
        self.assertEqual(list_notices(q="未命中", limit=10, offset=0)["total"], 0)
        recovered = list_notices(q="未命中", only_matched=False, only_unmatched=True, limit=10, offset=0)
        self.assertEqual(recovered["total"], 1)
        self.assertEqual(recovered["category_counts"]["unmatched"], 1)
        self.assertEqual(recovered["items"][0]["id"], str(issue_id))
        self.assertNotEqual(recovered["items"][0]["id"], str(parsed_id))


if __name__ == "__main__":
    unittest.main()
