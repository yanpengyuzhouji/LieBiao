from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.adapters import NoticeData
from backend.config import settings
from backend.db import get_db, init_db, now_iso
from backend.main import app, list_notices, site_health_check
from backend.parsers import DocumentResult
from backend.service import attachment_tree_needs_reparse, create_attachment, find_site, ingest_notice_data, keyword_locator_url, permanently_delete_notices, process_local_attachment, refresh_notice_analysis, try_create_run
from backend.stabilization_migration import migrate_stabilization_schema
from backend.storage import save_raw_html


class StabilityIntegrationTests(unittest.TestCase):
    def test_yfb_keyword_locator_uses_member_route_and_native_highlight(self) -> None:
        old = keyword_locator_url(
            "yfb", "https://www.yfbzb.com/inviteBid/detail/20260911_630083453.html",
            "630083453", "储能系统",
        )
        self.assertIn("qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/infoDetail/630083453/1/zhaobiao", old)
        self.assertIn("searchKeyWord=%E5%82%A8%E8%83%BD%E7%B3%BB%E7%BB%9F", old)
        current = keyword_locator_url(
            "yfb", "https://qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/infoDetail/1/2831/zhaobiao?fromPage=searchPage",
            "1", "电池",
        )
        self.assertIn("/1/2831/zhaobiao?fromPage=searchPage&searchKeyWord=", current)

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

    def test_attachment_removed_while_parser_runs_does_not_write_stale_foreign_key(self) -> None:
        path = settings.temp_dir / "concurrent.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("附件内容", encoding="utf-8")
        with get_db() as connection:
            notice_id = ingest_notice_data(
                connection, 1, NoticeData("concurrent-attachment", "并发附件公告", "https://example.com/concurrent", "正文"),
                download_attachments=False,
            )
            attachment_id = create_attachment(connection, notice_id, "concurrent.txt", None, "stored")

            def remove_during_parse(_path):
                connection.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
                connection.commit()
                return DocumentResult(text="附件内容", parser="text", status="parsed")

            with patch("backend.service.parse_document", side_effect=remove_during_parse):
                self.assertEqual(process_local_attachment(connection, notice_id, attachment_id, path, path.name), [])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

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
            visible_id = ingest_notice_data(connection, 1, NoticeData("visible-1", "人工公告", "https://example.com/visible-1", "普通正文"), source_type="file_import", download_attachments=False)
            connection.execute("UPDATE notices SET business_mark='focus' WHERE id=?", (visible_id,))
        self.assertEqual(list_notices(q="未命中", limit=10, offset=0)["total"], 0)
        normal_counts = list_notices(limit=10, offset=0)["category_counts"]
        recovered = list_notices(q="未命中", only_matched=False, only_unmatched=True, limit=10, offset=0)
        self.assertEqual(recovered["total"], 1)
        self.assertEqual(recovered["category_counts"]["unmatched"], 1)
        self.assertEqual(recovered["items"][0]["id"], str(issue_id))
        self.assertNotEqual(recovered["items"][0]["id"], str(parsed_id))
        unsearched_recovery = list_notices(only_matched=False, only_unmatched=True, limit=10, offset=0)
        self.assertEqual(unsearched_recovery["category_counts"]["all"], normal_counts["all"])
        self.assertEqual(unsearched_recovery["category_counts"]["pending"], normal_counts["pending"])
        self.assertEqual(unsearched_recovery["category_counts"]["focus"], normal_counts["focus"])

    def test_collection_warning_without_bad_attachment_is_not_a_parse_issue(self) -> None:
        with get_db() as connection:
            notice_id = ingest_notice_data(
                connection, 1,
                NoticeData(
                    "warning-only", "会员内容受限公告", "https://example.com/warning-only", "公开正文",
                    collection_warning="部分正文受会员权限限制",
                ),
                source_type="file_import", download_attachments=False,
            )
        issues = list_notices(only_issues=True, limit=100, offset=0)
        self.assertNotIn(str(notice_id), {item["id"] for item in issues["items"]})

    def test_today_new_uses_ingest_time_not_publication_time(self) -> None:
        with get_db() as connection:
            today_id = ingest_notice_data(connection, 1, NoticeData("today-new", "今日入库旧发布时间公告", "https://example.com/today-new", "正文"), source_type="file_import", download_attachments=False)
            old_id = ingest_notice_data(connection, 1, NoticeData("old-new", "昨日入库公告", "https://example.com/old-new", "正文"), source_type="file_import", download_attachments=False)
            connection.execute("UPDATE notices SET created_at='2026-09-10T15:00:00+00:00' WHERE id=?", (old_id,))
        result = list_notices(only_today_new=True, only_matched=False, limit=10, offset=0)
        self.assertIn(str(today_id), {item["id"] for item in result["items"]})
        self.assertNotIn(str(old_id), {item["id"] for item in result["items"]})

    def test_generic_tender_template_is_ignored_without_running_word(self) -> None:
        path = settings.temp_dir / "template.doc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"legacy word placeholder")
        with get_db() as connection:
            notice_id = ingest_notice_data(
                connection, 1, NoticeData("template-ignore", "模板测试公告", "https://example.com/template", "正文"),
                download_attachments=False,
            )
            attachment_id = create_attachment(connection, notice_id, "电科院项目-招文修改建议.doc", None, "stored")
            with patch("backend.service.parse_document") as parser:
                process_local_attachment(connection, notice_id, attachment_id, path, "文件编制模板/电科院项目-招文修改建议.doc")
            parser.assert_not_called()
            row = connection.execute("SELECT status,parse_status,error_message FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            self.assertEqual(tuple(row), ("stored", "ignored", None))

    def test_ocr_runs_only_when_explicitly_enabled(self) -> None:
        from backend.parsers import parse_pdf
        path = settings.temp_dir / "scan.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        import fitz
        document = fitz.open()
        document.new_page()
        document.save(path)
        document.close()
        with patch("backend.ocr.parse_scanned_pdf", return_value=DocumentResult(text="OCR文字", parser="ppocr-v6-small", status="parsed")) as ocr:
            self.assertEqual(parse_pdf(path).status, "ocr_pending")
            self.assertEqual(parse_pdf(path, enable_ocr=True).text, "OCR文字")
            ocr.assert_called_once_with(path)

    def test_recycle_bin_lists_deleted_notice_and_crawl_cannot_restore_it(self) -> None:
        with get_db() as connection:
            group = connection.execute("SELECT id FROM keyword_groups ORDER BY id LIMIT 1").fetchone()["id"]
            notice = NoticeData("trash-1", "储能采购招标公告", "https://example.com/trash-1", "采购储能设备。")
            notice_id = ingest_notice_data(connection, 1, notice, keyword_group_id=group, download_attachments=False)
            deleted_at = now_iso()
            connection.execute("UPDATE notices SET deleted_at=? WHERE id=?", (deleted_at, notice_id))
            result = ingest_notice_data(connection, 1, notice, keyword_group_id=group, download_attachments=False, source_type="crawl")
            row = connection.execute("SELECT deleted_at FROM notices WHERE id=?", (notice_id,)).fetchone()
        self.assertIsNone(result)
        self.assertEqual(row["deleted_at"], deleted_at)
        trash = list_notices(only_deleted=True, only_matched=False, limit=10, offset=0)
        self.assertEqual(trash["total"], 1)
        self.assertEqual(trash["category_counts"]["trash"], 1)
        self.assertEqual(trash["items"][0]["id"], str(notice_id))

    def test_permanent_delete_removes_files_and_blocks_reingest(self) -> None:
        with get_db() as connection:
            notice_id = ingest_notice_data(
                connection, 1,
                NoticeData("purge-1", "待删除的储能招标公告", "https://example.com/purge-1", "正文"),
                download_attachments=False,
            )
            raw = save_raw_html(notice_id, "raw")
            extracted = settings.extracted_dir / "notices" / str(notice_id) / "parsed.txt"
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_text("parsed", encoding="utf-8")
            attachment_id = create_attachment(connection, notice_id, "技术要求.pdf", None)
            connection.execute(
                "UPDATE attachments SET relative_path=?,sha256=?,status='stored' WHERE id=?",
                (str(Path(raw).parent / "attachments" / "技术要求.pdf"), "attachment-hash", attachment_id),
            )
            connection.execute("UPDATE notices SET deleted_at=? WHERE id=?", (now_iso(), notice_id))
            self.assertEqual(permanently_delete_notices(connection, [notice_id]), 1)
            self.assertIsNone(connection.execute("SELECT id FROM notices WHERE id=?", (notice_id,)).fetchone())
            self.assertIsNone(connection.execute("SELECT id FROM attachments WHERE id=?", (attachment_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM deleted_notice_tombstones WHERE external_id='purge-1'").fetchone())
        self.assertFalse((settings.data_dir / raw).exists())
        self.assertFalse(extracted.exists())
        with get_db() as connection:
            self.assertIsNone(ingest_notice_data(connection, 1, NoticeData("purge-1", "待删除的储能招标公告", "https://example.com/purge-1", "正文"), download_attachments=False))

    def test_permanent_delete_requires_trash_state(self) -> None:
        with get_db() as connection:
            notice_id = ingest_notice_data(connection, 1, NoticeData("purge-2", "普通公告", "https://example.com/purge-2", "正文"), download_attachments=False)
            self.assertEqual(permanently_delete_notices(connection, [notice_id]), 0)
            self.assertIsNotNone(connection.execute("SELECT id FROM notices WHERE id=?", (notice_id,)).fetchone())

    def test_empty_trash_physically_deletes_all_trash_rows(self) -> None:
        with get_db() as connection:
            for index in range(2):
                notice_id = ingest_notice_data(
                    connection, 1,
                    NoticeData(f"clear-{index}", f"待清空公告{index}", f"https://example.com/clear-{index}", "正文"),
                    download_attachments=False,
                )
                connection.execute("UPDATE notices SET deleted_at=? WHERE id=?", (now_iso(), notice_id))
        response = TestClient(app).post("/api/notices/trash/empty")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted"], 2)
        with get_db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notices WHERE deleted_at IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM deleted_notice_tombstones WHERE external_id LIKE 'clear-%'").fetchone()[0], 2)

    def test_health_check_reuses_verified_manual_session(self) -> None:
        with get_db() as connection:
            site = connection.execute("SELECT id FROM sites ORDER BY id LIMIT 1").fetchone()
            connection.execute(
                "INSERT INTO site_accounts(site_id,alias,login_mode,credential_ref,session_status,last_login_at,status_reason,enabled,created_at) VALUES(?,?,'manual_session',?,'verified',?,'人工验证成功',1,?)",
                (site["id"], "人工验证会话", "SESSION=verified", now_iso(), now_iso()),
            )

        class HealthyAdapter:
            def health_check(self):
                return {"ok": True, "status_code": 200, "message": "正常"}

            def close(self):
                return None

        with patch("backend.main.make_adapter", return_value=HealthyAdapter()) as factory:
            result = site_health_check(site["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(factory.call_args.kwargs["session_cookie"], "SESSION=verified")


if __name__ == "__main__":
    unittest.main()
