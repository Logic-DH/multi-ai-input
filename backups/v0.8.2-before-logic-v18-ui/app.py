import asyncio
import base64
import hashlib
import json
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


APP_NAME = "多平台 AI 同步输入器"
VERSION = "0.8.2"


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
}

VERIFICATION_TEXTS = ("安全验证", "人机验证", "完成验证", "验证码", "Verify you are human", "Checking your browser")
LOGIN_TEXTS = ("登录", "Log in", "Sign in")
MODE_OPTION_SELECTORS = (
    "[role='menuitem']", "[role='option']", "[role='radio']",
    "[role='menu'] button", "[role='listbox'] button",
)
MODE_KEYWORDS = (
    "gpt", "claude", "gemini", "grok", "deepseek", "qwen", "sonnet", "opus", "haiku",
    "pro", "flash", "thinking", "instant", "fast", "expert", "search",
    "思考", "搜索", "联网", "快速", "专家", "专业", "自动", "模型",
)


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_data_dir() -> Path:
    preferred = Path(os.getenv("LOCALAPPDATA", Path.home())) / "MultiAIInput"
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
LOG_FILE = DATA_DIR / "error.log"
BROWSER_PATHS_FILE = DATA_DIR / "browser-paths.json"
CUSTOM_PLATFORMS_FILE = DATA_DIR / "custom-platforms.json"
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


def find_first(paths):
    for value in paths:
        path = Path(value)
        if path.is_file():
            return str(path)
    return None


