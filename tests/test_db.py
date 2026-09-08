from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from backend.adapters import NoticeData
from backend.config import settings
from backend.db import get_db, init_db
from backend.service import ingest_notice_data, rebuild_keyword_group_analysis


class SeedBehaviorTests(unittest.TestCase):
    def test_one_current_extracted_document_per_attachment(self) -> None:
        original_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    notice_id = ingest_notice_data(connection, 1, NoticeData("unique-doc", "测试公告", "https://example.com/unique-doc", "正文"), download_attachments=False)
                    attachment_id = connection.execute("INSERT INTO attachments(notice_id,name,status,created_at) VALUES(?,?,?,datetime('now'))", (notice_id, "a.docx", "stored")).lastrowid
                    connection.execute("INSERT INTO extracted_documents(attachment_id,status,created_at,updated_at) VALUES(?,?,datetime('now'),datetime('now'))", (attachment_id, "parsed"))
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("INSERT INTO extracted_documents(attachment_id,status,created_at,updated_at) VALUES(?,?,datetime('now'),datetime('now'))", (attachment_id, "parsed"))
            finally:
                settings.data_dir = original_data_dir

    def test_unmatched_crawl_candidate_is_not_persisted(self) -> None:
        original_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    group_id = connection.execute(
                        "SELECT id FROM keyword_groups WHERE name=?",
                        ("储能与新能源",),
                    ).fetchone()["id"]
                    result = ingest_notice_data(
                        connection,
                        1,
                        NoticeData(
                            external_id="unmatched-1",
                            title="办公用品采购招标公告",
                            url="https://example.com/unmatched-1",
                            body_text="办公用品采购招标公告",
                        ),
                        keyword_group_id=group_id,
                        download_attachments=False,
                        filter_unmatched=True,
                    )
                    self.assertIsNone(result)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0], 0)

                    result = ingest_notice_data(
                        connection,
                        1,
                        NoticeData(
                            external_id="matched-1",
                            title="储能设备采购招标公告",
                            url="https://example.com/matched-1",
                            body_text="本项目采购储能设备，采用公开招标方式。",
                        ),
                        keyword_group_id=group_id,
                        download_attachments=False,
                        filter_unmatched=True,
                    )
                    self.assertIsNotNone(result)
                    self.assertGreater(
                        connection.execute("SELECT COUNT(*) FROM keyword_hits WHERE notice_id=?", (result,)).fetchone()[0],
                        0,
                    )
            finally:
                settings.data_dir = original_data_dir

    def test_deleted_default_keyword_does_not_return_after_reinitialization(self) -> None:
        original_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT id FROM keyword_groups WHERE name=?",
                            ("储能与新能源",),
                        ).fetchone()
                    )
                    connection.execute("UPDATE crawl_jobs SET keyword_group_id=NULL")
                    connection.execute(
                        "DELETE FROM keyword_groups WHERE name=?",
                        ("储能与新能源",),
                    )

                init_db()
                with get_db() as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT id FROM keyword_groups WHERE name=?",
                            ("储能与新能源",),
                        ).fetchone()
                    )
            finally:
                settings.data_dir = original_data_dir


    def test_keyword_group_rebuild_removes_stale_hits(self) -> None:
        original_data_dir = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    group_id = connection.execute(
                        "SELECT id FROM keyword_groups ORDER BY id LIMIT 1"
                    ).fetchone()["id"]
                    notice_id = ingest_notice_data(
                        connection,
                        1,
                        NoticeData(
                            external_id="rebuild-1",
                            title="储能项目招标公告",
                            url="https://example.com/rebuild-1",
                            body_text="本项目采购储能设备。",
                        ),
                        keyword_group_id=group_id,
                        download_attachments=False,
                        filter_unmatched=True,
                    )
                    self.assertIsNotNone(notice_id)
                    self.assertGreater(
                        connection.execute(
                            "SELECT COUNT(*) FROM keyword_hits WHERE notice_id=?",
                            (notice_id,),
                        ).fetchone()[0],
                        0,
                    )

                    connection.execute(
                        "UPDATE keyword_groups SET include_any_json=?, include_all_json=?, phrases_json=? WHERE id=?",
                        ('["服务器"]', "[]", "[]", group_id),
                    )
                    result = rebuild_keyword_group_analysis(connection, group_id)
                    self.assertEqual(result["notice_count"], 1)
                    self.assertEqual(result["matched_notice_count"], 0)
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM keyword_hits WHERE notice_id=? AND keyword_group_id=?",
                            (notice_id, group_id),
                        ).fetchone()[0],
                        0,
                    )
            finally:
                settings.data_dir = original_data_dir


if __name__ == "__main__":
    unittest.main()
