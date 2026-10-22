import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import mimetypes
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from platform_adapters import PLATFORMS, PLATFORM_MAP, Platform
from runtime_logging import configure as configure_runtime_logging, new_correlation_id, safe_path, safe_url


APP_NAME = "多平台Ai同步输入器"
VERSION = "V1"
BUILD_ID = os.getenv("MULTIAI_BUILD_ID", VERSION)


LOGIN_RULES = {
    "claude": {
        "in": ("button[data-testid*='account']", "button[aria-label*='Account']", "button[aria-label*='Profile']"),
        "out": ("a[href*='/login']", "button[data-testid*='login']"),
    },
    "chatgpt": {
        "in": ("button[data-testid='profile-button']", "button[aria-label*='Profile']", "div[data-testid*='account'] button"),
        "out": ("button[data-testid='login-button']", "a[href*='/auth/login']"),
    },
    "grok": {
        "in": ("a[href*='/settings']", "button[aria-label*='Account']", "button[aria-label*='Profile']"),
        "out": ("a[href*='/sign-in']", "a[href*='/login']"),
    },
    "gemini": {
        "in": ("a[aria-label*='Google Account']", "button[aria-label*='Google Account']", "a[href*='SignOutOptions']"),
        "out": ("a[href*='accounts.google.com'][href*='signin']", "a[href*='accounts.google.com'][href*='ServiceLogin']"),
    },
    "deepseek": {
        "in": ("button[class*='avatar']", "[data-testid*='user'] [class*='avatar']", "button[aria-label*='用户']", "button[aria-label*='Account']"),
        "out": ("a[href*='/sign_in']", "a[href*='/login']", "button[class*='login']"),
    },
    "doubao": {
        "in": ("header [class*='avatar']", "nav [class*='avatar']", "[data-testid*='user-avatar']", "button[aria-label*='个人']", "button[aria-label*='账号']"),
        "out": ("button[class*='login']", "a[href*='/login']"),
    },
    "qwen": {
        "in": ("header [class*='avatar']", "nav [class*='avatar']", "[data-testid*='user-avatar']", "button[aria-label*='个人']", "button[aria-label*='账号']"),
        "out": ("button[class*='login']", "a[href*='/login']"),
    },
    "kimi": {
        "in": ("button.user-info-container img", "[class*='user-avatar']", "[class*='avatar'] img"),
        "out": ("button.next-sidebar-history-list__login", "a[href*='/login']"),
    },
    "zhipu": {
        "in": ("header [class*='avatar']", "nav [class*='avatar']", "[class*='user-info'] [class*='avatar']", "[data-testid*='user-avatar']"),
        "out": ("button[class*='login']", "a[href*='/login']", "a[href*='/signin']"),
    },
}

VERIFICATION_TEXTS = ("安全验证", "人机验证", "完成验证", "验证码", "Verify you are human", "Checking your browser")
LOGIN_TEXTS = ("登录", "Log in", "Sign in")
RESTRICTION_TEXTS = (
    "受区域限制", "地区限制", "当前地区不可用", "所在地区不可用",
    "not available in your region", "region is not supported", "unsupported region",
)
MODE_OPTION_SELECTORS = (
    "[role='menuitem']", "[role='option']", "[role='radio']",
    "[role='menu'] button", "[role='listbox'] button",
)
MODE_KEYWORDS = (
    "gpt", "claude", "gemini", "grok", "deepseek", "qwen", "sonnet", "opus", "haiku",
    "pro", "flash", "thinking", "instant", "fast", "expert", "search",
    "思考", "搜索", "联网", "快速", "专家", "专业", "自动", "模型",
)


def classify_platform_capability(
    *,
    verification=False,
    restricted=False,
    has_account=False,
    has_login=False,
    has_input=False,
    navigation_timed_out=False,
    page_has_content=True,
):
    """Keep page, account, and input capability as independent signals."""
    if restricted:
        page_state, page_label = "blocked", "地区受限"
    elif verification:
        page_state, page_label = "verification", "等待验证"
    elif navigation_timed_out and not page_has_content:
        page_state, page_label = "timeout", "加载超时"
    elif navigation_timed_out:
        page_state, page_label = "partial", "页面已显示"
    elif page_has_content or has_input or has_login or has_account:
        page_state, page_label = "ready", "页面已打开"
    else:
        page_state, page_label = "unknown", "页面状态未知"

    if verification:
        login_state, login_label = "verification", "需验证"
    elif has_account:
        login_state, login_label = "logged_in", "已登录"
    elif restricted and has_login:
        login_state, login_label = "logged_out", "需登录"
    elif has_login and has_input:
        login_state, login_label = "guest", "访客可用"
    elif has_login:
        login_state, login_label = "logged_out", "未登录"
    elif has_input:
        login_state, login_label = "usable_unknown", "登录状态未知"
    else:
        login_state, login_label = "unknown", "登录状态未知"

    if restricted:
        input_state, input_label = "blocked", "不可输入"
    elif verification:
        input_state, input_label = "verification", "验证后可用"
    elif has_input:
        input_state, input_label = "available", "可输入"
    else:
        input_state, input_label = "unavailable", "未检测到输入框"

    return {
        "page_state": page_state,
        "page_label": page_label,
        "login_state": login_state,
        "login_label": login_label,
        "input_state": input_state,
        "input_label": input_label,
    }


def capability_open_status(capability):
    page_state = capability.get("page_state", "unknown")
    input_state = capability.get("input_state", "unchecked")
    needs_manual = (
        page_state in {"blocked", "verification", "timeout", "error", "unknown", "closed"}
        or input_state in {"blocked", "verification", "unavailable", "unknown"}
    )
    return "需人工处理" if needs_manual else "已打开"


def infer_platform_account(platform_key, has_input, has_login, has_account):
    # DeepSeek 访客页只有登录表单，不提供聊天输入框。
    if platform_key == "deepseek" and has_input and not has_login:
        return True
    return has_account


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_data_dir() -> Path:
    override = os.getenv("MULTIAI_DATA_DIR", "").strip()
    preferred = Path(override) if override else Path(os.getenv("LOCALAPPDATA", Path.home())) / "MultiAIInput"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        fallback = base / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = app_data_dir()
HISTORY_FILE = DATA_DIR / "history.json"
BROWSER_PATHS_FILE = DATA_DIR / "browser-paths.json"
CUSTOM_PLATFORMS_FILE = DATA_DIR / "custom-platforms.json"
RUNTIME_LOG = configure_runtime_logging(DATA_DIR, VERSION, BUILD_ID)


def _unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return sys.__excepthook__(exc_type, exc_value, exc_traceback)
    RUNTIME_LOG.exception("exception", "exception.unhandled", "主线程出现未处理异常", exc_value, result="failure")
    RUNTIME_LOG.flush()
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def _thread_exception(args):
    RUNTIME_LOG.exception(
        "exception", "exception.thread_unhandled", "后台线程出现未处理异常", args.exc_value,
        thread_name=getattr(args.thread, "name", "unknown"), result="failure",
    )
    RUNTIME_LOG.flush()


sys.excepthook = _unhandled_exception
if hasattr(threading, "excepthook"):
    threading.excepthook = _thread_exception
