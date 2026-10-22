import app


def classify(**signals):
    defaults = {
        "verification": False,
        "restricted": False,
        "has_account": False,
        "has_login": False,
        "has_input": False,
        "navigation_timed_out": False,
        "page_has_content": True,
    }
    defaults.update(signals)
    return app.classify_platform_capability(**defaults)


def main():
    guest = classify(has_login=True, has_input=True)
    assert guest["page_state"] == "ready"
    assert guest["login_state"] == "guest"
    assert guest["input_state"] == "available"

    partial_guest = classify(
        has_login=True,
        has_input=True,
        navigation_timed_out=True,
    )
    assert partial_guest["page_state"] == "partial"
    assert partial_guest["login_state"] == "guest"
    assert partial_guest["input_state"] == "available"

    logged_in = classify(has_account=True, has_input=True)
    assert logged_in["login_state"] == "logged_in"
    assert logged_in["input_state"] == "available"

    restricted = classify(restricted=True, has_login=True)
    assert restricted["page_state"] == "blocked"
    assert restricted["login_state"] == "logged_out"
    assert restricted["input_state"] == "blocked"
    assert app.capability_open_status(restricted) == "需人工处理"

    blank_timeout = classify(
        navigation_timed_out=True,
        page_has_content=False,
    )
    assert blank_timeout["page_state"] == "timeout"
    assert blank_timeout["login_state"] == "unknown"
    assert blank_timeout["input_state"] == "unavailable"

    verification = classify(verification=True, has_input=True)
    assert verification["page_state"] == "verification"
    assert verification["login_state"] == "verification"
    assert verification["input_state"] == "verification"

    state = app.AppState()
    platforms = {p["key"]: p for p in state.snapshot()["platforms"]}
    assert {"kimi", "zhipu"}.issubset(platforms)
    assert len(platforms) >= 9
    chatgpt = platforms["chatgpt"]
    assert chatgpt["page_state"] == "unchecked"
    assert chatgpt["input_state"] == "unchecked"
    assert app.capability_open_status(logged_in) == "已打开"
    assert app.infer_platform_account("deepseek", True, False, False) is True
    assert app.infer_platform_account("qwen", True, False, False) is False

    print("STATUS_DETECTION_OK")


if __name__ == "__main__":
    main()
