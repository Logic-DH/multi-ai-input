"""Single structured runtime logging pipeline for MultiAIInput.

The application intentionally logs metadata and state transitions, never prompt,
response, cookie, token, or attachment contents.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_KEYS = re.compile(
    r"(password|passwd|token|cookie|authorization|secret|prompt|full_text|response_body|request_body|content)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\]\[\)\(\"']+", re.IGNORECASE)


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_correlation_id(prefix="op") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def safe_url(value) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"


def safe_path(value) -> dict:
    path = Path(str(value))
    raw = str(path)
    folded = raw.casefold()
    user = str(Path.home()).casefold()
    temp = str(Path(tempfile.gettempdir())).casefold()
    if folded.startswith(temp):
        location = "temp"
    elif folded.startswith(user):
        location = "user"
    elif "program files" in folded:
        location = "program_files"
    else:
        location = "other"
    import hashlib
    return {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "location": location,
        "path_hash": hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12],
    }


def redact_text(value) -> str:
    text = str(value)
    replacements = {
        str(Path.home()): "%USERPROFILE%",
        os.getenv("LOCALAPPDATA", ""): "%LOCALAPPDATA%",
        tempfile.gettempdir(): "%TEMP%",
    }
    for source, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        if source:
            text = re.sub(re.escape(source), lambda _m: replacement, text, flags=re.IGNORECASE)
            text = re.sub(re.escape(source.replace("\\", "/")), lambda _m: replacement, text, flags=re.IGNORECASE)
    text = URL_RE.sub(lambda match: safe_url(match.group(0)), text)
    return text


def sanitize(value, key=""):
    folded_key = str(key).casefold()
    if isinstance(value, (bool, int, float)) and (folded_key.endswith("_length") or folded_key.endswith("_count") or folded_key == "content_length"):
        return value
    if isinstance(value, str) and (folded_key.endswith("_url") or folded_key == "url"):
        return safe_url(value)
    if isinstance(value, str) and folded_key in {"path", "local_path", "executable", "profile", "directory"}:
        return safe_path(value)
    if SENSITIVE_KEYS.search(str(key)):
        if isinstance(value, (list, tuple, dict)):
            return f"<redacted:{len(value)}>"
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, Path):
        return safe_path(value)
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}")
    if isinstance(value, str):
        return redact_text(value[:12000])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(repr(value))


class JsonLineFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": getattr(record, "event_time", _iso_now()),
            "level": record.levelname,
            "module": getattr(record, "component", record.name),
            "event": getattr(record, "event_name", "log.message"),
            "session_id": getattr(record, "session_id", "unknown"),
            "message": redact_text(record.getMessage()),
        }
        extra_fields = sanitize(getattr(record, "fields", {}))
        payload.update({key: value for key, value in extra_fields.items() if key not in payload})
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    """Never let a logging I/O failure take down the product."""

    def __init__(self, *args, emergency_path=None, **kwargs):
        self.emergency_path = Path(emergency_path) if emergency_path else None
        self.io_failures = 0
        super().__init__(*args, **kwargs)

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self._emergency_emit(record)

    def handleError(self, record):
        self._emergency_emit(record)

    def _emergency_emit(self, record):
        try:
            self.io_failures += 1
            if self.emergency_path:
                self.emergency_path.parent.mkdir(parents=True, exist_ok=True)
                with self.emergency_path.open("a", encoding="utf-8") as stream:
                    stream.write(self.format(record) + "\n")
        except Exception:
            try:
                sys.stderr.write(self.format(record) + "\n")
            except Exception:
                pass


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Drop only low-priority records on saturation; write WARN/ERROR directly."""

    def __init__(self, target_queue, fallback_handler):
        super().__init__(target_queue)
        self.fallback_handler = fallback_handler
        self.dropped_low_priority = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            if record.levelno >= logging.WARNING:
                self.fallback_handler.handle(record)
            else:
                self.dropped_low_priority += 1