UPLOAD_DIR = DATA_DIR / "uploads"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    UPLOAD_DIR = Path(tempfile.gettempdir()) / "MultiAIInputUploads"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS = {}
UPLOADS_LOCK = threading.RLock()
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
MAX_ATTACHMENTS = 5
BLOCKED_ATTACHMENT_SUFFIXES = {".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".dll", ".lnk"}


def read_json_file(path, default, component, data_kind):
    started = time.monotonic()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        RUNTIME_LOG.info(
            component, "data.read.completed", "数据读取完成",
            data_kind=data_kind, file=safe_path(path), result="success",
            record_count=len(value) if isinstance(value, (list, dict)) else 1,
            data_version=value.get("schema_version") if isinstance(value, dict) else None,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return value
    except FileNotFoundError:
        RUNTIME_LOG.info(
            component, "data.read.defaulted", "数据文件尚不存在，使用默认值",
            data_kind=data_kind, file=safe_path(path), result="defaulted",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return default
    except Exception as exc:
        RUNTIME_LOG.exception(
            component, "data.read.failed", "数据读取失败，使用默认值继续运行", exc,
            data_kind=data_kind, file=safe_path(path), result="degraded",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return default


def write_json_file(path, value, component, data_kind, operation_id=None):
    started = time.monotonic()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    RUNTIME_LOG.debug(
        component, "data.write.started", "开始写入数据",
        operation_id=operation_id, data_kind=data_kind, file=safe_path(path),
        record_count=len(value) if isinstance(value, (list, dict)) else 1,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(serialized, encoding="utf-8")
        os.replace(temp_path, path)
        verified = json.loads(path.read_text(encoding="utf-8"))
        if type(verified) is not type(value):
            raise ValueError("written JSON type verification failed")
        RUNTIME_LOG.info(
            component, "data.write.verified", "数据写入并回读验证完成",
            operation_id=operation_id, data_kind=data_kind, file=safe_path(path), result="success",
            record_count=len(value) if isinstance(value, (list, dict)) else 1,
            data_version=value.get("schema_version") if isinstance(value, dict) else None,
            checksum_sha256=checksum, verification="json_reparsed",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        RUNTIME_LOG.exception(
            component, "data.write.failed", "数据写入失败", exc,
            operation_id=operation_id, data_kind=data_kind, file=safe_path(path), result="failure",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        raise


def find_first(paths):
    for value in paths:
        path = Path(value)
        if path.is_file():
            return str(path)
    return None


DESKTOP_BROWSER_ALIASES = {
    "edge": ("microsoft edge", "edge", "msedge"),
    "chrome": ("google chrome", "chrome"),
    "quark": ("夸克", "quark"),
    "360": ("360安全浏览器", "360极速浏览器", "360浏览器", "360se", "360chrome"),
}

DESKTOP_BROWSER_EXECUTABLES = {
    "edge": {"msedge.exe"},
    "chrome": {"chrome.exe"},
    "quark": {"quark.exe"},
    "360": {"360se.exe", "360chrome.exe"},
}


def desktop_directories():
    """Return per-machine desktop locations without assuming a fixed user name."""
    candidates = [Path.home() / "Desktop"]
    for variable in ("USERPROFILE", "PUBLIC", "ONEDRIVE", "ONEDRIVECONSUMER"):
        root = os.getenv(variable, "").strip()
        if root:
            candidates.append(Path(root) / "Desktop")
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                desktop_value, _ = winreg.QueryValueEx(key, "Desktop")
                candidates.insert(0, Path(os.path.expandvars(desktop_value)))
        except (ImportError, OSError, TypeError):
            pass
    result = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(os.path.expandvars(str(candidate))))
        if normalized not in seen:
            seen.add(normalized)
            result.append(Path(normalized))
    return result


def resolve_windows_shortcut(shortcut):
    """Resolve the local target stored in a Windows Shell Link without COM or PowerShell."""
    shortcut = Path(shortcut)
    if shortcut.suffix.casefold() != ".lnk":
        return None

    def read_string(data, offset, unicode=False):
        if not offset or offset >= len(data):
            return ""
        if unicode:
            end = offset
            while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            return data[offset:end].decode("utf-16le", errors="ignore").strip()
        end = data.find(b"\0", offset)
        if end < 0:
            end = len(data)
        return data[offset:end].decode("mbcs" if os.name == "nt" else "latin1", errors="ignore").strip()

    def valid_target(raw_path):
        if not raw_path:
            return None
        candidate = Path(os.path.expandvars(raw_path.strip().strip('"')))
        return str(candidate.resolve()) if candidate.is_file() else None

    try:
        data = shortcut.read_bytes()
        if len(data) < 76 or int.from_bytes(data[0:4], "little") != 0x4C:
            return None
        flags = int.from_bytes(data[20:24], "little")
        cursor = 76
        if flags & 0x1:
            id_list_size = int.from_bytes(data[cursor:cursor + 2], "little")
            cursor += 2 + id_list_size
        if flags & 0x2 and cursor + 28 <= len(data):
            info_start = cursor
            info_size = int.from_bytes(data[info_start:info_start + 4], "little")
            header_size = int.from_bytes(data[info_start + 4:info_start + 8], "little")
            local_offset = int.from_bytes(data[info_start + 16:info_start + 20], "little")
            suffix_offset = int.from_bytes(data[info_start + 24:info_start + 28], "little")
            local_unicode_offset = (
                int.from_bytes(data[info_start + 28:info_start + 32], "little")
                if header_size >= 0x24 and info_start + 36 <= len(data) else 0
            )
            suffix_unicode_offset = (
                int.from_bytes(data[info_start + 32:info_start + 36], "little")
                if header_size >= 0x24 and info_start + 36 <= len(data) else 0
            )
            local_unicode = read_string(data, info_start + local_unicode_offset, True) if local_unicode_offset else ""
            suffix_unicode = read_string(data, info_start + suffix_unicode_offset, True) if suffix_unicode_offset else ""
            local = local_unicode or read_string(data, info_start + local_offset)
            suffix = suffix_unicode or read_string(data, info_start + suffix_offset)
            direct = valid_target(local)
            if direct:
                return direct
            combined = valid_target(str(Path(local) / suffix)) if local and suffix else None
            if combined:
                return combined
            cursor = info_start + info_size

        # EnvironmentVariableDataBlock is used by some installer-created shortcuts.
        while cursor + 8 <= len(data):
            block_size = int.from_bytes(data[cursor:cursor + 4], "little")
            if block_size == 0:
                break
            if block_size < 8 or cursor + block_size > len(data):
                break
            signature = int.from_bytes(data[cursor + 4:cursor + 8], "little")
            if signature == 0xA0000001:
                unicode_value = read_string(data, cursor + 268, True) if block_size >= 788 else ""
                ansi_value = read_string(data, cursor + 8)
                target = valid_target(unicode_value) or valid_target(ansi_value)
                if target:
                    return target
            cursor += block_size
    except (OSError, ValueError):
        return None
    return None


def find_desktop_browser(browser_key, directories=None, shortcut_resolver=None):
    aliases = DESKTOP_BROWSER_ALIASES[browser_key]
    executable_names = DESKTOP_BROWSER_EXECUTABLES[browser_key]
    resolver = shortcut_resolver or resolve_windows_shortcut
    for directory in directories or desktop_directories():
        if not Path(directory).is_dir():
            continue
        try:
            candidates = sorted(
                (item for item in Path(directory).iterdir() if item.suffix.casefold() in {".lnk", ".exe"}),
                key=lambda item: (item.suffix.casefold() != ".lnk", item.name.casefold()),
            )
        except OSError:
            continue
        for candidate in candidates:
            label = candidate.stem.casefold()
            if not any(alias.casefold() in label for alias in aliases):
                continue
            target = resolver(candidate) if candidate.suffix.casefold() == ".lnk" else str(candidate.resolve())
            if target and Path(target).is_file() and Path(target).name.casefold() in executable_names:
                return str(Path(target).resolve())
    return None


def standard_browser_paths(browser_key):
    paths = {
        "edge": [
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        ],
        "chrome": [
            Path(os.getenv("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ],
        "quark": [
            Path(os.getenv("PROGRAMFILES", "")) / "Quark/quark.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Quark/quark.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Quark/quark.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Quark/quark.exe",
        ],
        "360": [
            Path(os.getenv("PROGRAMFILES", "")) / "360/360se6/Application/360se.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "360/360se6/Application/360se.exe",
            Path(os.getenv("APPDATA", "")) / "360se6/Application/360se.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "360se6/Application/360se.exe",
            Path(os.getenv("PROGRAMFILES", "")) / "360Chrome/Chrome/Application/360chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "360Chrome/Chrome/Application/360chrome.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/Application/360chrome.exe",
            Path(os.getenv("APPDATA", "")) / "360Chrome/Chrome/Application/360chrome.exe",
        ],
    }
    return paths[browser_key]


def detect_browser_executable(browser_key):
    desktop_path = find_desktop_browser(browser_key)
    if desktop_path:
        return desktop_path, "desktop"
    installed_path = find_first(standard_browser_paths(browser_key))
    return installed_path, "standard" if installed_path else "missing"


def browser_catalog():
    edge, edge_source = detect_browser_executable("edge")
    chrome, chrome_source = detect_browser_executable("chrome")
    quark, quark_source = detect_browser_executable("quark")
    browser360, browser360_source = detect_browser_executable("360")
    browser360_user_data = (
        Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/User Data"
        if browser360 and Path(browser360).name.casefold() == "360chrome.exe"
        else Path(os.getenv("APPDATA", "")) / "360se6/User Data"
    )
    catalog = {
        "edge": {
            "key": "edge", "name": "Edge", "short_name": "E", "color": "#0AA8A7", "path": edge, "available": bool(edge),
            "default_path": edge, "custom_path": False, "path_source": edge_source,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data"), "launch_args": [],
        },
        "chrome": {
            "key": "chrome", "name": "Chrome", "short_name": "C", "color": "#4285F4", "path": chrome, "available": bool(chrome),
            "default_path": chrome, "custom_path": False, "path_source": chrome_source,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/User Data"), "launch_args": [],
        },
        "quark": {
            "key": "quark", "name": "夸克", "short_name": "夸", "color": "#6A5CFF", "path": quark, "available": bool(quark),
            "default_path": quark, "custom_path": False, "path_source": quark_source,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Quark/User Data"),
            "launch_args": ["--launch-from=desktopicon", "--brand-quark"],
        },
        "360": {
            "key": "360", "name": "360浏览器", "short_name": "360", "color": "#28B55B",
            "path": browser360, "available": bool(browser360),
            "default_path": browser360, "custom_path": False, "path_source": browser360_source,
            "user_data": str(browser360_user_data), "launch_args": [],
        },
    }
    saved_paths = read_json_file(BROWSER_PATHS_FILE, {}, "browser.discovery", "browser_paths")
    for key, value in saved_paths.items():
        if key in catalog and isinstance(value, str) and Path(value).is_file():
            catalog[key]["path"] = str(Path(value).resolve())
            catalog[key]["available"] = True
            catalog[key]["custom_path"] = True
            catalog[key]["path_source"] = "custom"
    RUNTIME_LOG.info(
        "browser.discovery", "browser.discovery.completed", "浏览器路径检测完成",
        result="success",
        browsers={key: {"available": item["available"], "path_source": item["path_source"], "path": safe_path(item["path"]) if item["path"] else None} for key, item in catalog.items()},
    )
    return catalog


BROWSERS = browser_catalog()


BUILTIN_PLATFORMS = PLATFORMS
BUILTIN_PLATFORM_KEYS = {platform.key for platform in BUILTIN_PLATFORMS}


def custom_platform_from_record(record):
    url = record["url"]
    key = record.get("key") or f"custom_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"
    name = record["name"].strip()
    color = record.get("color") or f"#{hashlib.md5(url.encode('utf-8')).hexdigest()[:6]}"
    return Platform(
        key=key, name=name, short_name=name[:1].upper(), url=url, color=color,
        input_selectors=(
            "textarea[placeholder]", "textarea", "div[contenteditable='true'][role='textbox']",
            "div[contenteditable='true']",
        ),
        send_selectors=(
            "button[aria-label*='发送']", "button[aria-label*='Send']", "button[type='submit']",
            "[role='button'][aria-label*='发送']", "[role='button'][aria-label*='Send']",
        ),
        stop_selectors=(
            "button[aria-label*='停止']", "button[aria-label*='Stop']",
            "[role='button'][aria-label*='停止']", "[role='button'][aria-label*='Stop']",
        ),
        response_selectors=(
            "[data-message-author-role='assistant']", "[class*='markdown']", "[class*='assistant']",
            "[class*='answer']", "article",
        ),
    )


def load_custom_platform_records():
    records = read_json_file(CUSTOM_PLATFORMS_FILE, [], "platform.registry", "custom_platforms")
    if not isinstance(records, list):
        RUNTIME_LOG.warning("platform.registry", "data.schema.invalid", "自定义平台数据类型无效，已使用空列表", data_kind="custom_platforms", actual_type=type(records).__name__, result="degraded")
        return []
    return records


CUSTOM_PLATFORM_RECORDS = load_custom_platform_records()


def rebuild_platform_registry():
    global PLATFORMS, PLATFORM_MAP
    custom_platforms = []
    for record in CUSTOM_PLATFORM_RECORDS:
        try:
            custom_platforms.append(custom_platform_from_record(record))
        except Exception:
            continue
    PLATFORMS = BUILTIN_PLATFORMS + tuple(custom_platforms)
    PLATFORM_MAP = {platform.key: platform for platform in PLATFORMS}


def save_custom_platform_records(operation_id=None):
    write_json_file(CUSTOM_PLATFORMS_FILE, CUSTOM_PLATFORM_RECORDS, "platform.registry", "custom_platforms", operation_id)


def add_custom_platform(name, raw_url, operation_id=None):
    name = str(name or "").strip()
    raw_url = str(raw_url or "").strip()
    if not name:
        raise ValueError("请输入平台名称")
    if len(name) > 30:
        raise ValueError("平台名称最多 30 个字符")
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("请输入以 http:// 或 https:// 开头的完整聊天页面网址")
    normalized_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
    if any(record.get("url") == normalized_url for record in CUSTOM_PLATFORM_RECORDS):
        raise ValueError("该平台网址已经添加")
    key = f"custom_{hashlib.sha1(normalized_url.encode('utf-8')).hexdigest()[:12]}"
    record = {"key": key, "name": name, "url": normalized_url}
    CUSTOM_PLATFORM_RECORDS.append(record)
    save_custom_platform_records(operation_id)
    rebuild_platform_registry()
    RUNTIME_LOG.info("platform.registry", "platform.custom.added", "自定义平台已添加", operation_id=operation_id, object_id=key, url=safe_url(normalized_url), result="success")
    return PLATFORM_MAP[key]


def delete_custom_platform(key, operation_id=None):
    if key in BUILTIN_PLATFORM_KEYS:
        raise ValueError("内置平台不能删除")
    before = len(CUSTOM_PLATFORM_RECORDS)
    CUSTOM_PLATFORM_RECORDS[:] = [record for record in CUSTOM_PLATFORM_RECORDS if record.get("key") != key]
    if len(CUSTOM_PLATFORM_RECORDS) == before:
        raise ValueError("未找到自定义平台")
    save_custom_platform_records(operation_id)
    rebuild_platform_registry()
    RUNTIME_LOG.info("platform.registry", "platform.custom.deleted", "自定义平台已删除", operation_id=operation_id, object_id=key, result="success")


rebuild_platform_registry()


def browser_public_info(info):
    return {field: info[field] for field in (
        "key", "name", "short_name", "color", "available", "path", "default_path", "custom_path", "path_source"
    )}


def sync_browser_profile_path(key):
    if key != "360":
        return
    executable = Path(BROWSERS[key].get("path") or "")
    if executable.name.casefold() == "360chrome.exe":
        BROWSERS[key]["user_data"] = str(Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/User Data")
    else:
        BROWSERS[key]["user_data"] = str(Path(os.getenv("APPDATA", "")) / "360se6/User Data")


def configure_browser_path(key, raw_path, operation_id=None):
    if key not in BROWSERS:
        raise ValueError("未知浏览器")
    value = str(raw_path or "").strip().strip('"').strip("'")
    if value:
        candidate = Path(os.path.expandvars(value)).expanduser()
        if candidate.suffix.lower() != ".exe" or not candidate.is_file():
            raise ValueError("路径无效，请填写浏览器主程序的完整 .exe 路径")
        BROWSERS[key]["path"] = str(candidate.resolve())
        BROWSERS[key]["available"] = True
        BROWSERS[key]["custom_path"] = True
        BROWSERS[key]["path_source"] = "custom"
    else:
        default_path, path_source = detect_browser_executable(key)
        BROWSERS[key]["default_path"] = default_path
        BROWSERS[key]["path"] = default_path
        BROWSERS[key]["available"] = bool(default_path and Path(default_path).is_file())
        BROWSERS[key]["custom_path"] = False
        BROWSERS[key]["path_source"] = path_source
    sync_browser_profile_path(key)
    saved = {
        browser_key: info["path"]
        for browser_key, info in BROWSERS.items()
        if info["custom_path"] and info["path"]
    }
    write_json_file(BROWSER_PATHS_FILE, saved, "browser.configuration", "browser_paths", operation_id)
    RUNTIME_LOG.info(
        "browser.configuration", "browser.path.changed", "浏览器路径配置已更新",
        operation_id=operation_id, object_id=key, result="success",
        available=BROWSERS[key]["available"], path_source=BROWSERS[key]["path_source"],
        executable=safe_path(BROWSERS[key]["path"]) if BROWSERS[key]["path"] else None,
    )
    return browser_public_info(BROWSERS[key])


def _copy_login_item(source, destination):
    if not source.exists():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source, destination, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "DawnCache", "Media Cache", "*.tmp", "LOCK"),
        )
    else:
        shutil.copy2(source, destination)
    return 1


def import_existing_login(browser_key, destination_root):
    """One-time import of the last-used profile into an isolated automation profile."""
    marker = destination_root / ".login-imported.json"
    if marker.exists():
        return {"status": "already", "copied": 0, "errors": 0}
    source_root = Path(BROWSERS[browser_key]["user_data"])
    if not source_root.is_dir():
        return {"status": "missing", "copied": 0, "errors": 0}
    profile_name = "Default"
    try:
        local_state_data = json.loads((source_root / "Local State").read_text(encoding="utf-8"))
        candidate = local_state_data.get("profile", {}).get("last_used", "Default")
        if candidate and (source_root / candidate).is_dir():
            profile_name = candidate
    except Exception:
        pass
    source_profile = source_root / profile_name
    if not source_profile.is_dir():
        try:
            candidates = [p for p in source_root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))]
        except OSError:
            candidates = []
        if not candidates:
            return {"status": "missing", "copied": 0, "errors": 0}
        source_profile = candidates[0]
        profile_name = source_profile.name

    copied, errors = 0, 0
    try:
        copied += _copy_login_item(source_root / "Local State", destination_root / "Local State")
    except OSError:
        errors += 1
    for item in (
        "Network", "Local Storage", "Session Storage", "IndexedDB", "Service Worker", "Sessions",
        "Preferences", "Secure Preferences", "Web Data", "Login Data",
    ):
        try:
            copied += _copy_login_item(source_profile / item, destination_root / "Default" / item)
        except OSError:
            errors += 1
    if copied:
        marker.write_text(json.dumps({
            "source": str(source_root), "profile": profile_name, "copied": copied,
            "errors": errors, "time": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "copied" if copied else "locked", "copied": copied, "errors": errors, "profile": profile_name}


def load_history():
    value = read_json_file(HISTORY_FILE, [], "history", "history")
    if not isinstance(value, list):
        RUNTIME_LOG.warning("history", "data.schema.invalid", "历史记录数据类型无效，已使用空列表", data_kind="history", actual_type=type(value).__name__, result="degraded")
        return []
    return value


def write_history(items, operation_id=None):
    write_json_file(HISTORY_FILE, items[:100], "history", "history", operation_id)


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.browser_message = "本地运行"
        self.selected_browser = next((key for key in ("edge", "chrome", "quark", "360") if BROWSERS[key]["available"]), "edge")
        self.browser_started = False
        self.last_prompt = ""
        self.last_attachments = []
        self.last_modes = {}
        self.active_tasks = {}
        self.capabilities = {
            p.key: self._new_capability(p)
            for p in PLATFORMS
        }
        self.history = load_history()
        self.statuses = {
            p.key: {
                "status": "未开始", "detail": "等待发送", "preview": "",
                "full_text": "", "elapsed": 0, "updated_at": time.time(),
            }
            for p in PLATFORMS
        }

    @staticmethod
    def _new_capability(platform):
        return {
            "page_state": "unchecked",
            "page_label": "未打开",
            "login_state": "unchecked",
            "login_label": "未检测",
            "input_state": "unchecked",
            "input_label": "未检测",
            "capability_detail": "拉起平台后自动检测登录状态与可用模式",
            "mode_status": "unchecked",
            "available_modes": [{"key": "default", "label": "保持当前模式"}],
            "mode_aliases": {},
            "detected_at": 0,
        }

    def snapshot(self):
        with self.lock:
            return {
                "app_name": APP_NAME,
                "version": VERSION,
                "browser_message": self.browser_message,
                "selected_browser": self.selected_browser,
                "browser_started": self.browser_started,
                "browsers": [
                    browser_public_info(info)
                    for info in BROWSERS.values()
                ],
                "last_prompt": self.last_prompt,
                "platforms": [
                    {
                        "key": p.key, "name": p.name, "short_name": p.short_name,
                        "color": p.color, "url": p.url, "custom": p.key not in BUILTIN_PLATFORM_KEYS,
                        "page_state": self.capabilities.get(p.key, self._new_capability(p))["page_state"],
                        "page_label": self.capabilities.get(p.key, self._new_capability(p))["page_label"],
                        "login_state": self.capabilities.get(p.key, self._new_capability(p))["login_state"],
                        "login_label": self.capabilities.get(p.key, self._new_capability(p))["login_label"],
                        "input_state": self.capabilities.get(p.key, self._new_capability(p))["input_state"],
                        "input_label": self.capabilities.get(p.key, self._new_capability(p))["input_label"],
                        "capability_detail": self.capabilities.get(p.key, self._new_capability(p))["capability_detail"],
                        "mode_status": self.capabilities.get(p.key, self._new_capability(p))["mode_status"],
                        "modes": json.loads(json.dumps(
                            self.capabilities.get(p.key, self._new_capability(p))["available_modes"],
                            ensure_ascii=False,
                        )),
                    }
                    for p in PLATFORMS
                ],
                "statuses": json.loads(json.dumps(self.statuses, ensure_ascii=False)),
                "history": self.history[:100],
            }

    def apply_event(self, event):
        with self.lock:
            if event.get("kind") == "browser":
                old_message = self.browser_message
                self.browser_message = event.get("message", "本地运行")
                if old_message != self.browser_message:
                    RUNTIME_LOG.info(
                        "state", "state.browser.changed", "浏览器运行状态发生变化",
                        operation_id=event.get("operation_id"), object_id=event.get("browser_key"),
                        previous=old_message, current=self.browser_message, reason=event.get("reason", "automation_event"),
                    )
                return
            if event.get("kind") == "status":
                key = event.get("platform")
                if key not in self.statuses:
                    return
                target = self.statuses[key]
                old_status = target.get("status")
                for field in ("status", "detail", "preview", "full_text", "elapsed"):
                    if field in event:
                        target[field] = event[field]
                for field in ("task_id", "operation_id"):
                    if event.get(field):
                        target[field] = event[field]
                target["updated_at"] = time.time()
                new_status = target.get("status")
                if new_status != old_status:
                    RUNTIME_LOG.info(
                        "state", "state.platform_status.changed", "平台任务状态发生变化",
                        operation_id=event.get("operation_id"), task_id=event.get("task_id") or target.get("task_id"),
                        object_id=key, previous=old_status, current=new_status,
                        reason=event.get("reason", "automation_event"), elapsed_seconds=target.get("elapsed", 0),
                        has_preview=bool(target.get("preview")),
                    )
                return
            if event.get("kind") == "capability":
                key = event.get("platform")
                if key not in self.capabilities:
                    return
                target = self.capabilities[key]
                old_values = {field: target.get(field) for field in ("page_state", "login_state", "input_state", "mode_status")}
                for field in (
                    "page_state", "page_label",
                    "login_state", "login_label", "capability_detail", "mode_status",
                    "input_state", "input_label",
                    "available_modes", "mode_aliases", "detected_at",
                ):
                    if field in event:
                        target[field] = event[field]
                new_values = {field: target.get(field) for field in old_values}
                if new_values != old_values:
                    RUNTIME_LOG.info(
                        "state", "state.capability.changed", "平台能力状态发生变化",
                        operation_id=event.get("operation_id"), object_id=key,
                        previous=old_values, current=new_values, reason=event.get("reason", "inspection"),
                    )

    def start_task(self, prompt, keys, attachments=None, modes=None, operation_id=None, task_id=None):
        task_id = task_id or new_correlation_id("task")
        with self.lock:
            self.last_prompt = prompt
            self.last_attachments = list(attachments or [])
            self.last_modes = dict(modes or {})
            for key in keys:
                self.active_tasks[key] = task_id
                self.statuses[key] = {
                    "status": "准备中", "detail": "等待浏览器就绪", "preview": "",
                    "full_text": "", "elapsed": 0, "updated_at": time.time(),
                    "task_id": task_id, "operation_id": operation_id,
                }
            self.history.insert(0, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "prompt": prompt, "platforms": keys,
                "attachments": [item["name"] for item in self.last_attachments],
                "modes": self.last_modes,
                "task_id": task_id,
            })
            self.history = self.history[:100]
            write_history(self.history, operation_id)
        RUNTIME_LOG.info(
            "task", "task.created", "同步发送任务已创建",
            operation_id=operation_id, task_id=task_id, result="created",
            platform_count=len(keys), platforms=keys, prompt_length=len(prompt),
            attachment_count=len(attachments or []), modes={key: modes.get(key, "default") for key in keys} if modes else {},
        )
        return task_id

    def select_browser(self, key, started=None):
        with self.lock:
            previous_key = self.selected_browser
            changed = self.selected_browser != key
            self.selected_browser = key
            if started is not None:
                self.browser_started = started
            if changed:
                for platform in PLATFORMS:
                    self.capabilities[platform.key] = self._new_capability(platform)
            if changed or started is not None:
                RUNTIME_LOG.info(
                    "state", "state.browser_selection.changed", "浏览器选择状态已更新",
                    object_id=key, previous=previous_key,
                    current=key, browser_started=self.browser_started, reason="user_or_automation",
                )

    def add_platform(self, platform):
        with self.lock:
            self.capabilities[platform.key] = self._new_capability(platform)
            self.statuses[platform.key] = {
                "status": "未开始", "detail": "通用适配，等待发送", "preview": "",
                "full_text": "", "elapsed": 0, "updated_at": time.time(),
            }

    def remove_platform(self, key):
        with self.lock:
            self.capabilities.pop(key, None)
            self.statuses.pop(key, None)

    def allowed_mode_keys(self, key):
        with self.lock:
            capability = self.capabilities.get(key, {})
            return {item["key"] for item in capability.get("available_modes", [])}

    def mode_alias(self, key, mode_key):
        with self.lock:
            return self.capabilities.get(key, {}).get("mode_aliases", {}).get(mode_key)


class BrowserAutomation:
    """All Playwright operations stay on one dedicated asyncio thread."""

    def __init__(self, event_queue: queue.Queue, initial_browser="edge"):
        self.event_queue = event_queue
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="browser-automation")
        self.thread.start()
        self.context = None
        self.playwright = None
        self.pages = {}
        self.inspections = {}
        self.tasks = {}
        self.platform_context = {}
        self.monitor_task = None
        self.browser_key = initial_browser
        self.browser_started = False
        self.selection_lock = threading.Lock()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def emit(self, kind, platform_key=None, **payload):
        if platform_key and platform_key in self.platform_context:
            context = self.platform_context[platform_key]
            payload.setdefault("operation_id", context.get("operation_id"))
            payload.setdefault("task_id", context.get("task_id"))
        self.event_queue.put({"kind": kind, "platform": platform_key, **payload})

    def _submit(self, coroutine, *, operation, operation_id=None, task_id=None, object_id=None, fields=None):
        operation_id = operation_id or new_correlation_id("op")
        started = RUNTIME_LOG.start_operation(
            operation_id, operation, task_id=task_id, object_id=object_id, **(fields or {}),
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def completed(done):
            try:
                outcome = done.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                RUNTIME_LOG.finish_operation(operation_id, operation, started, "cancelled", task_id=task_id, object_id=object_id)
            except Exception as exc:
                if not getattr(exc, "_runtime_logged", False):
                    RUNTIME_LOG.exception(
                        "background", "background.task.failed", "后台任务执行失败", exc,
                        operation_id=operation_id, task_id=task_id, object_id=object_id, operation=operation,
                    )
                RUNTIME_LOG.finish_operation(operation_id, operation, started, "failure", task_id=task_id, object_id=object_id, error_code=getattr(exc, "errno", None))
            else:
                result = outcome.get("_operation_result", "success") if isinstance(outcome, dict) else "success"
                summary = outcome.get("summary", {}) if isinstance(outcome, dict) else {}
                RUNTIME_LOG.finish_operation(operation_id, operation, started, result, task_id=task_id, object_id=object_id, **summary)

        future.add_done_callback(completed)
        return future

    def open_platform(self, key, operation_id=None):
        self.platform_context[key] = {"operation_id": operation_id}
        self._submit(self._open_platform(PLATFORM_MAP[key], focus=True), operation="platform.open", operation_id=operation_id, object_id=key)

    def open_many(self, keys, operation_id=None):
        for key in keys:
            self.platform_context[key] = {"operation_id": operation_id}
        self._submit(self._open_many(keys), operation="platform.launch_many", operation_id=operation_id, fields={"platform_count": len(keys), "platforms": keys})

    def inspect_many(self, keys, operation_id=None):
        for key in keys:
            self.platform_context[key] = {"operation_id": operation_id}
        self._submit(self._inspect_many(keys), operation="platform.inspect_many", operation_id=operation_id, fields={"platform_count": len(keys), "platforms": keys})

    def remove_platform(self, key, operation_id=None):
        self._submit(self._remove_platform(key), operation="platform.remove", operation_id=operation_id, object_id=key)

    def send(self, keys, prompt, attachments=None, modes=None, operation_id=None, task_id=None):
        for key in keys:
            self.platform_context[key] = {"operation_id": operation_id, "task_id": task_id}
        self._submit(
            self._send_batch(keys, prompt, attachments or [], modes or {}),
            operation="task.send", operation_id=operation_id, task_id=task_id,
            fields={"platform_count": len(keys), "platforms": keys, "attachment_count": len(attachments or []), "prompt_length": len(prompt)},
        )

    def cancel(self, key, operation_id=None):
        task_id = self.platform_context.get(key, {}).get("task_id")
        self._submit(self._cancel(key), operation="task.cancel", operation_id=operation_id, task_id=task_id, object_id=key)

    def retry(self, key, prompt, attachments=None, modes=None, operation_id=None, task_id=None):
        RUNTIME_LOG.info("task", "task.retry.requested", "平台任务请求重试", operation_id=operation_id, task_id=task_id, object_id=key, retry_attempt=1)
        self.send([key], prompt, attachments or [], modes or {}, operation_id, task_id)

    def switch_browser(self, key, keys, operation_id=None):
        self._submit(self._switch_browser(key, keys), operation="browser.switch", operation_id=operation_id, object_id=key, fields={"platform_count": len(keys)})

    def reload_browser(self, key, keys, operation_id=None):
        self._submit(self._switch_browser(key, keys, force=True), operation="browser.reload", operation_id=operation_id, object_id=key, fields={"platform_count": len(keys)})

    def close(self):
        operation_id = new_correlation_id("op")
        started = RUNTIME_LOG.start_operation(operation_id, "browser.shutdown")
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=8)
        except Exception as exc:
            RUNTIME_LOG.exception("browser", "browser.shutdown.failed", "浏览器资源释放失败", exc, result="failure")
            RUNTIME_LOG.finish_operation(operation_id, "browser.shutdown", started, "failure", error_code=getattr(exc, "errno", None))
        else:
            RUNTIME_LOG.finish_operation(operation_id, "browser.shutdown", started, "success")
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _ensure_browser(self):
        if self.context:
            return
        from playwright.async_api import async_playwright

        with self.selection_lock:
            browser_key = self.browser_key
            self.browser_started = True
        browser_info = BROWSERS[browser_key]
        if not browser_info["available"]:
            with self.selection_lock:
                self.browser_started = False
            raise RuntimeError(f"未检测到 {browser_info['name']}，请先安装或改选其他浏览器")
        self.emit("browser", message=f"正在启动 {browser_info['name']}…", browser_key=browser_key, locked=True)
        launch_started = time.monotonic()
        RUNTIME_LOG.info(
            "browser", "external.browser_launch.requested", "请求启动受控浏览器",
            object_id=browser_key, executable=safe_path(browser_info["path"]),
            profile=safe_path(DATA_DIR / f"browser-profile-{browser_key}"), result="requested",
        )
        self.playwright = await async_playwright().start()
        profile_dir = DATA_DIR / f"browser-profile-{browser_key}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.emit("browser", message=f"正在读取 {browser_info['name']} 已有登录状态…", browser_key=browser_key, started=False)
        import_result = await asyncio.to_thread(import_existing_login, browser_key, profile_dir)
        RUNTIME_LOG.info(
            "browser", "browser.profile_import.finished", "浏览器登录资料导入结束",
            object_id=browser_key, result=import_result.get("status"),
            copied_count=import_result.get("copied", 0), error_count=import_result.get("errors", 0),
        )
        if import_result["status"] == "copied":
            self.emit("browser", message=f"已导入 {browser_info['name']} 最近使用的登录状态", browser_key=browser_key, started=False)
        elif import_result["status"] == "locked":
            self.emit("browser", message=f"未能导入登录状态，请关闭普通 {browser_info['name']} 后重试", browser_key=browser_key, started=False)
        options = dict(
            user_data_dir=str(profile_dir), headless=False,
            executable_path=browser_info["path"],
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", *browser_info.get("launch_args", [])],
            viewport={"width": 1280, "height": 900}, locale="zh-CN",
        )
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(**options)
        except Exception as launch_error:
            with self.selection_lock:
                self.browser_started = False
            RUNTIME_LOG.exception(
                "browser", "external.browser_launch.failed", "受控浏览器启动失败", launch_error,
                object_id=browser_key, executable=safe_path(browser_info["path"]), result="failure",
                duration_ms=round((time.monotonic() - launch_started) * 1000, 2),
            )
            wrapped = RuntimeError(f"无法启动 {browser_info['name']}：{launch_error}")
            wrapped._runtime_logged = True
            raise wrapped from launch_error
        self.context.set_default_timeout(3500)
        if not self.monitor_task or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self._monitor_platform_states())
        self.emit("browser", message=f"{browser_info['name']} 已就绪", browser_key=browser_key, locked=True)
        RUNTIME_LOG.info(
            "browser", "external.browser_launch.verified", "受控浏览器启动并验证完成",
            object_id=browser_key, result="success", duration_ms=round((time.monotonic() - launch_started) * 1000, 2),
        )

    async def _monitor_platform_states(self):
        """Continuously refresh non-invasive page/login/input signals."""
        try:
            while self.context:
                await asyncio.sleep(8)
                jobs = []
                for key, page in list(self.pages.items()):
                    running = self.tasks.get(key)
                    if key not in PLATFORM_MAP or not page:
                        continue
                    if page.is_closed():
                        capability = {
                            "page_state": "closed", "page_label": "页面已关闭",
                            "login_state": "unknown", "login_label": "未检测",
                            "input_state": "unavailable", "input_label": "不可输入",
                            "capability_detail": "平台页面已关闭；请点击打开重新拉起",
                            "mode_status": "unavailable",
                            "available_modes": [{"key": "default", "label": "保持当前模式"}],
                            "mode_aliases": {}, "detected_at": time.time(),
                        }
                        self.inspections[key] = capability
                        self.emit("capability", key, **capability)
                        if key not in self.tasks:
                            self.emit("status", key, status="需人工处理", detail=capability["capability_detail"])
                        continue
                    if running and not running.done():
                        continue
                    jobs.append(self._monitor_one_platform(key, page))
                if jobs:
                    results = await asyncio.gather(*jobs, return_exceptions=True)
                    failures = [item for item in results if isinstance(item, Exception)]
                    if failures:
                        RUNTIME_LOG.rate_limited(
                            logging.WARNING, "monitor", "monitor.poll.failed", "平台状态轮询出现异常",
                            rate_key="monitor-poll-failure", failure_count=len(failures), checked_count=len(results),
                        )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            RUNTIME_LOG.exception("monitor", "monitor.loop.failed", "持续状态监测任务异常退出", exc, result="failure")

    async def _monitor_one_platform(self, key, page):
        platform = PLATFORM_MAP.get(key)
        if not platform or self.pages.get(key) is not page or page.is_closed():
            return
        capability = await self._inspect_platform(
            page, platform, detect_modes=False, announce=False,
        )
        self.inspections[key] = capability
        # Do not replace the progress/result label of a task that has already run.
        if key not in self.tasks:
            self.emit(
                "status", key,
                status=capability_open_status(capability),
                detail=capability.get("capability_detail", "页面状态已自动更新"),
            )

    async def _open_many(self, keys):
        valid_keys = [key for key in keys if key in PLATFORM_MAP]
        if not valid_keys:
            return
        for key in valid_keys:
            self.emit("status", key, status="准备中", detail="等待统一拉起")
        await self._ensure_browser()
        results = await asyncio.gather(
            *(self._open_platform(PLATFORM_MAP[key]) for key in valid_keys),
            return_exceptions=True,
        )
        for key, result in zip(valid_keys, results):
            if not isinstance(result, Exception):
                capability = self.inspections.get(key, {})
                status = capability_open_status(capability)
                self.emit("status", key, status=status, detail=capability.get("capability_detail", "页面已打开"))
        failure_count = sum(isinstance(item, Exception) for item in results)
        return {"_operation_result": "success" if not failure_count else "degraded", "summary": {"success_count": len(results) - failure_count, "failure_count": failure_count}}

    async def _inspect_many(self, keys):
        valid_keys = [key for key in keys if key in PLATFORM_MAP]
        if not valid_keys:
            return
        await self._ensure_browser()
        results = await asyncio.gather(
            *(self._open_platform(PLATFORM_MAP[key]) for key in valid_keys),
            return_exceptions=True,
        )
        for key, result in zip(valid_keys, results):
            if not isinstance(result, Exception):
                capability = self.inspections.get(key, {})
                self.emit(
                    "status", key,
                    status=capability_open_status(capability),
                    detail=capability.get("capability_detail", "页面状态已重新检测"),
                )
        failure_count = sum(isinstance(item, Exception) for item in results)
        return {"_operation_result": "success" if not failure_count else "degraded", "summary": {"success_count": len(results) - failure_count, "failure_count": failure_count}}

    async def _remove_platform(self, key):
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        self.inspections.pop(key, None)
        page = self.pages.pop(key, None)
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass

    async def _switch_browser(self, key, keys, force=False):
        with self.selection_lock:
            old_key = self.browser_key
            was_started = self.browser_started
        if key == old_key and not force:
            return
        new_info = BROWSERS[key]
        self.emit("browser", message=f"正在切换到 {new_info['name']}…", browser_key=key, started=False)
        for platform_key, task in list(self.tasks.items()):
            if not task.done():
                task.cancel()
                self.emit("status", platform_key, status="已取消", detail="浏览器已切换，请重新发送")
        await asyncio.sleep(0)
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        self.monitor_task = None
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.context = None
        self.playwright = None
        self.pages = {}
        self.inspections = {}
        self.tasks = {}
        with self.selection_lock:
            self.browser_key = key
            self.browser_started = False
        if was_started and keys:
            self.emit("browser", message=f"正在用 {new_info['name']} 重新打开全部平台…", browser_key=key, started=False)
            await self._ensure_browser()
            await asyncio.gather(
                *(self._open_platform(PLATFORM_MAP[platform_key]) for platform_key in keys if platform_key in PLATFORM_MAP),
                return_exceptions=True,
            )

    async def _open_platform(self, platform: Platform, focus=False):
        try:
            await self._ensure_browser()
            page = self.pages.get(platform.key)
            navigation_warning = ""
            navigated = False
            if not page or page.is_closed():
                page = await self.context.new_page()
                self.pages[platform.key] = page
                try:
                    await page.goto(platform.url, wait_until="domcontentloaded", timeout=60000)
                    navigated = True
                except Exception as exc:
                    current_host = urllib.parse.urlparse(page.url or "").netloc
                    target_host = urllib.parse.urlparse(platform.url).netloc
                    if current_host and (current_host == target_host or current_host.endswith("." + target_host)) and "timeout" in str(exc).casefold():
                        navigation_warning = "页面已显示，但加载等待超时"
                        navigated = True
                        RUNTIME_LOG.warning(
                            "browser", "external.navigation.timeout_recovered", "平台导航超时，但页面内容已显示，继续检测",
                            operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                            object_id=platform.key, url=safe_url(platform.url), result="degraded", error_type=type(exc).__name__,
                        )
                    else:
                        raise
            elif urllib.parse.urlparse(platform.url).netloc not in page.url:
                try:
                    await page.goto(platform.url, wait_until="domcontentloaded", timeout=60000)
                    navigated = True
                except Exception as exc:
                    if urllib.parse.urlparse(platform.url).netloc in (page.url or "") and "timeout" in str(exc).casefold():
                        navigation_warning = "页面已显示，但加载等待超时"
                        navigated = True
                        RUNTIME_LOG.warning(
                            "browser", "external.navigation.timeout_recovered", "平台导航超时，但页面内容已显示，继续检测",
                            operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                            object_id=platform.key, url=safe_url(platform.url), result="degraded", error_type=type(exc).__name__,
                        )
                    else:
                        raise
            if navigated:
                # SPA 页面会在 DOMContentLoaded 后继续渲染或跳转，先等待最终页面稳定。
                await asyncio.sleep(3.0)
            capability = await self._inspect_platform(page, platform, navigation_warning=navigation_warning)
            self.inspections[platform.key] = capability
            if focus:
                await page.bring_to_front()
            return page
        except Exception as exc:
            self.emit("status", platform.key, status="失败", detail=f"无法打开平台：{str(exc)[:220]}")
            RUNTIME_LOG.exception(
                "browser", "external.platform_open.failed", "平台页面打开失败", exc,
                operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                task_id=self.platform_context.get(platform.key, {}).get("task_id"),
                object_id=platform.key, url=safe_url(platform.url), result="failure",
            )
            try:
                exc._runtime_logged = True
            except Exception:
                pass
            raise

    async def _send_batch(self, keys, prompt, attachments, modes):
        jobs = []
        for key in keys:
            old = self.tasks.get(key)
            if old and not old.done():
                old.cancel()
            task = asyncio.create_task(
                self._send_one(PLATFORM_MAP[key], prompt, attachments, modes.get(key, "default"))
            )
            self.tasks[key] = task
            jobs.append(task)
        results = await asyncio.gather(*jobs, return_exceptions=True)
        normalized = ["failure" if isinstance(item, Exception) else item for item in results]
        counts = {name: normalized.count(name) for name in ("success", "failure", "manual", "cancelled", "timeout")}
        if counts["failure"]:
            result = "failure" if counts["failure"] == len(results) else "degraded"
        elif counts["timeout"]:
            result = "timeout" if counts["timeout"] == len(results) else "degraded"
        elif counts["cancelled"]:
            result = "cancelled" if counts["cancelled"] == len(results) else "degraded"
        elif counts["manual"]:
            result = "degraded"
        else:
            result = "success"
        return {"_operation_result": result, "summary": counts}

    async def _visible_locator(self, page, selectors, timeout_seconds=15):
        end = time.monotonic() + timeout_seconds
        while time.monotonic() < end:
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    count = min(await locator.count(), 20)
                    for index in range(count - 1, -1, -1):
                        candidate = locator.nth(index)
                        if await candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            await asyncio.sleep(0.5)
        return None

    async def _any_visible(self, page, selectors):
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(await locator.count(), 12)
                for index in range(count):
                    if await locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    async def _visible_named(self, page, names):
        for name in names:
            for role in ("button", "link"):
                try:
                    locator = page.get_by_role(role, name=name, exact=True)
                    count = min(await locator.count(), 8)
                    for index in range(count):
                        if await locator.nth(index).is_visible():
                            return True
                except Exception:
                    continue
        return False

    async def _detect_modes(self, page, platform):
        available = [{"key": "default", "label": "保持当前模式"}]
        aliases_by_key = {}
        opened_trigger = False

        for selector in platform.mode_trigger_selectors:
            try:
                trigger = page.locator(selector).last
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click()
                    await asyncio.sleep(0.45)
                    opened_trigger = True
                    break
            except Exception:
                continue

        discovered_texts = []
        for selector in MODE_OPTION_SELECTORS:
            try:
                locator = page.locator(selector)
                count = min(await locator.count(), 60)
                for index in range(count):
                    item = locator.nth(index)
                    if not await item.is_visible():
                        continue
                    text = " ".join((await item.inner_text(timeout=900)).split())
                    if 1 < len(text) <= 42 and text not in discovered_texts:
                        discovered_texts.append(text)
            except Exception:
                continue

        for mode_key, label, aliases in platform.modes:
            if mode_key == "default":
                continue
            matched = None
            for alias in aliases:
                if any(alias.casefold() in text.casefold() for text in discovered_texts):
                    matched = next(text for text in discovered_texts if alias.casefold() in text.casefold())
                    break
                for role in ("button", "menuitem", "option", "radio"):
                    try:
                        locator = page.get_by_role(role, name=alias, exact=True)
                        count = min(await locator.count(), 8)
                        if any([await locator.nth(index).is_visible() for index in range(count)]):
                            matched = alias
                            break
                    except Exception:
                        continue
                if matched:
                    break
            if matched:
                available.append({"key": mode_key, "label": matched})
                aliases_by_key[mode_key] = matched

        known_labels = {item["label"].casefold() for item in available}
        for text in discovered_texts:
            folded = text.casefold()
            if folded in known_labels or not any(keyword in folded for keyword in MODE_KEYWORDS):
                continue
            mode_key = "detected-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            available.append({"key": mode_key, "label": text})
            aliases_by_key[mode_key] = text
            known_labels.add(folded)

        if opened_trigger:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        return available, aliases_by_key, opened_trigger

    async def _inspect_platform(self, page, platform, navigation_warning="", detect_modes=True, announce=True):
        if announce:
            self.emit(
                "capability", platform.key,
                page_state="loading", page_label="正在加载",
                login_state="detecting", login_label="检测中",
                input_state="detecting", input_label="检测中",
                capability_detail="正在识别登录状态与可用模式", mode_status="detecting",
            )
        try:
            await asyncio.sleep(0.65)
            url = (page.url or "").casefold()
            verification = any(token in url for token in ("captcha", "challenge", "verify"))
            if not verification:
                for text in VERIFICATION_TEXTS:
                    try:
                        locator = page.get_by_text(text, exact=False)
                        if await locator.count() and await locator.last.is_visible():
                            verification = True
                            break
                    except Exception:
                        continue

            rules = LOGIN_RULES.get(platform.key, {"in": (), "out": ()})
            input_timeout = 4.5 if platform.key in {"kimi", "zhipu"} else 2.2
            has_input = bool(await self._visible_locator(
                page, platform.input_selectors, timeout_seconds=input_timeout,
            ))
            has_account = await self._any_visible(page, rules.get("in", ()))
            has_login = await self._any_visible(page, rules.get("out", ())) or await self._visible_named(page, LOGIN_TEXTS)
            has_account = infer_platform_account(platform.key, has_input, has_login, has_account)
            try:
                body_text = " ".join((await page.locator("body").inner_text(timeout=1500)).split())
            except Exception:
                body_text = ""
            current_url = (page.url or url).casefold()
            restricted = "doubao-region-ban" in current_url or any(text.casefold() in body_text.casefold() for text in RESTRICTION_TEXTS)
            signals = classify_platform_capability(
                verification=verification,
                restricted=restricted,
                has_account=has_account,
                has_login=has_login,
                has_input=has_input,
                navigation_timed_out=bool(navigation_warning),
                page_has_content=len(body_text) >= 20,
            )

            if detect_modes:
                available_modes, aliases_by_key, mode_menu_found = await self._detect_modes(page, platform)
                mode_status = "detected" if len(available_modes) > 1 else ("current_only" if has_input else "unavailable")
            else:
                previous = self.inspections.get(platform.key, {})
                available_modes = previous.get("available_modes", [{"key": "default", "label": "保持当前模式"}])
                aliases_by_key = previous.get("mode_aliases", {})
                mode_status = previous.get("mode_status", "current_only" if has_input else "unavailable")
                mode_menu_found = False
            detail = f"{signals['page_label']}；{signals['login_label']}；{signals['input_label']}"
            if navigation_warning and signals["page_state"] == "partial":
                detail += "（后台加载等待超时，不影响当前可用性判断）"
            if detect_modes and len(available_modes) > 1:
                detail += f"；检测到 {len(available_modes) - 1} 个可切换模式"
            elif detect_modes and mode_menu_found:
                detail += "；模式菜单中未识别到可用选项"
            elif detect_modes:
                detail += "；将保持页面当前模式"
            else:
                detail += "；状态持续监测中"
            result = {
                **signals,
                "capability_detail": detail,
                "mode_status": mode_status,
                "available_modes": available_modes,
                "mode_aliases": aliases_by_key,
                "detected_at": time.time(),
            }
            self.emit(
                "capability", platform.key, **result,
            )
            return result
        except Exception as exc:
            RUNTIME_LOG.rate_limited(
                logging.WARNING, "inspection", "inspection.platform.failed", "平台状态检测失败，已保留页面并等待下次检测",
                rate_key=f"inspection:{platform.key}:{type(exc).__name__}", window_seconds=60,
                operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                object_id=platform.key, error_type=type(exc).__name__, error_message=str(exc),
                stack_trace=traceback.format_exc(), result="degraded",
            )
            result = {
                "page_state": "error", "page_label": "检测异常",
                "login_state": "unknown", "login_label": "登录状态未知",
                "input_state": "unknown", "input_label": "输入能力未知",
                "capability_detail": f"状态检测异常：{str(exc)[:120]}", "mode_status": "failed",
                "available_modes": [{"key": "default", "label": "保持当前模式"}],
                "mode_aliases": {}, "detected_at": time.time(),
            }
            self.emit("capability", platform.key, **result)
            return result

    async def _read_latest_response(self, page, selectors):
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                if count:
                    text = (await locator.nth(count - 1).inner_text(timeout=1500)).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    async def _apply_mode(self, page, platform, mode_key):
        if not mode_key or mode_key == "default":
            return True
        mode = next((item for item in platform.modes if item[0] == mode_key), None)
        if mode:
            aliases = mode[2]
        else:
            detected_alias = APP.state.mode_alias(platform.key, mode_key)
            if not detected_alias:
                return False
            aliases = (detected_alias,)

        async def click_alias(allow_text=False):
            for alias in aliases:
                locators = [
                    page.get_by_role("button", name=alias, exact=True),
                    page.get_by_role("menuitem", name=alias, exact=True),
                    page.get_by_role("option", name=alias, exact=True),
                ]
                if allow_text:
                    locators.append(page.get_by_text(alias, exact=True))
                for locator in locators:
                    try:
                        count = await locator.count()
                        for index in range(count - 1, -1, -1):
                            item = locator.nth(index)
                            if await item.is_visible():
                                await item.click()
                                return True
                    except Exception:
                        continue
            return False

        if await click_alias():
            return True
        for selector in platform.mode_trigger_selectors:
            try:
                trigger = page.locator(selector).last
                if await trigger.count() and await trigger.is_visible():
                    await trigger.click()
                    await asyncio.sleep(0.35)
                    if await click_alias(allow_text=True):
                        return True
            except Exception:
                continue
        return False

    async def _upload_attachments(self, page, platform, attachments):
        if not attachments:
            return True
        paths = [item["path"] for item in attachments if Path(item["path"]).is_file()]
        if len(paths) != len(attachments):
            return False
        for selector in platform.attachment_selectors:
            try:
                locator = page.locator(selector).last
                if not await locator.count():
                    continue
                try:
                    await locator.set_input_files(paths)
                except Exception:
                    for path in paths:
                        locator = page.locator(selector).last
                        if not await locator.count():
                            return False
                        await locator.set_input_files(path)
                        await asyncio.sleep(0.35)
                await asyncio.sleep(min(4, 0.8 + len(paths) * 0.45))
                return True
            except Exception:
                continue
        return False

    async def _send_one(self, platform, prompt, attachments=None, mode_key="default"):
        started = time.monotonic()
        try:
            self.emit("status", platform.key, status="准备中", detail="正在打开平台")
            page = await self._open_platform(platform)
            if not await self._apply_mode(page, platform, mode_key):
                mode_label = next(
                    (item[1] for item in platform.modes if item[0] == mode_key),
                    APP.state.mode_alias(platform.key, mode_key) or mode_key,
                )
                self.emit(
                    "status", platform.key, status="需人工处理",
                    detail=f"未能自动切换到“{mode_label}”。请在浏览器中选择后点击重试。",
                )
                await page.bring_to_front()
                return "manual"
            if attachments:
                self.emit("status", platform.key, status="准备中", detail="正在上传附件")
                if not await self._upload_attachments(page, platform, attachments):
                    self.emit(
                        "status", platform.key, status="需人工处理",
                        detail="未找到可用的附件上传入口。请手动上传后点击重试。",
                    )
                    await page.bring_to_front()
                    return "manual"
            self.emit("status", platform.key, status="正在输入", detail="正在定位输入框")
            input_box = await self._visible_locator(page, platform.input_selectors)
            if not input_box:
                self.emit(
                    "status", platform.key, status="需人工处理",
                    detail="未找到输入框。请在浏览器中完成登录或验证，然后点击重试。",
                )
                await page.bring_to_front()
                return "manual"
            try:
                await input_box.click()
                await input_box.fill(prompt)
            except Exception:
                await input_box.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.type(prompt)

            self.emit("status", platform.key, status="已提交", detail="正在发送")
            sent = False
            for selector in platform.send_selectors:
                try:
                    button = page.locator(selector).last
                    if await button.count() and await button.is_visible() and await button.is_enabled():
                        await button.click()
                        sent = True
                        break
                except Exception:
                    continue
            if not sent:
                await input_box.press("Enter")

            self.emit("status", platform.key, status="生成中", detail="正在等待回复")
            last_text, stable_rounds, saw_output = "", 0, False
            while time.monotonic() - started < 300:
                await asyncio.sleep(1.2)
                current = await self._read_latest_response(page, platform.response_selectors)
                if current:
                    saw_output = True
                    self.emit(
                        "status", platform.key, status="生成中", detail="正在接收回复",
                        preview=current[:600], full_text=current,
                        elapsed=int(time.monotonic() - started),
                    )
                    if current == last_text:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_text = current
                stop_visible = False
                for selector in platform.stop_selectors:
                    try:
                        item = page.locator(selector).last
                        if await item.count() and await item.is_visible():
                            stop_visible = True
                            break
                    except Exception:
                        pass
                if saw_output and not stop_visible and stable_rounds >= 3:
                    self.emit(
                        "status", platform.key, status="已完成", detail="回复已完成",
                        preview=last_text[:600], full_text=last_text,
                        elapsed=int(time.monotonic() - started),
                    )
                    RUNTIME_LOG.info(
                        "task", "task.platform_result.verified", "平台回复完成状态已验证",
                        operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                        task_id=self.platform_context.get(platform.key, {}).get("task_id"),
                        object_id=platform.key, result="success", response_length=len(last_text),
                        duration_ms=round((time.monotonic() - started) * 1000, 2),
                    )
                    return "success"
            self.emit(
                "status", platform.key, status="失败", detail="等待回复超时，可打开原会话检查后重试",
                preview=last_text[:600], full_text=last_text,
                elapsed=int(time.monotonic() - started),
            )
            return "timeout"
        except asyncio.CancelledError:
            self.emit("status", platform.key, status="已取消", detail="任务已停止")
            return "cancelled"
        except Exception as exc:
            self.emit("status", platform.key, status="失败", detail=str(exc)[:300])
            RUNTIME_LOG.exception(
                "task", "task.platform_send.failed", "平台发送任务失败", exc,
                operation_id=self.platform_context.get(platform.key, {}).get("operation_id"),
                task_id=self.platform_context.get(platform.key, {}).get("task_id"),
                object_id=platform.key, result="failure",
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            return "failure"

    async def _cancel(self, key):
        task = self.tasks.get(key)
        if task and not task.done():
            task.cancel()
        page = self.pages.get(key)
        if page and not page.is_closed():
            for selector in PLATFORM_MAP[key].stop_selectors:
                try:
                    button = page.locator(selector).last
                    if await button.count() and await button.is_visible():
                        await button.click()
                        break
                except Exception:
                    continue

    async def _close(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        self.monitor_task = None
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()


class Application:
    def __init__(self):
        init_started = time.monotonic()
        self.events = queue.Queue()
        self.state = AppState()
        self.automation = BrowserAutomation(self.events, self.state.selected_browser)
        self.shutdown_event = threading.Event()
        self.stopped = False
        self.consumer_thread = threading.Thread(target=self._consume_events, daemon=True, name="state-event-consumer")
        self.consumer_thread.start()
        RUNTIME_LOG.info(
            "application", "application.initialization.completed", "应用核心模块初始化完成",
            result="success", duration_ms=round((time.monotonic() - init_started) * 1000, 2),
            platform_count=len(PLATFORMS), history_count=len(self.state.history), selected_browser=self.state.selected_browser,
        )

    def _consume_events(self):
        while not self.shutdown_event.is_set():
            try:
                event = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if event.get("kind") == "log":
                RUNTIME_LOG.error(
                    "automation", "automation.legacy_error", "浏览器自动化报告异常",
                    operation_id=event.get("operation_id"), task_id=event.get("task_id"),
                    object_id=event.get("platform"), detail=event.get("message", ""),
                )
            else:
                self.state.apply_event(event)
                if event.get("kind") == "browser" and event.get("browser_key"):
                    started = event.get("started")
                    if event.get("locked"):
                        started = True
                    self.state.select_browser(event["browser_key"], started=started)

    def stop(self, clean=True):
        if self.stopped:
            return
        self.stopped = True
        started = time.monotonic()
        RUNTIME_LOG.info("application", "application.shutdown.started", "应用开始退出", result="started")
        self.shutdown_event.set()
        self.automation.close()
        for token in list(UPLOADS):
            delete_attachment(token)
        RUNTIME_LOG.info(
            "application", "application.shutdown.completed", "应用退出清理完成",
            result="success" if clean else "failure", duration_ms=round((time.monotonic() - started) * 1000, 2),
            remaining_upload_count=len(UPLOADS), active_thread_count=threading.active_count(),
        )
        RUNTIME_LOG.close(clean=clean)


APP = Application()


def store_attachment(name, encoded, operation_id=None):
    started = time.monotonic()
    safe_name = Path(str(name or "attachment")).name.strip()
    if not safe_name:
        raise ValueError("附件名称无效")
    if Path(safe_name).suffix.lower() in BLOCKED_ATTACHMENT_SUFFIXES:
        raise ValueError("不支持上传可执行文件或快捷方式")
    if "," in encoded and encoded.lstrip().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("附件内容无法读取") from exc
    if not content:
        raise ValueError("附件内容为空")
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise ValueError("单个附件不能超过 20 MB")
    token = uuid.uuid4().hex
    target_dir = UPLOAD_DIR / token
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / safe_name
    target.write_bytes(content)
    record = {"token": token, "name": safe_name, "size": len(content), "path": str(target)}
    with UPLOADS_LOCK:
        UPLOADS[token] = record
    RUNTIME_LOG.info(
        "attachment", "attachment.store.verified", "附件已写入本地缓存并验证",
        operation_id=operation_id, object_id=token[:12], result="success",
        file_name=Path(safe_name).name, suffix=Path(safe_name).suffix.lower(), size_bytes=len(content),
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return record


def resolve_attachments(tokens):
    if len(tokens) > MAX_ATTACHMENTS:
        raise ValueError("每次最多上传 5 个附件")
    with UPLOADS_LOCK:
        records = [UPLOADS.get(str(token)) for token in tokens]
    if any(record is None for record in records):
        raise ValueError("附件已失效，请重新选择")
    return [dict(record) for record in records]


def delete_attachment(token, operation_id=None):
    with UPLOADS_LOCK:
        record = UPLOADS.pop(str(token), None)
    if not record:
        RUNTIME_LOG.debug("attachment", "attachment.delete.skipped", "附件缓存不存在，无需删除", operation_id=operation_id, object_id=str(token)[:12], result="not_found")
        return
    target = Path(record["path"])
    try:
        if target.is_file():
            target.unlink()
        target.parent.rmdir()
    except OSError:
        RUNTIME_LOG.warning("attachment", "attachment.delete.degraded", "附件记录已移除，但缓存文件清理失败", operation_id=operation_id, object_id=str(token)[:12], result="degraded", file=safe_path(target))
    else:
        RUNTIME_LOG.info("attachment", "attachment.delete.completed", "附件缓存已删除", operation_id=operation_id, object_id=str(token)[:12], result="success", file=safe_path(target))


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MultiAIInput/0.1"

    def log_message(self, *_args):
        return

    def _json(self, value, status=200):
        if getattr(self, "_operation_id", None) and isinstance(value, dict):
            value = dict(value)
            value.setdefault("operation_id", self._operation_id)
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
        if getattr(self, "_operation_id", None) and not getattr(self, "_response_logged", False):
            self._response_logged = True
            asynchronous_actions = {"send", "launch", "inspect", "browser", "browser-path", "open", "retry", "cancel", "shutdown", "custom-platform.delete"}
            if status >= 400:
                event_name, message, result = "user_action.rejected", "用户操作请求被拒绝", "rejected"
            elif getattr(self, "_action", "") in asynchronous_actions:
                event_name, message, result = "user_action.accepted", "用户操作已接收，后台执行已启动", "accepted"
            else:
                event_name, message, result = "user_action.completed", "用户操作执行完成", "success"
            RUNTIME_LOG.info(
                "http", event_name, message,
                operation_id=self._operation_id, action=getattr(self, "_action", "unknown"),
                result=result, http_status=status,
                duration_ms=round((time.monotonic() - getattr(self, "_request_started", time.monotonic())) * 1000, 2),
            )

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            return self._json(APP.state.snapshot())
        if path == "/api/data-dir":
            operation_id = new_correlation_id("op")
            started = RUNTIME_LOG.start_operation(operation_id, "data_directory.open")
            try:
                os.startfile(DATA_DIR)
                RUNTIME_LOG.finish_operation(operation_id, "data_directory.open", started, "success", directory=safe_path(DATA_DIR))
                return self._json({"ok": True, "operation_id": operation_id})
            except Exception as exc:
                RUNTIME_LOG.exception("external", "data_directory.open.failed", "打开数据目录失败", exc, operation_id=operation_id, directory=safe_path(DATA_DIR), result="failure")
                RUNTIME_LOG.finish_operation(operation_id, "data_directory.open", started, "failure", error_code=getattr(exc, "errno", None))
                return self._json({"ok": False, "error": str(exc)}, 500)
        if path == "/" or path == "/index.html":
            file_path = resource_path("web/index.html")
        else:
            safe = path.lstrip("/").replace("..", "")
            file_path = resource_path(f"web/{safe}")
        if not file_path.is_file():
            RUNTIME_LOG.warning("http", "http.static.not_found", "静态资源不存在", route=path, file=safe_path(file_path), result="not_found")
            self.send_error(404)
            return
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        self._operation_id = new_correlation_id("op")
        self._request_started = time.monotonic()
        self._response_logged = False
        self._action = path.removeprefix("/api/").replace("/", ".") or "unknown"
        RUNTIME_LOG.info(
            "http", "user_action.received", "收到用户操作请求",
            operation_id=self._operation_id, action=self._action, route=path,
            content_length=int(self.headers.get("Content-Length", "0") or 0),
        )
        try:
            body = self._body()
            if path == "/api/attachment":
                try:
                    record = store_attachment(body.get("name"), str(body.get("data", "")), self._operation_id)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                return self._json({
                    "ok": True,
                    "attachment": {
                        "token": record["token"], "name": record["name"], "size": record["size"],
                    },
                })
            if path == "/api/attachment/delete":
                delete_attachment(body.get("token"), self._operation_id)
                return self._json({"ok": True})
            if path == "/api/custom-platform":
                try:
                    platform = add_custom_platform(body.get("name"), body.get("url"), self._operation_id)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                APP.state.add_platform(platform)
                return self._json({
                    "ok": True,
                    "platform": {
                        "key": platform.key, "name": platform.name, "short_name": platform.short_name,
                        "color": platform.color, "url": platform.url, "custom": True,
                    },
                })
            if path == "/api/custom-platform/delete":
                key = body.get("platform")
                try:
                    delete_custom_platform(key, self._operation_id)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                APP.automation.remove_platform(key, self._operation_id)
                APP.state.remove_platform(key)
                return self._json({"ok": True})
            if path == "/api/send":
                prompt = str(body.get("prompt", "")).strip()
                keys = [key for key in body.get("platforms", []) if key in PLATFORM_MAP]
                try:
                    attachments = resolve_attachments(list(body.get("attachments", [])))
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                if (not prompt and not attachments) or not keys:
                    return self._json({"ok": False, "error": "请输入内容或添加附件，并至少选择一个平台"}, 400)
                if len(prompt) > 10000:
                    return self._json({"ok": False, "error": "单次最多发送 10000 个字符"}, 400)
                requested_modes = body.get("modes", {})
                modes = {}
                for key in keys:
                    allowed = APP.state.allowed_mode_keys(key)
                    modes[key] = requested_modes.get(key, "default")
                    if modes[key] not in allowed:
                        modes[key] = "default"
                task_id = APP.state.start_task(prompt, keys, attachments, modes, self._operation_id)
                APP.automation.send(keys, prompt, attachments, modes, self._operation_id, task_id)
                return self._json({"ok": True, "task_id": task_id})
            if path == "/api/launch":
                keys = [key for key in body.get("platforms", []) if key in PLATFORM_MAP]
                if not keys:
                    return self._json({"ok": False, "error": "请至少选择一个 AI 平台"}, 400)
                APP.automation.open_many(keys, self._operation_id)
                return self._json({"ok": True})
            if path == "/api/inspect":
                keys = [key for key in body.get("platforms", []) if key in PLATFORM_MAP]
                if not keys:
                    return self._json({"ok": False, "error": "请至少选择一个 AI 平台"}, 400)
                APP.automation.inspect_many(keys, self._operation_id)
                return self._json({"ok": True})
            if path == "/api/browser-path":
                key = body.get("browser")
                try:
                    browser_info = configure_browser_path(key, body.get("path", ""), self._operation_id)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                state = APP.state.snapshot()
                reloaded = state["browser_started"] and state["selected_browser"] == key
                if reloaded:
                    keys = [platform_key for platform_key in body.get("platforms", []) if platform_key in PLATFORM_MAP]
                    APP.automation.reload_browser(key, keys, self._operation_id)
                    APP.state.select_browser(key, started=False)
                return self._json({"ok": True, "browser": browser_info, "reloaded": reloaded})
            if path == "/api/browser":
                key = body.get("browser")
                if key not in BROWSERS:
                    return self._json({"ok": False, "error": "未知浏览器"}, 400)
                current_state = APP.state.snapshot()
                if not BROWSERS[key]["available"] and current_state["browser_started"]:
                    return self._json({"ok": False, "error": f"未检测到 {BROWSERS[key]['name']}，请先保存其自定义 .exe 路径"}, 400)
                keys = [platform_key for platform_key in body.get("platforms", []) if platform_key in PLATFORM_MAP]
                APP.automation.switch_browser(key, keys, self._operation_id)
                APP.state.select_browser(key, started=False)
                return self._json({"ok": True, "browser": key, "needs_path": not BROWSERS[key]["available"]})
            if path == "/api/open":
                key = body.get("platform")
                if key not in PLATFORM_MAP:
                    return self._json({"ok": False, "error": "未知平台"}, 400)
                APP.automation.open_platform(key, self._operation_id)
                return self._json({"ok": True})
            if path == "/api/retry":
                key = body.get("platform")
                prompt = APP.state.last_prompt
                if key not in PLATFORM_MAP or (not prompt and not APP.state.last_attachments):
                    return self._json({"ok": False, "error": "没有可重试的任务"}, 400)
                retry_task_id = new_correlation_id("task")
                APP.automation.retry(
                    key, prompt, APP.state.last_attachments,
                    {key: APP.state.last_modes.get(key, "default")},
                    self._operation_id, retry_task_id,
                )
                return self._json({"ok": True, "task_id": retry_task_id})
            if path == "/api/cancel":
                key = body.get("platform")
                if key in PLATFORM_MAP:
                    APP.automation.cancel(key, self._operation_id)
                return self._json({"ok": True})
            if path == "/api/diagnostics/export":
                snapshot = APP.state.snapshot()
                summary = {
                    "version": snapshot.get("version"),
                    "selected_browser": snapshot.get("selected_browser"),
                    "browser_started": snapshot.get("browser_started"),
                    "platforms": [
                        {key: item.get(key) for key in ("key", "page_state", "login_state", "input_state", "mode_status")}
                        for item in snapshot.get("platforms", [])
                    ],
                    "statuses": {
                        key: {field: value.get(field) for field in ("status", "elapsed", "updated_at", "task_id", "operation_id")}
                        for key, value in snapshot.get("statuses", {}).items()
                    },
                    "history_count": len(snapshot.get("history", [])),
                    "attachment_cache_count": len(UPLOADS),
                }
                target = RUNTIME_LOG.export_diagnostics(summary, DATA_DIR / "diagnostics", self._operation_id)
                try:
                    os.startfile(target.parent)
                except Exception as exc:
                    RUNTIME_LOG.warning("diagnostics", "diagnostics.folder_open.failed", "诊断包已生成，但无法自动打开目录", operation_id=self._operation_id, error_type=type(exc).__name__, result="degraded")
                return self._json({"ok": True, "file": target.name})
            if path == "/api/minimize":
                if os.name == "nt":
                    import ctypes
                    window = ctypes.windll.user32.GetForegroundWindow()
                    if window:
                        ctypes.windll.user32.ShowWindow(window, 6)
                return self._json({"ok": True})
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self._json({"ok": False, "error": "Not found"}, 404)
        except Exception as exc:
            RUNTIME_LOG.exception(
                "http", "user_action.failed", "用户操作执行失败", exc,
                operation_id=self._operation_id, action=self._action, route=path, result="failure",
            )
            return self._json({"ok": False, "error": str(exc)}, 500)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_edge():
    candidates = [
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def open_app_window(url):
    operation_id = new_correlation_id("ui")
    started = RUNTIME_LOG.start_operation(operation_id, "ui.launch")
    edge = find_edge()
    try:
        if edge:
            ui_profile = DATA_DIR / "ui-profile"
            process = subprocess.Popen([
                edge, f"--app={url}", f"--user-data-dir={ui_profile}",
                "--no-first-run", "--disable-features=msEdgeSidebarV2",
                "--window-size=1500,920",
            ])
            RUNTIME_LOG.info(
                "external", "external.ui_process.started", "桌面界面子进程已启动",
                operation_id=operation_id, process_id=process.pid, executable=safe_path(edge), result="started",
            )

            def observe_child():
                code = process.wait()
                level = RUNTIME_LOG.warning if code else RUNTIME_LOG.info
                level(
                    "external", "external.ui_process.exited", "桌面界面子进程已退出",
                    operation_id=operation_id, process_id=process.pid, exit_code=code,
                    result="success" if code == 0 else "failure",
                )

            threading.Thread(target=observe_child, daemon=True, name="ui-process-observer").start()
        else:
            import webbrowser
            opened = webbrowser.open(url)
            if not opened:
                raise RuntimeError("system browser rejected the local URL")
            RUNTIME_LOG.warning("external", "external.ui_process.degraded", "未找到 Edge，已使用系统默认浏览器打开界面", operation_id=operation_id, result="degraded")
        RUNTIME_LOG.finish_operation(operation_id, "ui.launch", started, "success")
    except Exception as exc:
        RUNTIME_LOG.exception("external", "external.ui_process.failed", "桌面界面启动失败", exc, operation_id=operation_id, result="failure")
        RUNTIME_LOG.finish_operation(operation_id, "ui.launch", started, "failure", error_code=getattr(exc, "errno", None))


def main():
    if "--self-test" in sys.argv:
        snapshot = APP.state.snapshot()
        if not BUILTIN_PLATFORM_KEYS.issubset({item["key"] for item in snapshot["platforms"]}) or len(snapshot["browsers"]) != 4 or not resource_path("web/index.html").is_file():
            APP.stop()
            raise SystemExit(2)
        APP.stop()
        raise SystemExit(0)
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            raise SystemExit("--port requires a valid port number")
    else:
        port = free_port()
    RUNTIME_LOG.info(
        "lifecycle", "application.startup.started", "应用启动流程开始",
        version=VERSION, build_id=BUILD_ID, process_id=os.getpid(), session_id=RUNTIME_LOG.session_id,
        port=port, serve_only="--serve-only" in sys.argv, result="started",
    )
    startup_started = time.monotonic()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    except Exception as exc:
        RUNTIME_LOG.exception("server", "application.startup.failed", "本地服务初始化失败", exc, port=port, result="failure")
        APP.stop(clean=False)
        raise
    url = f"http://127.0.0.1:{port}/"
    if "--serve-only" not in sys.argv:
        threading.Timer(0.4, open_app_window, args=(url,)).start()
    RUNTIME_LOG.info(
        "lifecycle", "application.startup.verified", "本地服务已监听，启动验证完成",
        result="success", port=port, duration_ms=round((time.monotonic() - startup_started) * 1000, 2),
    )
    clean_exit = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        RUNTIME_LOG.info("lifecycle", "application.shutdown.requested", "收到键盘中断退出请求", reason="keyboard_interrupt")
    except Exception as exc:
        clean_exit = False
        RUNTIME_LOG.exception("server", "server.runtime.failed", "本地 HTTP 服务异常退出", exc, port=port, result="failure")
        raise
    finally:
        server.server_close()
        RUNTIME_LOG.info("server", "server.closed", "本地 HTTP 服务已关闭", result="success", port=port)
        APP.stop(clean=clean_exit)


if __name__ == "__main__":
    main()
