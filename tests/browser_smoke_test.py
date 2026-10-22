import asyncio

import app
from playwright.async_api import async_playwright


async def main():
    app.APP.stop()
    async with async_playwright() as playwright:
        for key, info in app.BROWSERS.items():
            if not info["available"]:
                print(f"{key}: NOT_INSTALLED")
                continue
            browser = await playwright.chromium.launch(
                executable_path=info["path"],
                headless=True,
                args=["--no-first-run", *info.get("launch_args", [])],
            )
            page = await browser.new_page()
            await page.set_content(f"<title>{key}</title><p>ok</p>")
            assert await page.title() == key
            await browser.close()
            print(f"{key}: OK")


if __name__ == "__main__":
    asyncio.run(main())