def browser_catalog():
    edge = find_first([
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ])
    chrome = find_first([
        Path(os.getenv("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ])
    quark = find_first([
        Path(os.getenv("PROGRAMFILES", "")) / "Quark/quark.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Quark/quark.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Quark/quark.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Quark/quark.exe",
    ])
    browser360 = find_first([
        Path(os.getenv("PROGRAMFILES", "")) / "360/360se6/Application/360se.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "360/360se6/Application/360se.exe",
        Path(os.getenv("APPDATA", "")) / "360se6/Application/360se.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "360se6/Application/360se.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "360Chrome/Chrome/Application/360chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "360Chrome/Chrome/Application/360chrome.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/Application/360chrome.exe",
        Path(os.getenv("APPDATA", "")) / "360Chrome/Chrome/Application/360chrome.exe",
    ])
    browser360_user_data = (
        Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/User Data"
        if browser360 and Path(browser360).name.casefold() == "360chrome.exe"
        else Path(os.getenv("APPDATA", "")) / "360se6/User Data"
    )
    catalog = {
        "edge": {
            "key": "edge", "name": "Edge", "short_name": "E", "color": "#0AA8A7", "path": edge, "available": bool(edge),
            "default_path": edge, "custom_path": False,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data"), "launch_args": [],
        },
        "chrome": {
            "key": "chrome", "name": "Chrome", "short_name": "C", "color": "#4285F4", "path": chrome, "available": bool(chrome),
            "default_path": chrome, "custom_path": False,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/User Data"), "launch_args": [],
        },
        "quark": {
            "key": "quark", "name": "夸克", "short_name": "夸", "color": "#6A5CFF", "path": quark, "available": bool(quark),
            "default_path": quark, "custom_path": False,
            "user_data": str(Path(os.getenv("LOCALAPPDATA", "")) / "Quark/User Data"),
            "launch_args": ["--launch-from=desktopicon", "--brand-quark"],
        },
        "360": {
            "key": "360", "name": "360浏览器", "short_name": "360", "color": "#28B55B",
            "path": browser360, "available": bool(browser360),
            "default_path": browser360, "custom_path": False,
            "user_data": str(browser360_user_data), "launch_args": [],
        },
    }
    try:
        saved_paths = json.loads(BROWSER_PATHS_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved_paths = {}
    for key, value in saved_paths.items():
        if key in catalog and isinstance(value, str) and Path(value).is_file():
            catalog[key]["path"] = str(Path(value).resolve())
            catalog[key]["available"] = True
            catalog[key]["custom_path"] = True
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
    try:
        records = json.loads(CUSTOM_PLATFORMS_FILE.read_text(encoding="utf-8"))
        return records if isinstance(records, list) else []
    except Exception:
        return []


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


def save_custom_platform_records():
    CUSTOM_PLATFORMS_FILE.write_text(
        json.dumps(CUSTOM_PLATFORM_RECORDS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_custom_platform(name, raw_url):
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
    save_custom_platform_records()
    rebuild_platform_registry()
    return PLATFORM_MAP[key]


def delete_custom_platform(key):
    if key in BUILTIN_PLATFORM_KEYS:
        raise ValueError("内置平台不能删除")
    before = len(CUSTOM_PLATFORM_RECORDS)
    CUSTOM_PLATFORM_RECORDS[:] = [record for record in CUSTOM_PLATFORM_RECORDS if record.get("key") != key]
    if len(CUSTOM_PLATFORM_RECORDS) == before:
        raise ValueError("未找到自定义平台")
    save_custom_platform_records()
    rebuild_platform_registry()


rebuild_platform_registry()


def browser_public_info(info):
    return {field: info[field] for field in (
        "key", "name", "short_name", "color", "available", "path", "default_path", "custom_path"
    )}


def sync_browser_profile_path(key):
    if key != "360":
        return
    executable = Path(BROWSERS[key].get("path") or "")
    if executable.name.casefold() == "360chrome.exe":
        BROWSERS[key]["user_data"] = str(Path(os.getenv("LOCALAPPDATA", "")) / "360Chrome/Chrome/User Data")
    else:
        BROWSERS[key]["user_data"] = str(Path(os.getenv("APPDATA", "")) / "360se6/User Data")


def configure_browser_path(key, raw_path):
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
    else:
        default_path = BROWSERS[key]["default_path"]
        BROWSERS[key]["path"] = default_path
        BROWSERS[key]["available"] = bool(default_path and Path(default_path).is_file())
        BROWSERS[key]["custom_path"] = False
    sync_browser_profile_path(key)
    saved = {
        browser_key: info["path"]
        for browser_key, info in BROWSERS.items()
        if info["custom_path"] and info["path"]
    }
    BROWSER_PATHS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
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
    try:
        value = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def write_history(items):
    HISTORY_FILE.write_text(json.dumps(items[:100], ensure_ascii=False, indent=2), encoding="utf-8")


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.browser_message = "本地运行"
        self.selected_browser = next((key for key in ("edge", "chrome", "quark", "360") if BROWSERS[key]["available"]), "edge")
        self.browser_started = False
        self.last_prompt = ""
        self.last_attachments = []
        self.last_modes = {}
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
            "login_state": "unchecked",
            "login_label": "未检测",
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
                        "login_state": self.capabilities.get(p.key, self._new_capability(p))["login_state"],
                        "login_label": self.capabilities.get(p.key, self._new_capability(p))["login_label"],
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
                self.browser_message = event.get("message", "本地运行")
                return
            if event.get("kind") == "status":
                key = event.get("platform")
                if key not in self.statuses:
                    return
                target = self.statuses[key]
                for field in ("status", "detail", "preview", "full_text", "elapsed"):
                    if field in event:
                        target[field] = event[field]
                target["updated_at"] = time.time()
                return
            if event.get("kind") == "capability":
                key = event.get("platform")
                if key not in self.capabilities:
                    return
                target = self.capabilities[key]
                for field in (
                    "login_state", "login_label", "capability_detail", "mode_status",
                    "available_modes", "mode_aliases", "detected_at",
                ):
                    if field in event:
                        target[field] = event[field]

    def start_task(self, prompt, keys, attachments=None, modes=None):
        with self.lock:
            self.last_prompt = prompt
            self.last_attachments = list(attachments or [])
            self.last_modes = dict(modes or {})
            for key in keys:
                self.statuses[key] = {
                    "status": "准备中", "detail": "等待浏览器就绪", "preview": "",
                    "full_text": "", "elapsed": 0, "updated_at": time.time(),
                }
            self.history.insert(0, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "prompt": prompt, "platforms": keys,
                "attachments": [item["name"] for item in self.last_attachments],
                "modes": self.last_modes,
            })
            self.history = self.history[:100]
            write_history(self.history)

    def select_browser(self, key, started=None):
        with self.lock:
            changed = self.selected_browser != key
            self.selected_browser = key
            if started is not None:
                self.browser_started = started
            if changed:
                for platform in PLATFORMS:
                    self.capabilities[platform.key] = self._new_capability(platform)

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
        self.tasks = {}
        self.browser_key = initial_browser
        self.browser_started = False
        self.selection_lock = threading.Lock()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def emit(self, kind, platform_key=None, **payload):
        self.event_queue.put({"kind": kind, "platform": platform_key, **payload})

    def _submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def open_platform(self, key):
        self._submit(self._open_platform(PLATFORM_MAP[key], focus=True))

    def open_many(self, keys):
        self._submit(self._open_many(keys))

    def inspect_many(self, keys):
        self._submit(self._inspect_many(keys))

    def remove_platform(self, key):
        self._submit(self._remove_platform(key))

    def send(self, keys, prompt, attachments=None, modes=None):
        self._submit(self._send_batch(keys, prompt, attachments or [], modes or {}))

    def cancel(self, key):
        self._submit(self._cancel(key))

    def retry(self, key, prompt, attachments=None, modes=None):
        self.send([key], prompt, attachments or [], modes or {})

    def switch_browser(self, key, keys):
        self._submit(self._switch_browser(key, keys))

    def reload_browser(self, key, keys):
        self._submit(self._switch_browser(key, keys, force=True))

    def close(self):
        try:
            self._submit(self._close()).result(timeout=8)
        except Exception:
            pass
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
        self.playwright = await async_playwright().start()
        profile_dir = DATA_DIR / f"browser-profile-{browser_key}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.emit("browser", message=f"正在读取 {browser_info['name']} 已有登录状态…", browser_key=browser_key, started=False)
        import_result = await asyncio.to_thread(import_existing_login, browser_key, profile_dir)
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
            raise RuntimeError(f"无法启动 {browser_info['name']}：{launch_error}")
        self.context.set_default_timeout(3500)
        self.emit("browser", message=f"{browser_info['name']} 已就绪", browser_key=browser_key, locked=True)

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
                capability = APP.state.capabilities.get(key, {})
                login_label = capability.get("login_label", "未检测")
                mode_count = max(0, len(capability.get("available_modes", [])) - 1)
                detail = f"{login_label}；检测到 {mode_count} 个可切换模式" if mode_count else f"{login_label}；保持页面当前模式"
                self.emit("status", key, status="已打开", detail=detail)

    async def _inspect_many(self, keys):
        valid_keys = [key for key in keys if key in PLATFORM_MAP]
        if not valid_keys:
            return
        await self._ensure_browser()
        await asyncio.gather(
            *(self._open_platform(PLATFORM_MAP[key]) for key in valid_keys),
            return_exceptions=True,
        )

    async def _remove_platform(self, key):
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
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
            if not page or page.is_closed():
                page = await self.context.new_page()
                self.pages[platform.key] = page
                await page.goto(platform.url, wait_until="domcontentloaded", timeout=60000)
            elif urllib.parse.urlparse(platform.url).netloc not in page.url:
                await page.goto(platform.url, wait_until="domcontentloaded", timeout=60000)
            await self._inspect_platform(page, platform)
            if focus:
                await page.bring_to_front()
            return page
        except Exception as exc:
            self.emit("status", platform.key, status="失败", detail=f"无法打开平台：{str(exc)[:220]}")
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
        await asyncio.gather(*jobs, return_exceptions=True)

    async def _visible_locator(self, page, selectors, timeout_seconds=15):
        end = time.monotonic() + timeout_seconds
        while time.monotonic() < end:
            for selector in selectors:
                try:
                    locator = page.locator(selector).last
                    if await locator.count() and await locator.is_visible():
                        return locator
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

    async def _inspect_platform(self, page, platform):
        self.emit(
            "capability", platform.key, login_state="detecting", login_label="检测中",
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
            has_account = await self._any_visible(page, rules.get("in", ()))
            has_login = await self._any_visible(page, rules.get("out", ())) or await self._visible_named(page, LOGIN_TEXTS)
            has_input = bool(await self._visible_locator(page, platform.input_selectors, timeout_seconds=1.2))

            if verification:
                login_state, login_label = "verification", "需验证"
            elif has_account:
                login_state, login_label = "logged_in", "已登录"
            elif has_login and has_input:
                login_state, login_label = "guest", "访客可用"
            elif has_login:
                login_state, login_label = "logged_out", "未登录"
            elif has_input:
                login_state, login_label = "usable_unknown", "可用·登录未知"
            else:
                login_state, login_label = "unavailable", "不可用或未登录"

            available_modes, aliases_by_key, mode_menu_found = await self._detect_modes(page, platform)
            mode_status = "detected" if len(available_modes) > 1 else ("current_only" if has_input else "unavailable")
            detail = login_label
            if len(available_modes) > 1:
                detail += f"；检测到 {len(available_modes) - 1} 个可切换模式"
            elif mode_menu_found:
                detail += "；模式菜单中未识别到可用选项"
            else:
                detail += "；将保持页面当前模式"
            self.emit(
                "capability", platform.key,
                login_state=login_state, login_label=login_label,
                capability_detail=detail, mode_status=mode_status,
                available_modes=available_modes, mode_aliases=aliases_by_key,
                detected_at=time.time(),
            )
        except Exception as exc:
            self.emit(
                "capability", platform.key, login_state="failed", login_label="检测失败",
                capability_detail=f"检测失败：{str(exc)[:120]}", mode_status="failed",
                available_modes=[{"key": "default", "label": "保持当前模式"}],
                mode_aliases={}, detected_at=time.time(),
            )

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
                return
            if attachments:
                self.emit("status", platform.key, status="准备中", detail="正在上传附件")
                if not await self._upload_attachments(page, platform, attachments):
                    self.emit(
                        "status", platform.key, status="需人工处理",
                        detail="未找到可用的附件上传入口。请手动上传后点击重试。",
                    )
                    await page.bring_to_front()
                    return
            self.emit("status", platform.key, status="正在输入", detail="正在定位输入框")
            input_box = await self._visible_locator(page, platform.input_selectors)
            if not input_box:
                self.emit(
                    "status", platform.key, status="需人工处理",
                    detail="未找到输入框。请在浏览器中完成登录或验证，然后点击重试。",
                )
                await page.bring_to_front()
                return
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
                        preview=current[:600], elapsed=int(time.monotonic() - started),
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
                    return
            self.emit(
                "status", platform.key, status="失败", detail="等待回复超时，可打开原会话检查后重试",
                preview=last_text[:600], elapsed=int(time.monotonic() - started),
            )
        except asyncio.CancelledError:
            self.emit("status", platform.key, status="已取消", detail="任务已停止")
        except Exception as exc:
            self.emit("status", platform.key, status="失败", detail=str(exc)[:300])
            self.emit("log", platform.key, message=traceback.format_exc())

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
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()


class Application:
    def __init__(self):
        self.events = queue.Queue()
        self.state = AppState()
        self.automation = BrowserAutomation(self.events, self.state.selected_browser)
        self.shutdown_event = threading.Event()
        threading.Thread(target=self._consume_events, daemon=True).start()

    def _consume_events(self):
        while not self.shutdown_event.is_set():
            try:
                event = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if event.get("kind") == "log":
                try:
                    with LOG_FILE.open("a", encoding="utf-8") as stream:
                        stream.write(f"\n[{datetime.now().isoformat()}] {event.get('platform')}\n{event.get('message')}\n")
                except OSError:
                    pass
            else:
                self.state.apply_event(event)
                if event.get("kind") == "browser" and event.get("browser_key"):
                    started = event.get("started")
                    if event.get("locked"):
                        started = True
                    self.state.select_browser(event["browser_key"], started=started)

    def stop(self):
        self.shutdown_event.set()
        self.automation.close()
        for token in list(UPLOADS):
            delete_attachment(token)


APP = Application()


def store_attachment(name, encoded):
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
    return record


def resolve_attachments(tokens):
    if len(tokens) > MAX_ATTACHMENTS:
        raise ValueError("每次最多上传 5 个附件")
    with UPLOADS_LOCK:
        records = [UPLOADS.get(str(token)) for token in tokens]
    if any(record is None for record in records):
        raise ValueError("附件已失效，请重新选择")
    return [dict(record) for record in records]


def delete_attachment(token):
    with UPLOADS_LOCK:
        record = UPLOADS.pop(str(token), None)
    if not record:
        return
    target = Path(record["path"])
    try:
        if target.is_file():
            target.unlink()
        target.parent.rmdir()
    except OSError:
        pass


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MultiAIInput/0.1"

    def log_message(self, *_args):
        return

    def _json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

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
            try:
                os.startfile(DATA_DIR)
                return self._json({"ok": True})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if path == "/" or path == "/index.html":
            file_path = resource_path("web/index.html")
        else:
            safe = path.lstrip("/").replace("..", "")
            file_path = resource_path(f"web/{safe}")
        if not file_path.is_file():
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
        try:
            body = self._body()
            if path == "/api/attachment":
                try:
                    record = store_attachment(body.get("name"), str(body.get("data", "")))
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                return self._json({
                    "ok": True,
                    "attachment": {
                        "token": record["token"], "name": record["name"], "size": record["size"],
                    },
                })
            if path == "/api/attachment/delete":
                delete_attachment(body.get("token"))
                return self._json({"ok": True})
            if path == "/api/custom-platform":
                try:
                    platform = add_custom_platform(body.get("name"), body.get("url"))
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
                    delete_custom_platform(key)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                APP.automation.remove_platform(key)
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
                APP.state.start_task(prompt, keys, attachments, modes)
                APP.automation.send(keys, prompt, attachments, modes)
                return self._json({"ok": True})
            if path == "/api/launch":
                keys = [key for key in body.get("platforms", []) if key in PLATFORM_MAP]
                if not keys:
                    return self._json({"ok": False, "error": "请至少选择一个 AI 平台"}, 400)
                APP.automation.open_many(keys)
                return self._json({"ok": True})
            if path == "/api/inspect":
                keys = [key for key in body.get("platforms", []) if key in PLATFORM_MAP]
                if not keys:
                    return self._json({"ok": False, "error": "请至少选择一个 AI 平台"}, 400)
                APP.automation.inspect_many(keys)
                return self._json({"ok": True})
            if path == "/api/browser-path":
                key = body.get("browser")
                try:
                    browser_info = configure_browser_path(key, body.get("path", ""))
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                state = APP.state.snapshot()
                reloaded = state["browser_started"] and state["selected_browser"] == key
                if reloaded:
                    keys = [platform_key for platform_key in body.get("platforms", []) if platform_key in PLATFORM_MAP]
                    APP.automation.reload_browser(key, keys)
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
                APP.automation.switch_browser(key, keys)
                APP.state.select_browser(key, started=False)
                return self._json({"ok": True, "browser": key, "needs_path": not BROWSERS[key]["available"]})
            if path == "/api/open":
                key = body.get("platform")
                if key not in PLATFORM_MAP:
                    return self._json({"ok": False, "error": "未知平台"}, 400)
                APP.automation.open_platform(key)
                return self._json({"ok": True})
            if path == "/api/retry":
                key = body.get("platform")
                prompt = APP.state.last_prompt
                if key not in PLATFORM_MAP or (not prompt and not APP.state.last_attachments):
                    return self._json({"ok": False, "error": "没有可重试的任务"}, 400)
                APP.automation.retry(
                    key, prompt, APP.state.last_attachments,
                    {key: APP.state.last_modes.get(key, "default")},
                )
                return self._json({"ok": True})
            if path == "/api/cancel":
                key = body.get("platform")
                if key in PLATFORM_MAP:
                    APP.automation.cancel(key)
                return self._json({"ok": True})
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
    edge = find_edge()
    if edge:
        ui_profile = DATA_DIR / "ui-profile"
        subprocess.Popen([
            edge, f"--app={url}", f"--user-data-dir={ui_profile}",
            "--no-first-run", "--disable-features=msEdgeSidebarV2",
            "--window-size=1500,920",
        ])
    else:
        import webbrowser
        webbrowser.open(url)


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
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    url = f"http://127.0.0.1:{port}/"
    if "--serve-only" not in sys.argv:
        threading.Timer(0.4, open_app_window, args=(url,)).start()
    try:
        server.serve_forever()
    finally:
        APP.stop()
        server.server_close()


if __name__ == "__main__":
    main()
