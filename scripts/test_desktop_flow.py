"""Run browser acceptance against an isolated temporary database (requires Playwright)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import socket
import tempfile
import threading
import time
import urllib.request
import uvicorn
from playwright.sync_api import sync_playwright, expect
from backend.config import settings
from backend.db import init_db, get_db
from backend.adapters import NoticeData
from backend.service import ingest_notice_data
from backend.main import app
from backend.scheduler import scheduler


def main():
    original = settings.data_dir, settings.config_dir
    with tempfile.TemporaryDirectory() as folder:
        settings.data_dir = Path(folder) / 'data'
        settings.config_dir = Path(folder) / 'config'
        settings.load_persisted_data_dir = lambda: False
        scheduler.start = lambda: None
        init_db()
        with get_db() as c:
            for i in range(23):
                notice_id = ingest_notice_data(c,None,NoticeData(f'browser-{i}',f'浏览器验收公告 {i}','manual://browser/'+str(i),'正文'),source_type='file_import',download_attachments=False)
                if i == 0:
                    c.execute("UPDATE notices SET ingest_status='partial' WHERE id=?", (notice_id,))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True); thread.start()
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.1)
            assert server.started
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(); errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base); page.wait_for_load_state('networkidle')
                assert page.locator('#notice-tbody tr[data-row-id]').count()==10
                page.get_by_role('button',name='2',exact=True).click(); page.wait_for_load_state('networkidle')
                page.locator('#select-all').check()
                assert page.locator('.row-checkbox:checked').count()==10
                page.locator('#select-all').uncheck()
                assert page.locator('.row-checkbox:checked').count()==0
                page.locator('[data-filter-tab="focus"]').click(); page.wait_for_load_state('networkidle')
                expect(page.locator('#notice-tbody tr[data-row-id]')).to_have_count(0)
                page.locator('[data-filter-tab="issues"]').click(); page.wait_for_load_state('networkidle')
                expect(page.locator('#notice-tbody tr[data-row-id]')).to_have_count(1)
                page.locator('[data-filter-tab="pending"]').click(); page.wait_for_load_state('networkidle')
                expect(page.locator('#notice-tbody tr[data-row-id]')).to_have_count(10)
                page.locator('[data-filter-tab="all"]').click(); page.wait_for_load_state('networkidle')
                page.locator('.notice-title').first.click()
                page.locator('[data-reparse-notice]').click()
                page.wait_for_function('activeReparseWatchers.size === 0 && document.querySelector("#toast-region").textContent.includes("重新解析完成")',timeout=15000)
                assert not errors, errors
                browser.close()
                print('PASS: category clicks, page-2 selection, reparse completion, no page errors')
        finally:
            server.should_exit=True; thread.join(timeout=10)
            settings.data_dir,settings.config_dir=original


if __name__=='__main__':
    main()
