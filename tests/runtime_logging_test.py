import json
import logging
import queue
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_logging import BoundedQueueHandler, RuntimeLog


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        marker = root / "active-session.json"
        marker.write_text(json.dumps({
            "session_id": "previous-session",
            "status": "running",
            "pending_operations": {"op_previous": {"operation": "task.send"}},
        }), encoding="utf-8")

        runtime = RuntimeLog(root, "test", "test-build", max_bytes=1600, backup_count=2)
        operation_id = "op_validation"
        started = runtime.start_operation(operation_id, "validation.flow", task_id="task_validation", object_id="chatgpt")
        runtime.info(
            "validation", "validation.sensitive_input", "敏感字段脱敏验证",
            operation_id=operation_id, prompt="this must not appear", token="secret-token",
            url="https://example.com/path?access_token=secret", local_path=str(Path.home() / "private" / "file.txt"),
        )
        try:
            raise TimeoutError("simulated upstream timeout")
        except TimeoutError as exc:
            runtime.exception(
                "external", "external.request.attempt_failed", "外部请求首次尝试超时", exc,
                operation_id=operation_id, task_id="task_validation", retry_attempt=1, result="retrying",
            )
        runtime.info(
            "external", "external.request.retry_started", "外部请求开始重试",
            operation_id=operation_id, task_id="task_validation", retry_attempt=2,
        )
        runtime.finish_operation(
            operation_id, "validation.flow", started, "success",
            task_id="task_validation", object_id="chatgpt", retry_count=1, verified=True,
        )
        diagnostic = runtime.export_diagnostics({
            "status": "completed", "prompt": "must be redacted", "response_body": "must be redacted",
        }, root / "diagnostics")
        runtime.close()

        events = []
        for log_file in (root / "logs").glob("runtime.jsonl*"):
            events.extend(read_events(log_file))
        assert events
        required = {"timestamp", "level", "module", "event", "session_id", "message"}
        assert all(required.issubset(item) for item in events)
        assert any(item["event"] == "lifecycle.previous_session_incomplete" and item["pending_operation_ids"] == ["op_previous"] for item in events)
        assert any(item["event"] == "validation.flow.started" and item["operation_id"] == operation_id for item in events)
        assert any(item["event"] == "validation.flow.finished" and item["result"] == "success" and item["duration_ms"] >= 0 for item in events)
        combined = "\n".join(json.dumps(item, ensure_ascii=False) for item in events)
        assert "this must not appear" not in combined
        assert "secret-token" not in combined
        assert "access_token=secret" not in combined
        assert str(Path.home()) not in combined
        assert "%USERPROFILE%" in combined
        with zipfile.ZipFile(diagnostic) as archive:
            names = set(archive.namelist())
            assert "environment.json" in names
            state = archive.read("state-summary.json").decode("utf-8")
            assert "must be redacted" not in state

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "logs").write_text("blocks-directory", encoding="utf-8")
        degraded = RuntimeLog(root, "test", "unwritable-test")
        assert degraded.degraded_storage is True
        degraded.error("logging", "logging.degraded.test", "备用日志目录可用", result="success")
        degraded.close()

    target_queue = queue.Queue(maxsize=1)
    fallback_records = []

    class Collector(logging.Handler):
        def emit(self, record):
            fallback_records.append(record)

    handler = BoundedQueueHandler(target_queue, Collector())
    logger = logging.getLogger("queue-policy-test")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.info("fills queue")
    logger.info("may drop")
    logger.error("must survive")
    assert handler.dropped_low_priority == 1
    assert len(fallback_records) == 1 and fallback_records[0].levelno == logging.ERROR

    print("RUNTIME_LOGGING_OK")


if __name__ == "__main__":
    main()
