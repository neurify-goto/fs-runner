"""
Playwrightブラウザのライフサイクル管理
"""
import asyncio
import logging
import os
import platform
from typing import Optional, Dict, Any, List

from playwright.async_api import (
    async_playwright,
    Playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    Route
)

logger = logging.getLogger(__name__)


class BrowserManager:
    """Playwrightブラウザの起動、ページ作成、終了を管理する"""

    def __init__(self, worker_id: int, headless: bool = None, config: Dict[str, Any] = None):
        self.worker_id = worker_id
        self.headless = headless
        self.config = config or {}

        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

        # 設定値
        # デフォルトはやや長め（初回読み込みの安定性重視）
        self.timeout_settings = self.config.get("timeout_settings", {"page_load": 30000})
        worker_cfg = self.config.get("worker_config", {})
        browser_cfg = worker_cfg.get("browser", {})
        rb_cfg = browser_cfg.get("resource_blocking", {})
        # 既定は設計方針に合わせてブロックON（画像/フォント）。設定で上書き可能。
        self._rb_block_images = bool(rb_cfg.get("block_images", True))
        self._rb_block_fonts = bool(rb_cfg.get("block_fonts", True))
        self._rb_block_stylesheets = bool(rb_cfg.get("block_stylesheets", False))

    async def launch(self) -> bool:
        """Playwrightブラウザを初期化して起動する"""
        try:
            logger.info(f"Worker {self.worker_id}: Initializing Playwright browser")
            is_github_actions = os.getenv("GITHUB_ACTIONS") == "true"

            self.playwright = await async_playwright().start()
            if is_github_actions:
                await asyncio.sleep(0.5)

            browser_args = self._get_browser_args(is_github_actions)
            launch_timeout = 60000 if is_github_actions else 30000

            # 環境変数で強制切替を許可（ローカル検証の安定化用）
            env_headless = os.getenv('PLAYWRIGHT_HEADLESS', '').lower()
            use_headless_env = True if env_headless in ['1', 'true', 'yes'] else False if env_headless in ['0', 'false', 'no'] else None

            use_headless = (
                use_headless_env if use_headless_env is not None else (self.headless if self.headless is not None else True)
            )
            mode_desc = "headless" if use_headless else "GUI"
            logger.info(f"Worker {self.worker_id}: Using {mode_desc} mode")

            # slow_mo はデフォルト無効。必要時のみ環境変数で指定（ms）
            slow_env = os.getenv('PLAYWRIGHT_SLOW_MO_MS', '').strip()
            slow_kw = {}
            if slow_env.isdigit() and int(slow_env) > 0:
                slow_kw = {"slow_mo": int(slow_env)}

            # macOS の GUI 実行ではシステムの Chrome を優先利用（安定化）
            use_chrome_channel = (platform.system().lower() == 'darwin' and not use_headless and not is_github_actions)
            launch_succeeded = False
            last_err: Optional[Exception] = None

            if use_chrome_channel:
                try:
                    self.browser = await self.playwright.chromium.launch(
                        headless=False,
                        channel='chrome',  # システム Chrome 経由
                        timeout=launch_timeout,
                        **slow_kw,
                    )
                    launch_succeeded = True
                    logger.info(f"Worker {self.worker_id}: Launched system Chrome via channel")
                except Exception as e:
                    last_err = e
                    logger.warning(f"Worker {self.worker_id}: Failed to launch system Chrome, falling back to bundled Chromium: {e}")

            if not launch_succeeded:
                # 既定: バンドルされた Chromium を利用
                self.browser = await self.playwright.chromium.launch(
                    headless=use_headless,
                    args=browser_args,
                    timeout=launch_timeout,
                    **slow_kw,
                )

            # 環境に関わらず、起動直後は短い待機を入れて安定化
            await asyncio.sleep(0.5 if not is_github_actions else 1.0)

            logger.info(f"Worker {self.worker_id}: Browser initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Browser initialization failed: {e}")
            return False

    def _get_browser_args(self, is_github_actions: bool) -> List[str]:
        """起動時のブラウザ引数を取得する"""
        # ローカル（macOS等）では安定性を最優先し、ブラウザ引数は極力付けない
        if not is_github_actions and platform.system().lower() == 'darwin':
            return []

        base_args = [
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
            "--disable-software-rasterizer", "--disable-web-security",
            "--disable-extensions", "--disable-plugins", "--disable-images", "--no-first-run",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", "--disable-ipc-flooding-protection",
            "--disable-features=VizDisplayCompositor", "--disable-background-networking",
        ]
        if is_github_actions:
            base_args.extend([
                "--memory-pressure-off", "--max_old_space_size=2048",
                "--disable-sync", "--disable-translate", 
                "--force-color-profile=srgb", "--disable-accelerated-2d-canvas",
                "--disable-accelerated-jpeg-decoding", "--disable-accelerated-mjpeg-decode",
                "--disable-accelerated-video-decode", "--disable-threaded-animation",
                "--disable-threaded-scrolling",
            ])
        return base_args

    async def create_new_page(self, form_url: str) -> Page:
        """新しいブラウザコンテキストとページを作成し、指定URLにアクセスする"""
        if not self.browser:
            raise ConnectionError("Browser is not launched. Call launch() first.")
        
        # ブラウザが閉じられていないかチェック
        try:
            # ブラウザの状態を確認
            contexts = self.browser.contexts
        except Exception as e:
            raise ConnectionError(f"Browser connection lost: {e}")

        try:
            # 既存のコンテキストは極力再利用（GUI安定性優先）
            last_err: Optional[Exception] = None
            page: Optional[Page] = None  # retryスコープ外で初期化して参照安全性を確保
            for i in range(2):
                page = None
                try:
                    # コンテキストが無い場合のみ作成
                    if not self.context:
                        if platform.system().lower() == 'darwin' and (self.headless is False or self.headless is None):
                            self.context = await self.browser.new_context()
                        else:
                            self.context = await self.browser.new_context(
                                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                            )
                    # 新規ページをオープン
                    page = await self.context.new_page()
                    # 資源ブロックは常に有効化（計画: 画像ブロックON、フォントON）。
                    # UAの変更はmacOS GUIでは避けて安定性を優先。
                    await self._setup_resource_blocking_routes(page)
                    if not (platform.system().lower() == 'darwin' and (self.headless is False or self.headless is None)):
                        await page.set_extra_http_headers({
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        })

                    logger.info(f"Worker {self.worker_id}: Accessing target form page: ***URL_REDACTED***")
                    # 初期ロードは macOS GUI でも安定性重視で 'domcontentloaded' を既定とする
                    # （一部サイトで 'load' 待機中に対象が閉じられる事象を回避）
                    wait_state = 'domcontentloaded'
                    await page.goto(
                        form_url,
                        timeout=int(self.timeout_settings.get("page_load", 30000)),
                        wait_until=wait_state,
                    )
                    # 追加の待機はローカルGUIでは行わない（安定優先）
                    if not (platform.system().lower() == 'darwin' and (self.headless is False or self.headless is None)):
                        try:
                            await page.wait_for_load_state('networkidle', timeout=5000)
                        except Exception:
                            pass
                    return page
                except PlaywrightTimeoutError as e:
                    last_err = e
                    try:
                        if page:
                            await page.close()
                    except Exception:
                        pass
                    logger.error(f"Worker {self.worker_id}: Page load timeout for ***URL_REDACTED*** (attempt {i+1}/2)")
                except Exception as e:
                    last_err = e
                    try:
                        if page:
                            await page.close()
                    except Exception:
                        pass
                    # ターゲット/接続クローズは一度だけ再試行
                    if any(k in str(e) for k in ["Target page", "Connection closed", "Browser connection lost"]):
                        logger.warning(f"Worker {self.worker_id}: Retrying after transient page error (attempt {i+1}/2): {e}")
                        await asyncio.sleep(0.5)
                        continue
                    logger.error(f"Worker {self.worker_id}: Page access error for ***URL_REDACTED*** {e}")
                    break
            # ここまで来たら最後のエラーを送出
            raise last_err or Exception("Unknown page access error")

        except PlaywrightTimeoutError as e:
            # エラー時のクリーンアップ
            try:
                await self._cleanup_context_on_error()
            except Exception:
                pass
            logger.error(f"Worker {self.worker_id}: Page load timeout for ***URL_REDACTED***")
            raise e
        except Exception as e:
            # エラー時のクリーンアップ
            try:
                await self._cleanup_context_on_error()
            except Exception:
                pass
            logger.error(f"Worker {self.worker_id}: Page access error for ***URL_REDACTED*** {e}")
            raise e

    async def _setup_resource_blocking_routes(self, page: Page) -> None:
        """不要なリソースをブロックして安定性を向上させる"""
        async def handle_route(route: Route):
            try:
                req = route.request
                r_type = req.resource_type
                if (self._rb_block_images and r_type in ["image", "media"]) or \
                   (self._rb_block_fonts and r_type == "font") or \
                   (self._rb_block_stylesheets and r_type == "stylesheet"):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
        try:
            await page.route("**/*", handle_route)
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: Failed to setup resource blocking routes: {e}")

    async def _cleanup_context_on_error(self):
        """エラー時のコンテキストクリーンアップ"""
        # ページ起因のエラーではページのみを閉じ、コンテキストは維持して安定化
        try:
            if self.context:
                pages = self.context.pages
                for p in pages:
                    try:
                        await p.close()
                    except Exception:
                        pass
        except Exception:
            pass

    async def close(self):
        """ブラウザとPlaywrightインスタンスを閉じる"""
        # コンテキストを先にクローズ
        if self.context:
            try:
                await self.context.close()
                logger.info(f"Worker {self.worker_id}: Context closed.")
            except Exception as e:
                if "Connection closed" in str(e) or "Target closed" in str(e):
                    logger.warning(f"Worker {self.worker_id}: Context was already closed: {e}")
                else:
                    logger.error(f"Worker {self.worker_id}: Error closing context: {e}")
            finally:
                self.context = None

        if self.browser:
            try:
                # ブラウザが既に閉じられているかチェック
                if hasattr(self.browser, '_connection') and self.browser._connection and not self.browser._connection._closed:
                    await self.browser.close()
                    logger.info(f"Worker {self.worker_id}: Browser closed.")
                else:
                    logger.info(f"Worker {self.worker_id}: Browser already closed.")
            except Exception as e:
                # 接続が既に切れている場合は警告レベルでログ出力
                if "Connection closed" in str(e) or "Target closed" in str(e) or "invalid state" in str(e):
                    logger.warning(f"Worker {self.worker_id}: Browser was already closed: {e}")
                else:
                    logger.error(f"Worker {self.worker_id}: Error closing browser: {e}")
            finally:
                self.browser = None

        if self.playwright:
            try:
                await self.playwright.stop()
                logger.info(f"Worker {self.worker_id}: Playwright stopped.")
            except Exception as e:
                # Playwrightが既に停止している場合は警告レベルでログ出力
                if "invalid state" in str(e) or "Connection closed" in str(e):
                    logger.warning(f"Worker {self.worker_id}: Playwright was already stopped: {e}")
                else:
                    logger.error(f"Worker {self.worker_id}: Error stopping Playwright: {e}")
            finally:
                self.playwright = None
