from dataclasses import dataclass, field


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    short_name: str
    url: str
    color: str
    input_selectors: tuple[str, ...]
    send_selectors: tuple[str, ...] = field(default_factory=tuple)
    stop_selectors: tuple[str, ...] = field(default_factory=tuple)
    response_selectors: tuple[str, ...] = field(default_factory=tuple)
    attachment_selectors: tuple[str, ...] = ("input[type='file']",)
    mode_trigger_selectors: tuple[str, ...] = field(default_factory=tuple)
    modes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("default", "默认", ()),
    )


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="claude",
        name="Claude",
        short_name="C",
        url="https://claude.ai/new",
        color="#F59E72",
        input_selectors=(
            "div[contenteditable='true'][data-testid*='input']",
            "div.ProseMirror[contenteditable='true']",
            "fieldset div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='Send']", "button[data-testid*='send']"),
        stop_selectors=("button[aria-label*='Stop']", "button[data-testid*='stop']"),
        response_selectors=("div[data-testid*='assistant']", "div.font-claude-response"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        mode_trigger_selectors=(
            "button[data-testid*='model']",
            "button[aria-label*='model']",
            "button[aria-label*='Model']",
        ),
        modes=(
            ("default", "默认", ()),
            ("sonnet", "Sonnet", ("Sonnet",)),
            ("opus", "Opus", ("Opus",)),
        ),
    ),
    Platform(
        key="chatgpt",
        name="ChatGPT",
        short_name="G",
        url="https://chatgpt.com/",
        color="#10A37F",
        input_selectors=("#prompt-textarea", "div[contenteditable='true'][data-testid='composer-text-input']"),
        send_selectors=("button[data-testid='send-button']", "button[aria-label*='Send']"),
        stop_selectors=("button[data-testid='stop-button']", "button[aria-label*='Stop']"),
        response_selectors=("[data-message-author-role='assistant']", "article[data-testid^='conversation-turn']"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        mode_trigger_selectors=(
            "button[data-testid='model-switcher-dropdown-button']",
            "button[aria-label*='model']",
            "button[aria-label*='Model']",
        ),
        modes=(
            ("default", "自动", ()),
            ("instant", "快速", ("Instant", "快速")),
            ("thinking", "深度思考", ("Thinking", "深度思考", "思考")),
        ),
    ),
    Platform(
        key="grok",
        name="Grok",
        short_name="X",
        url="https://grok.com/",
        color="#E5E7EB",
        input_selectors=(
            "textarea[placeholder]",
            "div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='Submit']", "button[aria-label*='Send']", "button[type='submit']"),
        stop_selectors=("button[aria-label*='Stop']",),
        response_selectors=("div.message-bubble", "div[class*='response']", "article"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        mode_trigger_selectors=(
            "button[aria-haspopup='menu']",
            "button[aria-label*='mode']",
            "button[aria-label*='Mode']",
        ),
        modes=(
            ("default", "自动", ()),
            ("fast", "快速", ("Fast", "快速")),
            ("expert", "专家", ("Expert", "专家")),
        ),
    ),
    Platform(
        key="gemini",
        name="Google Gemini",
        short_name="G",
        url="https://gemini.google.com/app",
        color="#8AB4F8",
        input_selectors=(
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true'][aria-label*='prompt']",
            "div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='Send']", "button.send-button"),
        stop_selectors=("button[aria-label*='Stop']", "button.stop-button"),
        response_selectors=("model-response", "message-content", "div[class*='response-container']"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        mode_trigger_selectors=(
            "button[aria-label*='model']",
            "button[aria-label*='Model']",
            "button[data-test-id*='model']",
        ),
        modes=(
            ("default", "快速", ()),
            ("thinking", "思考", ("Thinking", "思考")),
            ("pro", "Pro", ("Pro",)),
        ),
    ),
    Platform(
        key="deepseek",
        name="DeepSeek",
        short_name="D",
        url="https://chat.deepseek.com/",
        color="#5B8FF9",
        input_selectors=(
            "textarea#chat-input",
            "textarea[placeholder]",
            "div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='Send']", "button[type='submit']"),
        stop_selectors=("button[aria-label*='Stop']", "div[role='button'][aria-label*='Stop']"),
        response_selectors=("div.ds-markdown", "div[class*='markdown']"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        modes=(
            ("default", "对话", ()),
            ("thinking", "深度思考", ("深度思考", "DeepThink")),
            ("search", "联网搜索", ("联网搜索", "Search")),
        ),
    ),
    Platform(
        key="doubao",
        name="豆包",
        short_name="豆",
        url="https://www.doubao.com/chat/",
        color="#77A7FF",
        input_selectors=(
            "textarea[placeholder]",
            "div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='发送']", "button[aria-label*='Send']", "button[type='submit']"),
        stop_selectors=("button[aria-label*='停止']", "button[aria-label*='Stop']"),
        response_selectors=("div[class*='markdown']", "div[class*='message-content']"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        modes=(
            ("default", "默认", ()),
            ("thinking", "深度思考", ("深度思考", "思考")),
            ("search", "联网搜索", ("联网搜索", "搜索")),
        ),
    ),
    Platform(
        key="qwen",
        name="千问",
        short_name="千",
        url="https://www.qianwen.com/",
        color="#665CFF",
        input_selectors=(
            "textarea[placeholder]",
            "div[contenteditable='true']",
        ),
        send_selectors=("button[aria-label*='发送']", "button[aria-label*='Send']", "button[type='submit']"),
        stop_selectors=("button[aria-label*='停止']", "button[aria-label*='Stop']"),
        response_selectors=("div[class*='markdown']", "div[class*='answer']", "div[class*='message-content']"),
        attachment_selectors=("input[type='file'][multiple]", "input[type='file']"),
        modes=(
            ("default", "默认", ()),
            ("thinking", "深度思考", ("深度思考", "思考")),
            ("search", "联网搜索", ("联网搜索", "搜索")),
        ),
    ),
)


PLATFORM_MAP = {platform.key: platform for platform in PLATFORMS}
