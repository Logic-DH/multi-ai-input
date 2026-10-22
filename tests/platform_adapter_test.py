from pathlib import Path

from platform_adapters import PLATFORM_MAP


def main():
    kimi = PLATFORM_MAP["kimi"]
    assert kimi.url == "https://www.kimi.com/"
    assert "div.chat-input-editor[contenteditable='true']" in kimi.input_selectors
    assert any("send-button-container" in selector for selector in kimi.send_selectors)
    assert any(mode[0] == "k3" for mode in kimi.modes)

    zhipu = PLATFORM_MAP["zhipu"]
    assert zhipu.url == "https://www.chatglm.cn/"
    assert "textarea[placeholder]" in zhipu.input_selectors
    assert any("发送" in selector for selector in zhipu.send_selectors)
    assert any(mode[0] == "thinking" for mode in zhipu.modes)

    for key in ("kimi", "zhipu"):
        assert Path(f"web/assets/icons/{key}.png").is_file()

    print("PLATFORM_ADAPTER_OK")


if __name__ == "__main__":
    main()
