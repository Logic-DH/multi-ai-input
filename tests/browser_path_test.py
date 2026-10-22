import tempfile
from pathlib import Path

import app


def main():
    app.APP.stop()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        fake_browser = root / "custom-browser.exe"
        fake_browser.write_bytes(b"test")
        fake_360 = root / "360chrome.exe"
        fake_360.write_bytes(b"test")
        desktop = root / "Desktop"
        desktop.mkdir()
        desktop_edge = desktop / "msedge.exe"
        desktop_edge.write_bytes(b"test")
        shortcut = desktop / "Google Chrome.lnk"
        shortcut.write_bytes(b"shortcut")
        shortcut_target = root / "chrome.exe"
        shortcut_target.write_bytes(b"test")
        parser_shortcut = desktop / "Chrome parser.lnk"
        target_bytes = str(shortcut_target).encode("ascii") + b"\0"
        link_info_size = 28 + len(target_bytes) + 1
        link_info = bytearray(link_info_size)
        link_info[0:4] = link_info_size.to_bytes(4, "little")
        link_info[4:8] = (28).to_bytes(4, "little")
        link_info[8:12] = (1).to_bytes(4, "little")
        link_info[16:20] = (28).to_bytes(4, "little")
        link_info[24:28] = (28 + len(target_bytes)).to_bytes(4, "little")
        link_info[28:28 + len(target_bytes)] = target_bytes
        shell_header = bytearray(76)
        shell_header[0:4] = (0x4C).to_bytes(4, "little")
        shell_header[20:24] = (0x2).to_bytes(4, "little")
        parser_shortcut.write_bytes(shell_header + link_info)
        assert app.resolve_windows_shortcut(parser_shortcut) == str(shortcut_target.resolve())
        assert app.find_desktop_browser("edge", [desktop]) == str(desktop_edge.resolve())
        assert app.find_desktop_browser(
            "chrome", [desktop], shortcut_resolver=lambda candidate: str(shortcut_target)
        ) == str(shortcut_target.resolve())
        settings = root / "browser-paths.json"
        original_settings = app.BROWSER_PATHS_FILE
        original = dict(app.BROWSERS["quark"])
        original_360 = dict(app.BROWSERS["360"])
        app.BROWSER_PATHS_FILE = settings
        try:
            updated = app.configure_browser_path("quark", f'"{fake_browser}"')
            assert updated["path"] == str(fake_browser.resolve())
            assert updated["custom_path"] is True
            assert updated["path_source"] == "custom"
            assert settings.is_file()
            updated_360 = app.configure_browser_path("360", str(fake_360))
            assert updated_360["path"] == str(fake_360.resolve())
            assert updated_360["custom_path"] is True
            assert app.BROWSERS["360"]["user_data"].endswith("360Chrome\\Chrome\\User Data")
            restored = app.configure_browser_path("quark", "")
            assert restored["custom_path"] is False
            assert restored["path"] == original["default_path"]
        finally:
            app.BROWSER_PATHS_FILE = original_settings
            app.BROWSERS["quark"].update(original)
            app.BROWSERS["360"].update(original_360)
    print("BROWSER_PATH_OK")


if __name__ == "__main__":
    main()
