import base64
import tempfile
from pathlib import Path

import app


def main():
    original_dir = app.UPLOAD_DIR
    original_uploads = app.UPLOADS
    with tempfile.TemporaryDirectory() as temp:
        app.UPLOAD_DIR = Path(temp)
        app.UPLOADS = {}
        try:
            payload = base64.b64encode("附件测试".encode("utf-8")).decode("ascii")
            record = app.store_attachment("说明.txt", payload)
            assert record["name"] == "说明.txt"
            assert Path(record["path"]).read_text(encoding="utf-8") == "附件测试"
            assert app.resolve_attachments([record["token"]])[0]["path"] == record["path"]

            try:
                app.store_attachment("危险.exe", payload)
                raise AssertionError("executable attachment should be blocked")
            except ValueError:
                pass

            app.delete_attachment(record["token"])
            assert not Path(record["path"]).exists()
        finally:
            app.UPLOAD_DIR = original_dir
            app.UPLOADS = original_uploads

    snapshot = app.APP.state.snapshot()
    builtins = {item["key"]: item for item in snapshot["platforms"]}
    assert builtins["chatgpt"]["page_state"] == "unchecked"
    assert builtins["chatgpt"]["login_state"] == "unchecked"
    assert builtins["chatgpt"]["input_state"] == "unchecked"
    assert builtins["chatgpt"]["modes"] == [{"key": "default", "label": "保持当前模式"}]

    app.APP.state.apply_event({
        "kind": "capability",
        "platform": "chatgpt",
        "page_state": "ready",
        "page_label": "页面已打开",
        "login_state": "logged_in",
        "login_label": "已登录",
        "input_state": "available",
        "input_label": "可输入",
        "capability_detail": "已登录；检测到 1 个可切换模式",
        "mode_status": "detected",
        "available_modes": [
            {"key": "default", "label": "保持当前模式"},
            {"key": "thinking", "label": "Thinking"},
        ],
        "mode_aliases": {"thinking": "Thinking"},
    })
    detected = {item["key"]: item for item in app.APP.state.snapshot()["platforms"]}
    assert detected["chatgpt"]["page_state"] == "ready"
    assert detected["chatgpt"]["login_state"] == "logged_in"
    assert detected["chatgpt"]["input_state"] == "available"
    assert any(mode["key"] == "thinking" for mode in detected["chatgpt"]["modes"])
    assert app.APP.state.mode_alias("chatgpt", "thinking") == "Thinking"
    print("ATTACHMENT_MODE_OK")


if __name__ == "__main__":
    main()
