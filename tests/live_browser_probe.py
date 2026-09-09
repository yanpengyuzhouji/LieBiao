"""Read-only browser probe for public pages; never solves verification."""
import json
import sys
from playwright.sync_api import sync_playwright, TimeoutError

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(sys.argv[1], wait_until='domcontentloaded', timeout=30000)
        try:
            page.wait_for_load_state('networkidle', timeout=12000)
        except TimeoutError:
            pass
        print(page.url)
        print(page.locator('body').inner_text()[:5000])
        print(json.dumps(page.locator('a').evaluate_all('(els)=>els.slice(0,20).map(a=>({text:a.innerText,url:a.href}))'), ensure_ascii=False))
    finally:
        browser.close()
