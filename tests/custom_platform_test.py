import tempfile
from pathlib import Path

import app


def main():
    app.APP.stop()
    original_file = app.CUSTOM_PLATFORMS_FILE
    original_records = list(app.CUSTOM_PLATFORM_RECORDS)
    with tempfile.TemporaryDirectory() as temp:
        app.CUSTOM_PLATFORMS_FILE = Path(temp) / "custom-platforms.json"
        app.CUSTOM_PLATFORM_RECORDS[:] = []
        app.rebuild_platform_registry()
        try:
            platform = app.add_custom_platform("测试 AI", "https://example.com/chat#fragment")
            assert platform.key.startswith("custom_")
            assert platform.url == "https://example.com/chat"
            assert platform.key in app.PLATFORM_MAP
            assert app.CUSTOM_PLATFORMS_FILE.is_file()
            try:
                app.add_custom_platform("重复", "https://example.com/chat")
                raise AssertionError("duplicate URL should fail")
            except ValueError:
                pass
            app.delete_custom_platform(platform.key)
            assert platform.key not in app.PLATFORM_MAP
        finally:
            app.CUSTOM_PLATFORM_RECORDS[:] = original_records
            app.CUSTOM_PLATFORMS_FILE = original_file
            app.rebuild_platform_registry()
    print("CUSTOM_PLATFORM_OK")


if __name__ == "__main__":
    main()