class RuntimeLog:
    def __init__(self, data_dir: Path, version: str, build_id: str, *, queue_size=5000, max_bytes=5 * 1024 * 1024, backup_count=5, retention_days=14):
        self.requested_dir = Path(data_dir) / "logs"
        self.session_id = uuid.uuid4().hex
        self.version = version
        self.build_id = build_id
        self.started_at = _iso_now()
        self.started_monotonic = time.monotonic()
        self.closed = False
        self.pending_lock = threading.RLock()
        self.rate_lock = threading.RLock()
        self.rate_events = {}
        self.marker_path = Path(data_dir) / "active-session.json"
        self.degraded_storage = False
        try:
            self.requested_dir.mkdir(parents=True, exist_ok=True)
            probe = self.requested_dir / f".write-test-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            self.log_dir = self.requested_dir
        except Exception:
            self.degraded_storage = True
            self.log_dir = Path(tempfile.gettempdir()) / "MultiAIInput" / "logs"
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "runtime.jsonl"
        self.emergency_path = Path(tempfile.gettempdir()) / "MultiAIInput" / "runtime-emergency.jsonl"
        self._cleanup_old_logs(retention_days)
        self.file_handler = SafeRotatingHandler(
            self.log_path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8", delay=True, emergency_path=self.emergency_path,
        )
        self.file_handler.setFormatter(JsonLineFormatter())
        self.log_queue = queue.Queue(maxsize=queue_size)
        self.queue_handler = BoundedQueueHandler(self.log_queue, self.file_handler)
        self.logger = logging.getLogger(f"multiai.runtime.{self.session_id}")
        self.logger.setLevel(getattr(logging, os.getenv("MULTIAI_LOG_LEVEL", "INFO").upper(), logging.INFO))
        self.logger.propagate = False
        self.logger.handlers[:] = [self.queue_handler]
        self.listener = logging.handlers.QueueListener(self.log_queue, self.file_handler, respect_handler_level=True)
        self.listener.start()
        self.pending = {}
        previous = self._load_previous_marker()
        self._write_marker("running")
        if previous and previous.get("status") == "running":
            self.warning(
                "lifecycle", "lifecycle.previous_session_incomplete",
                "检测到上次会话未正常结束",
                previous_session_id=previous.get("session_id"),
                pending_operation_ids=list((previous.get("pending_operations") or {}).keys()),
                result="incomplete",
            )
        self.info(
            "lifecycle", "lifecycle.session_started", "运行日志会话已启动",
            version=version, build_id=build_id, process_id=os.getpid(),
            python=platform.python_version(), os=platform.platform(), frozen=bool(getattr(sys, "frozen", False)),
            log_level=logging.getLevelName(self.logger.level), log_directory=safe_path(self.log_dir),
            rotation={"max_bytes": max_bytes, "backup_count": backup_count, "retention_days": retention_days},
            queue_capacity=queue_size, storage_degraded=self.degraded_storage,
        )

    def _cleanup_old_logs(self, retention_days):
        cutoff = time.time() - retention_days * 86400
        try:
            for candidate in self.log_dir.glob("runtime.jsonl.*"):
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
        except Exception:
            pass

    def _load_previous_marker(self):
        try:
            return json.loads(self.marker_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_marker(self, status):
        payload = {
            "session_id": self.session_id,
            "process_id": os.getpid(),
            "started_at": self.started_at,
            "status": status,
            "pending_operations": self.pending,
        }
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.marker_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self.marker_path)
        except Exception:
            pass

    def _event(self, level, component, event_name, message, **fields):
        if self.closed:
            return
        record_fields = sanitize(fields)
        self.logger.log(level, message, extra={
            "event_time": _iso_now(), "component": component, "event_name": event_name,
            "session_id": self.session_id, "fields": record_fields,
        })

    def debug(self, component, event_name, message, **fields): self._event(logging.DEBUG, component, event_name, message, **fields)
    def info(self, component, event_name, message, **fields): self._event(logging.INFO, component, event_name, message, **fields)
    def warning(self, component, event_name, message, **fields): self._event(logging.WARNING, component, event_name, message, **fields)
    def error(self, component, event_name, message, **fields): self._event(logging.ERROR, component, event_name, message, **fields)

    def exception(self, component, event_name, message, exc, **fields):
        chain = []
        current = exc
        while current and len(chain) < 6:
            chain.append({"type": type(current).__name__, "message": redact_text(str(current))})
            current = current.__cause__ or current.__context__
        fields.update({
            "error_type": type(exc).__name__,
            "error_code": getattr(exc, "winerror", None) or getattr(exc, "errno", None),
            "exception_chain": chain,
            "stack_trace": redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
        })
        self.error(component, event_name, message, **fields)

    def start_operation(self, operation_id, operation, *, task_id=None, object_id=None, **fields):
        with self.pending_lock:
            self.pending[operation_id] = {
                "operation": operation, "task_id": task_id, "object_id": object_id,
                "started_at": _iso_now(),
            }
            self._write_marker("running")
        self.info("operation", f"{operation}.started", "开始执行操作", operation_id=operation_id, task_id=task_id, object_id=object_id, result="started", **fields)
        return time.monotonic()

    def finish_operation(self, operation_id, operation, started, result, *, task_id=None, object_id=None, error_code=None, **fields):
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        with self.pending_lock:
            self.pending.pop(operation_id, None)
            self._write_marker("running")
        level = self.info if result == "success" else (self.warning if result in {"cancelled", "timeout", "degraded"} else self.error)
        level("operation", f"{operation}.finished", "操作已结束", operation_id=operation_id, task_id=task_id, object_id=object_id, result=result, duration_ms=duration_ms, error_code=error_code, **fields)

    def rate_limited(self, level, component, event_name, message, *, rate_key, window_seconds=60, **fields):
        now = time.monotonic()
        with self.rate_lock:
            item = self.rate_events.get(rate_key)
            if item and now - item["first"] < window_seconds:
                item["count"] += 1
                item["last"] = now
                return
            repeated = item["count"] if item else 0
            self.rate_events[rate_key] = {"first": now, "last": now, "count": 1}
        if repeated:
            fields["previous_suppressed_count"] = repeated
        self._event(level, component, event_name, message, **fields)

    def flush(self):
        deadline = time.monotonic() + 3
        while not self.log_queue.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            self.file_handler.flush()
        except Exception:
            pass

    def close(self, clean=True):
        if self.closed:
            return
        self.info(
            "lifecycle", "lifecycle.session_finished", "运行日志会话结束",
            result="success" if clean else "failure",
            duration_ms=round((time.monotonic() - self.started_monotonic) * 1000, 2),
            pending_operation_count=len(self.pending),
            dropped_low_priority=self.queue_handler.dropped_low_priority,
            logging_io_failures=self.file_handler.io_failures,
        )
        self.flush()
        if clean:
            try:
                self.marker_path.unlink(missing_ok=True)
            except Exception:
                self._write_marker("clean_exit")
        else:
            self._write_marker("failed_exit")
        self.listener.stop()
        self.file_handler.close()
        self.closed = True

    def export_diagnostics(self, state_summary: dict, output_dir: Path, operation_id=None) -> Path:
        operation_id = operation_id or new_correlation_id("diag")
        started = self.start_operation(operation_id, "diagnostics.export")
        try:
            self.flush()
            output_dir.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("MultiAIInput-diagnostics-%Y%m%d-%H%M%S.zip")
            target = output_dir / name
            environment = {
                "generated_at": _iso_now(), "app_version": self.version, "build_id": self.build_id,
                "session_id": self.session_id, "python": platform.python_version(), "os": platform.platform(),
                "frozen": bool(getattr(sys, "frozen", False)), "log_storage_degraded": self.degraded_storage,
            }
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for log_file in sorted(self.log_dir.glob("runtime.jsonl*")):
                    archive.write(log_file, f"logs/{log_file.name}")
                if self.emergency_path.is_file():
                    archive.write(self.emergency_path, f"logs/{self.emergency_path.name}")
                archive.writestr("environment.json", json.dumps(sanitize(environment), ensure_ascii=False, indent=2))
                archive.writestr("state-summary.json", json.dumps(sanitize(state_summary), ensure_ascii=False, indent=2))
                archive.writestr("README.txt", "诊断包不包含提示词、AI完整回复、Cookie、令牌或附件内容。请按 session_id、operation_id、task_id 串联事件。\n")
            self.finish_operation(operation_id, "diagnostics.export", started, "success", file=safe_path(target))
            self.flush()
            return target
        except Exception as exc:
            self.exception("diagnostics", "diagnostics.export.failed", "导出诊断日志失败", exc, operation_id=operation_id)
            self.finish_operation(operation_id, "diagnostics.export", started, "failure", error_code=getattr(exc, "errno", None))
            raise


_runtime = None


def configure(data_dir: Path, version: str, build_id: str) -> RuntimeLog:
    global _runtime
    if _runtime and not _runtime.closed:
        return _runtime
    _runtime = RuntimeLog(data_dir, version, build_id)
    return _runtime


def get_runtime() -> RuntimeLog:
    if _runtime is None:
        raise RuntimeError("runtime logging is not configured")
    return _runtime

