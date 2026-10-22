import asyncio
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        os.environ["MULTIAI_DATA_DIR"] = str(root / "isolated-data")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import app

        # Missing and corrupt files must produce different, truthful outcomes.
        assert app.read_json_file(root / "missing.json", {"default": True}, "validation.data", "missing") == {"default": True}
        corrupt = root / "corrupt.json"
        corrupt.write_text("{broken", encoding="utf-8")
        assert app.read_json_file(corrupt, [], "validation.data", "corrupt") == []

        saved = root / "saved.json"
        app.write_json_file(saved, {"schema_version": 1, "records": [1, 2]}, "validation.data", "save", "op_save")
        assert json.loads(saved.read_text(encoding="utf-8"))["schema_version"] == 1

        blocked_parent = root / "blocked-parent"
        blocked_parent.write_text("not a directory", encoding="utf-8")
        try:
            app.write_json_file(blocked_parent / "cannot-save.json", {"x": 1}, "validation.data", "save_failure", "op_save_failure")
            raise AssertionError("save failure was not raised")
        except OSError:
            pass

        # A task is correlated from creation through state transitions without logging its content.
        secret_prompt = "PRIVATE_PROMPT_SHOULD_NOT_BE_LOGGED"
        original_history = app.HISTORY_FILE
        app.HISTORY_FILE = root / "history.json"
        task_id = app.APP.state.start_task(secret_prompt, ["chatgpt"], [], {"chatgpt": "default"}, "op_task", "task_validation")
        assert task_id == "task_validation"
        app.APP.state.apply_event({
            "kind": "status", "platform": "chatgpt", "status": "生成中",
            "operation_id": "op_task", "task_id": task_id, "elapsed": 1,
            "preview": "PRIVATE_RESPONSE_SHOULD_NOT_BE_LOGGED",
        })
        app.APP.state.apply_event({
            "kind": "status", "platform": "chatgpt", "status": "已完成",
            "operation_id": "op_task", "task_id": task_id, "elapsed": 2,
            "full_text": "PRIVATE_RESPONSE_SHOULD_NOT_BE_LOGGED",
        })
        app.HISTORY_FILE = original_history

        async def fail_background():
            await asyncio.sleep(0)
            raise RuntimeError("simulated background failure")

        future = app.APP.automation._submit(
            fail_background(), operation="validation.background",
            operation_id="op_background", task_id="task_background", object_id="chatgpt",
        )
        try:
            future.result(timeout=3)
        except Exception:
            pass

        # Missing executable exercises the real browser dependency failure path.
        browser_key = app.APP.automation.browser_key
        original_browser = dict(app.BROWSERS[browser_key])
        app.BROWSERS[browser_key]["available"] = False
        app.BROWSERS[browser_key]["path"] = None
        future = app.APP.automation._submit(
            app.APP.automation._ensure_browser(), operation="validation.browser_launch",
            operation_id="op_browser_missing", object_id=browser_key,
        )
        try:
            future.result(timeout=3)
        except Exception:
            pass
        app.BROWSERS[browser_key].update(original_browser)

        diagnostic = app.RUNTIME_LOG.export_diagnostics(
            {"prompt": secret_prompt, "response_body": "PRIVATE_RESPONSE_SHOULD_NOT_BE_LOGGED", "task_id": task_id},
            root / "diagnostics", "op_diagnostics",
        )
        assert diagnostic.is_file()
        with zipfile.ZipFile(diagnostic) as archive:
            assert secret_prompt not in archive.read("state-summary.json").decode("utf-8")

        time.sleep(0.1)
        app.APP.stop()

        events = []
        for file in (root / "isolated-data" / "logs").glob("runtime.jsonl*"):
            events.extend(json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip())
        event_names = {item["event"] for item in events}
        for required in (
            "data.read.defaulted", "data.read.failed", "data.write.verified", "data.write.failed",
            "task.created", "state.platform_status.changed", "background.task.failed",
            "validation.background.finished", "validation.browser_launch.finished", "diagnostics.export.finished",
        ):
            assert required in event_names, required
        text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events)
        assert secret_prompt not in text
        assert "PRIVATE_RESPONSE_SHOULD_NOT_BE_LOGGED" not in text
        assert any(item.get("operation_id") == "op_task" and item.get("task_id") == task_id for item in events)
        assert any(item["event"] == "validation.background.finished" and item["result"] == "failure" for item in events)
        assert any(item["event"] == "validation.browser_launch.finished" and item["result"] == "failure" for item in events)

        representative = [
            item for item in events
            if item["event"] in {
                "data.write.failed", "task.created", "state.platform_status.changed",
                "background.task.failed", "validation.browser_launch.finished", "diagnostics.export.finished",
            }
        ]
        print("REPRESENTATIVE_LOGS")
        for item in representative[:10]:
            print(json.dumps(item, ensure_ascii=False))
        print("LOGGING_INTEGRATION_OK")


if __name__ == "__main__":
    main()
