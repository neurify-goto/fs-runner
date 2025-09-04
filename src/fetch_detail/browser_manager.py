"""
ブラウザ管理モジュール

Playwrightブラウザの初期化、ページ管理、クリーンアップ機能を提供
"""

import asyncio
import logging
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page

logger = logging.getLogger(__name__)


class BrowserManager:
    """ブラウザ管理クラス"""
    
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
    
    async def _initialize_browser(self):
        """Playwrightブラウザを初期化"""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            
            # User-Agent設定をコンテキストレベルで行う
            context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            self.page = await context.new_page()
            
            logger.info("Playwrightブラウザを初期化しました")
    
    async def create_clean_page(self):
        """完全にクリーンなページを作成（企業処理ごとに呼び出し）"""
        try:
            # 既存のページがあれば徹底的にクリーンアップ
            if self.page:
                await self.cleanup_page()
            
            # ブラウザが初期化されていなければ初期化
            if not self.browser:
                await self._initialize_browser()
                return
            
            # 新しいコンテキストを完全にクリーンな状態で作成
            context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                # キャッシュを無効化
                bypass_csp=True,
                ignore_https_errors=True,
                # JavaScript を有効化（必要な場合）
                java_script_enabled=True,
                # 新しいコンテキストでキャッシュクリア
                no_viewport=False
            )
            
            # 新しいページを作成
            self.page = await context.new_page()
            
            # ページレベルでもクリーンな状態を保証
            await self.page.set_extra_http_headers({
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            })
            
            logger.debug("完全にクリーンなページを作成しました")
            
        except Exception as e:
            logger.warning(f"クリーンページ作成エラー: {e}")
            # ブラウザ全体を再初期化
            await self._initialize_browser()
    
    async def cleanup_page(self):
        """ページを徹底的にクリーンアップ（企業処理完了後に呼び出し）"""
        try:
            if self.page:
                # DOM・JavaScript・キャッシュの完全クリア
                try:
                    # ブラウザ内のキャッシュクリア
                    await self.page.evaluate("window.localStorage.clear()")
                    await self.page.evaluate("window.sessionStorage.clear()")
                    
                    # JavaScript ガベージコレクション（利用可能な場合）
                    await self.page.evaluate("window.gc && window.gc()")
                    
                    # 全てのイベントリスナーを削除
                    await self.page.evaluate("""
                        // DOM要素の参照を完全にクリア
                        if (typeof window !== 'undefined') {
                            // グローバル変数をクリア
                            for (let prop in window) {
                                if (window.hasOwnProperty(prop) && typeof window[prop] === 'object') {
                                    try {
                                        delete window[prop];
                                    } catch (e) {}
                                }
                            }
                        }
                    """)
                    
                    # ページを空白ページに移動してリソース解放
                    await self.page.goto('about:blank', timeout=5000)
                    
                except Exception as cleanup_error:
                    logger.debug(f"詳細クリーンアップ中にエラー（継続します）: {cleanup_error}")
                
                # ページとコンテキストを完全に閉じる
                context = self.page.context
                await self.page.close()
                await context.close()
                self.page = None
                logger.debug("ページとコンテキストを完全にクリーンアップしました")
                
                # 少し待機してリソース解放を確実にする
                await asyncio.sleep(0.1)
                
        except Exception as e:
            logger.warning(f"ページクリーンアップエラー: {e}")
            # エラーが発生してもページ参照はクリアしておく
            self.page = None
    
    async def reset_page_state(self):
        """ページ状態をリセット"""
        try:
            if self.page:
                # ページを閉じて新しいページを作成
                await self.page.close()
                
            if self.browser:
                context = await self.browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                )
                self.page = await context.new_page()
                logger.debug("ページ状態をリセットしました")
        except Exception as e:
            logger.warning(f"ページ状態リセットエラー: {e}")
            # ブラウザ全体を再初期化
            await self._initialize_browser()
    
    async def restart_browser(self):
        """ブラウザプロセスを再起動（深刻なエラー時）"""
        try:
            logger.info("ブラウザプロセスを再起動します")
            
            # 既存のブラウザを完全にクリーンアップ
            if self.page:
                await self.page.close()
                self.page = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            
            # 新しいブラウザを初期化
            await self._initialize_browser()
            logger.info("ブラウザプロセス再起動完了")
            
        except Exception as e:
            logger.error(f"ブラウザ再起動エラー: {e}")
            raise
    
    async def __aenter__(self):
        """非同期コンテキストマネージャー開始"""
        await self._initialize_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャー終了"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
    
    def is_retryable_error(self, error: Exception) -> bool:
        """エラーがリトライ可能かどうかを判定"""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
        
        # リトライ可能なエラー
        retryable_errors = (
            PlaywrightTimeoutError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        )
        
        # PlaywrightErrorの一部はリトライ可能
        if isinstance(error, PlaywrightError):
            error_message = str(error).lower()
            # ネットワーク関連エラーはリトライ可能
            if any(keyword in error_message for keyword in [
                'net::err_', 'network', 'connection', 'timeout', 'dns'
            ]):
                return True
            # 404, 403などのHTTPエラーはリトライ不可
            if any(keyword in error_message for keyword in [
                '404', '403', '401', '500', 'not found', 'forbidden'
            ]):
                return False
        
        return isinstance(error, retryable_errors)