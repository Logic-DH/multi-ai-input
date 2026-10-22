import json
import tempfile
from pathlib import Path

import app


def main():
    app.APP.stop()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "source"
        destination = root / "destination"
        profile = source / "Profile 2"
        (profile / "Network").mkdir(parents=True)
        (profile / "Local Storage").mkdir(parents=True)
        (source / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 2"}}), encoding="utf-8"
        )
        (profile / "Network/Cookies").write_bytes(b"cookie-test")
        (profile / "Local Storage/session.test").write_text("token", encoding="utf-8")
        original = app.BROWSERS["edge"]["user_data"]
        app.BROWSERS["edge"]["user_data"] = str(source)
        try:
            result = app.import_existing_login("edge", destination)
        finally:
            app.BROWSERS["edge"]["user_data"] = original
        assert result["status"] == "copied"
        assert (destination / "Default/Network/Cookies").read_bytes() == b"cookie-test"
        assert (destination / "Default/Local Storage/session.test").read_text(encoding="utf-8") == "token"
        assert (destination / ".login-imported.json").is_file()
    print("PROFILE_IMPORT_OK")


if __name__ == "__main__":
    main()
