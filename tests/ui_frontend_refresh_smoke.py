from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto("http://127.0.0.1:8192/", wait_until="networkidle")
        page.wait_for_selector("#mark-filter")

        identity = page.locator(".user-identity")
        assert "杨正月" in identity.inner_text()
        assert "林晓彤" not in page.locator("body").inner_text()
        assert page.locator('.filter-more').count() == 0

        mark_filter = page.locator("#mark-filter")
        mark_filter.evaluate("element => { element.dataset.smoke = 'preserved'; }")
        mark_filter.select_option("focus")
        page.wait_for_timeout(800)
        assert mark_filter.input_value() == "focus"
        assert mark_filter.get_attribute("data-smoke") == "preserved"
        assert page.locator('.filter-tab[data-filter-tab="all"] .tab-number').count() == 1

        page.locator('[data-view="imports"]').click()
        page.wait_for_timeout(300)
        assert page.locator("h3", has_text="最近导入").count() == 0
        page.screenshot(path="outputs/ui-refresh-smoke.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
