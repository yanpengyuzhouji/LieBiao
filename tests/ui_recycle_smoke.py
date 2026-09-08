from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


data_dir = Path(__file__).resolve().parents[1] / ".runtime" / "codex-ui-recycle-test"
database = data_dir / "scout.db"
deadline = time.time() + 10
while not database.exists() and time.time() < deadline:
    time.sleep(0.1)

with sqlite3.connect(database) as connection:
    connection.execute(
        "INSERT INTO notices(site_id,external_id,source_type,source_url,title,notice_type,published_at,ingest_status,business_mark,deleted_at,created_at,updated_at) VALUES(1,?,'crawl',?,'回收站恢复验证公告','招标公告','2026-09-08 12:00:00','parsed','pending','2026-09-08 13:00:00','2026-09-08 12:00:00','2026-09-08 13:00:00')",
        (f"ui-trash-{time.time_ns()}", "https://example.com/ui-trash"),
    )

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://127.0.0.1:8091", wait_until="networkidle")
    page.get_by_role("button", name="回收站 1").click()
    page.wait_for_load_state("networkidle")
    page.get_by_text("回收站恢复验证公告", exact=True).click()
    page.get_by_role("button", name="恢复到公告库").click()
    page.get_by_role("button", name="全部 1").click()
    page.wait_for_load_state("networkidle")
    assert page.get_by_text("回收站恢复验证公告", exact=True).count() == 1
    page.get_by_role("button", name="平台与账号").click()
    assert page.get_by_text("人工会话", exact=True).count() == 1
    browser.close()
