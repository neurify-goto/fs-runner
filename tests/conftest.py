import sys
import types


def _install_playwright_stub():
    if "playwright.async_api" in sys.modules:
        return

    pw = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    sync_api = types.ModuleType("playwright.sync_api")

    class _Placeholder:
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, *args, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __await__(self):
            async def _coro():
                return self

            return _coro().__await__()

    class _TimeoutError(Exception):
        pass

    async def _dummy_async_playwright():  # 使われない想定
        return _Placeholder()

    def _dummy_sync_playwright():
        return _Placeholder()

    for module in (async_api, sync_api):
        module.Page = _Placeholder
        module.Browser = _Placeholder
        module.Response = _Placeholder
        module.Locator = _Placeholder
        module.ElementHandle = _Placeholder
        module.Playwright = _Placeholder
        module.BrowserContext = _Placeholder
        module.__getattr__ = lambda name, _placeholder=_Placeholder: _placeholder

    async_api.TimeoutError = _TimeoutError
    async_api.async_playwright = _dummy_async_playwright
    sync_api.sync_playwright = _dummy_sync_playwright

    pw.async_api = async_api
    pw.sync_api = sync_api

    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = async_api
    sys.modules["playwright.sync_api"] = sync_api


_install_playwright_stub()
