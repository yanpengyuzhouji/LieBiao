import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.config import settings
from backend.db import get_db, init_db
from backend.adapters import NoticeData
from backend.service import ingest_notice_data, create_attachment, process_local_attachment
from backend.parsers import DocumentResult
from backend.migration import migrate_storage
from backend.maintenance import activity
from backend import reparse_tasks


class ReparseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.old = settings.data_dir, settings.config_dir
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        settings.data_dir, settings.config_dir = self.root / 'source', self.root / 'config'
        init_db()
        with get_db() as c:
            self.notice = ingest_notice_data(c, None, NoticeData('test','测试公告','manual://test','测试正文'), source_type='file_import',download_attachments=False)

    def tearDown(self):
        settings.data_dir, settings.config_dir = self.old
        self.temp.cleanup()

    def test_parse_releases_writer_and_preserves_previous_text_on_failure(self):
        path = settings.data_dir / 'sample.txt'
        path.write_text('previous', encoding='utf-8')
        with get_db() as c:
            aid = create_attachment(c,self.notice,'sample.txt',None)
            process_local_attachment(c,self.notice,aid,path,'sample.txt')
        def failed_parse(_):
            with get_db() as writer:
                writer.execute("UPDATE notices SET business_mark='focus' WHERE id=?", (self.notice,))
            return DocumentResult(status='failed',error='injected failure')
        with get_db() as c, patch('backend.service.parse_document',side_effect=failed_parse):
            c.execute("UPDATE attachments SET status='stored' WHERE id=?",(aid,))
            process_local_attachment(c,self.notice,aid,path,'sample.txt')
        with get_db() as c:
            self.assertEqual(c.execute('SELECT text_content FROM extracted_documents WHERE attachment_id=?',(aid,)).fetchone()[0],'previous')
            self.assertEqual(c.execute('SELECT business_mark FROM notices WHERE id=?',(self.notice,)).fetchone()[0],'focus')

    def test_task_deduplication_and_restart_recovery(self):
        first,created = reparse_tasks.queue(self.notice)
        self.assertTrue(created)
        self.assertEqual(reparse_tasks.queue(self.notice),(first,False))
        reparse_tasks.initialize(recover=True)
        self.assertEqual(reparse_tasks.get(first)['status'],'failed')
        new,_ = reparse_tasks.queue(self.notice)
        reparse_tasks.execute(new)
        self.assertEqual(reparse_tasks.get(new)['status'],'completed')

    def test_migration_busy_guard_and_consistent_copy(self):
        source=settings.data_dir
        (source/'file.txt').write_text('data',encoding='utf-8')
        target=self.root/'destination'
        with activity(), self.assertRaises(RuntimeError):
            migrate_storage(target)
        migrate_storage(target)
        self.assertEqual(settings.data_dir,target)
        self.assertTrue((source/'scout.db').exists())
        self.assertEqual((target/'file.txt').read_text(encoding='utf-8'),'data')
        with get_db() as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertIsNotNone(c.execute('SELECT id FROM notices WHERE id=?',(self.notice,)).fetchone())

    def test_copy_failure_does_not_switch_source(self):
        source=settings.data_dir
        (source/'file.txt').write_text('data',encoding='utf-8')
        with patch('backend.migration.shutil.copy2',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                migrate_storage(self.root/'destination')
        self.assertEqual(settings.data_dir,source)

    def test_nonempty_destination_is_preserved(self):
        target=self.root/'existing'; target.mkdir()
        existing=target/'keep.txt'; existing.write_text('keep',encoding='utf-8')
        with self.assertRaises(ValueError):
            migrate_storage(target)
        self.assertEqual(existing.read_text(encoding='utf-8'),'keep')
