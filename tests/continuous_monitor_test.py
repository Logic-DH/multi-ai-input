import asyncio

import app


class Page:
    def is_closed(self):
        return False


async def run_test():
    automation = object.__new__(app.BrowserAutomation)
    page = Page()
    automation.pages = {"qwen": page}
    automation.tasks = {}
    automation.inspections = {}
    emitted = []
    calls = []

    async def inspect(target_page, platform, **options):
        calls.append((target_page, platform.key, options))
        return {
            "page_state": "ready", "page_label": "页面已打开",
            "login_state": "guest", "login_label": "访客可用",
            "input_state": "available", "input_label": "可输入",
            "capability_detail": "页面已打开；访客可用；可输入；状态持续监测中",
        }

    automation._inspect_platform = inspect
    automation.emit = lambda kind, key=None, **payload: emitted.append((kind, key, payload))

    await automation._monitor_one_platform("qwen", page)
    assert calls[0][2] == {"detect_modes": False, "announce": False}
    assert automation.inspections["qwen"]["input_state"] == "available"
    assert any(item[0] == "status" and item[2]["status"] == "已打开" for item in emitted)

    state = app.AppState()
    state.apply_event({
        "kind": "status", "platform": "qwen", "status": "生成中",
        "preview": "摘要", "full_text": "完整回复正文",
    })
    assert state.snapshot()["statuses"]["qwen"]["full_text"] == "完整回复正文"


if __name__ == "__main__":
    asyncio.run(run_test())
    print("CONTINUOUS_MONITOR_OK")
