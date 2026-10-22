import asyncio
import json
import os
import sys

import app
from platform_adapters import PLATFORMS
from playwright.async_api import async_playwright


async def main():
    app.APP.stop()
    edge = app.BROWSERS["edge"]
    if not edge["available"]:
        raise SystemExit("EDGE_NOT_INSTALLED")

    inspector = object.__new__(app.BrowserAutomation)
    inspector.emit = lambda *args, **kwargs: None
    results = {}
    requested = set(sys.argv[1:])
    targets = [platform for platform in PLATFORMS if not requested or platform.key in requested]

    async with async_playwright() as playwright:
        headful = os.getenv("MULTIAI_HEADFUL") == "1"
        browser = await playwright.chromium.launch(
            executable_path=edge["path"],
            headless=not headful,
            args=["--no-first-run", *(["--window-position=-32000,-32000"] if headful else [])],
        )
        context = await browser.new_context(locale="zh-CN")
        context.set_default_timeout(3500)
        for platform in targets:
            page = await context.new_page()
            warning = ""
            try:
                await page.goto(platform.url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                warning = "页面已显示，但加载等待超时" if "timeout" in str(exc).casefold() else str(exc)[:120]
            try:
                result = await inspector._inspect_platform(
                    page, platform, navigation_warning=warning,
                )
                results[platform.key] = {
                    "url": page.url,
                    "page": result["page_label"],
                    "account": result["login_label"],
                    "input": result["input_label"],
                    "modes": [item["label"] for item in result["available_modes"]],
                }
            finally:
                await page.close()
        await context.close()
        await browser.close()

    assert set(results) == {platform.key for platform in targets}
    for result in results.values():
        assert result["page"] and result["account"] and result["input"]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("PLATFORM_STATUS_LIVE_OK")


if __name__ == "__main__":
    asyncio.run(main())
