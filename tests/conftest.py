import sys
import types


def _install_playwright_stub():
    if "playwright.async_api" in sys.modules:
        return
    pw = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")

    class Page:  # ダミー型（参照のみ）
        pass

    class Browser:  # ダミー型（参照のみ）
        pass

    class _TimeoutError(Exception):
        pass

    async def _dummy_async_playwright():  # 使われない想定
        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Ctx()

    async_api.Page = Page
    async_api.Browser = Browser
    async_api.TimeoutError = _TimeoutError
    async_api.async_playwright = _dummy_async_playwright
    pw.async_api = async_api
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = async_api


_install_playwright_stub()

