"""Run via with_server.py on port 8097 with an isolated LIEBIAO_DATA_DIR."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if '--serve' in sys.argv:
    import uvicorn
    from backend.db import init_db
    init_db()
    # No scheduler/update checks during UI verification.
    uvicorn.run('backend.main:app', host='127.0.0.1', port=8097, lifespan='off')
else:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            def sites_route(route):
                response = route.fetch()
                payload = response.json()
                for site in payload['items']:
                    if site['code'] in ('csg', 'epec'):
                        site['health_status'] = 'healthy'
                        site['health_message'] = '公开公告列表正常'
                route.fulfill(response=response, json=payload)
            page.route('**/api/sites', sites_route)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:8097', wait_until='networkidle')
            page.locator('button[data-view="platforms"]').click()
            page.wait_for_load_state('networkidle')
            page.wait_for_function("document.querySelector('.platform-table')?.innerText.includes('无需验证')")
            for name in ('中国招标投标公共服务平台', '乙方宝', '国家能源（国能e招）', '中国电力设备信息网', '中广核', '中国华电集团电子商务平台'):
                assert page.locator('.platform-name').filter(has_text=name).count() == 1, name
            assert page.locator('.platform-table td').filter(has_text='访问受限 · 待完成适配').count() == 2
            assert page.locator('.platform-table td').filter(has_text='公开内容 · 部分内容受限').count() == 1
            assert page.get_by_text('招标公告：货物、工程、服务；不包含询价采购。', exact=True).count() == 1
            for name in ('南方电网', '中国石化物资'):
                row = page.locator('tr').filter(has_text=name)
                assert row.get_by_text('无需验证', exact=True).count() == 1
                assert row.locator('[data-open-verification]').count() == 0
                assert row.locator('[data-complete-verification]').count() == 0
            for name in ('中国华能', '大唐集团', '中国华电集团电子商务平台', '中国电力设备信息网', '乙方宝'):
                row = page.locator('tr').filter(has_text=name)
                assert row.locator('[data-open-verification]').count() == 1, name
                assert row.locator('[data-complete-verification]').count() == 1, name
            assert not errors, errors
            print('PASS: five platform entries, scope/restriction labels, no browser errors')
        finally:
            browser.close()
