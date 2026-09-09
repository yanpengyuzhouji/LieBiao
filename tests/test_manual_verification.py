from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend.manual_verification import _cookie_header
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import complete_site_verification
from backend.main import app
from fastapi.testclient import TestClient


class ManualVerificationTests(unittest.TestCase):
    def test_capture_uses_platform_tab_and_keeps_public_page_for_diagnosis(self):
        from backend.manual_verification import BrowserSession, complete_verification
        process = unittest.mock.Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            session = BrowserSession(12345, process, 'ec.chng.com.cn', Path(folder))
            ws = unittest.mock.MagicMock()
            ws.__enter__.return_value = ws
            ws.recv.side_effect = [json.dumps(item) for item in (
                {'id': 3, 'result': {'result': {'value': '{"title":"招标公告","links":[]}'}}},
                {'id': 1, 'result': {'cookies': [{'domain': 'ec.chng.com.cn', 'name': 'session', 'value': 'test'}]}},
                {'id': 2, 'result': {'result': {'value': 'Mozilla/5.0 Test'}}},
            )]
            targets = [
                {'type': 'page', 'url': 'https://example.com', 'webSocketDebuggerUrl': 'ws://wrong'},
                {'type': 'page', 'url': 'https://ec.chng.com.cn/channel/home/', 'webSocketDebuggerUrl': 'ws://right'},
            ]
            with patch('backend.manual_verification._sessions', {1: session}), patch('backend.manual_verification.httpx.get') as getter, patch('backend.manual_verification.connect', return_value=ws) as connector:
                getter.return_value.json.return_value = targets
                self.assertIn('session=test', complete_verification(1))
                self.assertEqual(connector.call_args.args[0], 'ws://right')
                saved = json.loads((Path(folder) / 'last-public-page.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['title'], '招标公告')

    def test_yfb_verification_captures_enterprise_host_session(self):
        from backend.manual_verification import BrowserSession, complete_verification
        process = unittest.mock.Mock()
        process.poll.return_value = None
        session = BrowserSession(12345, process, 'qiye.qianlima.com')
        ws = unittest.mock.MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.side_effect = [json.dumps(item) for item in (
            {'id': 1, 'result': {'cookies': [
                {'domain': '.qianlima.com', 'name': 'enterprise_session', 'value': 'signed-in'},
            ]}},
            {'id': 2, 'result': {'result': {'value': 'Mozilla/5.0 Test'}}},
            {'id': 3, 'result': {'result': {'value': '{"title":"信息中心"}'}}},
        )]
        targets = [{
            'type': 'page',
            'url': 'https://qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/search',
            'webSocketDebuggerUrl': 'ws://enterprise',
        }]
        with patch('backend.manual_verification._sessions', {9: session}), \
                patch('backend.manual_verification.httpx.get') as getter, \
                patch('backend.manual_verification.connect', return_value=ws) as connector:
            getter.return_value.json.return_value = targets
            credential = complete_verification(9)
        self.assertIn('enterprise_session=signed-in', credential)
        self.assertEqual(connector.call_args.args[0], 'ws://enterprise')

    def test_verification_reconnects_to_persisted_edge_port(self):
        from backend.manual_verification import complete_verification
        ws = unittest.mock.MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.side_effect = [json.dumps(item) for item in (
            {'id': 1, 'result': {'cookies': [
                {'domain': '.chng.com.cn', 'name': 'session', 'value': 'reconnected'},
            ]}},
            {'id': 2, 'result': {'result': {'value': 'Mozilla/5.0 Test'}}},
            {'id': 3, 'result': {'result': {'value': '{"title":"华能电子商务平台"}'}}},
        )]
        targets = [{
            'type': 'page',
            'url': 'https://ec.chng.com.cn/channel/home/#/purchase?top=0',
            'webSocketDebuggerUrl': 'ws://reconnected',
        }]
        with patch('backend.manual_verification._sessions', {}), \
                patch('backend.manual_verification.httpx.get') as getter, \
                patch('backend.manual_verification.connect', return_value=ws) as connector:
            getter.return_value.json.return_value = targets
            credential = complete_verification(257, port=43210)
        self.assertIn('session=reconnected', credential)
        self.assertIn('__scout_browser_port=43210', credential)
        self.assertEqual(connector.call_args.args[0], 'ws://reconnected')

    def test_open_verification_routes_all_platforms_without_server_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'data_dir', Path(folder)):
            init_db()
            with get_db() as connection:
                sites = connection.execute('SELECT id,code,base_url FROM sites').fetchall()
            client = TestClient(app)
            for site in sites:
                with self.subTest(code=site['code']), patch('backend.main.open_verification', return_value={'opened': True}) as opener:
                    response = client.post(f"/api/sites/{site['id']}/manual-verification/open")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()['opened'])
                    self.assertEqual(opener.call_args.args[0], site['id'])
                    self.assertTrue(opener.call_args.args[1].startswith('https://'))
                    if site['code'] == 'chng':
                        self.assertEqual(opener.call_args.args[1], 'https://ec.chng.com.cn/channel/home/#/purchase?top=0')
                    if site['code'] == 'yfb':
                        self.assertEqual(opener.call_args.args[1], 'https://qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/search')

    def test_failed_collection_check_keeps_verification_window_open(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'data_dir', Path(folder)):
            init_db()
            with get_db() as connection:
                site_id = connection.execute("SELECT id FROM sites WHERE code='chng'").fetchone()[0]
            adapter = unittest.mock.Mock()
            adapter.health_check.return_value = {'ok': False, 'message': '列表触发平台安全验证'}
            with patch('backend.main.complete_verification', return_value='session=test'), patch('backend.main.make_adapter', return_value=adapter), patch('backend.main.close_verification') as closer:
                response = TestClient(app).post(f'/api/sites/{site_id}/manual-verification/complete')
                self.assertEqual(response.status_code, 400)
                closer.assert_not_called()

    def test_cookie_header_only_keeps_target_platform(self) -> None:
        cookies = [
            {"name": "session", "value": "ok", "domain": ".cdt-ec.com"},
            {"name": "token", "value": "yes", "domain": "tang.cdt-ec.com"},
            {"name": "foreign", "value": "no", "domain": ".example.com"},
        ]
        self.assertEqual(_cookie_header(cookies, "tang.cdt-ec.com"), "session=ok; token=yes")

    def test_public_platform_without_cookie_is_reported_as_no_verification_needed(self) -> None:
        original = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                with get_db() as connection:
                    site_id = connection.execute("SELECT id FROM sites WHERE code='csg'").fetchone()[0]
                adapter = unittest.mock.Mock()
                adapter.health_check.return_value = {"ok": True, "status_code": 200, "message": "公开公告列表正常"}
                from backend.manual_verification import ManualVerificationError
                with patch("backend.main.complete_verification", side_effect=ManualVerificationError("未读取到平台会话")), patch("backend.main.make_adapter", return_value=adapter), patch("backend.main.close_verification"):
                    result = complete_site_verification(site_id)
                self.assertTrue(result["ok"])
                self.assertEqual(result["mode"], "public")
                self.assertIn("无需人工验证", result["message"])
                with get_db() as connection:
                    site = connection.execute("SELECT health_status,health_message FROM sites WHERE id=?", (site_id,)).fetchone()
                    self.assertEqual(site["health_status"], "healthy")
                    self.assertIn("无需人工验证", site["health_message"])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM site_accounts WHERE site_id=?", (site_id,)).fetchone()[0], 0)
            finally:
                settings.data_dir = original


if __name__ == "__main__":
    unittest.main()
