import asyncio

import app


class Candidate:
    def __init__(self, visible):
        self.visible = visible

    async def is_visible(self):
        return self.visible


class Locator:
    def __init__(self, candidates):
        self.candidates = candidates

    async def count(self):
        return len(self.candidates)

    def nth(self, index):
        return self.candidates[index]


class Page:
    def __init__(self, candidates):
        self.candidates = candidates

    def locator(self, _selector):
        return Locator(self.candidates)


async def run_test():
    visible = Candidate(True)
    hidden = Candidate(False)
    page = Page([visible, hidden])
    automation = object.__new__(app.BrowserAutomation)
    result = await automation._visible_locator(page, ("textarea",), timeout_seconds=0.01)
    assert result is visible, "a hidden last match must not mask an earlier visible input"


if __name__ == "__main__":
    asyncio.run(run_test())
    print("VISIBLE_LOCATOR_OK")
