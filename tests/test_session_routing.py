import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.adapters import NoticeData
from backend.config import settings
from backend.db import get_db, init_db, now_iso, select_site_account
from backend.service import ingest_url, run_crawl, try_create_run


class SessionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original = settings.data_dir
        self.folder = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.folder.name)
        init_db()
        with get_db() as db:
            self.site_id = db.execute("SELECT id FROM sites WHERE code='csg'").fetchone()[0]
            self.job_id = db.execute('SELECT id FROM crawl_jobs LIMIT 1').fetchone()[0]
            self.account_id = db.execute("INSERT INTO site_accounts(site_id,alias,credential_ref,session_status,created_at) VALUES(?,'测试会话','session=test-only','verified',?)", (self.site_id, now_iso())).lastrowid

    def tearDown(self):
        settings.data_dir = self.original
        self.folder.cleanup()

    def test_unbound_task_uses_verified_platform_session(self):
        adapter = Mock()
        adapter.list_notices.return_value = []
        with patch('backend.service.make_adapter', return_value=adapter) as factory:
            run_crawl(self.job_id, try_create_run(self.job_id))
        self.assertEqual(factory.call_args.kwargs.get('session_cookie'), 'session=test-only')

    def test_url_import_uses_same_verified_session(self):
        adapter = Mock()
        adapter.fetch_notice.return_value = NoticeData('test', '设备招标公告', 'https://www.bidding.csg.cn/zbgg/test.jhtml', '招标正文')
        with patch('backend.service.make_adapter', return_value=adapter) as factory:
            ingest_url('https://www.bidding.csg.cn/zbgg/test.jhtml')
        self.assertEqual(factory.call_args.kwargs.get('session_cookie'), 'session=test-only')

    def test_bound_disabled_account_is_not_used(self):
        with get_db() as db:
            db.execute('UPDATE crawl_jobs SET account_id=? WHERE id=?', (self.account_id, self.job_id))
            db.execute('UPDATE site_accounts SET enabled=0 WHERE id=?', (self.account_id,))
        with patch('backend.service.make_adapter') as factory:
            run_crawl(self.job_id, try_create_run(self.job_id))
        factory.assert_not_called()

    def test_other_platform_account_never_leaks_into_task(self):
        with get_db() as db:
            other = db.execute("SELECT id FROM sites WHERE code='ecp'").fetchone()[0]
            db.execute('UPDATE site_accounts SET site_id=? WHERE id=?', (other, self.account_id))
            db.execute('UPDATE crawl_jobs SET account_id=? WHERE id=?', (self.account_id, self.job_id))
        with patch('backend.service.make_adapter') as factory:
            run_crawl(self.job_id, try_create_run(self.job_id))
        factory.assert_not_called()

    def test_expired_or_malformed_expiration_is_not_reused(self):
        with get_db() as db:
            for expires in ('2020-01-01T00:00:00Z', 'invalid-date'):
                db.execute('UPDATE site_accounts SET expires_at=? WHERE id=?', (expires, self.account_id))
                self.assertIsNone(select_site_account(db, self.site_id))
                self.assertIsNone(select_site_account(db, self.site_id, self.account_id))

    def test_disabled_explicit_binding_never_falls_back_to_another_account(self):
        with get_db() as db:
            db.execute('UPDATE site_accounts SET enabled=0 WHERE id=?', (self.account_id,))
            db.execute("INSERT INTO site_accounts(site_id,alias,credential_ref,session_status,created_at) VALUES(?,'另一会话','second=test-only','verified',?)", (self.site_id, now_iso()))
            self.assertIsNone(select_site_account(db, self.site_id, self.account_id))
            self.assertEqual(select_site_account(db, self.site_id)['credential_ref'], 'second=test-only')
